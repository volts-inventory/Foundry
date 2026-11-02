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

import pytest
from langchain_core.runnables import RunnableConfig

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


@pytest.mark.parametrize(
    ("caps", "present", "absent"),
    [
        ([], {"system_design", "ownership_map", "adr", "interface_contract", "prd"},
         {"domain_addendum", "ux_spec"}),
        (["regulated"], {"system_design", "domain_addendum"}, {"ux_spec"}),
        (["frontend"], {"ux_spec"}, {"domain_addendum"}),
        (["regulated", "ui"], {"system_design", "domain_addendum", "ux_spec"}, set()),
    ],
)
def test_capabilities_route_the_conditionals(
    caps: list[str], present: set[str], absent: set[str]
) -> None:
    final, tx = _run(caps)

    kinds = _kinds(final)
    assert present <= kinds
    assert not absent & kinds
    assert len(tx.requests) == 1
    if not caps:
        assert {c.id for c in final["pinned_constraints"]} == {"PC-001", "PC-002"}
