"""Product & Planning subgraph (architecture.md §1 / agents.md Layer 1).

Composes the four Layer-1 product node factories into one mountable
``StateGraph(FactoryState)`` in SOP order:

    stakeholder_proxy -> product_manager -> criteria_completeness_critic
                      -> eng_program_mgr

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
    make_stakeholder_proxy_node,
)
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime

__all__ = ["build_product_subgraph"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]

# Node ids match the agent node_ids (so Seq.node_id == the graph node name).
_STAKEHOLDER_PROXY = "stakeholder_proxy"
_PRODUCT_MANAGER = "product_manager"
_CRITERIA_CRITIC = "criteria_completeness_critic"
_ENG_PROGRAM_MGR = "eng_program_mgr"


def build_product_subgraph(rt: AgentRuntime) -> StateGraph[FactoryState]:
    """Build the Layer-1 product subgraph (UNCOMPILED) over ``FactoryState``.

    Wires the four product node factories (each built with ``rt``) as a linear
    SOP chain ``START -> stakeholder_proxy -> product_manager ->
    criteria_completeness_critic -> eng_program_mgr -> END``. Returns the
    ``StateGraph`` itself; the caller compiles it (the supervisor with the shared
    checkpointer, or a test standalone). Do NOT compile with a checkpointer here.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    stakeholder: _Node = make_stakeholder_proxy_node(rt)
    pm: _Node = make_product_manager_node(rt)
    critic: _Node = make_criteria_completeness_critic_node(rt)
    eng_pm: _Node = make_eng_program_mgr_node(rt)

    # The factory closures are typed as the ``_Node`` alias; LangGraph's
    # ``add_node`` resolves its node argument against the ``_NodeWithConfig``
    # Protocol, and mypy cannot bind that Protocol's contravariant ``NodeInputT``
    # TypeVar through an opaque ``Callable`` alias (only through a named, concrete
    # ``def``). Re-express each as a thin concrete wrapper to restore structural
    # matching WITHOUT a private import or ``cast`` (mirrors the architecture /
    # implementation / quality subgraphs; full type-checking preserved — SG2/IM5).
    def _stakeholder(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return stakeholder(state, config)

    def _pm(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return pm(state, config)

    def _critic(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return critic(state, config)

    def _eng_pm(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return eng_pm(state, config)

    g.add_node(_STAKEHOLDER_PROXY, _stakeholder)
    g.add_node(_PRODUCT_MANAGER, _pm)
    g.add_node(_CRITERIA_CRITIC, _critic)
    g.add_node(_ENG_PROGRAM_MGR, _eng_pm)
    g.add_edge(START, _STAKEHOLDER_PROXY)
    g.add_edge(_STAKEHOLDER_PROXY, _PRODUCT_MANAGER)
    g.add_edge(_PRODUCT_MANAGER, _CRITERIA_CRITIC)
    g.add_edge(_CRITERIA_CRITIC, _ENG_PROGRAM_MGR)
    g.add_edge(_ENG_PROGRAM_MGR, END)
    return g
