"""Planner subgraph — planner -> conditional domain_specialist / ux_design.

Compiles the REAL ``build_planner_subgraph`` standalone over a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport` and asserts both
conditional paths:

* the planner declares no matching capability -> straight to END: only the
  planner's artifacts + ``pinned_constraints`` (no domain_addendum / ux_spec);
* the planner declares a matching capability ("regulated" / "frontend") -> the
  conditional node is entered and contributes its addendum, alongside the plan.

The addenda nodes make no model call, so each run is exactly ONE request.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.subgraphs.planner import build_planner_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.state.factory_state import FactoryState
from tests.test_planner import PLAN

KEY = b"dev-zone-b-planner-subgraph-key"
KEY_ID = "dev-key-planner-subgraph"


def _runtime(capabilities: list[str]) -> tuple[AgentRuntime, MockTransport]:
    payload: dict[str, Any] = {
        **PLAN,
        "prd": {**PLAN["prd"], "required_capabilities": capabilities},
    }
    tx = MockTransport(completions=[json.dumps(payload)])
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy), tx


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": 1}}


def _run(capabilities: list[str]) -> tuple[FactoryState, MockTransport]:
    rt, tx = _runtime(capabilities)
    final: FactoryState = build_planner_subgraph(rt).compile().invoke(
        {"request": "todo app"}, _config("run")
    )
    return final, tx


def _kinds(state: FactoryState) -> set[str]:
    return {a.kind for a in state.get("artifacts", [])}


def test_builder_returns_an_uncompiled_state_graph() -> None:
    rt, _ = _runtime([])
    assert isinstance(build_planner_subgraph(rt), StateGraph)


def test_no_capabilities_skips_conditionals() -> None:
    final, tx = _run([])

    kinds = _kinds(final)
    assert {"system_design", "ownership_map", "adr", "interface_contract", "prd"} <= kinds
    assert {c.id for c in final["pinned_constraints"]} == {"PC-001", "PC-002"}
    assert "domain_addendum" not in kinds
    assert "ux_spec" not in kinds
    assert len(tx.requests) == 1


def test_domain_capability_enters_domain_specialist() -> None:
    final, tx = _run(["regulated"])

    kinds = _kinds(final)
    assert "system_design" in kinds
    assert "domain_addendum" in kinds
    assert "ux_spec" not in kinds
    assert len(tx.requests) == 1


def test_ui_capability_enters_ux_design() -> None:
    final, _ = _run(["frontend"])

    kinds = _kinds(final)
    assert "ux_spec" in kinds
    assert "domain_addendum" not in kinds


def test_both_capabilities_enter_both_conditionals() -> None:
    final, tx = _run(["regulated", "ui"])

    assert {"system_design", "domain_addendum", "ux_spec"} <= _kinds(final)
    assert len(tx.requests) == 1
