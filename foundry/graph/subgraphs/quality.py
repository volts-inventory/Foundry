"""Quality & Verification subgraph (architecture.md §1 / agents.md Layer 4).

One node::

    START -> reviewer -> END

``reviewer`` makes ONE model call per quality pass — the review verdict via the
trusted ``rt.proxy.judge`` path, written proxy-stamped to ``gates`` (L5/Q2) — and
derives the ``test_plan`` / ``oracle_binding`` / ``test_report`` / ``e2e_report``
records from the acceptance criteria (see :mod:`foundry.agents.quality.reviewer`).
The server-issued ``verdict_nonce`` it consumes is a RUN INPUT threaded onto
``config['configurable']`` by the runner (Q1); absent it the node fails closed
(``MissingVerdictNonceError``).

Returns the ``StateGraph`` UNCOMPILED so the caller owns the checkpointer/compile
(compiling here with a checkpointer would give the layer its own checkpoint
boundary, which the composition avoids).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.quality import make_reviewer_node
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.quality._common import ArtifactResolver

__all__ = ["build_quality_subgraph"]

# Node id matches the agent node_id (so Seq.node_id == the graph node name).
_REVIEWER = "reviewer"


def build_quality_subgraph(
    rt: AgentRuntime, *, resolve: ArtifactResolver
) -> StateGraph[FactoryState]:
    """Build the Layer-4 quality subgraph (UNCOMPILED) over ``FactoryState``.

    ``resolve`` is REQUIRED (C-R1-01): the product layer emits criteria ONLY as
    handles, so a ``None`` resolver would read zero criteria → an empty
    ``TestPlan`` with no oracles → a VACUOUS Test gate pass.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)
    reviewer = make_reviewer_node(rt, resolve=resolve)

    # A concrete ``def`` (not the ``NodeFn`` Callable alias) so mypy can bind
    # ``add_node``'s ``_NodeWithConfig`` Protocol TypeVar (SG2/IM5).
    def _reviewer(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return reviewer(state, config)

    g.add_node(_REVIEWER, _reviewer)
    g.add_edge(START, _REVIEWER)
    g.add_edge(_REVIEWER, END)
    return g
