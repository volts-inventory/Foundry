"""Upstream prose bodies persist into the CAS under their handles.

The planner authors narrative prose (PRD body, system-design body, ADR
rationale). These tests prove the prose round-trips through the
:class:`ArtifactStore` (as an allowlisted :class:`ProseBody`), while the
``store=None`` mint-only path stays byte-identical to the historical behaviour.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import cas_uri, persist_prose
from foundry.agents.planner import make_planner_node
from foundry.durability.artifact_store import ArtifactStore, UnknownArtifactError
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import ADR, SystemDesign
from foundry.schemas.docs import ProseBody
from foundry.schemas.product import PRD
from foundry.state.seq import Seq
from tests.planner_payload import plan_payload

SEQ = Seq("r", 1, 1, "n", 0)

PRD_BODY = "Users add, list, and complete todos via a REST API. Two personas..."
DESIGN_BODY = "A hexagonal core with a thin REST adapter and a SQL repository..."
ADR_BODY = "We chose SQLite over Postgres for the dev-grade single-node profile..."

PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": PRD_BODY,
    "required_capabilities": ["ui"],
    "acceptance_criteria": [
        {"id": "AC-001", "text": "Given a user, When add, Then it lists."},
    ],
}

DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "design_body": DESIGN_BODY,
    "components": ["api", "store"],
    "adrs": [
        {
            "id": "ADR-001",
            "title": "Storage engine",
            "decision": "Use SQLite",
            "body": ADR_BODY,
            "rejected_alternatives": ["Postgres"],
        }
    ],
    "interfaces": [{"name": "TodoStore.add", "spec": "add(todo) -> id"}],
    "ownership": {"foundry/api": "api-team"},
}

PLAN_PAYLOAD: dict[str, Any] = plan_payload(
    intent={"intent": "todo"},
    prd=PRD_PAYLOAD,
    critique={"missing": [], "security_abuse_omissions": []},
    sprint={"milestones": [], "usd_cap_microusd": 0, "done_definition": "done"},
    design=DESIGN_PAYLOAD,
)


def _config(run_id: str = "run-prose") -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": 1}}


def _runtime(payload: dict[str, Any], *, cas: ArtifactStore | None) -> AgentRuntime:
    tx = MockTransport(completions=[json.dumps(payload)])
    return AgentRuntime(proxy=ModelProviderProxy(tx), cas=cas)


# ---- producer side: prose round-trips through the store ----------------------


def test_planner_persists_prose_under_its_handles() -> None:
    store = ArtifactStore()
    node = make_planner_node(_runtime(PLAN_PAYLOAD, cas=store))
    writes = node({"request": "build a todo app"}, _config())
    bodies = [store.get_body(a.uri) for a in writes["artifacts"]]

    prd = next(b for b in bodies if isinstance(b, PRD))
    # The handle is the historical content digest of the prose string (stable).
    assert prd.prd_uri == cas_uri(PRD_BODY)
    # ...and the prose itself now resolves out of the store.
    body = store.get_body(prd.prd_uri)
    assert isinstance(body, ProseBody) and body.text == PRD_BODY

    design = next(b for b in bodies if isinstance(b, SystemDesign))
    adr = next(b for b in bodies if isinstance(b, ADR))
    assert design.design_uri == cas_uri(DESIGN_BODY)
    assert adr.body_uri == cas_uri(ADR_BODY)
    assert store.get_body(design.design_uri) == ProseBody(text=DESIGN_BODY)
    assert store.get_body(adr.body_uri) == ProseBody(text=ADR_BODY)


def test_persist_prose_store_none_is_mint_only() -> None:
    """Backward-compatible: store=None mints the SAME handle and persists nothing."""
    store = ArtifactStore()
    uri = persist_prose(PRD_BODY, store=None)
    assert uri == cas_uri(PRD_BODY)
    # nothing was written, so a resolve fails closed
    try:
        store.get_body(uri)
    except UnknownArtifactError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("store=None must not persist a body")
