"""Upstream prose bodies persist into the CAS and inline into the doc set (task [2]).

The PM/architect author narrative prose (PRD body, system-design body, ADR
rationale) but historically kept only the ``cas_uri(text)`` handle and DISCARDED
the text — so ``docs/render.py`` rendered a dead handle. These tests prove the
prose now round-trips through the :class:`ArtifactStore` (as an allowlisted
:class:`ProseBody`) and that the assembler resolves + inlines it, while the
``store=None`` mint-only path stays byte-identical to the historical behaviour.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import cas_uri, persist_prose
from foundry.agents.architecture import make_principal_architect_node
from foundry.agents.product import make_product_manager_node
from foundry.docs import assemble_documentation
from foundry.docs.render import render_adrs, render_design, render_prd
from foundry.durability.artifact_store import ArtifactStore, UnknownArtifactError
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import ADR, SystemDesign
from foundry.schemas.docs import ProseBody
from foundry.schemas.product import PRD
from foundry.state.seq import Seq

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


def _config(run_id: str = "run-prose") -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": 1}}


def _runtime(payload: dict[str, Any], *, cas: ArtifactStore | None) -> AgentRuntime:
    tx = MockTransport(completions=[json.dumps(payload)])
    return AgentRuntime(proxy=ModelProviderProxy(tx), cas=cas)


# ---- producer side: prose round-trips through the store ----------------------


def test_pm_persists_prd_prose_under_its_handle() -> None:
    store = ArtifactStore()
    node = make_product_manager_node(_runtime(PRD_PAYLOAD, cas=store))
    writes = node({"request": "build a todo app"}, _config())

    bodies = [store.get_body(a.uri) for a in writes["artifacts"]]
    prd = next(b for b in bodies if isinstance(b, PRD))
    # The handle is the historical content digest of the prose string (stable).
    assert prd.prd_uri == cas_uri(PRD_BODY)
    # ...and the prose itself now resolves out of the store.
    body = store.get_body(prd.prd_uri)
    assert isinstance(body, ProseBody) and body.text == PRD_BODY


def test_architect_persists_design_and_adr_prose() -> None:
    store = ArtifactStore()
    node = make_principal_architect_node(_runtime(DESIGN_PAYLOAD, cas=store))
    writes = node({"request": "build a todo app"}, _config())
    bodies = [store.get_body(a.uri) for a in writes["artifacts"]]

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


def test_pm_store_none_keeps_handle_without_storing() -> None:
    node = make_product_manager_node(_runtime(PRD_PAYLOAD, cas=None))
    writes = node({"request": "x"}, _config())
    # The node still emits a PRD handle whose prd_uri is the prose digest, but no
    # body is stored anywhere (the synthetic-resolver tests are untouched).
    assert any(a.kind == "prd" for a in writes["artifacts"])


# ---- renderer side: inline when present, fall back to handle when absent ------


def test_render_prd_inlines_resolved_prose() -> None:
    prd = PRD(title="Todo", prd_uri="cas://sha256/abc", seq=SEQ)
    body = render_prd(prd, (), prose=PRD_BODY).body
    assert PRD_BODY in body
    assert "cas://sha256/abc" not in body  # the dead handle is gone


def test_render_prd_falls_back_to_handle_without_prose() -> None:
    prd = PRD(title="Todo", prd_uri="cas://sha256/abc", seq=SEQ)
    body = render_prd(prd, ()).body
    assert "cas://sha256/abc" in body
    assert "Narrative body" in body


def test_render_design_inlines_resolved_prose() -> None:
    design = SystemDesign(title="Todo", design_uri="cas://sha256/d", seq=SEQ)
    body = render_design(design, prose=DESIGN_BODY).body
    assert DESIGN_BODY in body and "cas://sha256/d" not in body


def test_render_adrs_inlines_per_adr_prose() -> None:
    adr = ADR(id="ADR-001", title="Storage", decision="SQLite", body_uri="cas://sha256/a", seq=SEQ)
    body = render_adrs((adr,), prose={"ADR-001": ADR_BODY}).body
    assert ADR_BODY in body and "cas://sha256/a" not in body
    # an ADR without resolved prose still renders its handle note
    other = ADR(id="ADR-002", title="X", decision="y", body_uri="cas://sha256/b", seq=SEQ)
    body2 = render_adrs((adr, other), prose={"ADR-001": ADR_BODY}).body
    assert "cas://sha256/b" in body2


# ---- end-to-end: assembler resolves + inlines the prose ----------------------


def test_assemble_inlines_upstream_prose_end_to_end() -> None:
    store = ArtifactStore()
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport(default_text="{}")), cas=store)

    # Drive the real PM + architect so the prose lands in the store under its handle.
    pm_rt = _runtime(PRD_PAYLOAD, cas=store)
    arch_rt = _runtime(DESIGN_PAYLOAD, cas=store)
    pm_writes = make_product_manager_node(pm_rt)({"request": "todo"}, _config())
    arch_writes = make_principal_architect_node(arch_rt)({"request": "todo"}, _config())
    artifacts = [*pm_writes["artifacts"], *arch_writes["artifacts"]]

    bundle = assemble_documentation(
        request="todo",
        thread_id="r",
        state={"artifacts": artifacts, "gates": []},
        rt=rt,
        passed=True,
        phase="done",
        clock=lambda: "2026-01-01T00:00:00+00:00",
    )
    docs = {d.path: d.body for d in bundle.docs}
    assert PRD_BODY in docs["docs/PRD.md"]
    assert DESIGN_BODY in docs["docs/architecture/DESIGN.md"]
    assert ADR_BODY in docs["docs/architecture/ADRS.md"]
