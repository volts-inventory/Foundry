r"""Architecture (Layer 2) role subgraph (T-M1-09).

Composes the Layer-2 agent node factories into an UNCOMPILED
``StateGraph(FactoryState)`` the supervisor mounts at the architecture phase
boundary. Topology (architecture.md §1, agents.md Layer 2 SOP):

    START -> principal_architect --(conditional)--> domain_specialist -> END
                               \--(conditional)--> ux_design         -> END
                               \--(else)----------------------------> END

The Principal Architect (T1) is the entry: it authors the SystemDesign / ADRs /
InterfaceContracts / OwnershipMap (content-addressed handles on ``artifacts``)
and the PinnedConstraint records on the SEPARATE ``pinned_constraints`` channel,
which flow downstream VERBATIM into adapter task packets (architecture.md §8).

The two conditional Layer-2 nodes contribute only when the PM-declared
``required_capabilities`` match a registry capability. We route into them with
``add_conditional_edges`` keyed on the SAME ``should_enter_<role>`` predicates the
nodes guard on internally (AC2) — so the edge and the node never disagree — and
fan straight to END otherwise. Each conditional node also converges to END. A run
may enter BOTH conditionals (e.g. a regulated UI project), so the router returns a
LIST of branch targets; LangGraph fans out to all of them in one superstep and
the per-channel reducers (``merge_by_uri`` / ``merge_constraints``) merge the
fan-in. When neither matches, the router returns ``[END]`` and the conditional
nodes are never scheduled (their internal no-op guard is then moot).

This builder holds NO agent logic — only topology (LEARNINGS AR3): the agent
behaviour lives in the ``make_<agent>_node`` factories. It returns the UNCOMPILED
graph so the caller owns checkpointer/compile (mirrors ``skeleton.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.architecture import (
    make_domain_specialist_node,
    make_principal_architect_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)
from foundry.agents.architecture.conditional import (
    DOMAIN_SPECIALIST_NODE_ID,
    UX_DESIGN_NODE_ID,
)
from foundry.agents.architecture.principal_architect import NODE_ID as ARCHITECT_NODE_ID
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.context import SymbolIndex

__all__ = ["build_architecture_subgraph"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]


def _route_after_architect(state: FactoryState) -> list[str]:
    """Branch targets after the Principal Architect (the conditional router).

    Returns every conditional Layer-2 node whose entry predicate matches the
    PM-declared ``required_capabilities`` (a run may enter BOTH — e.g. a
    regulated UI project), so the result is a LIST: LangGraph fans out to all
    targets in one superstep and the channel reducers merge the fan-in. When no
    capability matches, it routes straight to ``END`` so the conditional nodes
    are never scheduled. The router branches on the exact ``should_enter_<role>``
    predicates the nodes guard on internally (AC2): edge and node never disagree.
    """
    targets: list[str] = []
    if should_enter_domain_specialist(state):
        targets.append(DOMAIN_SPECIALIST_NODE_ID)
    if should_enter_ux_design(state):
        targets.append(UX_DESIGN_NODE_ID)
    return targets or [END]


def build_architecture_subgraph(
    rt: AgentRuntime, index: SymbolIndex | None = None
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED architecture (Layer 2) role subgraph.

    ``rt`` is the shared :class:`AgentRuntime` threaded into every node factory;
    ``index`` is the context retriever the Principal Architect probes for
    existing code spans (``None`` is greenfield — empty retrieval, AC1).

    Returns the ``StateGraph`` UNCOMPILED so the caller owns compilation
    (checkpointer, etc.), mirroring ``skeleton.build_skeleton_graph``'s split.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    architect: _Node = make_principal_architect_node(rt, index)
    domain_specialist: _Node = make_domain_specialist_node(rt)
    ux_design: _Node = make_ux_design_node(rt)

    # The factory closures are typed as the ``_Node`` alias; LangGraph's
    # ``add_node`` resolves its node argument against the ``_NodeWithConfig``
    # Protocol, and mypy cannot bind that Protocol's contravariant ``NodeInputT``
    # TypeVar through an opaque ``Callable`` alias (only through a named, concrete
    # ``def`` — the limitation skeleton.py sidesteps by defining nodes inline).
    # Re-expressing each as a thin concrete wrapper restores structural matching
    # WITHOUT a private import or ``cast`` (full type-checking preserved).
    def _architect(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return architect(state, config)

    def _domain(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return domain_specialist(state, config)

    def _ux(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return ux_design(state, config)

    g.add_node(ARCHITECT_NODE_ID, _architect)
    g.add_node(DOMAIN_SPECIALIST_NODE_ID, _domain)
    g.add_node(UX_DESIGN_NODE_ID, _ux)

    g.add_edge(START, ARCHITECT_NODE_ID)
    # Conditional fan-out: into the matching Layer-2 specialists, else straight
    # to END. The path map's keys are the legal router return values (the node
    # ids + END); add_conditional_edges drives both branch-target validation and
    # the rendered graph topology off it.
    g.add_conditional_edges(
        ARCHITECT_NODE_ID,
        _route_after_architect,
        {
            DOMAIN_SPECIALIST_NODE_ID: DOMAIN_SPECIALIST_NODE_ID,
            UX_DESIGN_NODE_ID: UX_DESIGN_NODE_ID,
            END: END,
        },
    )
    # Both conditional branches converge to END.
    g.add_edge(DOMAIN_SPECIALIST_NODE_ID, END)
    g.add_edge(UX_DESIGN_NODE_ID, END)

    return g
