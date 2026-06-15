r"""Supervisor — phase routing across the four layer subgraphs (T-M1-13).

(architecture.md §1/§7, agents.md "Triage SOP" + Layer 1-4, governance §1)

The supervisor is the spine that SEQUENCES the four role subgraphs and wires the
deterministic gates at the phase boundaries. It is the only place phase order +
gate-wiring routing live; the layer subgraphs themselves hold no cross-phase
knowledge (each is an UNCOMPILED ``StateGraph`` the supervisor mounts as a node).

Topology (the SOP phase order — agents.md, governance §1 gate boundaries)::

    START
      -> triage              # classify -> route + seed per-route budget
      -> product             # Layer 1 subgraph (PRD + AcceptanceCriterion[])
      -> architecture        # Layer 2 subgraph (SystemDesign + constraints)
      -> spec_gate           # [SPEC gate] criteria(product) x design(arch)
         --(advance)--> implementation
         --(fix)-----> spec_fix -> architecture        (bounded re-verification)
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

This builder returns the UNCOMPILED ``StateGraph`` (mirrors ``skeleton.py`` and
the four subgraph builders, SG4): ``factory_graph`` (T-M1-14) compiles it, adds
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

from foundry.governance.budget_controller import BudgetCaps, BudgetController
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    SPEC_BOUNDARY,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
)
from foundry.graph.subgraphs.architecture import build_architecture_subgraph
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.graph.subgraphs.product import build_product_subgraph
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.schemas.base import BudgetLedger
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver

__all__ = [
    "DEFAULT_ROUTE_CAPS_MICROUSD",
    "ROUTES",
    "build_supervisor_graph",
    "classify_request",
]

# ---- supervisor node ids ----------------------------------------------------

_TRIAGE = "triage"
_PRODUCT = "product"
_ARCHITECTURE = "architecture"
_SPEC_GATE = "spec_gate"
_IMPLEMENTATION = "implementation"
_QUALITY = "quality"
_BUILD_TEST_GATE = "build_test_gate"
# The bounded re-verification nodes: bump the convergence counter, then edge
# back to the responsible layer (the layer that must regenerate the tree).
_SPEC_FIX = "spec_fix"
_BUILD_TEST_FIX = "build_test_fix"

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
        Context retriever for the Principal Architect (``None`` = greenfield,
        empty retrieval; AC1).
    max_fix_attempts:
        Convergence bound: a gate boundary may route ``fix`` (re-verification)
        at most this many times before ``route_after_gates`` returns ``halt``.
        Tracked on ``budget.fix_attempts_since_all_green`` (bumped by the
        ``*_fix`` node), so the rework loop can never spin forever.

    Returns the ``StateGraph`` UNCOMPILED — ``factory_graph`` (T-M1-14) compiles
    it, attaches the shared checkpointer, and appends the emit node.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # -- triage: classify + seed route/budget (single-writer channels) --------
    def triage(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        route = classify_request(state.get("request", ""))
        return {
            "phase": _TRIAGE,
            "route": route,
            "budget": _seed_budget(route),
        }

    # -- the four layer subgraphs, compiled + mounted as nodes ----------------
    # Each builder returns an UNCOMPILED StateGraph (SG4); we ``.compile()`` each
    # so it can be added as a single node of the supervisor (its internal
    # channel writes fold through FactoryState's reducers on the parent run).
    product = build_product_subgraph(rt).compile()
    architecture = build_architecture_subgraph(rt, index).compile()
    implementation = build_implementation_subgraph(
        rt, source_dir=source_dir, resolve=resolve
    ).compile()
    quality = build_quality_subgraph(rt, resolve=resolve).compile()

    def product_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(product.invoke(state, config))
        out["phase"] = _PRODUCT
        return out

    def architecture_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(architecture.invoke(state, config))
        out["phase"] = _ARCHITECTURE
        return out

    def implementation_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(implementation.invoke(state, config))
        out["phase"] = _IMPLEMENTATION
        return out

    def quality_node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out: dict[str, Any] = dict(quality.invoke(state, config))
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
        # route_after_gates reads only ``gates`` (the freshly attested verdicts in
        # ``out``) + ``budget`` (the pre-bump ledger on ``state``); assemble just
        # those so the in-node decision matches the conditional edge's.
        post: FactoryState = {
            "gates": out.get("gates", []),
            "budget": state.get("budget") or BudgetLedger(),
        }
        decision = route_after_gates(
            post,
            config,
            boundary=boundary,
            records=runner._records,
            max_fix_attempts=max_fix_attempts,
        )
        if decision == "fix":
            ledger = state.get("budget") or BudgetLedger()
            out["budget"] = ledger.model_copy(
                update={
                    "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + 1
                }
            )

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

    def build_test_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        out = run_build_test_gates(
            state,
            config,
            rt=rt,
            runner=runner,
            resolve=resolve,
            resolve_blob=resolve_blob,
            workdir=_gate_workdir("build_test_gate"),
        )
        out["phase"] = _BUILD_TEST_GATE
        _bump_on_non_advance(state, config, out, boundary=BUILD_TEST_BOUNDARY)
        return out

    # -- bounded re-verification (fix) nodes ----------------------------------
    # The counter is already bumped by the gate node; the fix node just marks the
    # phase and edges back to the responsible layer. (Kept distinct so the
    # rendered topology shows the re-verification path explicitly.)
    def spec_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _SPEC_FIX}

    def build_test_fix(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": _BUILD_TEST_FIX}

    # -- conditional routers (advance / fix / halt) ---------------------------
    def _route_spec(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=SPEC_BOUNDARY,
            records=runner._records,
            max_fix_attempts=max_fix_attempts,
        )

    def _route_build_test(state: FactoryState, config: RunnableConfig) -> str:
        return route_after_gates(
            state,
            config,
            boundary=BUILD_TEST_BOUNDARY,
            records=runner._records,
            max_fix_attempts=max_fix_attempts,
        )

    # -- register nodes (concrete defs; SG2/IM5 mypy seam) --------------------
    g.add_node(_TRIAGE, triage)
    g.add_node(_PRODUCT, product_node)
    g.add_node(_ARCHITECTURE, architecture_node)
    g.add_node(_SPEC_GATE, spec_gate)
    g.add_node(_IMPLEMENTATION, implementation_node)
    g.add_node(_QUALITY, quality_node)
    g.add_node(_BUILD_TEST_GATE, build_test_gate)
    g.add_node(_SPEC_FIX, spec_fix)
    g.add_node(_BUILD_TEST_FIX, build_test_fix)

    # -- wire the phase sequence ----------------------------------------------
    g.add_edge(START, _TRIAGE)
    g.add_edge(_TRIAGE, _PRODUCT)
    g.add_edge(_PRODUCT, _ARCHITECTURE)
    g.add_edge(_ARCHITECTURE, _SPEC_GATE)

    # SPEC boundary: advance -> implementation; fix -> back to architecture;
    # halt -> END. The path map lists every legal router return value.
    g.add_conditional_edges(
        _SPEC_GATE,
        _route_spec,
        {"advance": _IMPLEMENTATION, "fix": _SPEC_FIX, "halt": END},
    )
    g.add_edge(_SPEC_FIX, _ARCHITECTURE)  # re-verify the responsible layer

    g.add_edge(_IMPLEMENTATION, _QUALITY)
    g.add_edge(_QUALITY, _BUILD_TEST_GATE)

    # BUILD+TEST boundary: advance -> END; fix -> back to implementation;
    # halt -> END.
    g.add_conditional_edges(
        _BUILD_TEST_GATE,
        _route_build_test,
        {"advance": END, "fix": _BUILD_TEST_FIX, "halt": END},
    )
    g.add_edge(_BUILD_TEST_FIX, _IMPLEMENTATION)  # re-verify the responsible layer

    return g
