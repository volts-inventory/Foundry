r"""Supervisor — phase routing across the four layer subgraphs (T-M1-13).

(architecture.md §1/§7, agents.md "Triage SOP" + Layer 1-4, governance §1)

The supervisor is the spine that SEQUENCES the four role subgraphs and wires the
deterministic gates at the phase boundaries. It is the only place phase order +
gate-wiring routing live; the layer subgraphs themselves hold no cross-phase
knowledge (each is an UNCOMPILED ``StateGraph`` the supervisor mounts as a node).

Topology (the SOP phase order — agents.md, governance §1 gate boundaries)::

    START
      -> triage              # classify -> route + seed per-route budget
         --(resume)--> spec_fix | implementation | build_test_gate   (see below)
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

Resume after a HALT: a halted run ends (``emit`` marks it ``halted``) with no
pending node, so a rerun must start a new LangGraph run. A seed carrying
``phase=RESUME_PHASE`` asks triage to re-enter at the boundary that halted
(:func:`resume_boundary`) with a fresh fix budget instead of planning again: a
failed Spec gate re-plans through ``spec_fix``; a failed per-wave check re-runs
that check (the implementation subgraph routes a resume to its ``wave_gate``); a
failed final gate re-runs the build/test gates. Built tickets and the plan stay
on state, so nothing upstream of the boundary runs again.

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

from foundry.agents.implementation.tickets import cap_stopped, pending_wave
from foundry.governance.budget_controller import BudgetCaps, BudgetController
from foundry.graph.gate_wiring import (
    BUILD_TEST_SMOKE_BOUNDARY,
    SPEC_BOUNDARY,
    WaveBuildCheck,
    bump_fix_attempts,
    fix_cap,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
    settle_fix_attempts,
)
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.graph.subgraphs.planner import build_planner_subgraph
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import JUDGMENT_VERDICTS, merge_gates

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    "RESUME_FINAL",
    "RESUME_PHASE",
    "RESUME_SPEC",
    "RESUME_WAVE",
    "ROUTES",
    "build_supervisor_graph",
    "classify_request",
    "resume_boundary",
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

# The seed phase that asks triage to resume a halted run (see the module doc), and
# the boundaries it can resume from.
RESUME_PHASE = "resume"
RESUME_SPEC = "the Spec gate"
RESUME_WAVE = "the per-wave check"
RESUME_FINAL = "the final gates"


def resume_boundary(state: FactoryState, resolve: ArtifactResolver) -> str:
    """The boundary a halted run stopped at, read off its state: a Spec gate that
    has not passed, else a wave left unbuilt (the per-wave check spent its budget),
    else the final build/test gates."""
    spec = max((g for g in state.get("gates") or () if g.name == "spec"),
               key=lambda g: g.seq, default=None)
    if spec is None or spec.status is not GateStatus.PASS:
        return RESUME_SPEC
    if pending_wave(state, resolve):
        return RESUME_WAVE
    return RESUME_FINAL

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
    Returns the ``StateGraph`` UNCOMPILED — ``factory_graph`` (T-M1-14) compiles
    it, attaches the shared checkpointer, and appends the emit node.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # -- triage: classify + seed route/budget ---------------------------------
    def triage(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        route = classify_request(state.get("request", ""))
        resuming = state.get("phase") == RESUME_PHASE
        return {
            # A resume keeps the marker for the entry router (and the implementation
            # subgraph); either way the fix budget starts fresh.
            "phase": RESUME_PHASE if resuming else _TRIAGE,
            "route": route,
            "budget": _seed_budget(route),
            "mode": MODE_ASSISTANT,
        }

    def _route_entry(state: FactoryState) -> str:
        if state.get("phase") != RESUME_PHASE:
            return _PLANNER
        return {
            RESUME_SPEC: _SPEC_FIX,
            RESUME_WAVE: _IMPLEMENTATION,
            RESUME_FINAL: _BUILD_TEST_GATE,
        }[resume_boundary(state, resolve)]

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
                          records=runner.records, max_fix_attempts=max_fix_attempts,
                          total_cap=fix_cap(state, resolve, max_fix_attempts))

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
        # A Spec PASS resets the counter: the re-plans it took must not shrink the
        # build/test boundary's fix budget.
        settle_fix_attempts(state, config, out, boundary=SPEC_BOUNDARY,
                            records=runner.records, max_fix_attempts=max_fix_attempts)
        return out

    # The gates read the stack off state at gate time, so the boundary cannot
    # depend on it: it is always {build, test, smoke}, and a stack without a run
    # command records an attested "not pinned" smoke PASS.
    bt_boundary = BUILD_TEST_SMOKE_BOUNDARY

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
    # (a higher cap) — or no meter at all (the cap was removed) — it returns and the
    # unbuilt tickets are built, which clears the stop.
    def token_cap(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        meter = getattr(rt.proxy, "token_meter", None)
        if meter is not None:
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
            total_cap=fix_cap(state, resolve, max_fix_attempts),
        )

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

    # -- wire the phase sequence ----------------------------------------------
    g.add_edge(START, _TRIAGE)
    g.add_conditional_edges(
        _TRIAGE,
        _route_entry,
        {n: n for n in (_PLANNER, _SPEC_FIX, _IMPLEMENTATION, _BUILD_TEST_GATE)},
    )
    g.add_edge(_PLANNER, _SPEC_GATE)

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

    # BUILD+TEST boundary: advance -> END; fix -> back to implementation; halt -> END.
    g.add_conditional_edges(
        _BUILD_TEST_GATE,
        _route_build_test,
        {"advance": END, "fix": _BUILD_TEST_FIX, "halt": END},
    )
    g.add_edge(_BUILD_TEST_FIX, _IMPLEMENTATION)  # re-verify the responsible layer

    return g
