r"""Supervisor — phase routing across the four layer subgraphs (T-M1-13).

(architecture.md §1/§7, agents.md "Triage SOP" + Layer 1-4, governance §1)

The supervisor is the spine that SEQUENCES the four role subgraphs and wires the
deterministic gates at the phase boundaries. It is the only place phase order +
gate-wiring routing live; the layer subgraphs themselves hold no cross-phase
knowledge (each is an UNCOMPILED ``StateGraph`` the supervisor mounts as a node).

Topology (the SOP phase order — agents.md, governance §1 gate boundaries)::

    START
      -> triage              # classify -> route + seed per-route budget
      -> planner             # ONE model call: PRD + criteria + design + ownership
      -> spec_gate           # [SPEC gate] criteria x design
         --(advance)--> implementation
         --(fix)-----> spec_fix -> planner             (bounded re-verification)
         --(halt)----> END
      -> implementation      # Layer 3 subgraph (Task[] -> FeatureDiff[])
      -> quality             # Layer 4 subgraph (TestPlan/Report/E2E + review)
      -> build_test_gate     # [BUILD + TEST gates] on the integrated tree
         --(advance)--> END
         --(fix)-----> build_test_fix -> implementation   (bounded re-verification)
         --(halt)----> END

Gate nodes (``spec_gate`` / ``build_test_gate``) call the gate-wiring helpers
(:func:`run_spec_gate` / :func:`run_build_test_gates`) — the verdict comes ONLY
from the injected ``TwoZoneGateRunner`` (LEARNINGS L5/GW2); the node merely
forwards the attested ``GateResult`` onto the ``gates`` channel.

Routing at each boundary uses :func:`route_after_gates` (LEARNINGS GW3,
fail-closed): ``advance`` proceeds to the next phase, ``fix`` routes back to the
responsible layer for re-verification within ``max_fix_attempts``, ``halt`` ends
the run (a stable-FAIL latch, or the convergence bound spent). The convergence
bound is read off ``budget.fix_attempts_since_all_green`` (BudgetLedger,
single-writer); a dedicated ``*_fix`` node BUMPS it on each fix iteration so the
loop is bounded — no infinite cycle. (LEARNINGS SUP1.)

This builder returns the UNCOMPILED ``StateGraph`` (mirrors the subgraph
builders, SG4): ``factory_graph`` (T-M1-14) compiles it, adds
the shared checkpointer, and appends the assistant-mode emit node. The supervisor
defines NONE of that here.

The ``add_node`` mypy seam (LEARNINGS SG2/IM5): a factory closure typed as the
``NodeFn`` ``Callable`` alias trips ``add_node``'s overload resolution, so every
node is wrapped in a thin concrete ``def`` (no ``cast``, no private import).

"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foundry.agents.implementation.senior_engineer import DESIGN_KIND
from foundry.agents.implementation.tickets import cap_stopped, pending_wave
from foundry.governance.budget_controller import BudgetCaps, BudgetController
from foundry.governance.release_anchors import newest_artifact_uri, state_sha
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    BUILD_TEST_SMOKE_BOUNDARY,
    SPEC_BOUNDARY,
    WaveBuildCheck,
    bump_fix_attempts,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
)
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.graph.subgraphs.planner import build_planner_subgraph
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.proxy.token_meter import TokenCapExceeded
from foundry.schemas.base import BudgetLedger
from foundry.schemas.checkpoint import (
    CheckpointBoundary,
    CheckpointDecision,
    CheckpointVerdict,
    inspection_payload,
)
from foundry.schemas.gates import GateResult, GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import JUDGMENT_VERDICTS, merge_gates

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.execution.work_pool import TaskEventHook
    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver

__all__ = [
    "DEFAULT_ROUTE_CAPS_MICROUSD",
    "MODE_ASSISTANT",
    "ROUTES",
    "build_supervisor_graph",
    "classify_request",
    "normalize_checkpoints",
]

# ---- supervisor mode --------------------------------------------------------

MODE_ASSISTANT = "assistant"  # the only release mode

# ---- supervisor node ids ----------------------------------------------------

_TRIAGE = "triage"
# The planner subgraph (product + architecture in one model call).
_PLANNER = "planner"
_SPEC_GATE = "spec_gate"
_IMPLEMENTATION = "implementation"
_QUALITY = "quality"
_BUILD_TEST_GATE = "build_test_gate"
# The bounded re-verification nodes: bump the convergence counter, then edge
# back to the responsible layer (the layer that must regenerate the tree).
_SPEC_FIX = "spec_fix"
_BUILD_TEST_FIX = "build_test_fix"
# Raises a token cap the implementation layer hit, AFTER its writes are committed.
_TOKEN_CAP = "token_cap"
# ---- checkpoint/HITL pause node ids (plan [10]) -----------------------------
# A pause node per stage boundary; built+wired ONLY when that boundary is in the
# selected ``checkpoints`` set. The id encodes the boundary so the rendered
# topology shows which stage each pause guards.
_CHECKPOINT_NODE: dict[CheckpointBoundary, str] = {
    CheckpointBoundary.PRODUCT: "checkpoint_product",
    CheckpointBoundary.DESIGN: "checkpoint_design",
    CheckpointBoundary.RELEASE: "checkpoint_release",
}
# Boundaries after the planner: prompted only on its first pass, not on re-plans.
_PLAN_BOUNDARIES = frozenset({CheckpointBoundary.PRODUCT, CheckpointBoundary.DESIGN})
# The conditional-edge labels a checkpoint router returns.
_CKPT_PROCEED = "proceed"  # human approve -> continue to the gate boundary
_CKPT_HALT = "halt"  # human reject/needs-changes -> END (no PR / no release)


def _commit_judgment_ledger(
    prior: list[GateResult] | None,
    delta: list[GateResult] | None,
    records: Any,
) -> None:
    """Trusted post-admission ledger WRITE for judgment verdicts (H3 / arch §3).

    The orchestrator-side write that makes ``merge_gates``' read-only nonce +
    negative-latch checks live: for each judgment verdict that SURVIVES the
    ``merge_gates`` trust filter, consume its single-use ``verdict_nonce`` (keyed to
    the verdict's content identity — idempotent, so re-folding the SAME verdict on a
    resume is a no-op) and latch a request-changes (status FAIL) monotonically so a
    later approve can't supersede it without a fresh nonce-bound re-review (LEARNINGS
    Q3). Only ADMITTED verdicts are committed, so a forged/replayed verdict can never
    burn the legit verdict's nonce. ``merge_gates`` is a PURE reducer and cannot do
    this; the supervisor (which holds the cross-fork ``records``) does it here.
    """
    for g in merge_gates(prior, delta):
        if g.name in JUDGMENT_VERDICTS and g.verdict_nonce:
            records.consume_nonce(g.verdict_nonce, g.content_identity())
            if g.status is GateStatus.FAIL:
                records.latch_negative_verdict(g.latch_key())


def normalize_checkpoints(
    checkpoints: bool | Sequence[str | CheckpointBoundary] | None,
) -> frozenset[CheckpointBoundary]:
    """Resolve the ``checkpoints`` argument to the SET of boundaries to pause at.

    The selectable affordance (plan [10]): ``None``/``False`` → no checkpoints
    (the DEFAULT — a non-checkpoint run is byte-identical to today); ``True`` →
    ALL three stage boundaries (:data:`CHECKPOINT_BOUNDARIES`); an explicit
    sequence of boundary names/enums → exactly those. Unknown names raise
    ``ValueError`` (fail-closed — a typo never silently disables a checkpoint)."""
    if not checkpoints:
        return frozenset()
    if checkpoints is True:
        return frozenset(CheckpointBoundary)
    out: set[CheckpointBoundary] = set()
    for item in checkpoints:
        if isinstance(item, CheckpointBoundary):
            out.add(item)
        else:
            try:
                out.add(CheckpointBoundary(item))
            except ValueError as exc:
                allowed = ", ".join(b.value for b in CheckpointBoundary)
                raise ValueError(
                    f"unknown checkpoint boundary {item!r}; choose from {allowed}"
                ) from exc
    return frozenset(out)


def _coerce_decision(
    raw: Any,
    *,
    boundary: CheckpointBoundary,
    state_sha: str,
    subject_artifact_uri: str,
    seq: Any,
) -> CheckpointDecision:
    """Coerce the value fed back via ``Command(resume=...)`` into a recorded
    :class:`CheckpointDecision`, binding it to THIS boundary + the integrated
    ``state_sha`` (so the recorded decision is provenance-keyed, never replayable
    across boundaries).

    Accepts a ready :class:`CheckpointDecision` (re-keyed to this boundary/state),
    a bare verdict string/enum (``"approve"``/``"reject"``/``"needs_changes"``), or
    a mapping carrying ``verdict`` plus optional ``steered_by``/``note`` (steering
    AUTHORSHIP, lifecycle §2). Fail-closed: an unrecognized/empty value is treated
    as REJECT — an absent/garbled human decision NEVER auto-approves a release."""
    verdict: CheckpointVerdict = CheckpointVerdict.REJECT
    steered_by = ""
    note = ""
    if isinstance(raw, CheckpointDecision):
        verdict, steered_by, note = raw.verdict, raw.steered_by, raw.note
    elif isinstance(raw, CheckpointVerdict):
        verdict = raw
    elif isinstance(raw, str):
        verdict = _verdict_from_str(raw)
    elif isinstance(raw, dict):
        verdict = _verdict_from_str(str(raw.get("verdict", "")))
        steered_by = str(raw.get("steered_by", "") or "")
        note = str(raw.get("note", "") or "")
    return CheckpointDecision(
        boundary=boundary,
        verdict=verdict,
        state_sha=state_sha,
        subject_artifact_uri=subject_artifact_uri,
        steered_by=steered_by,
        note=note,
        seq=seq,
    )


def _verdict_from_str(value: str) -> CheckpointVerdict:
    """Map a verdict string to :class:`CheckpointVerdict`; fail-closed to REJECT.

    An unrecognized token is REJECT (never auto-approve) — a checkpoint can only
    HALT or proceed-to-the-gates, so a garbled human decision halts the run."""
    try:
        return CheckpointVerdict(value.strip().lower())
    except ValueError:
        return CheckpointVerdict.REJECT


def _newest_decision(
    state: FactoryState, boundary: CheckpointBoundary
) -> CheckpointDecision | None:
    """The newest-by-``Seq`` checkpoint decision recorded at ``boundary`` (or
    ``None`` if none yet) — what the router and emit guard read."""
    best: CheckpointDecision | None = None
    for d in state.get("checkpoint_decisions") or ():
        if d.boundary != boundary:
            continue
        if best is None or d.seq > best.seq:
            best = d
    return best

# ---- triage classification (Phase-1 deterministic classifier) ---------------

ROUTES = ("greenfield", "evolution", "bugfix", "spike")

# Default per-route absolute budget cap (integer micro-USD; refined later by the
# Eng/Program Manager on routes that visit PLAN — agents.md Triage SOP). A
# bugfix is the cheapest route; a greenfield the most expensive.
DEFAULT_ROUTE_CAPS_MICROUSD: dict[str, int] = {
    "greenfield": 50_000_000,
    "evolution": 30_000_000,
    "bugfix": 10_000_000,
    "spike": 8_000_000,
}

# Substring markers that classify a request string (first match wins, in order).
# Deterministic + cheap (Phase 1); a learned classifier is a later phase. The
# DEFAULT is greenfield (the most capable route — never under-provision).
_ROUTE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bugfix", ("bug", "fix ", "regression", "broken", "crash", "defect", "hotfix")),
    ("spike", ("spike", "investigate", "research", "explore", "prototype", "feasibility")),
    ("evolution", ("add ", "extend", "evolve", "refactor", "migrate", "change ", "update ")),
)


def classify_request(request: str) -> str:
    """Classify a raw request string into one of :data:`ROUTES` (deterministic).

    A simple substring classifier (Phase 1, agents.md Triage SOP): bugfix /
    spike / evolution markers win in order; otherwise the request is treated as
    a ``greenfield`` build (the safe default — the most capable route). The
    classification is purely a function of the request text, so it is stable
    across a kill-then-resume.
    """
    text = request.lower()
    for route, markers in _ROUTE_MARKERS:
        if any(marker in text for marker in markers):
            return route
    return "greenfield"


def _seed_budget(route: str) -> BudgetLedger:
    """Seed the per-route :class:`BudgetLedger` cap via the Budget Controller.

    The controller is the single budget authority (governance §7); we use it to
    derive the per-route absolute cap, then stamp it onto the single-writer
    ``budget`` channel as the initial ledger (zero spend, zero fix attempts).
    """
    cap = DEFAULT_ROUTE_CAPS_MICROUSD.get(route, DEFAULT_ROUTE_CAPS_MICROUSD["greenfield"])
    controller = BudgetController(BudgetCaps(total_microusd=cap))
    return BudgetLedger(cap_microusd=controller.caps.total_microusd)


# ---- builder ----------------------------------------------------------------


def build_supervisor_graph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str,
    index: SymbolIndex | None = None,
    max_fix_attempts: int = 3,
    discriminator_factory: DiscriminatorFactory | None = None,
    profile: VerificationProfile | None = None,
    checkpoints: bool | Sequence[str | CheckpointBoundary] | None = None,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
    workers: Mapping[str, int] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
    on_task: TaskEventHook | None = None,
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED supervisor graph over :class:`FactoryState`.

    Parameters
    ----------
    rt:
        Shared :class:`AgentRuntime` threaded into every layer subgraph + gate
        node (one ``EpochCounter`` + one model egress; AR1/L6). Its ``adapter``
        must be set (the implementation layer's Junior requires it).
    runner:
        The :class:`TwoZoneGateRunner` the gate nodes run Spec / Build / Test
        through — verdicts come ONLY from it (L5/GW2). Holds the zone-B signer +
        the cross-fork :class:`GlobalRecords` (the stable-FAIL latch routing
        reads).
    resolve / resolve_blob:
        The two materialization seams (GW1a/D-R1-02): ``resolve`` reconstitutes
        a TYPED artifact body (AcceptanceCriterion / FeatureDiff / SystemDesign /
        OwnershipMap) from its content-addressed handle; ``resolve_blob`` fetches
        a raw diff BODY keyed on its ``cas://`` uri (prod: ``sandbox.get_blob``).
        Threaded into both the implementation/quality subgraphs and the gate
        nodes.
    source_dir:
        Base tree cloned into each per-task sandbox (the implementation layer's
        Junior; INFRA).
    index:
        Context retriever for the planner (``None`` = greenfield,
        empty retrieval; AC1).
    max_fix_attempts:
        Convergence bound: a gate boundary may route ``fix`` (re-verification)
        at most this many times before ``route_after_gates`` returns ``halt``.
        Tracked on ``budget.fix_attempts_since_all_green`` (bumped by the
        ``*_fix`` node), so the rework loop can never spin forever.
    discriminator_factory:
        OPTIONAL malicious-test defense (A-M4-01). When provided, the
        ``build_test_gate`` node threads it into :func:`run_build_test_gates`, so
        the COMPILED Test gate consults the held-out-mutant + behavioral-invariance
        :class:`~foundry.gates.discriminator.TestSuiteDiscriminator` the factory
        produces — a gamed-green suite that clears the adequacy floor but does not
        kill a held-out mutant FAILs the gate in the SHIPPING graph. ``None`` (the
        default) keeps the adequacy floor only.
    checkpoints:
        OPTIONAL checkpoint/HITL mode (plan [10]). ``None``/``False`` (the
        DEFAULT) inserts NO pause nodes — the topology is byte-identical to today
        (back-compat). ``True`` pauses at ALL three stage boundaries; a sequence
        of boundary names/enums (``"product"``/``"design"``/``"release"``) pauses
        at exactly those. At each enabled boundary a pause node calls LangGraph's
        ``interrupt()`` with a READ-ONLY :func:`inspection_payload` (the stage's
        artifacts + the attested gate verdicts SO FAR + the advisory
        ``agent_journal`` from [11]) and waits for a human :class:`CheckpointDecision`
        fed back via ``Command(resume=...)``. The run is resumable on the EXISTING
        checkpointer. **Trust:** the checkpoint is HUMAN STEERING LAYERED ON TOP of
        the gates — an *approve* only lets the run PROCEED to the (unchanged) gate
        boundary, where the runner-attested verdict still decides; a *reject* halts
        to END. A checkpoint approve can NEVER turn a gate FAIL into a pass: the
        pause node writes only the steering channel, never ``gates``, and the emit
        guard re-checks the gates regardless (factory_graph). The release
        checkpoint sits after the Build+Test gate has run.

    Returns the ``StateGraph`` UNCOMPILED — ``factory_graph`` (T-M1-14) compiles
    it, attaches the shared checkpointer, and appends the emit node.
    """
    ckpt_set = normalize_checkpoints(checkpoints)
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # -- triage: classify + seed route/budget ---------------------------------
    def triage(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        route = classify_request(state.get("request", ""))
        return {
            "phase": _TRIAGE,
            "route": route,
            "budget": _seed_budget(route),
            "mode": MODE_ASSISTANT,
        }

    # -- the four layer subgraphs, compiled + mounted as nodes ----------------
    # Each builder returns an UNCOMPILED StateGraph (SG4); we ``.compile()`` each
    # so it can be added as a single node of the supervisor (its internal
    # channel writes fold through FactoryState's reducers on the parent run).
    planner = build_planner_subgraph(rt, index).compile()
    implementation = build_implementation_subgraph(
        rt, source_dir=source_dir, resolve=resolve, resolve_blob=resolve_blob,
        max_slots=max_slots, cost_cap_microusd=cost_cap_microusd,
        workers=workers, ticket_tiers=ticket_tiers, on_task=on_task,
        wave_check=WaveBuildCheck(
            rt, runner, resolve, resolve_blob,
            max_fix_attempts=max_fix_attempts, profile=profile,
        ),
    ).compile()
    quality = build_quality_subgraph(rt, resolve=resolve).compile()

    def planner_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(planner.invoke(state, config))
        out["phase"] = _PLANNER
        return out

    def implementation_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(implementation.invoke(state, config))
        out["phase"] = _IMPLEMENTATION
        return out

    def quality_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(quality.invoke(state, config))
        # H3: commit the admitted review verdict's nonce/negative-latch to the
        # cross-fork ledger (the trusted write merge_gates' checks read).
        _commit_judgment_ledger(state.get("gates"), out.get("gates"), runner.records)
        out["phase"] = _QUALITY
        return out

    # -- gate nodes: run the gates via the runner; verdicts ONLY from it -------
    # The gate node ALSO bumps the convergence counter when its own boundary is
    # not advancing, so ``route_after_gates`` (the conditional edge that fires
    # in the SAME superstep, reading the post-fold state) sees the updated
    # ``fix_attempts`` and halts once the bound is spent. ``budget`` is
    # single-writer; the gate node is its sole writer in that superstep.
    def _bump_on_non_advance(
        state: FactoryState, config: RunnableConfig, out: dict[str, Any], *, boundary: str
    ) -> None:
        bump_fix_attempts(state, config, out, boundary=boundary,
                          records=runner.records, max_fix_attempts=max_fix_attempts)

    def _gate_workdir(name: str) -> Path:
        # A fresh materialization tree per gate run (materialize_workdir writes
        # into it from the content-addressed handles; the integrated tree_sha is
        # a pure function of those handles, so a per-run temp dir is fine).
        return Path(tempfile.mkdtemp(prefix=f"foundry_{name}_"))

    def spec_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out = run_spec_gate(
            state,
            config,
            rt=rt,
            runner=runner,
            resolve=resolve,
            resolve_blob=resolve_blob,
            workdir=_gate_workdir("spec_gate"),
        )
        out["phase"] = _SPEC_GATE
        _bump_on_non_advance(state, config, out, boundary=SPEC_BOUNDARY)
        return out

    # A profile that pins a smoke (runtime boot) command widens the boundary's
    # responsible verdict set to {build, test, smoke} (ROADMAP Phase 3).
    bt_boundary = (
        BUILD_TEST_SMOKE_BOUNDARY
        if profile is not None and profile.smoke_cmd is not None
        else BUILD_TEST_BOUNDARY
    )

    def build_test_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out = run_build_test_gates(
            state,
            config,
            rt=rt,
            runner=runner,
            resolve=resolve,
            resolve_blob=resolve_blob,
            workdir=_gate_workdir("build_test_gate"),
            discriminator_factory=discriminator_factory,
            profile=profile,
        )
        out["phase"] = _BUILD_TEST_GATE
        _bump_on_non_advance(state, config, out, boundary=bt_boundary)
        return out

    # -- bounded re-verification (fix) nodes ----------------------------------
    # The counter is already bumped by the gate node; the fix node just marks the
    # phase and edges back to the responsible layer. (Kept distinct so the
    # rendered topology shows the re-verification path explicitly.)
    def spec_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _SPEC_FIX}

    def build_test_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _BUILD_TEST_FIX}

    # -- token cap: stop only once the implementation writes are committed -----
    # A ticket that hit the cap ends the implementation subgraph cleanly (its
    # wave-mates' diffs ride out with it); this node, a superstep later, raises.
    # Raising inside the subgraph would discard every write of the pass. On a
    # resume it raises again while the run's meter is still spent; with headroom
    # (a higher cap) it returns and the unbuilt tickets are built.
    def token_cap(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        meter = getattr(rt.proxy, "token_meter", None)
        if meter is None:
            raise TokenCapExceeded(cap_stopped(state)[0].explanation)
        meter.check()
        return {}

    def _route_implementation(state: FactoryState) -> str:
        if cap_stopped(state):
            return _TOKEN_CAP
        # Waves left unbuilt: the per-wave check spent the fix budget (halt in order).
        if pending_wave(state, resolve):
            return "halt"
        return _QUALITY

    # -- conditional routers (advance / fix / halt) ---------------------------
    def _route_spec(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=SPEC_BOUNDARY,
            records=runner.records,
            max_fix_attempts=max_fix_attempts,
        )

    def _route_build_test(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=bt_boundary,
            records=runner.records,
            max_fix_attempts=max_fix_attempts,
        )

    # -- checkpoint/HITL pause nodes (plan [10]) ------------------------------
    # A pause node per ENABLED stage boundary. It PAUSES the run via LangGraph's
    # dynamic ``interrupt()`` (the run is resumable on the EXISTING checkpointer),
    # surfacing a READ-ONLY inspection payload to the caller, then RECORDS the
    # human decision fed back via ``Command(resume=...)`` onto the steering-only
    # ``checkpoint_decisions`` channel and routes proceed/halt.
    #
    # TRUST (the whole point of [10]): the pause node writes ONLY the steering
    # channel — never ``gates``, never via the runner — so it can never mint or
    # weaken a verdict. An *approve* routes the run onward to the (unchanged) gate
    # boundary, where the runner-attested verdict still decides; a *reject* routes
    # to END (no PR / no release). A checkpoint approve can NEVER turn a gate FAIL
    # into a pass: the gate boundaries are untouched and the emit guard re-checks
    # the gates regardless (factory_graph). The release checkpoint sits AFTER the
    # Build+Test gate. (Default OFF: when a boundary is not in
    # ``ckpt_set`` no pause node is built and the topology is byte-identical.)
    def _make_checkpoint(boundary: CheckpointBoundary):
        def _checkpoint(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
            # A spec_fix re-plan re-enters the planner; the operator already
            # approved this plan boundary on the first pass, so don't re-prompt.
            # The router reads that standing approval and proceeds.
            if boundary in _PLAN_BOUNDARIES:
                prior = _newest_decision(state, boundary)
                if prior is not None and prior.proceeds():
                    return {}
            sha = state_sha(config)
            subject_uri = newest_artifact_uri(state, DESIGN_KIND)
            # PAUSE: surface the read-only inspection payload + block for a human
            # decision. On the FIRST run ``interrupt()`` raises to suspend the
            # graph (resumable via the checkpointer); on the resume invoke
            # (``Command(resume=...)``) it RETURNS the supplied value here.
            raw = interrupt(
                inspection_payload(
                    state,
                    boundary=boundary,
                    state_sha=sha,
                    # Advisory org chart (Wave E): reconstitute the OwnershipMap
                    # via the CAS when available; None falls back to no org.
                    resolve=rt.cas.resolve if rt.cas is not None else None,
                )
            )
            decision = _coerce_decision(
                raw,
                boundary=boundary,
                state_sha=sha,
                subject_artifact_uri=subject_uri,
                seq=rt.seq(config, _CHECKPOINT_NODE[boundary], "checkpoint"),
            )
            # Record the steering decision (authorship via steered_by/note,
            # lifecycle §2) on the steering-only channel. NOT a gate write.
            return {"phase": _CHECKPOINT_NODE[boundary], "checkpoint_decisions": [decision]}

        return _checkpoint

    def _route_checkpoint(boundary: CheckpointBoundary):
        def _route(state: FactoryState, config: RunnableConfig) -> str:
            # Route on the NEWEST decision recorded at THIS boundary. proceed only
            # on a human APPROVE; reject/needs-changes halt to END. This is purely
            # human steering — proceeding merely reaches the gate boundary (the
            # gates still decide); it can never make a FAILing gate pass.
            decision = _newest_decision(state, boundary)
            if decision is not None and decision.proceeds():
                return _CKPT_PROCEED
            return _CKPT_HALT

        return _route

    # -- register nodes (concrete defs; SG2/IM5 mypy seam) --------------------
    g.add_node(_TRIAGE, triage)
    g.add_node(_PLANNER, planner_node)
    g.add_node(_SPEC_GATE, spec_gate)
    g.add_node(_IMPLEMENTATION, implementation_node)
    g.add_node(_QUALITY, quality_node)
    g.add_node(_BUILD_TEST_GATE, build_test_gate)
    g.add_node(_SPEC_FIX, spec_fix)
    g.add_node(_BUILD_TEST_FIX, build_test_fix)
    g.add_node(_TOKEN_CAP, token_cap)

    # Register the ENABLED checkpoint pause nodes (default OFF → none built, so the
    # topology is byte-identical). Each gets a conditional router (proceed/halt).
    for boundary in ckpt_set:
        node_id = _CHECKPOINT_NODE[boundary]
        g.add_node(node_id, _make_checkpoint(boundary))

    def _ckpt_edge(predecessor: str, boundary: CheckpointBoundary, succeed: str) -> None:
        """Insert the checkpoint pause node between ``predecessor`` and ``succeed``
        when ``boundary`` is enabled; otherwise wire the plain edge unchanged.

        Enabled: ``predecessor -> checkpoint_<boundary>``; the checkpoint routes
        ``proceed -> succeed`` (continue to the gate boundary) / ``halt -> END``
        (a human reject ends the run; no PR / no release). The pause writes ONLY
        the steering channel, so it can never affect a gate verdict."""
        if boundary in ckpt_set:
            node_id = _CHECKPOINT_NODE[boundary]
            g.add_edge(predecessor, node_id)
            g.add_conditional_edges(
                node_id,
                _route_checkpoint(boundary),
                {_CKPT_PROCEED: succeed, _CKPT_HALT: END},
            )
        else:
            g.add_edge(predecessor, succeed)

    # -- wire the phase sequence ----------------------------------------------
    g.add_edge(START, _TRIAGE)
    g.add_edge(_TRIAGE, _PLANNER)
    # The planner emits product AND design in one pass, so the PRODUCT and DESIGN
    # checkpoints both sit after it, in that order (each only when enabled).
    design_entry = (
        _CHECKPOINT_NODE[CheckpointBoundary.DESIGN]
        if CheckpointBoundary.DESIGN in ckpt_set else _SPEC_GATE
    )
    _ckpt_edge(_PLANNER, CheckpointBoundary.PRODUCT, design_entry)
    if CheckpointBoundary.DESIGN in ckpt_set:
        g.add_conditional_edges(
            _CHECKPOINT_NODE[CheckpointBoundary.DESIGN],
            _route_checkpoint(CheckpointBoundary.DESIGN),
            {_CKPT_PROCEED: _SPEC_GATE, _CKPT_HALT: END},
        )

    # SPEC boundary: advance -> implementation; fix -> back to the planner (which
    # reads the failing findings off the gates channel); halt -> END.
    g.add_conditional_edges(
        _SPEC_GATE,
        _route_spec,
        {"advance": _IMPLEMENTATION, "fix": _SPEC_FIX, "halt": END},
    )
    g.add_edge(_SPEC_FIX, _PLANNER)  # re-plan with the Spec gate's findings

    g.add_conditional_edges(
        _IMPLEMENTATION,
        _route_implementation,
        {_QUALITY: _QUALITY, _TOKEN_CAP: _TOKEN_CAP, "halt": END},
    )
    g.add_edge(_TOKEN_CAP, _IMPLEMENTATION)
    g.add_edge(_QUALITY, _BUILD_TEST_GATE)

    # The RELEASE checkpoint sits AFTER the Build+Test gate has run (the human
    # inspects the attested verdicts) and BEFORE the release path. On a build_test
    # ADVANCE we route into the checkpoint; proceed continues to END (emit), halt
    # ends the run. CRUCIAL: the gate already ran — an
    # approve here cannot make a FAILing build pass (a non-advance never reaches
    # the checkpoint, and the emit guard re-checks the gates regardless).
    release_enabled = CheckpointBoundary.RELEASE in ckpt_set
    # BUILD+TEST boundary: advance -> [release ckpt ->] END; fix -> back to
    # implementation; halt -> END.
    advance_target = _CHECKPOINT_NODE[CheckpointBoundary.RELEASE] if release_enabled else END
    g.add_conditional_edges(
        _BUILD_TEST_GATE,
        _route_build_test,
        {"advance": advance_target, "fix": _BUILD_TEST_FIX, "halt": END},
    )
    if release_enabled:
        g.add_conditional_edges(
            _CHECKPOINT_NODE[CheckpointBoundary.RELEASE],
            _route_checkpoint(CheckpointBoundary.RELEASE),
            {_CKPT_PROCEED: END, _CKPT_HALT: END},
        )
    g.add_edge(_BUILD_TEST_FIX, _IMPLEMENTATION)  # re-verify the responsible layer

    return g
