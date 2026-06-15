"""Quality & Verification subgraph (architecture.md §1 / agents.md Layer 4).

Composes the three Quality-layer node factories into one mountable
``StateGraph(FactoryState)`` in SOP order (agents.md Layer 4 — Reviewer/Critic,
QA Engineer, SDET):

    START -> reviewer_critic -> qa_engineer -> sdet -> END

* ``reviewer_critic`` (T1) is the evaluator of the evaluator-optimizer loop: it
  issues the review verdict via the trusted ``rt.proxy.judge`` path and writes
  the proxy-stamped ``GateResult`` to the ``gates`` channel (the ONE quality
  node that touches ``gates``, and only because the PROXY — not the agent —
  signed the binding; L5/Q2). The server-issued ``verdict_nonce`` it consumes
  is a RUN INPUT threaded onto ``config['configurable']`` by the runner (Q1),
  exactly like ``thread_id`` / ``state_sha`` / ``subject_artifact_uri`` — never
  agent-minted; absent it the node fails closed (``MissingVerdictNonceError``).
* ``qa_engineer`` authors the ``TestPlan`` (criterion_id traceability) + a
  NEGATIVE ``OracleBinding`` per high-risk criterion + a ``TestReport`` (the
  N=2 quarantine), feeding the Test gate.
* ``sdet`` authors the ``E2EReport`` (determinism notes + scenario→criterion
  traceability + quarantine), feeding the E2E gate.

The two author nodes read their acceptance criteria from ``state`` (the product
layer seeds the typed ``AcceptanceCriterion`` records onto ``artifacts``; AR1-AR4
— DATA inputs come from state, never as build-time kwargs) and emit their typed
bodies as content-addressed ``Artifact`` handles on ``artifacts`` (DS9/L4).

This builder holds NO agent logic — only topology (LEARNINGS AR3). It returns the
``StateGraph`` UNCOMPILED so the caller owns the checkpointer/compile (mirrors
``skeleton.py`` and the product/architecture subgraphs); compiling here with a
checkpointer would give the layer its own checkpoint boundary, which the
composition avoids. Callers exercising the layer standalone call
``build_quality_subgraph(rt).compile()`` themselves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.quality import (
    make_qa_engineer_node,
    make_reviewer_critic_node,
    make_sdet_node,
)
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.quality.qa_engineer import ArtifactResolver

__all__ = ["build_quality_subgraph"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]

# Node ids match the agent node_ids (so Seq.node_id == the graph node name).
_REVIEWER_CRITIC = "reviewer_critic"
_QA_ENGINEER = "qa_engineer"
_SDET = "sdet"


def build_quality_subgraph(
    rt: AgentRuntime, *, resolve: ArtifactResolver
) -> StateGraph[FactoryState]:
    """Build the Layer-4 quality subgraph (UNCOMPILED) over ``FactoryState``.

    Wires the three quality node factories (each built with the shared ``rt`` so
    they share one ``EpochCounter`` + one model egress; AR1/L6) as a linear SOP
    chain ``START -> reviewer_critic -> qa_engineer -> sdet -> END``. Returns the
    ``StateGraph`` itself; the caller compiles it (the supervisor with the shared
    checkpointer, or a test standalone). Do NOT compile with a checkpointer here.

    ``resolve`` is a REQUIRED INFRA seam (CAS in prod, an in-memory map in tests):
    the QA / SDET nodes reconstitute each ``AcceptanceCriterion`` body from its
    content-addressed ``acceptance_criterion`` handle with it — the same dependency
    ``build_implementation_subgraph`` requires for SystemDesign / OwnershipMap.
    It is REQUIRED (not defaulted to ``None``, C-R1-01) because the product layer
    emits criteria ONLY as handles in the chained graph; a ``None`` resolver would
    read zero criteria → an empty ``TestPlan`` with no oracles → a VACUOUS Test
    gate pass. The DATA (WHICH criteria) is read from ``state['artifacts']`` inside
    the nodes.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    reviewer: _Node = make_reviewer_critic_node(rt)
    qa: _Node = make_qa_engineer_node(rt, resolve=resolve)
    sdet: _Node = make_sdet_node(rt, resolve=resolve)

    # The factory closures are typed as the ``_Node`` alias; LangGraph's
    # ``add_node`` resolves its node argument against the ``_NodeWithConfig``
    # Protocol, and mypy cannot bind that Protocol's contravariant ``NodeInputT``
    # TypeVar through an opaque ``Callable`` alias (only through a named, concrete
    # ``def``). Re-express each as a thin concrete wrapper to restore structural
    # matching WITHOUT a private import or ``cast`` (mirrors the architecture /
    # implementation subgraphs; full type-checking preserved — SG2/IM5).
    def _reviewer(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return reviewer(state, config)

    def _qa(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return qa(state, config)

    def _sdet(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return sdet(state, config)

    g.add_node(_REVIEWER_CRITIC, _reviewer)
    g.add_node(_QA_ENGINEER, _qa)
    g.add_node(_SDET, _sdet)
    g.add_edge(START, _REVIEWER_CRITIC)
    g.add_edge(_REVIEWER_CRITIC, _QA_ENGINEER)
    g.add_edge(_QA_ENGINEER, _SDET)
    g.add_edge(_SDET, END)
    return g
