"""Shared per-task build helpers for the implementation layer (agents.md Layer 3).

The ticket scheduler (``tickets.py``) and the build/test :func:`make_fixer_node`
run the swappable coding substrate (``rt.adapter``) inside a scoped
:class:`~foundry.execution.sandbox.Sandbox` and emit a content-addressed
:class:`~foundry.schemas.implementation.FeatureDiff` through the helpers here.

What these helpers are careful about (LEARNINGS L4/L5/L7, arch §2/§2b):

* **Pinned constraints VERBATIM + FIRST.** The architecture-pinned constraint
  texts are pulled from the live (non-superseded) view and injected into the
  :class:`TaskPacket` byte-for-byte, ahead of the task body.
* **Content-addressed keys (L7).** The diff/idempotency key is derived with
  ``content_digest``, never builtin ``hash()``, so the SAME inputs produce the
  SAME key across a kill-then-resume in a fresh process.
* **Lineage is proxy-observed, never self-reported (arch §2b).** The adapter's
  ``adapter_self_reported_model`` is IGNORED for lineage.
* **Agents never mint verdicts (L5).** The adapter returns advisory
  ``GateTriage`` only; it is forwarded to ``gate_triage`` and NEVER written as a
  ``GateResult`` to ``gates``.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.architecture import PinnedConstraint
from foundry.schemas.base import Artifact
from foundry.schemas.gates import FixPass, GateResult, GateStatus
from foundry.schemas.implementation import FeatureDiff, Task, TaskStatus
from foundry.state.reducers import DETERMINISTIC_GATES, live_constraints

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.self_check import CheckBase, CheckSpec, SelfCheckSettings
    from foundry.graph.gate_wiring import BlobResolver, FailureCluster, _DiffOps
    from foundry.state.factory_state import FactoryState

    # Reconstitutes a typed body from its content-addressed handle (the same INFRA
    # seam the gates take; CAS in prod, an in-memory map in tests).
    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "idempotency_key",
    "make_fixer_node",
    "pinned_texts",
    "ready_tasks",
]

NODE_ID = "junior_engineer"
FIXER_NODE_ID = "fixer"

_FIXER_PERSONA = (
    "You are fixing an integrated build that failed verification. The worktree holds "
    "the whole project as built so far. Make the smallest changes that make the failing "
    "gates below pass; do not rewrite working code or add features."
)
_FIXER_RULES = (
    "The failures below are the tail of each gate's output: re-run the failing "
    "command yourself (see `verify`) to read the whole output before you edit, unless "
    "the summary says it cannot run here.",
    "After each fix re-run the commands in `verify` (install only once its package "
    "manifest exists; then build and typecheck; then the tests) and keep going until "
    "they all pass or you run out of turns. If one cannot run here (missing tool, no "
    "network), say so in a TRIAGE line.",
    "Acceptance scenarios live only in the gate-owned features/<ticket>.feature files: "
    "never write, copy or regenerate a .feature file (any you write is discarded). A "
    "scenario that \"was not executed\" has no step module bound to that exact file: "
    "point the step module's binding at it (pytest-bdd: scenarios(\"<path relative to "
    "the module>\")) and implement its steps. Fix one feature file group at a time.",
)
# The first two rules for a fixer session with the ``check`` tool.
_FIXER_CHECK_RULES = (
    "The failures below are the tail of each gate's output: call `check` to see them as "
    "the gates report them on this tree (the build and the acceptance scenarios), or "
    "re-run the failing command in `verify` for its whole output, before you edit.",
    "After each fix call `check`, and keep going until it lists nothing as yours or you "
    "run out of turns: done is refused while it does. `check` does not run the stack's "
    "own test command: re-run a failing test named below from `verify` yourself. If one "
    "cannot run here (missing tool, no network), say so in a TRIAGE line.",
)

# Claude Code needs several turns to read, write and verify; 8 left contract stubs empty.
_DEFAULT_TURN_BUDGET = 25
# A local model spends ~10 of a fix pass's turns re-reading files before its first edit.
_LOCAL_FIX_TURN_BUDGET = 40
# The files a failure names are inlined in the fix packet, so they cost no read turns.
_INLINE_FILES = 3
_INLINE_CHARS = 8_000
_DEFAULT_COST_BUDGET_MICROUSD = 1_000_000  # integer micro-USD, never floats


def pinned_texts(constraints: Sequence[PinnedConstraint]) -> tuple[str, ...]:
    """The constraint TEXTS to inject, in a deterministic order.

    Reads the live (non-superseded) view (``live_constraints``) so a retired
    constraint is not injected, then orders by ``(id)`` for a stable, fold-order-
    independent sequence — the packet must be byte-identical across a resume so
    its idempotency key is stable (L7)."""
    live = live_constraints(list(constraints))
    return tuple(c.text for c in sorted(live, key=lambda c: c.id))


def idempotency_key(task: Task, constraints: Sequence[PinnedConstraint], base_tree_sha: str) -> str:
    """Process-stable idempotency key for this task's diff (LEARNINGS L7).

    Derived with ``content_digest`` over the task identity + the verbatim
    pinned-constraint texts + the base tree — NEVER builtin ``hash()`` (seed-
    randomized per process; would defeat outbox dedup on resume). A re-run with
    identical inputs re-derives the identical key, so the keyed-union channel
    coalesces the supersession instead of duplicating it."""
    return content_digest(
        {
            "task_id": task.id,
            "branch": task.branch,
            "pinned_constraints": list(pinned_texts(constraints)),
            "base_tree_sha": base_tree_sha,
        }
    )


def ready_tasks(state: FactoryState) -> tuple[Task, ...]:
    """The Task(s) on ``state['tasks']`` (AR1), typed and ordered by ``Seq``.

    The ``tasks`` channel carries plain ``dict``s (keyed by id, ``union_by_id``);
    reconstitute the typed :class:`Task` from each and order by ``Seq`` so the
    order is deterministic and recoverable from the records alone."""
    raw = state.get("tasks") or ()
    fields = set(Task.model_fields)
    out: list[Task] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # A done-marker carries extra keys (diff_uri / base_tree_sha) the frozen
        # Task model forbids; keep only the Task fields to reconstitute it.
        out.append(Task.model_validate({k: v for k, v in entry.items() if k in fields}))
    return tuple(sorted(out, key=lambda t: t.seq))


def _prior_failures(state: FactoryState) -> tuple[str, ...]:
    """Findings from the latest pass's FAILED/ERRORed gates — fed back to the
    engineer (via the TaskPacket) so a fix attempt targets the SPECIFIC failure
    instead of re-rolling blind. Empty on the first pass (no gates yet).

    The ``gates`` channel keeps one record per (gate, tree), so superseded trees'
    failures stay in state. Only the latest verdict per gate counts, and a
    deterministic gate counts only if it ran against the newest tree: a gate skipped
    on this pass (tests after a build failure) must not resurface last pass's FAIL."""
    out: list[str] = []
    for g in _failed_gates(state):
        out.append(f"[{g.name} gate FAILED]")
        out.extend(g.findings)
    return tuple(out)


def _failed_gates(state: FactoryState) -> list[GateResult]:
    """The latest pass's FAILED/ERRORed gates with findings, oldest first (see
    :func:`_prior_failures`)."""
    gates = list(state.get("gates") or ())
    latest: dict[str, GateResult] = {}
    for g in gates:
        if g.name not in latest or g.seq > latest[g.name].seq:
            latest[g.name] = g
    tree_gates = [g for g in gates if g.name in DETERMINISTIC_GATES]
    current_tree = max(tree_gates, key=lambda g: g.seq).run_against if tree_gates else None
    return [
        g for g in sorted(latest.values(), key=lambda g: g.seq)
        if not (g.name in DETERMINISTIC_GATES and g.run_against != current_tree)
        and g.status in (GateStatus.FAIL, GateStatus.ERROR) and g.findings
    ]


# pytest's per-test detail header: ``____ test_name ____`` (or Class.test_name).
_TEST_HEADER = re.compile(r"^_{3,} (\S+) _{3,}$")
# ... and for a module that failed to collect: ``____ ERROR collecting tests/x.py ____``.
_COLLECT_HEADER = re.compile(r"^_{3,} ERROR collecting (\S+) _{3,}$")


def fix_target(
    state: FactoryState, resolve: ArtifactResolver, failed: Sequence[GateResult],
) -> tuple[FailureCluster | None, int]:
    """The failure cluster the fix loop's newest check aimed the next pass at
    (``FixPass.target``), among ``failed``'s clusters, and how many failures the
    other clusters hold. ``(None, 0)`` before a plan, or when the plan's cluster is
    no longer failing."""
    from foundry.agents.implementation.tickets import built_task_ids, read_tickets
    from foundry.graph.gate_wiring import failure_clusters, window_passes

    passes = window_passes(state, resolve)
    if not passes or passes[-1].target is None:
        return None, 0
    clusters = failure_clusters(failed, read_tickets(state, resolve), built_task_ids(state))
    aimed = next((c for c in clusters if c.key == passes[-1].target), None)
    if aimed is None or len(clusters) < 2:
        return None, 0
    return aimed, sum(len(c.items) for c in clusters if c is not aimed)


def scope_gates(
    failed: Sequence[GateResult], cluster: FailureCluster,
) -> list[GateResult]:
    """``failed`` cut down to ``cluster``: the gates it has failures in, and from a
    gate whose failures name ids, only the lines (and pytest's ``___ test ___``
    detail blocks) of the cluster's ids — lines naming no id are kept as context."""
    from foundry.graph.gate_wiring import failing_ids

    gates = {i.partition(":")[0] for i in cluster.items}
    mine = {i.partition(":")[2] for i in cluster.items}
    out: list[GateResult] = []
    for g in failed:
        if g.name not in gates:
            continue
        if not failing_ids(g.findings):
            out.append(g)
            continue
        names = {i.rsplit("::", 1)[-1] for i in mine}
        kept: list[str] = []
        skipping = False
        for line in g.findings:
            st = line.strip()
            header = _TEST_HEADER.match(st)
            collecting = _COLLECT_HEADER.match(st)
            if collecting is not None:
                skipping = collecting.group(1) not in mine
            elif header is not None:
                test = header.group(1).rsplit(".", 1)[-1]
                skipping = test not in names
            elif st.startswith("="):  # a section line ends the detail block
                skipping = False
            ids = failing_ids([line])
            if ids:
                if set(ids) & mine:
                    kept.append(line)
            elif not skipping:
                kept.append(line)
        out.append(g.model_copy(update={"findings": tuple(kept)}))
    return out


def _cluster_gates(
    state: FactoryState, resolve: ArtifactResolver, *, scoped: bool = True,
) -> tuple[list[GateResult], tuple[str, ...]]:
    """The failed gates a fixer pass is shown — scoped to the planned cluster — and
    the summary lines that say so."""
    failed = _failed_gates(state)
    if not scoped:
        return failed, ()
    cluster, others = fix_target(state, resolve, failed)
    if cluster is None:
        return failed, ()
    return scope_gates(failed, cluster), (
        f"this pass fixes ONE failure cluster: {cluster.key} ({len(cluster.items)} "
        "failure(s), listed below)",
        f"{others} other failure(s) are handled separately — don't touch them",
    )


def fix_failures(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """:func:`_prior_failures` scoped to the fix pass's planned cluster."""
    out: list[str] = []
    for g in _cluster_gates(state, resolve)[0]:
        out.append(f"[{g.name} gate FAILED]")
        out.extend(g.findings)
    return tuple(out)


def _fix_summary(
    state: FactoryState,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    *,
    attempt: int,
    tree: Mapping[str, str | None] | None = None,
    scoped: bool = True,
) -> tuple[str, ...]:
    """The head of a fixer pass's input: the failing gates and the tests/scenarios
    they name, the files to edit first (with their content from ``tree``, the
    integrated tree), then what the previous pass of this fix loop touched and
    reported. With ``scoped`` (a fixer pass) only the planned failure cluster's
    failures are listed (:func:`fix_target`).
    Fresh attempts from a summary of earlier rollouts beat one ever-growing repair
    context (arXiv 2602.18998, 2604.16529). Read off state; no model call."""
    from foundry.graph.gate_wiring import (
        FEATURE_DIFF_KIND,
        MaterializationError,
        _diff_ops,
        _of_kind,
        _resolve_typed,
        failing_ids,
        pip_manifest_failure,
    )

    failed, scope = _cluster_gates(state, resolve, scoped=scoped)
    ids = [i for g in failed for i in failing_ids(g.findings)]
    named = _named_files(failed, tree)
    out = [
        "[fix summary]",
        *scope,
        "failed gates: " + (", ".join(g.name for g in failed) or "none recorded"),
        "failing tests/scenarios: " + (", ".join(ids) or "none named; see the gate output"),
    ]
    if named:
        out.append("Edit these files first: " + ", ".join(named)
                   + " (the failure names them)")
    if tree is not None:
        from foundry.agents.implementation.tickets import (
            built_task_ids,
            read_tickets,
            unwritten_ticket_files,
        )

        out.extend(unwritten_ticket_files(
            [line for g in failed for line in g.findings], read_tickets(state, resolve),
            built_task_ids(state), tree, state.get("stack")))
    if pip_manifest_failure(failed):
        out.append("pip could not install a dependency the manifest declares. The install "
                   "cannot be re-run in this sandbox (no network), so do not try: edit the "
                   "manifest line the gate output names (a package that does not exist on "
                   "PyPI, e.g. a standard-library module, or a version that was never "
                   "released) and remove or correct it.")
    from foundry.graph.gate_wiring import window_passes

    fixes = [a for a in _of_kind(state, FEATURE_DIFF_KIND)
             if a.task_id and a.task_id.startswith("fix-")]
    passes = window_passes(state, resolve)
    if any(p.outcome not in ("initial", "restored") for p in passes):
        out.extend(_pass_log(state, passes, resolve, resolve_blob))
        newest = next((p for p in reversed(passes) if p.fix_task
                       and p.fix_task.startswith("fix-")), None)
        # A reverted pass's edits are no longer on the tree: the log says so.
        if (named and newest is not None and passes[-1].outcome != "restored"
                and not set(newest.touched) & set(named)):
            out.append(f"the last fix pass ({newest.fix_task}) never edited "
                       f"{', '.join(named)}, the file(s) the failure names: edit them "
                       "before anything else")
    # attempt counts the failing checks of this fix loop: from 2 on, the newest fix
    # diff is this loop's previous pass, not an earlier wave's. (A run from before the
    # pass ledger existed.)
    elif attempt >= 2 and fixes:
        prev = max(fixes, key=lambda a: a.seq)
        try:
            ops = _diff_ops(_resolve_typed(prev, resolve, FeatureDiff, FEATURE_DIFF_KIND),
                            resolve_blob)
            files = touched_files(ops)
            touched: set[str] | None = {*ops.writes, *ops.deletes, *ops.patches}
        except MaterializationError:
            files, touched = "an unreadable diff", None
        cut_off = any(t.run_against == f"task/{prev.task_id}"
                      and t.explanation.startswith(TURN_CAP_NOTE)
                      for t in state.get("gate_triage") or ())
        broke = regressed_gates(state)
        # A pass that changed nothing left the tree the wave built: a gate that broke
        # since the tree before was broken by the wave, not by this pass.
        if touched == set():
            out.append(f"previous fix pass ({prev.task_id}) changed no file"
                       + (" (it ran out of turns before its first edit)" if cut_off else "")
                       + ": reading files changes nothing; make the edits this time")
        elif broke:
            # A pass cut off mid-refactor leaves callers of what it renamed or moved
            # broken; "a different approach" would pile a second change on the first.
            out.append(f"previous fix pass ({prev.task_id}) touched {files} and broke the "
                       f"{', '.join(broke)} gate(s), which passed before it"
                       + (" (it ran out of turns before it finished)" if cut_off else "")
                       + ": its change is incomplete. Finish it first — update every caller "
                       "and test of what it renamed, moved or removed — or undo it")
        elif cut_off:
            out.append(f"previous fix pass ({prev.task_id}) touched {files} and ran out of "
                       "turns before it finished, leaving the gates above failing: check "
                       "whether its edits are complete before trying a different approach")
        else:
            out.append(f"previous fix pass ({prev.task_id}) touched {files} and left the "
                       "gates above failing: try a different approach")
        if named and touched is not None and not touched & set(named):
            out.append(f"previous fix pass never edited {', '.join(named)}, the file(s) the "
                       "failure names: edit them before anything else")
        out.extend(f"previous fix pass reported: {t.explanation[:300]}"
                   for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
                   if t.run_against == f"task/{prev.task_id}")
    out.extend(_inline(named, tree))
    out.append("gate output (tail):")
    return tuple(out)


# proxy_agent's note for a session that hit packet.turn_budget.
TURN_CAP_NOTE = "turn budget exhausted"

_LOG_ENTRIES = 8
_LOG_CHARS = 1_500
_LOG_REPORT_CHARS = 150
_LOG_PATCH_CHARS = 1_200
_LOG_IDS = 4


def _norm(failing: Iterable[str]) -> set[str]:
    """A tree's failures with the build's compiler lines folded into one: whether
    the build broke, not which lines changed."""
    return {"build:" if f.startswith("build:") else f for f in failing}


def _what(failing: Iterable[str]) -> str:
    """Ledger failures (``gate:id``) for the log: a build failure as "the build"
    (its signature is compiler lines, not ids), tests as ``test_a``, a few of them."""
    failing = set(failing)
    ids = sorted({f.split(":", 1)[-1].rsplit("::", 1)[-1][:60] for f in failing
                  if not f.startswith("build:")})
    more = len(ids) - _LOG_IDS
    parts = ["the build"] if any(f.startswith("build:") for f in failing) else []
    if ids:
        parts.append(",".join(ids[:_LOG_IDS]) + (f" +{more} more" if more > 0 else ""))
    return ", ".join(parts)


def _pass_line(entry: FixPass, base: FixPass | None, reverted: bool, report: str) -> str:
    head = f"pass {entry.pass_no} ({entry.fix_task or 'no diff'}): "
    if not entry.touched:
        line = head + "no file edited"
    elif entry.outcome == "error":
        line = head + f"edited {', '.join(entry.touched)} — the gates ERRORed (not judged)"
    elif entry.outcome == "no-change":
        line = head + f"edited {', '.join(entry.touched)} — no change to the tree"
    else:
        was = _norm(base.failing) if base is not None else set()
        now = _norm(entry.failing)
        fixed, broke = was - now, now - was
        build = "build broken" if any(f.startswith("build:") for f in now) else "build ok"
        line = head + f"edited {', '.join(entry.touched)} — "
        if entry.outcome == "lateral":
            n_was = base.score[2] if base is not None and base.score else len(was)
            n_now = entry.score[2] if entry.score else len(now)
            line += f"lateral: {n_was}→{n_now}"
            if broke:
                line += f", now failing {_what(broke)}"
                if fixed:
                    line += f" instead of {_what(fixed)}"
        else:
            line += entry.outcome.upper() + (", reverted" if reverted else "")
            parts = ([f"fixed {_what(fixed)}"] if fixed else []) + (
                [f"broke {_what(broke)}"] if broke else [])
            if parts:
                line += ": " + "; ".join(parts)
        line += f" ({build})"
    return line + (f" — reported: {report}" if report else "")


def _patch_text(
    state: FactoryState, task_id: str, resolve: ArtifactResolver, resolve_blob: BlobResolver,
) -> str | None:
    from foundry.graph.gate_wiring import (
        FEATURE_DIFF_KIND,
        MaterializationError,
        _maybe_snapshot_files,
        _of_kind,
        _resolve_typed,
    )

    art = next((a for a in _of_kind(state, FEATURE_DIFF_KIND) if a.task_id == task_id), None)
    if art is None:
        return None
    try:
        diff = _resolve_typed(art, resolve, FeatureDiff, FEATURE_DIFF_KIND)
        body = resolve_blob(diff.diff_uri)
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        files = _maybe_snapshot_files(diff.diff_uri, text)
    except (MaterializationError, KeyError, UnicodeDecodeError):
        return None
    if files is not None:
        text = "\n".join(f"--- {rel}\n{content}" for rel, content in sorted(files.items()))
    return text[:_LOG_PATCH_CHARS]


def _pass_log(
    state: FactoryState, passes: Sequence[FixPass], resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> list[str]:
    """What this fix loop's passes tried, newest last, from its ledger: the files each
    edited, how the tree came out against the best before it (the tests it fixed
    and broke), whether it was reverted, and what the pass itself reported. A
    worse pass that fixed something hands its patch on, to reapply without the
    breakage. Fixer passes that each start from a summary of the earlier ones
    instead of only the last stop repeating a reverted approach."""
    notes: dict[str, list[str]] = {}
    for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq):
        task = t.run_against.removeprefix("task/")
        if task != t.run_against:
            notes.setdefault(task, []).append(" ".join(t.explanation.split()))
    reports = {task: "; ".join(n)[:_LOG_REPORT_CHARS] for task, n in notes.items()}
    lines: list[str] = []
    salvage: tuple[FixPass, set[str], set[str]] | None = None
    for i, entry in enumerate(passes):
        if entry.outcome in ("initial", "restored"):
            continue
        base = _best_before(passes[:i])
        reverted = i + 1 < len(passes) and passes[i + 1].outcome == "restored"
        lines.append(_pass_line(entry, base, reverted,
                                reports.get(entry.fix_task or "", "")))
        if entry.outcome == "worse" and base is not None and entry.fix_task:
            fixed = _norm(base.failing) - _norm(entry.failing)
            if fixed:
                salvage = (entry, fixed, _norm(entry.failing) - _norm(base.failing))
    shown: list[str] = []
    room = _LOG_CHARS
    for line in reversed(lines[-_LOG_ENTRIES:]):
        if len(line) > room and shown:
            break
        shown.insert(0, line[:room])
        room -= len(line)
    out = ["[fix passes so far in this loop]"]
    if len(lines) > len(shown):
        out.append(f"({len(lines) - len(shown)} earlier pass(es) not shown)")
    out.extend(shown)
    if salvage is not None:
        entry, fixed, broke = salvage
        patch = _patch_text(state, entry.fix_task or "", resolve, resolve_blob)
        if patch:
            out.append(f"{entry.fix_task}'s change fixed {_what(fixed)} but was worse "
                       f"overall: reapply this part without breaking "
                       f"{_what(broke) or 'the build'}:\n{patch}")
    out.append("do not repeat an approach marked WORSE or no-change from this tree.")
    return out


def _best_before(passes: Sequence[FixPass]) -> FixPass | None:
    from foundry.graph.gate_wiring import best_entry

    return best_entry(passes, passes[0].window) if passes else None


def regressed_gates(state: FactoryState) -> tuple[str, ...]:
    """The tree gates that FAIL on the newest tree and PASSed on the tree checked
    before it: what the change between the two broke."""
    trees: dict[str, dict[str, GateResult]] = {}
    for g in sorted(state.get("gates") or (), key=lambda g: g.seq):
        if g.name in DETERMINISTIC_GATES and g.name != "spec":
            trees.setdefault(g.run_against, {})[g.name] = g
    order = sorted(trees.values(), key=lambda v: max(g.seq for g in v.values()))
    if len(order) < 2:
        return ()
    prev, cur = order[-2:]
    return tuple(sorted(n for n, g in cur.items() if g.status is GateStatus.FAIL
                        and n in prev and prev[n].status is GateStatus.PASS))


def _named_files(
    failed: Sequence[GateResult], tree: Mapping[str, str | None] | None
) -> tuple[str, ...]:
    """The files the failure names, kept to those in ``tree`` when one is given
    (both pip manifests are candidates; a project usually has one)."""
    from foundry.graph.gate_wiring import failure_files

    files = failure_files(failed)
    if tree is None:
        return files
    return tuple(f for f in files if tree.get(f) is not None)


def _inline(named: Sequence[str], tree: Mapping[str, str | None] | None) -> list[str]:
    """The current content of the first few named files, within a size cap; a file
    that does not fit is left for the agent to read."""
    out: list[str] = []
    room = _INLINE_CHARS
    for rel in named[:_INLINE_FILES]:
        content = (tree or {}).get(rel)
        if content is None or len(content) > room:
            continue
        room -= len(content)
        out.append(f"current content of {rel}:\n{content}")
    return out


def touched_files(ops: _DiffOps) -> str:
    """The paths one diff writes, deletes or patches, for a model-facing summary."""
    return ", ".join(sorted({*ops.writes, *ops.deletes, *ops.patches})) or "no files"


def _build_packet(
    task: Task,
    constraints: Sequence[PinnedConstraint],
    sandbox: Sandbox,
    *,
    turn_budget: int,
    cost_budget_microusd: int,
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
    rules: tuple[str, ...] = (),
    verify: Mapping[str, str] | None = None,
) -> TaskPacket:
    """Assemble the immutable packet: pinned constraints VERBATIM + FIRST.

    The constraint texts are placed in ``pinned_constraints`` byte-for-byte (no
    paraphrase, no reorder behind the task body); ``assemble_prompt`` renders
    them first. ``retrieved_spans`` grounds the call in the criteria/design the
    task traces to (ROADMAP Phase 4). The proxy endpoint is intentionally NOT
    handed to the adapter here (egress stays None) — the StubAdapter holds no
    egress and the dev sandbox's network denial is "nothing to talk to"."""
    body: dict[str, Any] = {
        "id": task.id,
        "title": task.id,
        "spec_uri": task.spec_uri,
        "branch": task.branch,
        "criterion_ids": list(task.criterion_ids),
    }
    if rules:
        body["rules"] = list(rules)
    if verify:
        body["verify"] = dict(verify)
    return TaskPacket(
        task=body,
        persona=persona,  # area-expert role preamble (empty == unchanged prompt)
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=retrieved_spans,  # spec grounding (empty == unchanged prompt)
        prior_failures=prior_failures,
        worktree_path=str(sandbox.worktree),
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )


def _apply_overlay(worktree: Path, overlay: Mapping[str, str | None]) -> None:
    """Write predecessor slices onto a fresh clone (content ⇒ write, None ⇒
    delete). Paths are containment-checked defensively even though they come
    from our own CAS (same posture as the integration boundary, D-R1-03)."""
    root = worktree.resolve()
    for rel, content in sorted(overlay.items()):
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        target = (root / pure).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        if content is None:
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _sandbox_for(
    rt: AgentRuntime, source_dir: str, snapshot_store: str | None, tier: str | None = None
) -> Sandbox:
    """Build the per-task Sandbox honoring the adapter's DECLARED model-auth needs.

    An agentic CLI substrate (claude-code) declares ``credential_grant`` /
    ``home_passthrough`` on the adapter; the Sandbox validates the grant
    fail-closed against ``GRANTABLE_MODEL_AUTH`` (ROADMAP Phase 1b — model-auth
    only, target-system credentials can never be granted). Adapters that declare
    nothing (stub, raw-react) get the fully-scrubbed cage, byte-identical to
    before. A per-tier adapter (model modes) answers for the substrate that will
    run ``tier``, so a local session's cage never gets the cloud credential."""
    adapter: Any = rt.adapter
    for_tier = getattr(adapter, "for_tier", None)
    if callable(for_tier):
        adapter = for_tier(tier)
    return Sandbox(
        source_dir,
        snapshot_store=snapshot_store,
        credential_grant=tuple(getattr(adapter, "credential_grant", ()) or ()),
        home_passthrough=bool(getattr(adapter, "home_passthrough", False)),
        toolchain_env=bool(getattr(adapter, "toolchain_env", False)),
    )


def _run_one_task(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    config: RunnableConfig,
    *,
    source_dir: str,
    snapshot_store: str | None,
    turn_budget: int,
    cost_budget_microusd: int,
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
    overlay: Mapping[str, str | None] | None = None,
    scope: tuple[str, ...] = (),
    rules: tuple[str, ...] = (),
    verify: Mapping[str, str] | None = None,
    review: Callable[[AdapterResult, Sandbox], AdapterResult] | None = None,
    check: CheckSpec | None = None,
) -> dict[str, Any]:
    """Run the adapter for ONE task in a fresh sandbox; return its channel writes.
    A non-empty ``scope`` cuts the diff back to those paths (a noted trim);
    ``review`` then annotates the kept diff while the sandbox is alive; ``check``
    is the session's gate check (:mod:`foundry.gates.self_check`).

    A FRESH ``Sandbox`` per task (arch §2(d): no reuse), then the diff keyed by
    ``content_digest`` (L7) so a re-run re-derives the same key and the
    keyed-union channels coalesce the supersession.

    ``overlay`` (the integrated tree, for the fixer) is written onto the fresh
    clone BEFORE the base snapshot, so the recorded base — and the idempotency
    key — reflect what the task actually built against."""
    assert rt.adapter is not None  # guaranteed by the factory's guard
    sandbox = _sandbox_for(rt, source_dir, snapshot_store)
    try:
        if overlay:
            _apply_overlay(sandbox.worktree, overlay)
        base = sandbox.snapshot()
        packet = _build_packet(
            task,
            constraints,
            sandbox,
            turn_budget=turn_budget,
            cost_budget_microusd=cost_budget_microusd,
            prior_failures=prior_failures,
            persona=persona,
            retrieved_spans=retrieved_spans,
            rules=rules,
            verify=verify,
        )
        from foundry.gates.self_check import check_scope

        with check_scope(check):
            result: AdapterResult = rt.adapter.run(packet, sandbox)
        from foundry.agents.implementation.tickets import trim_to_write_scope

        # An empty scope still drops gate inputs (feature files, criteria).
        result = trim_to_write_scope(result, sandbox, scope, task.id)
        if review is not None:
            result = review(result, sandbox)
        _mirror_diff_blob(rt, sandbox, result)  # while the sandbox is alive
    finally:
        sandbox.destroy()

    return _finalize_diff(rt, task, constraints, base, result, config)


def _mirror_diff_blob(rt: AgentRuntime, sandbox: Sandbox, result: AdapterResult) -> None:
    """Mirror the adapter's diff BLOB out of the per-task sandbox (destroyed after
    — arch §2(d): no reuse) into the run-scoped CAS so a store-backed resolve_blob
    reads it end to end. Both stores key by the RAW sha256, so the mirrored uri ==
    ``result.diff_uri`` (asserted). No-op when ``rt.cas`` is None (synthetic
    resolver). This is the ONLY shared-state write a pool worker makes — and it is
    safe to call concurrently: ``put_blob`` is content-addressed and idempotent
    (in-memory store = a single GIL-atomic dict setitem; filesystem store =
    thread-unique temp + ``os.replace``), so two workers mirroring the same digest
    converge on identical bytes."""
    if rt.cas is None:
        return
    mirrored = rt.cas.put_blob(sandbox.get_blob(result.diff_uri))
    assert mirrored == result.diff_uri, (
        f"mirrored diff uri {mirrored!r} != adapter uri {result.diff_uri!r}"
    )


def _finalize_diff(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    base: str,
    result: AdapterResult,
    config: RunnableConfig,
) -> dict[str, Any]:
    """The SEQUENTIAL post-processing for one task's adapter result: key the diff
    (``content_digest``, L7), mint the FeatureDiff + Artifact (``rt.seq``), persist
    the typed body, and emit the done-marker + advisory triage. Shared by the fixer
    and the ticket pool path — ALWAYS called single-threaded from the node so
    ``rt.seq`` (a non-thread-safe epoch counter) and ``rt.cas`` writes never race."""
    key = idempotency_key(task, constraints, base)
    diff = FeatureDiff(
        diff_uri=result.diff_uri,  # cas:// handle from the adapter (DS9)
        task_id=task.id,
        branch=task.branch,
        base_tree_sha=base,
        seq=rt.seq(config, NODE_ID, f"diff:{key}"),
    )
    artifact = Artifact(
        kind="feature_diff",
        uri=f"cas://feature_diff/{key}",  # uri carries the idempotency key
        produced_by=NODE_ID,
        task_id=task.id,
        seq=rt.seq(config, NODE_ID, f"artifact:{key}"),
    )
    # Store the TYPED FeatureDiff body keyed by the handle's OWN uri (which
    # carries the idempotency key, not content_identity), so gate_wiring's
    # resolve(feature_diff_handle) == get_body(artifact.uri) reconstitutes it.
    if rt.cas is not None:
        rt.cas.put_body_at(artifact.uri, diff)
    writes: dict[str, Any] = {
        "artifacts": [artifact],
        "tasks": [_task_done_marker(task, diff)],
    }
    if result.triage:
        writes["gate_triage"] = list(result.triage)
    return writes


def _adapter_name(rt: AgentRuntime) -> str:
    """Stable substrate id for the ExecutionTask (only an identity component)."""
    return str(getattr(rt.adapter, "name", type(rt.adapter).__name__))


def _task_done_marker(task: Task, diff: FeatureDiff) -> dict[str, Any]:
    """The task channel entry, updated with the produced diff handle.

    Keyed by ``id`` (the tasks reducer is a keyed union). NOTE (LEARNINGS IM3): the
    reducer is FIRST-keyed-wins for dicts, so an earlier plain Task dict survives and
    THIS marker is dropped on the `tasks` channel — the produced
    `diff_uri`/`base_tree_sha` are read off the **`artifacts`** channel (the
    FeatureDiff). Do NOT assume a downstream node reads them off the reduced `tasks`
    channel."""
    d = task.model_dump(mode="json")
    d["status"] = TaskStatus.DONE.value
    d["diff_uri"] = diff.diff_uri
    d["base_tree_sha"] = diff.base_tree_sha
    return d


def _fix_focus(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """The built tickets' files: what the fixer is repairing. Its writes elsewhere
    are kept but noted. Empty = no focus (a built ticket declared no scope)."""
    from foundry.agents.implementation.tickets import (
        built_task_ids,
        read_tickets,
        task_id_for_ticket,
    )

    built = built_task_ids(state)
    files: list[str] = []
    for t in read_tickets(state, resolve):
        if task_id_for_ticket(t.id) not in built:
            continue
        if not t.files:
            return ()
        files.extend(f for f in t.files if f not in files)
    return tuple(files)


def _fix_check(base: CheckBase, state: FactoryState, resolve: ArtifactResolver) -> CheckSpec:
    """A fixer session's gate check: the scenarios a wave check judges, its own the
    planned failure cluster's items (every failure when no cluster is planned)."""
    from foundry.gates.self_check import CheckSpec
    from foundry.graph.gate_wiring import wave_scenarios

    cluster, _ = fix_target(state, resolve, _failed_gates(state))
    items = cluster.items if cluster is not None else ()
    return CheckSpec(
        base=base, scope=wave_scenarios(state, resolve),
        own_refs=(frozenset(i.partition(":")[2] for i in items if i.startswith("test:"))
                  if cluster is not None else None),
        own_build=cluster is None or any(i.startswith("build:") for i in items),
        rules=dict(zip(_FIXER_RULES, _FIXER_CHECK_RULES, strict=False)),
    )


def make_fixer_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
    self_check: SelfCheckSettings | None = None,
) -> NodeFn:
    """One coding session that repairs the INTEGRATED tree after a failed gate.

    Re-running every task from scratch on each fix iteration rebuilt the whole
    project (one fresh agent session per task) to repair what is usually a local
    failure. The fixer instead seeds a single sandbox with the integrated tree, hands
    the adapter the latest failures behind a :func:`_fix_summary`, and emits one
    FeatureDiff whose seq is lifted past every existing diff, so integration applies
    it on top (later-by-Seq wins). ``self_check`` gives the session the gates as a
    ``check`` tool, its own failures the planned cluster's."""
    if rt.adapter is None or rt.cas is None:
        raise ValueError("fixer requires rt.adapter and rt.cas (it rebuilds the integrated tree)")

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        from foundry.graph.gate_wiring import (
            FEATURE_DIFF_KIND,
            MaterializationError,
            _fix_attempts,
            _of_kind,
            integrated_files,
            lift_epoch,
        )

        attempt = _fix_attempts(state, config)
        budget = turn_budget
        # The fixer's packet carries no tier, so it runs on the default level.
        fix_tier: str | None = None
        is_local = getattr(rt.adapter, "is_local", None)
        if callable(is_local) and is_local(fix_tier):
            budget = max(turn_budget, _LOCAL_FIX_TURN_BUDGET)
        try:
            overlay: dict[str, str | None] = dict(
                integrated_files(state, resolve=resolve, resolve_blob=resolve_blob))
        except MaterializationError as exc:
            # The gate already recorded this tree as ERROR; returning no diff lets the
            # bounded fix loop halt in order instead of crashing the run.
            return {"agent_journal": [journal_entry(
                node_id=FIXER_NODE_ID, role="Backend Engineer",
                seq=rt.seq(config, FIXER_NODE_ID, f"journal:fix-{attempt}"),
                summary="Could not rebuild the integrated tree to fix it",
                concerns=(str(exc)[:400],),
            )]}
        from foundry.agents.implementation.tickets import (
            CONTRACT_DUP_TRIAGE,
            contract_duplicates,
            verify_commands,
        )
        from foundry.graph.gate_wiring import contract_paths

        # Contract-type copies type-check alone and then misfit their callers; hand
        # any still in the integrated tree to the fixer with the gate failures.
        duplicates = contract_duplicates(overlay, contract_paths(state, resolve))
        from foundry.agents.implementation.tickets import group_failures_by_ticket, read_tickets

        failures = (
            *_fix_summary(state, resolve, resolve_blob, attempt=attempt, tree=overlay),
            *group_failures_by_ticket(fix_failures(state, resolve), read_tickets(state, resolve)),
        )
        if duplicates:
            failures = (*failures, f"[{CONTRACT_DUP_TRIAGE}]", *duplicates)
        from foundry.agents.implementation.diagnosis import diagnose, diagnosis_tier

        diagnosis = None
        if (cloud := diagnosis_tier(rt, fix_tier)) is not None:
            diagnosis = diagnose(rt, cloud, label=f"fix attempt {attempt}", summary=failures,
                                 files=_named_files(_failed_gates(state), overlay),
                                 tree=overlay)
            if diagnosis is not None:
                failures = (*diagnosis.packet_lines(), *failures)
        from foundry.agents.implementation.tickets import note_fix_writes, pending_wave

        focus = _fix_focus(state, resolve)
        contracts = contract_paths(state, resolve)
        persona = _FIXER_PERSONA
        if focus and pending_wave(state, resolve):
            persona += (" Later waves have not been built yet: fix the files built so far ("
                        + ", ".join(focus) + "), and never build a later ticket's files. "
                        "A change anywhere else is kept but flagged for review.")
        if contracts:
            persona += (" Every ticket builds against the contract files ("
                        + ", ".join(contracts) + "): change them only when the failure is "
                        "in the contract itself; each change is recorded for review.")
        # Numbered by the fixes so far, not the attempt counter: that resets per wave
        # and on a resume, and a reused id would collide with the earlier fix's notes.
        fixes = {a.task_id for a in _of_kind(state, FEATURE_DIFF_KIND)
                 if a.task_id and a.task_id.startswith("fix-")}
        task_id = f"fix-{len(fixes) + 1}"
        task = Task(
            id=task_id,
            spec_uri=f"fix://{attempt}",
            criterion_ids=(),
            branch=f"task/{task_id}",
            seq=rt.seq(config, FIXER_NODE_ID, f"task:{task_id}"),
        )
        with contextlib.ExitStack() as base_scope:
            base: CheckBase | None = None
            if self_check is not None and self_check.enabled():
                from foundry.gates.self_check import check_base

                base = base_scope.enter_context(check_base(
                    state, resolve=resolve, resolve_blob=resolve_blob, rt=rt,
                    settings=self_check))
            writes = _run_one_task(
                rt,
                task,
                state.get("pinned_constraints") or (),
                config,
                source_dir=source_dir,
                snapshot_store=snapshot_store,
                turn_budget=budget,
                cost_budget_microusd=cost_budget_microusd,
                prior_failures=failures,
                persona=persona,
                overlay=overlay,
                rules=_FIXER_RULES,
                verify=verify_commands(state.get("stack")),
                review=lambda result, sandbox: note_fix_writes(
                    result, sandbox, task_id, focus=focus, contracts=contracts),
                check=_fix_check(base, state, resolve) if base is not None else None,
            )
        # Integration applies it on top of the diffs it repairs (none yet when every
        # ticket failed).
        writes["artifacts"] = [a.model_copy(update={"seq": lift_epoch(state, a.seq)})
                               for a in writes["artifacts"]]
        writes.setdefault("agent_journal", []).append(journal_entry(
            node_id=FIXER_NODE_ID, role="Backend Engineer",
            seq=rt.seq(config, FIXER_NODE_ID, f"journal:{task_id}"),
            summary=f"Fixed the failing gates (attempt {attempt})",
            concerns=((f"diagnosis from {cloud}: {diagnosis.summary()}",)
                      if diagnosis is not None else ()),
        ))
        return writes

    return _node
