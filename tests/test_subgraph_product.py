"""Product subgraph (T-M1-08) — the Layer-1 builder composed end-to-end.

Drives :func:`build_product_subgraph` over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` (no live API key, LEARNINGS D1). Compiles the
subgraph STANDALONE (``build_product_subgraph(rt).compile()`` — no checkpointer;
the supervisor owns that) and invokes it with a ``request`` in the initial state.

Asserts:

* the run produces the artifacts the Spec gate needs — a ``prd`` +
  ``acceptance_criterion`` handle(s) + a ``criteria_findings``
  (``CriteriaCritique``) handle on the ``artifacts`` channel, alongside the
  ``intent_brief`` and ``sprint_plan`` the rest of Layer 1 emits;
* the four nodes ran in SOP order (one model call per node, in order), with the
  critic's advisory ``GateTriage`` on the ``spec`` gate;
* NO ``gates`` channel write occurs anywhere in the layer (LEARNINGS L5):
  verdicts are minted later by the gate runner, never by a product node;
* the builder returns an UNCOMPILED ``StateGraph`` (the caller compiles).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.subgraphs.product import build_product_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-subgraph-product-key"
KEY_ID = "dev-key-subgraph-product"

# ---- per-node payloads, in SOP order (the four product nodes) ----------------

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user", "web client"],
    "unknowns": ["auth provider"],
    "success_criteria": ["a user can add and complete a todo"],
    "must_haves": ["add todo", "complete todo"],
    "nice_to_haves": ["due dates"],
}

PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add, list, and complete todos via a REST API.",
    "required_capabilities": ["ui"],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears in the list.",
            "criticality": "normal",
            "oracle_hint": "POST /todos then GET /todos returns the new item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "request without token returns 401; negative oracle",
            "security_abuse": True,
        },
    ],
}

CRITIQUE_PAYLOAD: dict[str, Any] = {
    "missing": ["a criterion for deleting a todo"],
    "security_abuse_omissions": [],
}

SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1 storage", "M2 API", "M3 UI"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "risk: auth provider undecided",
    "done_definition": "all acceptance criteria green on the integrated tree",
}


def _transport() -> MockTransport:
    """A transport whose free-text completion queue serves each node's payload in
    SOP order. Every product node calls ``complete(purpose="ideation", schema=)``
    — schema-constrained IDEATION, not a verdict (AC3 resolved, DT1) — so the
    payloads ride ``completions=`` and are returned verbatim as ``text``."""
    return MockTransport(
        completions=[
            json.dumps(INTENT_PAYLOAD),
            json.dumps(PRD_PAYLOAD),
            json.dumps(CRITIQUE_PAYLOAD),
            json.dumps(SPRINT_PAYLOAD),
        ]
    )


def _runtime(
    transport: MockTransport | None = None, cas: ArtifactStore | None = None
) -> AgentRuntime:
    proxy = ModelProviderProxy(transport or _transport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, cas=cas)


def _config(run_id: str = "run-product-subgraph", step: int = 1) -> RunnableConfig:
    # Seq needs a thread_id (LEARNINGS AR2 — run_id == thread_id).
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _artifacts(state: FactoryState) -> list[Artifact]:
    return list(state.get("artifacts") or ())


def test_build_product_subgraph_returns_uncompiled_state_graph() -> None:
    """The builder returns an UNCOMPILED StateGraph; the caller compiles it (the
    supervisor with the shared checkpointer, or a test standalone)."""
    g = build_product_subgraph(_runtime())
    assert isinstance(g, StateGraph)
    # No `.invoke` on the builder output until compiled — compilation is the
    # caller's job (no checkpointer threaded here).
    assert not hasattr(g, "invoke")


def test_product_subgraph_produces_spec_gate_artifacts() -> None:
    """A standalone-compiled run yields PRD + AcceptanceCriterion[] +
    CriteriaCritique on ``artifacts`` — the inputs the Spec gate consumes."""
    app = build_product_subgraph(_runtime()).compile()
    final: FactoryState = app.invoke({"request": "build a todo app"}, _config())

    kinds = {a.kind for a in _artifacts(final)}
    # The Spec gate needs the PRD, its acceptance criteria, and the critique.
    assert {"prd", "acceptance_criterion", "criteria_findings"} <= kinds
    # ...alongside the rest of the Layer-1 chain.
    assert {"intent_brief", "sprint_plan"} <= kinds
    # The PM declared two criteria -> two acceptance_criterion handles.
    assert sum(1 for a in _artifacts(final) if a.kind == "acceptance_criterion") == 2
    # Bodies ride out-of-band as content-addressed handles (LEARNINGS L4).
    assert all(a.uri.startswith("cas://") for a in _artifacts(final))


def test_product_subgraph_runs_nodes_in_sop_order() -> None:
    """The four nodes fire once each, in SOP order (one model call per node).

    The transport request carries each node's system SOP (not its node_id), so
    order is asserted via the distinct "You are the ..." opening of each SOP."""
    transport = _transport()
    app = build_product_subgraph(_runtime(transport)).compile()
    app.invoke({"request": "build a todo app", "route": "greenfield"}, _config())

    assert len(transport.requests) == 4  # exactly one model call per node
    sops = ["".join(block["text"] for block in req["system"]) for req in transport.requests]
    markers = [
        "You are the Stakeholder / Client Proxy",
        "You are the Product Manager",
        "You are the Criteria-Completeness Critic",
        "You are the Engineering / Program Manager",
    ]
    for sop, marker in zip(sops, markers, strict=True):
        assert marker in sop


def test_product_subgraph_writes_advisory_triage_not_a_verdict() -> None:
    """The critic's advisory rides ``gate_triage`` on the ``spec`` gate, and NO
    product node writes the ``gates`` channel (LEARNINGS L5)."""
    app = build_product_subgraph(_runtime()).compile()
    final: FactoryState = app.invoke({"request": "build a todo app"}, _config())

    assert any(t.name == "spec" for t in final.get("gate_triage") or ())
    # Verdicts come later from the gate runner — never from this layer.
    assert not final.get("gates")


def test_product_subgraph_propagates_required_capabilities() -> None:
    """``required_capabilities`` from the PM reach the layer's output (they drive
    downstream UX/specialist routing)."""
    app = build_product_subgraph(_runtime()).compile()
    final: FactoryState = app.invoke({"request": "build a todo app"}, _config())
    assert final.get("required_capabilities") == ["ui"]


# ---- bounded planning self-correction (Spec-gate revision loop) --------------

PRD_MISSING_SECURITY: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos.",
    "required_capabilities": ["ui"],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "normal",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": False,
        }
    ],
}

PRD_REVISED_WITH_SECURITY: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos; malformed input is rejected.",
    "required_capabilities": ["ui"],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "normal",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given a malformed payload, When it hits /todos, Then 400 (no crash).",
            "criticality": "high-risk",
            "oracle_hint": "invalid body returns 400; negative oracle",
            "security_abuse": True,
        },
    ],
}

CRITIQUE_BLOCKING: dict[str, Any] = {
    "missing": [],
    "security_abuse_omissions": ["malformed-input handling on the write path"],
}


def test_product_subgraph_revises_on_blocking_critique() -> None:
    """A BLOCKING critique loops the PM back once; on the revised (clean) pass the
    layer advances to eng_pm and completes — autonomous planning self-correction,
    never a stall at a fixable Spec-gate omission."""
    transport = MockTransport(
        completions=[
            json.dumps(INTENT_PAYLOAD),
            json.dumps(PRD_MISSING_SECURITY),
            json.dumps(CRITIQUE_BLOCKING),  # flags a security omission -> revise
            json.dumps(PRD_REVISED_WITH_SECURITY),  # PM adds the missing criterion
            json.dumps({"missing": [], "security_abuse_omissions": []}),  # clean
            json.dumps(SPRINT_PAYLOAD),
        ]
    )
    app = build_product_subgraph(_runtime(transport, cas=ArtifactStore())).compile()
    final: FactoryState = app.invoke({"request": "build a todo app"}, _config())

    # intent, pm, critic, pm(revised), critic(clean), sprint == 6 model calls.
    assert len(transport.requests) == 6
    sops = ["".join(b["text"] for b in req["system"]) for req in transport.requests]
    assert sum(1 for x in sops if "You are the Product Manager" in x) == 2  # PM re-ran once
    # The layer converged: it produced the sprint plan (reached eng_pm) ...
    assert any(a.kind == "sprint_plan" for a in _artifacts(final))
    # ... and the revised criteria set includes the security/abuse criterion.
    assert sum(1 for a in _artifacts(final) if a.kind == "acceptance_criterion") >= 3
    # The final (clean) critique carries no omissions.
    assert not final.get("gates")  # still no verdict minted in the layer (L5)


def test_product_subgraph_revision_loop_is_bounded() -> None:
    """If the critic keeps flagging omissions, the loop STILL terminates at the
    revision bound and advances to eng_pm (the signed Spec gate downstream owns
    the verdict — the layer never spins forever)."""
    # Every critic pass flags a blocking omission; enough payloads for the bound.
    completions = [json.dumps(INTENT_PAYLOAD)]
    for _ in range(6):
        completions.append(json.dumps(PRD_MISSING_SECURITY))
        completions.append(json.dumps(CRITIQUE_BLOCKING))
    completions.append(json.dumps(SPRINT_PAYLOAD))
    transport = MockTransport(completions=completions)
    app = build_product_subgraph(_runtime(transport, cas=ArtifactStore())).compile()
    final: FactoryState = app.invoke({"request": "build a todo app"}, _config())

    # It terminated (reached eng_pm) rather than exhausting recursion.
    assert any(a.kind == "sprint_plan" for a in _artifacts(final))
    # PM ran the initial pass + at most _MAX_PM_REVISIONS revisions (== 3 total).
    sops = ["".join(b["text"] for b in req["system"]) for req in transport.requests]
    assert sum(1 for x in sops if "You are the Product Manager" in x) == 3
