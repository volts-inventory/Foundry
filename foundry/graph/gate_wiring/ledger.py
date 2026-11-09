"""The fix loop's pass ledger (``fix_passes``): scores, windows, regressions
and the snapshot / restore diffs between trees."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.canonical import content_digest
from foundry.graph.gate_wiring.diffs import (
    FeatureDiffBodyError,
    MaterializationError,
    _diff_ops,
    _maybe_snapshot_files,
)
from foundry.graph.gate_wiring.materialize import (
    FEATURE_DIFF_KIND,
    RESET_PREFIX,
    RESTART_PREFIX,
    REVERT_PREFIX,
    _is_fix,
    _of_kind,
    _resolve_typed,
    integrated_files,
    lift_epoch,
)
from foundry.graph.gate_wiring.routing import (
    BUILD_BOUNDARY,
    BUILD_TEST_BOUNDARY,
    _failure_signature,
    is_collection_error,
)
from foundry.graph.gate_wiring.runs import wave_scenarios
from foundry.schemas.base import Artifact
from foundry.schemas.gates import FixOutcome, FixPass, GateResult, GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


# ---- the fix loop's best tree -----------------------------------------------
#
# Each check of a fix loop scores its tree; a pass that leaves the tree worse than
# the loop's best is undone by a restore diff back to the best tree, so the next
# fixer pass (and a halted run's draft) starts from the best tree rather than the
# newest. The pass ledger rides ``fix_passes``, written by the gate nodes alone.

RESTARTED_TRIAGE = "ticket-restarted"
REVERT_SKIPPED_TRIAGE = "fix-revert-skipped"
_FIX_PASS_NODE = "fix_pass"
_STAGES = ("build", "test", "smoke")
_FAILING_CAP = 40
# One epoch stream for every pass of the run: each entry's epoch is above the last
# (and raise_floor keeps it so across a resume). A per-pass key gave every entry
# the same epoch, leaving the order to langgraph_step, which does not order a wave
# subgraph's steps against the supervisor's.
_PASS_KEY = "pass"


def _pass_order(p: FixPass) -> tuple[int, Seq]:
    """Order within one window: ``pass_no`` counts up there whatever the Seqs say
    (a checkpoint from before ``_PASS_KEY`` holds equal-epoch entries)."""
    return (p.pass_no, p.seq)


# Stops that end a wave check's loop but not the run while waves remain: the wave
# advances on its best tree and the final gates repair what is left with the time
# the share saved. ``carried``: the check fails only on what the wave it builds on
# advanced with (:func:`advanced_from`).
CARRIED_STOP = "carried"
WAVE_ADVANCE_STOPS = frozenset({"wave-share", CARRIED_STOP})
_WAVE_BOUNDARIES = frozenset({BUILD_BOUNDARY, BUILD_TEST_BOUNDARY})


def advanced_from(state: FactoryState, window: str) -> FixPass | None:
    """The check the wave ``window`` builds on advanced from still failing: the
    newest wave-check entry of another window, if it stopped on a
    :data:`WAVE_ADVANCE_STOPS` rule (a later window that went green, or ended any
    other way, carries nothing)."""
    others = [
        p
        for p in state.get("fix_passes") or ()
        if p.boundary in _WAVE_BOUNDARIES and p.window != window
    ]
    newest = max(others, key=lambda p: p.seq, default=None)
    return newest if newest is not None and newest.stop in WAVE_ADVANCE_STOPS else None


def fix_window(state: FactoryState, boundary: str, scope: frozenset[str]) -> str:
    """One fix loop's identity: the boundary, the scenarios it judges, and the ticket
    diffs it repairs (fix and restore diffs excluded). A new wave, an escalation
    rebuild or the final gate starts a new window; a resumed loop keeps its own."""
    diffs = sorted(
        a.uri for a in _of_kind(state, FEATURE_DIFF_KIND) if not (a.task_id and _is_fix(a.task_id))
    )
    return content_digest({"boundary": boundary, "scope": sorted(scope), "diffs": diffs})


def typecheck_only(g: GateResult | None) -> bool:
    """True iff ``g`` is a build FAIL whose compile steps passed and whose static
    typecheck alone failed (``foundry.gates.build.TYPECHECK_ONLY``)."""
    from foundry.gates.build import TYPECHECK_ONLY

    return (
        g is not None
        and g.name == "build"
        and g.status is GateStatus.FAIL
        and bool(g.findings)
        and TYPECHECK_ONLY in g.findings[0]
    )


def fix_score(
    verdicts: Mapping[str, GateResult], names: frozenset[str]
) -> tuple[int, int, int] | None:
    """``(-stages passed in order, failing gates, failure signature size)`` for one
    tree's verdicts, lower is better; ``None`` when a verdict is missing or ERRORed
    (an infra error says nothing about the tree). A build that breaks scores worse
    than any number of failing tests on a tree that builds — except a build whose
    static typecheck alone failed: the test gate still ran on that tree, so it is
    scored by how far the tests got, the type errors one more signature entry. Fewer
    failing tests plus a type error beat more failing tests; equal tests plus a type
    error lose. A tree is all-PASS iff ``score[1:]`` is all zero."""
    if set(verdicts) != names:
        return None
    signature = _failure_signature(verdicts.values())
    if signature is None:
        return None
    typed = typecheck_only(verdicts.get("build"))
    if typed:
        signature = frozenset(s for s in signature if s[0] != "build")
    reached = 0
    for stage in _STAGES:
        if stage not in names:
            continue
        if verdicts[stage].status is not GateStatus.PASS and not (stage == "build" and typed):
            break
        reached += 1
    failing = sum(g.status is GateStatus.FAIL for g in verdicts.values()) - typed
    return (-reached, failing, len(signature) + typed)


def best_entry(passes: Iterable[FixPass], window: str) -> FixPass | None:
    """The window's best scored pass; a tie goes to the newest. A pass judged worse
    (it broke a check the best tree passed) never is, whatever its score."""
    best: FixPass | None = None
    for p in sorted(
        (p for p in passes if p.window == window and p.score is not None and p.outcome != "worse"),
        key=_pass_order,
    ):
        if best is None or tuple(p.score or ()) <= tuple(best.score or ()):
            best = p
    return best


def _newest_per_name(gates: Iterable[GateResult]) -> dict[str, GateResult]:
    out: dict[str, GateResult] = {}
    for g in gates:
        if g.name not in out or g.seq > out[g.name].seq:
            out[g.name] = g
    return out


def _failing(verdicts: Mapping[str, GateResult]) -> tuple[str, ...]:
    signature = _failure_signature(verdicts.values()) or frozenset()
    return tuple(sorted(f"{gate}:{ident}" for gate, ident in signature))[:_FAILING_CAP]


def regressions(best_failing: Iterable[str], failing: Iterable[str]) -> tuple[str, ...]:
    """The ledger failures (``gate:ident``) of a tree that the best tree passed: a
    gate the best tree did not fail, or a test/scenario id (or uncollectable test
    module) the best tree's same gate did not name. A gate whose findings name no id
    (a compiler line, a raw output line) is compared gate by gate only: its lines
    shift with every edit — except that a gate which named ids on the best tree and
    names none now (it no longer collects or runs: an ImportError, a usage error)
    regressed, whatever its raw line count. A best list cut at the ledger cap is not
    compared (what it left out is unknown)."""
    best_failing = tuple(best_failing)
    if len(best_failing) >= _FAILING_CAP:
        return ()
    by_gate: dict[str, set[str]] = {}
    for f in best_failing:
        gate, _, ident = f.partition(":")
        by_gate.setdefault(gate, set()).add(ident)
    failing = tuple(failing)
    named_now: dict[str, bool] = {}
    for f in failing:
        gate, _, ident = f.partition(":")
        named_now[gate] = named_now.get(gate, False) or _named(ident)
    out: list[str] = []
    for f in failing:
        gate, _, ident = f.partition(":")
        was = by_gate.get(gate)
        if was is None or (
            all(_named(w) for w in was)
            and (not named_now[gate] or (_named(ident) and ident not in was))
        ):
            out.append(f)
    return tuple(out)


def _named(ident: str) -> bool:
    return "::" in ident or is_collection_error(ident)


def _outcome(
    prior: list[FixPass],
    tree: str,
    score: tuple[int, int, int] | None,
    best: FixPass | None,
    failing: tuple[str, ...] = (),
) -> FixOutcome:
    """How a check compares with the window's best. Superset acceptance (Agentless
    keeps the patch with the fewest regressions, arXiv 2407.01489): a tree that
    breaks a check the best tree passed is worse even when its score is lower."""
    if not prior:
        return "initial"
    if score is None:
        return "error"
    if tree == prior[-1].tree:
        return "no-change"
    if best is None or best.score is None:
        return "improved"
    # More stages passing in a row (the build now passes) is progress whatever the
    # later stage's ids did: a test that "passed" on a tree that does not build is
    # not a check to keep. The superset rule judges trees that reach as far.
    if score[0] < best.score[0]:
        return "improved"
    if score > tuple(best.score) or regressions(best.failing, failing):
        return "worse"
    return "improved" if score < tuple(best.score) else "lateral"


def _pass_fix(
    state: FactoryState,
    prior: list[FixPass],
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> tuple[str | None, tuple[str, ...]]:
    """The fix diff this check is the first to judge, and the files it touched. A
    ticket restart's pass is its rebuilt tickets' diffs and their reset together."""
    fixes = [
        a
        for a in _of_kind(state, FEATURE_DIFF_KIND)
        if a.task_id and a.task_id.startswith(("fix-", RESTART_PREFIX))
    ]
    if not prior or not fixes:
        return None, ()
    newest = max(fixes, key=lambda a: a.seq)
    if any(p.fix_task == newest.task_id for p in state.get("fix_passes") or ()):
        return None, ()
    group = [newest]
    if newest.task_id and newest.task_id.startswith(RESTART_PREFIX):
        restarted = set(max(prior, key=_pass_order).restart_tickets)

        def ours(task_id: str) -> bool:
            if task_id.startswith(RESTART_PREFIX):
                return task_id.removeprefix(RESTART_PREFIX) in restarted
            return (
                task_id.startswith(RESET_PREFIX)
                and set(task_id.removeprefix(RESET_PREFIX).split("+")) <= restarted
            )

        group = [
            a for a in _of_kind(state, FEATURE_DIFF_KIND) if a.task_id and ours(a.task_id)
        ] or group
    touched: set[str] = set()
    for art in group:
        try:
            ops = _diff_ops(
                _resolve_typed(art, resolve, FeatureDiff, FEATURE_DIFF_KIND), resolve_blob
            )
        except MaterializationError:
            continue
        touched |= {*ops.writes, *ops.deletes, *ops.patches}
    return newest.task_id, tuple(sorted(touched))


def _snapshot_blob(files: Mapping[str, str], deleted: Iterable[str] = ()) -> bytes:
    body: dict[str, Any] = {"kind": "full-file-snapshot", "files": dict(files)}
    if deleted:
        body["deleted"] = sorted(deleted)
    return json.dumps(body, sort_keys=True).encode("utf-8")


def _snapshot(
    state: FactoryState,
    prior: list[FixPass],
    tree: str,
    *,
    rt: AgentRuntime,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> str | None:
    """The blob of the tree ``state`` integrates (the one just gated), scaffold-free."""
    known = next((p.snapshot_uri for p in prior if p.tree == tree and p.snapshot_uri), None)
    if known is not None or rt.cas is None:
        return known
    try:
        files = integrated_files(state, resolve=resolve, resolve_blob=resolve_blob)
    except MaterializationError:
        return None
    return rt.cas.put_blob(_snapshot_blob(files))


def _snapshot_files(uri: str, resolve_blob: BlobResolver) -> dict[str, str]:
    body = resolve_blob(uri)
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    files = _maybe_snapshot_files(uri, text)
    if files is None:
        raise FeatureDiffBodyError(f"fix-pass snapshot {uri!r} is not a full-file snapshot")
    return files


def restore_diff(
    state: FactoryState,
    best: FixPass,
    config: RunnableConfig,
    *,
    tree: str,
    rt: AgentRuntime,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> Artifact | None:
    """A FeatureDiff (``revert-<n>``) that turns ``tree``, the tree ``state``
    integrates, back into ``best``'s: every file that differs written whole, every
    file ``best`` lacks deleted, its epoch lifted past every diff so it applies last.
    ``None`` when the files already match or there is no store to write it to."""
    if rt.cas is None or best.snapshot_uri is None:
        return None
    current = integrated_files(state, resolve=resolve, resolve_blob=resolve_blob)
    target = _snapshot_files(best.snapshot_uri, resolve_blob)
    writes = {p: c for p, c in target.items() if current.get(p) != c}
    deleted = set(current) - set(target)
    if not writes and not deleted:
        return None
    diffs = _of_kind(state, FEATURE_DIFF_KIND)
    n = 1 + len({a.task_id for a in diffs if a.task_id and a.task_id.startswith(REVERT_PREFIX)})
    return snapshot_diff(
        state,
        config,
        writes,
        deleted,
        task_id=f"{REVERT_PREFIX}{n}",
        base_tree=tree,
        salt=best.tree,
        rt=rt,
    )


def snapshot_diff(
    state: FactoryState,
    config: RunnableConfig,
    writes: Mapping[str, str],
    deleted: Iterable[str],
    *,
    task_id: str,
    base_tree: str,
    salt: str,
    rt: AgentRuntime,
    node_id: str = _FIX_PASS_NODE,
) -> Artifact:
    """A FeatureDiff ``task_id`` writing ``writes`` whole and deleting ``deleted``,
    its epoch lifted past every diff on ``state`` so it applies last."""
    assert rt.cas is not None
    blob = rt.cas.put_blob(_snapshot_blob(writes, deleted))
    seq = lift_epoch(state, rt.seq(config, node_id, f"diff:{task_id}"))
    diff = FeatureDiff(
        diff_uri=blob, task_id=task_id, branch=f"task/{task_id}", base_tree_sha=base_tree, seq=seq
    )
    artifact = Artifact(
        kind=FEATURE_DIFF_KIND,
        uri=f"cas://feature_diff/{task_id}-{content_digest([salt, blob])[:32]}",
        produced_by=node_id,
        task_id=task_id,
        seq=seq,
    )
    rt.cas.put_body_at(artifact.uri, diff)
    return artifact


def draft_best(state: FactoryState, resolve: ArtifactResolver) -> FixPass | None:
    """The best tree of the fix loop ``state`` is still in, if any: what a halted
    or stopped run hands back instead of its newest, possibly worse or ungated, tree.
    ``None`` once a ticket diff landed after the loop's last check."""
    passes = window_passes(state, resolve)
    best = best_entry(passes, passes[0].window) if passes else None
    return best if best is not None and best.snapshot_uri is not None else None


def window_passes(state: FactoryState, resolve: ArtifactResolver) -> list[FixPass]:
    """The ledger of the fix loop ``state`` is still in, in pass order; empty once a
    ticket diff landed after the loop's last check."""
    passes = state.get("fix_passes") or ()
    if not passes:
        return []
    newest = max(passes, key=lambda p: p.seq)
    scope = (
        wave_scenarios(state, resolve) if newest.boundary == BUILD_TEST_BOUNDARY else frozenset()
    )
    if fix_window(state, newest.boundary, scope) != newest.window:
        return []
    return sorted((p for p in passes if p.window == newest.window), key=_pass_order)


def revisited_tree(state: FactoryState, out: Mapping[str, Any]) -> bool:
    """True iff the check ``out`` holds landed on a tree its window's ledger already
    holds: a fix pass that reproduced an earlier (say, reverted) tree made no
    progress, though the restore left the newest tree a different one."""
    new = out.get("fix_passes") or ()
    if not new or new[0].outcome == "initial":
        return False
    return any(
        p.window == new[0].window and p.tree == new[0].tree for p in state.get("fix_passes") or ()
    )
