r"""Planner subgraph — the product + architecture phase.

    START -> planner --(conditional)--> domain_specialist -> END
                     \--(conditional)--> ux_design         -> END
                     \--(else)----------------------------> END

The planner makes the phase's ONE model call; the two conditional addenda nodes
are deterministic and enter only when the planner's ``required_capabilities``
match (routed on the same ``should_enter_<role>`` predicates the nodes guard on,
so edge and node never disagree). A run may enter both, so the router returns a
list and LangGraph fans out in one superstep.

Returns the UNCOMPILED graph; the supervisor compiles and owns the checkpointer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.architecture import (
    make_domain_specialist_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)
from foundry.agents.architecture.conditional import (
    DOMAIN_SPECIALIST_NODE_ID,
    UX_DESIGN_NODE_ID,
)
from foundry.agents.planner import NODE_ID as PLANNER_NODE_ID
from foundry.agents.planner import make_planner_node
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.context import SymbolIndex

__all__ = ["build_planner_subgraph"]


def _route_after_planner(state: FactoryState) -> list[str]:
    targets: list[str] = []
    if should_enter_domain_specialist(state):
        targets.append(DOMAIN_SPECIALIST_NODE_ID)
    if should_enter_ux_design(state):
        targets.append(UX_DESIGN_NODE_ID)
    return targets or [END]


def build_planner_subgraph(
    rt: AgentRuntime, index: SymbolIndex | None = None
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED planner subgraph over ``FactoryState``."""
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    planner = make_planner_node(rt, index)
    domain_specialist = make_domain_specialist_node(rt)
    ux_design = make_ux_design_node(rt)

    # Concrete defs: mypy cannot bind add_node's _NodeWithConfig Protocol through
    # the opaque NodeFn Callable alias (SG2/IM5).
    def _planner(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return planner(state, config)

    def _domain(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return domain_specialist(state, config)

    def _ux(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return ux_design(state, config)

    g.add_node(PLANNER_NODE_ID, _planner)
    g.add_node(DOMAIN_SPECIALIST_NODE_ID, _domain)
    g.add_node(UX_DESIGN_NODE_ID, _ux)

    g.add_edge(START, PLANNER_NODE_ID)
    g.add_conditional_edges(
        PLANNER_NODE_ID,
        _route_after_planner,
        {
            DOMAIN_SPECIALIST_NODE_ID: DOMAIN_SPECIALIST_NODE_ID,
            UX_DESIGN_NODE_ID: UX_DESIGN_NODE_ID,
            END: END,
        },
    )
    g.add_edge(DOMAIN_SPECIALIST_NODE_ID, END)
    g.add_edge(UX_DESIGN_NODE_ID, END)
    return g
