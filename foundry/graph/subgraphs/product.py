"""Product & Planning subgraph (architecture.md §1 / agents.md Layer 1).

Composes the five Layer-1 product node factories into one mountable
``StateGraph(FactoryState)`` in SOP order:

    scope_judge -> stakeholder_proxy -> product_manager
                -> criteria_completeness_critic -> eng_program_mgr

The builder returns an **UNCOMPILED** ``StateGraph`` (LEARNINGS AR3): the
supervisor / ``factory_graph`` is the only place that compiles — and it owns the
checkpointer (so a kill-then-resume re-folds committed writes once). Compiling
here with a checkpointer would give the layer its own checkpoint boundary, which
the composition explicitly avoids; callers that want to exercise the layer
standalone call ``build_product_subgraph(rt).compile()`` themselves.

Each node is built with the shared ``rt`` (AgentRuntime) so they share one
``EpochCounter`` and one model egress (LEARNINGS AR1/L6). The nodes only write
channel deltas (PRD / AcceptanceCriterion[] / CriteriaCritique as
content-addressed ``artifacts`` handles, plus an advisory ``gate_triage`` on the
``spec`` gate) — none of them touches the ``gates`` channel (LEARNINGS L5): the
Spec-gate verdict is computed + signed later by the two-zone gate runner.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.product import (
    make_criteria_completeness_critic_node,
    make_eng_program_mgr_node,
    make_product_manager_node,
    make_scope_judge_node,
    make_stakeholder_proxy_node,
)
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime

__all__ = ["build_product_subgraph", "make_criteria_revision_router"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]

# Node ids match the agent node_ids (so Seq.node_id == the graph node name).
_SCOPE_JUDGE = "scope_judge"
_STAKEHOLDER_PROXY = "stakeholder_proxy"
_PRODUCT_MANAGER = "product_manager"
_CRITERIA_CRITIC = "criteria_completeness_critic"
_ENG_PROGRAM_MGR = "eng_program_mgr"

# Bounded planning self-correction: the PM re-runs at most this many times in
# response to a blocking criteria critique. The count of ``criteria_findings``
# artifacts equals the number of PM/critic passes so far (they alternate 1:1), so
# the router loops while that count is <= this bound and stops after — a hard
# termination guarantee independent of what the model keeps flagging.
_MAX_PM_REVISIONS = 2


def _latest_critique(state: FactoryState, rt: AgentRuntime) -> Any | None:
    """Resolve the most-recent ``CriteriaCritique`` on the artifacts channel
    (None if none yet, or if it cannot be resolved — a degenerate view that the
    router treats as non-blocking, so a resolution hiccup never traps the loop)."""
    if rt.cas is None:
        return None
    crits = [
        a
        for a in (state.get("artifacts") or ())
        if getattr(a, "kind", None) == "criteria_findings"
    ]
    if not crits:
        return None
    try:
        return rt.cas.resolve(max(crits, key=lambda a: a.seq))
    except Exception:
        return None


def _num_critiques(state: FactoryState) -> int:
    """How many critic passes have run (== PM passes; they alternate 1:1)."""
    return sum(
        1
        for a in (state.get("artifacts") or ())
        if getattr(a, "kind", None) == "criteria_findings"
    )


def make_criteria_revision_router(rt: AgentRuntime) -> Callable[[FactoryState], str]:
    """Route after the Criteria-Completeness Critic: loop back to the PM to revise
    when the latest critique flags an applicable security/abuse omission AND the
    revision budget is not spent; otherwise advance to eng_pm. Reads the critique
    from the ``artifacts`` channel (post-reduction), so it sees the pass the critic
    just wrote. Fail-open to eng_pm keeps the signed Spec gate as the sole verdict
    authority (L5) — the router only decides whether to give the PM another try."""

    def _route(state: FactoryState) -> str:
        critique = _latest_critique(state, rt)
        blocking = bool(critique is not None and critique.spec_gate_blocking())
        if blocking and _num_critiques(state) <= _MAX_PM_REVISIONS:
            return _PRODUCT_MANAGER
        return _ENG_PROGRAM_MGR

    return _route


def build_product_subgraph(rt: AgentRuntime) -> StateGraph[FactoryState]:
    """Build the Layer-1 product subgraph (UNCOMPILED) over ``FactoryState``.

    Wires the four product node factories (each built with ``rt``) as a linear
    SOP chain ``START -> stakeholder_proxy -> product_manager ->
    criteria_completeness_critic -> eng_program_mgr -> END``. Returns the
    ``StateGraph`` itself; the caller compiles it (the supervisor with the shared
    checkpointer, or a test standalone). Do NOT compile with a checkpointer here.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    scope: _Node = make_scope_judge_node(rt)
    stakeholder: _Node = make_stakeholder_proxy_node(rt)
    pm: _Node = make_product_manager_node(rt)
    critic: _Node = make_criteria_completeness_critic_node(rt)
    eng_pm: _Node = make_eng_program_mgr_node(rt)

    route_after_critic = make_criteria_revision_router(rt)

    # The factory closures are typed as the ``_Node`` alias; LangGraph's
    # ``add_node`` resolves its node argument against the ``_NodeWithConfig``
    # Protocol, and mypy cannot bind that Protocol's contravariant ``NodeInputT``
    # TypeVar through an opaque ``Callable`` alias (only through a named, concrete
    # ``def``). Re-express each as a thin concrete wrapper to restore structural
    # matching WITHOUT a private import or ``cast`` (mirrors the architecture /
    # implementation / quality subgraphs; full type-checking preserved — SG2/IM5).
    def _scope(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return scope(state, config)

    def _stakeholder(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return stakeholder(state, config)

    def _pm(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return pm(state, config)

    def _critic(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return critic(state, config)

    def _eng_pm(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return eng_pm(state, config)

    g.add_node(_SCOPE_JUDGE, _scope)
    g.add_node(_STAKEHOLDER_PROXY, _stakeholder)
    g.add_node(_PRODUCT_MANAGER, _pm)
    g.add_node(_CRITERIA_CRITIC, _critic)
    g.add_node(_ENG_PROGRAM_MGR, _eng_pm)
    # The Scope Judge runs FIRST: it writes the `scope_tier` channel the PM (and,
    # a layer later, the Principal Architect) read to cap what they propose. It
    # writes no artifact and no gate input — a mis-judgement can only make the
    # factory build too little, never let a weaker build through.
    g.add_edge(START, _SCOPE_JUDGE)
    g.add_edge(_SCOPE_JUDGE, _STAKEHOLDER_PROXY)
    g.add_edge(_STAKEHOLDER_PROXY, _PRODUCT_MANAGER)
    g.add_edge(_PRODUCT_MANAGER, _CRITERIA_CRITIC)
    # Bounded planning self-correction (autonomous mode never stalls at a fixable
    # Spec-gate omission): when the Criteria-Completeness Critic flags an
    # APPLICABLE security/abuse omission, loop back to the PM (which sees the
    # critique and adds the missing criteria) up to _MAX_PM_REVISIONS times;
    # otherwise proceed. A non-blocking critique goes straight to eng_pm, so the
    # historical linear SOP path (and its tests) is unchanged. The bound
    # guarantees termination — after the last allowed revision the layer proceeds
    # and the signed Spec gate downstream still owns the actual verdict (L5).
    g.add_conditional_edges(
        _CRITERIA_CRITIC,
        route_after_critic,
        {_PRODUCT_MANAGER: _PRODUCT_MANAGER, _ENG_PROGRAM_MGR: _ENG_PROGRAM_MGR},
    )
    g.add_edge(_ENG_PROGRAM_MGR, END)
    return g
