r"""Graph-side gate wiring — run the deterministic gates at phase boundaries.

(governance §1c, architecture.md §3/§4/§7, implementation-plan §3.1)

This is the TRUST SEAM on the graph side: it materializes the integrated tree
from the content-addressed artifacts on ``state``, runs the Spec / Build / Test
gates through the :class:`~foundry.gates.runner.TwoZoneGateRunner` at a phase
boundary, and folds the runner-ATTESTED verdicts onto the ``gates`` channel.

What this module is careful about (the invariants the next waves reuse):

* **Verdicts come ONLY from the runner (LEARNINGS L5).** Nothing here ever
  constructs a :class:`GateResult` — the runner executes zone A, decides +
  signs in zone B, and returns an attested record. ``merge_gates`` admits a
  deterministic-gate write iff it carries a valid zone-B attestation, so an
  unsigned ``GateResult`` is dropped fail-closed. We only ever forward what the
  runner handed back.

* **Fail-closed materialization.** ``materialize_workdir`` writes the
  integrated tree the gates run against. The Spec gate's ``criteria.json`` is
  written from the AcceptanceCriterion artifacts; the code + test sources come
  from the FeatureDiff artifacts via the injected ``resolve`` (the SAME
  ``ArtifactResolver`` seam senior_engineer / qa use, never a parallel one). A
  handle present on state but unresolvable is an ERROR we RAISE (fail-closed) —
  the boundary must never silently run the gates against a partial tree and
  mint a vacuous PASS.

* **Integrated digest is the ``run_against``.** The materialized tree's
  ``tree_sha`` is the integrated digest the verdict binds + (on FAIL) latches
  against — never a per-branch ``task/<id>`` ref. (architecture.md §4)

* **Routing is fail-closed + convergence-bounded.** ``route_after_gates`` reads
  the boundary's relevant verdicts off ``state["gates"]`` and returns one of
  ``advance`` / ``fix`` / ``halt``. A stable-FAIL latch (committed cross-fork)
  is terminal → ``halt``; an exhausted ``fix_attempts`` bound → ``halt``; an
  ERROR (infra, not a verdict — DS7) routes to ``fix`` (bounded retry, never a
  latch); a flake-quarantine ERROR (N=2 split) does NOT fail the boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.agents._runtime import AgentRuntime
from foundry.gates.build import BuildGate
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.runner import GateImpl, TwoZoneGateRunner
    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

    # The SAME resolver seam senior_engineer uses (LEARNINGS QS3 / IM4):
    # reconstitute an out-of-band body from its content-addressed handle. Typed
    # bodies (AcceptanceCriterion / FeatureDiff) resolve to their FrozenModel; a
    # FeatureDiff's diff_uri resolves to its path->content mapping — so the
    # return is ``Any`` (mirrors senior_engineer.ArtifactResolver).
    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "CRITERION_KIND",
    "FEATURE_DIFF_KIND",
    "FeatureDiffBodyError",
    "MaterializationError",
    "flake_quarantined",
    "materialize_workdir",
    "route_after_gates",
    "run_build_test_gates",
    "run_spec_gate",
]

# Artifact.kind tags the upstream layers stamp on their handles — the seam we
# read the boundary's DATA inputs back off the artifacts channel by.
CRITERION_KIND = "acceptance_criterion"  # product layer (LEARNINGS QS3)
FEATURE_DIFF_KIND = "feature_diff"  # implementation layer (junior_engineer)

# Dev-grade toolchain digest for tree_sha. Production threads the pinned builder
# image digest in; here a stable constant keeps the integrated digest a pure
# function of the materialized source (DS4).
_TOOLCHAIN_DIGEST = "foundry-dev-toolchain-v1"


class MaterializationError(RuntimeError):
    """A handle present on state could not be materialized into the workdir.

    Fail-closed: the boundary must never run the gates against a partial tree
    (that would risk a vacuous PASS, governance §1b). Raised when an artifact
    handle of a required kind resolves to the wrong type, or a FeatureDiff body
    cannot be turned into file content.
    """


class FeatureDiffBodyError(MaterializationError):
    """A resolved FeatureDiff body is not a path->content mapping.

    The materialization contract: ``resolve(feature_diff_artifact)`` returns the
    typed :class:`FeatureDiff`, whose ``diff_uri`` body — resolved through the
    SAME ``resolve`` — is a ``Mapping[str, str]`` of relative POSIX path to file
    content (the integrated worktree slice the diff contributes). A diff handle
    present but unresolvable to that shape is fail-closed.
    """


# ---- artifact reads (by kind, newest-by-Seq) --------------------------------


def _of_kind(state: FactoryState, kind: str) -> list[Artifact]:
    return [a for a in (state.get("artifacts") or ()) if a.kind == kind]


def _resolve_typed(
    artifact: Artifact, resolve: ArtifactResolver, expected: type[Any], kind: str
) -> Any:
    """Reconstitute a typed body from its handle; fail-closed on type mismatch."""
    body = resolve(artifact)
    if not isinstance(body, expected):
        raise MaterializationError(
            f"resolved {kind!r} handle {artifact.uri!r} to {type(body).__name__}, "
            f"expected {expected.__name__}"
        )
    return body


# ---- criteria.json (the exact shape SpecGate parses) ------------------------


def _criteria_payload(
    state: FactoryState, resolve: ArtifactResolver
) -> list[dict[str, Any]]:
    """The ``criteria.json`` array SpecGate parses, built from the artifacts.

    Each entry carries the fields SpecGate's zone-A read expects: ``id``,
    ``text``, ``oracle`` (mapped from :attr:`AcceptanceCriterion.oracle_hint`),
    and ``security_abuse``. Ordered by criterion ``id`` so the materialized file
    — and thus the integrated ``tree_sha`` — is byte-stable across folds/resume.
    """
    criteria: list[AcceptanceCriterion] = [
        _resolve_typed(a, resolve, AcceptanceCriterion, CRITERION_KIND)
        for a in _of_kind(state, CRITERION_KIND)
    ]
    return [
        {
            "id": c.id,
            "text": c.text,
            "oracle": c.oracle_hint,
            "security_abuse": c.security_abuse,
        }
        for c in sorted(criteria, key=lambda c: c.id)
    ]


# ---- code + test sources from FeatureDiff handles ---------------------------


def _diff_files(
    diff: FeatureDiff, artifact: Artifact, resolve: ArtifactResolver
) -> Mapping[str, str]:
    """The path->content slice a FeatureDiff contributes to the worktree.

    The diff BODY lives out-of-band behind ``diff.diff_uri`` (DS9); resolve it
    through the SAME seam. It must be a ``Mapping[str, str]`` (relative POSIX
    path -> file content) so the gates have real sources to compile/run.
    Fail-closed on any other shape — a present-but-unmaterializable diff must
    not let a partial tree clear the gate.
    """
    body = resolve(artifact.model_copy(update={"uri": diff.diff_uri}))
    if not isinstance(body, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in body.items()
    ):
        raise FeatureDiffBodyError(
            f"FeatureDiff body for {diff.diff_uri!r} is not a path->content mapping "
            f"(got {type(body).__name__})"
        )
    return body


def _write_sources(dest: Path, state: FactoryState, resolve: ArtifactResolver) -> None:
    """Materialize every FeatureDiff's files into ``dest`` (later-by-Seq wins).

    Diffs are applied in ``Seq`` order so a retried diff (higher epoch, L4)
    overwrites the predecessor it supersedes — the worktree reflects the
    integrated latest, deterministically and fold-order-independently.
    """
    diff_artifacts = sorted(_of_kind(state, FEATURE_DIFF_KIND), key=lambda a: a.seq)
    for artifact in diff_artifacts:
        diff = _resolve_typed(artifact, resolve, FeatureDiff, FEATURE_DIFF_KIND)
        for rel, content in _diff_files(diff, artifact, resolve).items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def materialize_workdir(
    state: FactoryState, dest: Path, *, resolve: ArtifactResolver
) -> str:
    """Write the integrated tree into ``dest`` and return its ``tree_sha``.

    Builds the workdir the deterministic gates run against from the
    content-addressed artifacts on ``state``:

    * ``criteria.json`` — from the AcceptanceCriterion handles, in the exact
      shape SpecGate parses (``id`` / ``text`` / ``oracle`` / ``security_abuse``).
    * code + test sources — from the FeatureDiff handles, each diff body
      (a path->content mapping behind its ``diff_uri``) materialized through the
      injected ``resolve`` (the SAME seam senior_engineer / qa use).

    Fail-closed (governance §1b): a handle present on state but unresolvable
    raises :class:`MaterializationError` — the boundary never runs against a
    partial tree. Returns the integrated ``tree_sha`` (DS4) to use as the
    verdict's ``run_against`` (the digest the FAIL latch keys on).
    """
    dest.mkdir(parents=True, exist_ok=True)
    # Spec gate input: the criteria document, byte-stable across folds.
    (dest / "criteria.json").write_text(
        as_text(_criteria_payload(state, resolve)), encoding="utf-8"
    )
    # Build / Test gate inputs: the integrated code + test sources.
    _write_sources(dest, state, resolve)
    # prefer_git=False: dest is a fresh materialized tree, not a git worktree.
    return tree_sha(dest, toolchain_digest=_TOOLCHAIN_DIGEST, prefer_git=False)


# ---- gate-run helpers (verdicts ONLY from the runner) -----------------------

# Spec is keyed under the criteria/diff emission point; build/test under their
# own keys so each verdict gets an independent resume-monotonic epoch stream.
_SPEC_NODE = "spec_gate"
_BUILD_NODE = "build_gate"
_TEST_NODE = "test_gate"


def _run_gate(
    gate: GateImpl,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    config: RunnableConfig,
    run_against: str,
    workdir: Path,
    node_id: str,
) -> GateResult:
    """Run ONE gate via the runner and return its ATTESTED verdict.

    Mints the verdict's ``Seq`` via ``rt.seq`` (resume-monotonic epoch, AR2)
    keyed on the gate's grounding so a re-execution outranks the stale record it
    supersedes. The runner attests the result; we never sign or construct one.
    """
    seq = rt.seq(config, node_id, f"verdict:{gate.name}")
    return runner.run(gate, run_against, seq, workdir)


def run_spec_gate(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    workdir: Path,
) -> dict[str, Any]:
    """Run the Spec gate at a product / architecture exit boundary.

    Materializes the workdir (criteria.json + any sources present) into
    ``workdir`` and runs :class:`SpecGate` through the runner against the
    integrated ``tree_sha``. Returns ``{"gates": [<attested GateResult>]}`` —
    the ONLY thing that reaches the channel, and only because the runner
    attested it (an unsigned write would be dropped, L5).
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve)
    result = _run_gate(
        SpecGate(),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_SPEC_NODE,
    )
    return {"gates": [result]}


def run_build_test_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    workdir: Path,
    min_passed: int = 1,
) -> dict[str, Any]:
    """Run the Build + Test gates at an implementation / quality exit boundary.

    Materializes the integrated tree (code + test sources + criteria.json) into
    ``workdir`` ONCE and runs :class:`BuildGate` then :class:`TestGate` against
    its ``tree_sha`` (the same integrated digest, so both verdicts latch/bind to
    one state). ``min_passed`` is the Test gate's adequacy floor — a vacuous
    (0-test) suite is FAIL, never PASS (governance §1c). Returns
    ``{"gates": [<attested build>, <attested test>]}``.
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve)
    build = _run_gate(
        BuildGate(),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_BUILD_NODE,
    )
    test = _run_gate(
        TestGate(min_passed=min_passed),
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
        node_id=_TEST_NODE,
    )
    return {"gates": [build, test]}


# ---- routing predicate (fail-closed, convergence-bounded) -------------------

# Which gate verdicts each phase boundary is responsible for.
SPEC_BOUNDARY = "spec"
BUILD_TEST_BOUNDARY = "build_test"

_BOUNDARY_GATES: dict[str, frozenset[str]] = {
    SPEC_BOUNDARY: frozenset({"spec"}),
    BUILD_TEST_BOUNDARY: frozenset({"build", "test"}),
}

# Routing outcomes.
ADVANCE = "advance"  # all relevant gates PASS — proceed to the next phase
FIX = "fix"  # a FAIL/ERROR within the convergence bound — back to the layer
HALT = "halt"  # bound exhausted or a stable-FAIL latch — escalate / stop


def _relevant_latest(
    state: FactoryState, gate_names: frozenset[str]
) -> dict[str, GateResult]:
    """Newest-by-Seq verdict per relevant gate name on ``state['gates']``.

    The channel is already trust-filtered (``merge_gates`` admitted only
    runner-attested records) and keyed by ``(name, run_against)``; pick the
    latest by ``Seq`` per name so a re-run's verdict (bumped epoch) wins.
    """
    latest: dict[str, GateResult] = {}
    for g in state.get("gates") or ():
        if g.name not in gate_names:
            continue
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def _is_flake_quarantine(g: GateResult) -> bool:
    """An execution gate's ERROR caused by flake quarantine, not a hard verdict.

    The N=2 flake rule (E2E) and a quarantining Test run surface a split/flaky
    outcome as ERROR (DS7 — a flake is a property of the execution, not the
    tree). Such an ERROR must NOT fail the boundary: it quarantines (routes to
    bounded retry), never latches, never advances. Detected by gate family +
    a quarantine/flake marker in the runner findings.
    """
    if g.status is not GateStatus.ERROR or g.name not in {"test", "e2e"}:
        return False
    blob = " ".join(g.findings).lower()
    return "flak" in blob or "quarantin" in blob


def flake_quarantined(state: FactoryState, *, boundary: str) -> bool:
    """True iff a relevant Test/E2E verdict at ``boundary`` is flake-quarantined.

    A supervisor seam: distinguishes a transient flake-quarantine ERROR (retry
    without disqualifying the digest — it never latches and must NOT fail the
    boundary, governance §1a) from a hard FAIL / infra ERROR. The routing
    predicate already keeps a quarantine ERROR out of ADVANCE; this exposes the
    REASON so the supervisor can quarantine-and-retry rather than report a
    failure.
    """
    gate_names = _BOUNDARY_GATES.get(boundary, frozenset())
    return any(
        _is_flake_quarantine(g) for g in _relevant_latest(state, gate_names).values()
    )


def _fix_attempts(state: FactoryState, config: RunnableConfig) -> int:
    """The convergence bound's spent attempts (architecture.md §7).

    Read from the budget ledger's global ``fix_attempts_since_all_green`` if
    present, else from the run config — the supervisor owns the counter; the
    routing predicate only READS it (fold-order independent)."""
    budget = state.get("budget")
    if budget is not None:
        return int(budget.fix_attempts_since_all_green)
    configurable = config.get("configurable") or {}
    return int(configurable.get("fix_attempts", 0))


def route_after_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int = 3,
) -> str:
    """Route a phase boundary on its relevant gate verdicts. Fail-closed.

    Returns:

    * :data:`ADVANCE` — every relevant gate has a PASS verdict (and none is
      latched); proceed to the next phase.
    * :data:`FIX` — a FAIL or (non-flake) ERROR within the convergence bound, or
      a relevant verdict missing entirely (defense-in-depth: never advance on an
      absent verdict); route back to the responsible layer.
    * :data:`HALT` — a stable-FAIL latch on the integrated digest (committed
      cross-fork, terminal — a later PASS never auto-clears it), or the
      ``max_fix_attempts`` convergence bound exhausted; escalate / stop.

    ``records`` is the cross-fork :class:`GlobalRecordsView` consulted for the
    stable-FAIL latch (a mutable committed read, never cached). A flake-
    quarantine ERROR (N=2 split) is treated as a transient retry, NOT a FAIL —
    it routes to FIX within the bound, HALT once the bound is spent, but never
    fails or advances the boundary on its own.
    """
    gate_names = _BOUNDARY_GATES.get(boundary)
    if gate_names is None:
        raise ValueError(f"unknown phase boundary: {boundary!r}")

    latest = _relevant_latest(state, gate_names)

    # 1. Stable-FAIL latch is terminal — keyed on the integrated digest the
    #    verdict ran against (monotonic, cross-fork; governance §5).
    for g in latest.values():
        if records.stable_fail_latched(g.run_against, g.name):
            return HALT

    bound_spent = _fix_attempts(state, config) >= max_fix_attempts

    # 2. A hard FAIL, or an ERROR that is not flake-quarantine, needs a fix —
    #    or HALT if the convergence bound is exhausted.
    needs_fix = False
    for name in gate_names:
        verdict = latest.get(name)
        if verdict is None:
            needs_fix = True  # missing relevant verdict: never advance (fail-closed)
            continue
        if verdict.status in (GateStatus.FAIL, GateStatus.ERROR):
            # FAIL, infra ERROR, OR flake-quarantine ERROR all route to bounded
            # retry — none advances the boundary (fail-closed).
            needs_fix = True

    if needs_fix:
        return HALT if bound_spent else FIX

    # 3. All relevant gates PASS and nothing is latched.
    return ADVANCE
