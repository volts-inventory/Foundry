"""ArtifactStore (T-M1-14b / DECISIONS D6) — the real body store behind cas://.

`emit_artifact` historically MINTED a content-addressed handle but DISCARDED the
body, so every downstream resolve/resolve_blob consumer only worked against a
SYNTHETIC resolver in tests; a real run resolved nothing. These tests pin the
store that closes the gap:

* the put/get store contract is pinned for BOTH backends in
  test_fs_artifact_store.py.
* emit_artifact WITH a store stores the body and the handle uri matches the
  stored uri; WITH store=None it mints only (no store write) — backward-compat.
* end-to-end-ish: drive a couple of REAL agent nodes with
  AgentRuntime(cas=ArtifactStore()) and assert the emitted handles resolve back
  to EQUAL bodies via store.resolve.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.planner import make_planner_node
from foundry.durability.artifact_store import ArtifactStore, UnknownArtifactError
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.seq import Seq
from tests.planner_payload import plan_payload

KEY = b"dev-zone-b-artifact-store-key"
KEY_ID = "dev-key-artifact-store"


def _seq(node_id: str = "n", local: int = 0) -> Seq:
    return Seq(
        run_id="run-store",
        emission_epoch=0,
        superstep_index=0,
        node_id=node_id,
        local=local,
    )


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id="AC-001",
        text="Given a user, When they add a todo, Then it appears.",
        criticality=Criticality.HIGH_RISK,
        oracle_hint="POST then GET returns the item",
        security_abuse=True,
        seq=_seq("product_manager"),
    )


# ---- emit_artifact: store-backed vs mint-only (backward-compat) --------------


def test_emit_artifact_with_store_stores_body_and_uri_matches() -> None:
    store = ArtifactStore()
    crit = _criterion()
    handle = emit_artifact(crit, "acceptance_criterion", "product_manager", crit.seq, store=store)
    # The handle uri equals the stored uri, and resolve gives back an equal body.
    assert handle.uri == f"cas://sha256/{crit.content_identity()}"
    assert store.get_body(handle.uri) == crit
    assert store.resolve(handle) == crit


def test_emit_artifact_store_none_mints_only_no_store_write() -> None:
    """store=None is the historical mint-only behavior — no body persisted, so
    the synthetic-resolver tests are untouched (purely additive change)."""
    store = ArtifactStore()
    crit = _criterion()
    handle = emit_artifact(crit, "acceptance_criterion", "product_manager", crit.seq)
    assert handle.uri == f"cas://sha256/{crit.content_identity()}"
    # Nothing was written to the store: resolve fails closed.
    with pytest.raises(UnknownArtifactError):
        store.get_body(handle.uri)


# ---- end-to-end-ish: drive REAL agent nodes with a cas-backed runtime --------

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user"],
    "success_criteria": ["a user can add a todo"],
    "must_haves": ["add todo"],
}

PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos.",
    "required_capabilities": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "high-risk",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": True,
        },
        {
            "id": "AC-002",
            "text": "Given a list, When fetched, Then it returns all todos.",
            "criticality": "normal",
            "oracle_hint": "GET /todos",
            "security_abuse": False,
        },
    ],
}


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": step}}


def _cas_runtime(*payloads: dict[str, Any]) -> tuple[AgentRuntime, ArtifactStore]:
    proxy = ModelProviderProxy(
        MockTransport(completions=[json.dumps(p) for p in payloads],
                      default_text=json.dumps(payloads[-1])),
        signer=ZoneBSigner(KEY, KEY_ID),
    )
    cas = ArtifactStore()
    return AgentRuntime(proxy=proxy, cas=cas), cas


def test_agent_nodes_emit_handles_that_resolve_back_via_store() -> None:
    """Drive the planner with a cas-backed runtime; every emitted handle resolves
    back to an EQUAL typed body via store.resolve."""
    rt, cas = _cas_runtime(
        plan_payload(
            intent=INTENT_PAYLOAD,
            prd=PRD_PAYLOAD,
            critique={"missing": [], "security_abuse_omissions": []},
            sprint={"milestones": [], "usd_cap_microusd": 0, "done_definition": "done"},
            design={"title": "Todo", "ownership": {"app.py": "owner-a"}},
        )
    )

    handles = make_planner_node(rt)({"request": "todo app"}, _config("run-E"))["artifacts"]
    # Every handle is content-addressed and resolves to a body of a matching kind.
    assert handles
    for handle in handles:
        assert handle.uri.startswith("cas://sha256/")
        body = cas.resolve(handle)
        # The resolved body re-mints the SAME content identity as the handle uri.
        assert handle.uri == f"cas://sha256/{body.content_identity()}"

    # Spot-check a typed criterion round-tripped with its fields intact.
    crit_handles = [h for h in handles if h.kind == "acceptance_criterion"]
    assert len(crit_handles) == 2
    resolved = {c.id: c for c in (cas.resolve(h) for h in crit_handles)}
    assert isinstance(resolved["AC-001"], AcceptanceCriterion)
    assert resolved["AC-001"].security_abuse is True
    assert resolved["AC-001"].criticality is Criticality.HIGH_RISK
