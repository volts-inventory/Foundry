"""Per-layer LangGraph subgraphs (planner / implementation / quality / assurance).

Each module exposes a ``build_<layer>_subgraph(rt: AgentRuntime, ...)`` builder that
composes that layer's ``make_<agent>_node(rt)`` factories into a ``StateGraph(FactoryState)``
in SOP order, returning a mountable subgraph the supervisor wires at phase boundaries.
Builders are imported directly from their modules (no eager re-export here, to avoid import
cycles while the package is assembled wave-by-wave).
"""
