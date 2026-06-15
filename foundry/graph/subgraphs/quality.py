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

from typing import TYPE_CHECKING, Any, cast

from langgraph.graph import END, START, StateGraph

from foundry.agents.quality import (
    make_qa_engineer_node,
    make_reviewer_critic_node,
    make_sdet_node,
)
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from langgraph.graph.state import StateNode

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.agents.quality.qa_engineer import ArtifactResolver

__all__ = ["build_quality_subgraph"]

# Node ids match the agent node_ids (so Seq.node_id == the graph node name).
_REVIEWER_CRITIC = "reviewer_critic"
_QA_ENGINEER = "qa_engineer"
_SDET = "sdet"


def _node(fn: NodeFn) -> StateNode[FactoryState, Any]:
    """Adapt a ``NodeFn`` for ``StateGraph.add_node``.

    A ``make_<agent>_node`` factory returns the ``NodeFn`` alias — a bare
    ``Callable[[FactoryState, RunnableConfig], dict]`` whose positional-only
    params don't unify with ``add_node``'s overloads, so mypy can't pick the
    ``_NodeWithConfig`` variant. The closure IS structurally a node; cast it to
    the public ``StateNode`` type ``add_node``'s signature declares.
    """
    return cast("StateNode[FactoryState, Any]", fn)


def build_quality_subgraph(
    rt: AgentRuntime, *, resolve: ArtifactResolver | None = None
) -> StateGraph[FactoryState]:
    """Build the Layer-4 quality subgraph (UNCOMPILED) over ``FactoryState``.

    Wires the three quality node factories (each built with the shared ``rt`` so
    they share one ``EpochCounter`` + one model egress; AR1/L6) as a linear SOP
    chain ``START -> reviewer_critic -> qa_engineer -> sdet -> END``. Returns the
    ``StateGraph`` itself; the caller compiles it (the supervisor with the shared
    checkpointer, or a test standalone). Do NOT compile with a checkpointer here.

    ``resolve`` is the INFRA seam (CAS in prod, an in-memory map in tests) the QA
    / SDET nodes reconstitute each ``AcceptanceCriterion`` body from its
    content-addressed ``acceptance_criterion`` handle with — the same dependency
    the implementation subgraph threads for SystemDesign / OwnershipMap. The DATA
    (WHICH criteria) is read from ``state['artifacts']`` inside the nodes.
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)
    g.add_node(_REVIEWER_CRITIC, _node(make_reviewer_critic_node(rt)))
    g.add_node(_QA_ENGINEER, _node(make_qa_engineer_node(rt, resolve=resolve)))
    g.add_node(_SDET, _node(make_sdet_node(rt, resolve=resolve)))
    g.add_edge(START, _REVIEWER_CRITIC)
    g.add_edge(_REVIEWER_CRITIC, _QA_ENGINEER)
    g.add_edge(_QA_ENGINEER, _SDET)
    g.add_edge(_SDET, END)
    return g
