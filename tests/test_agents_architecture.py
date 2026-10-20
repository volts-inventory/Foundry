"""Conditional Layer-2 nodes (domain_specialist / ux_design).

The design itself is authored by the planner (tests/test_planner.py). These
deterministic addenda nodes enter only when ``required_capabilities`` match
(predicate + node guard agree) and are a no-op otherwise (``{}``); a
factory-built node drops straight into a ``StateGraph(FactoryState)``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents.architecture import (
    make_domain_specialist_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)
from foundry.agents.architecture.conditional import (
    DOMAIN_SPECIALIST_NODE_ID,
    UX_DESIGN_NODE_ID,
)
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-architecture-key"
KEY_ID = "dev-key-architecture"


def make_runtime() -> AgentRuntime:
    proxy = ModelProviderProxy(MockTransport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _artifacts_of(writes: dict[str, Any]) -> list[Artifact]:
    return list(writes.get("artifacts", []))


def _kinds(artifacts: list[Artifact]) -> set[str]:
    return {a.kind for a in artifacts}


# ---- conditional entry predicates ----------------------------------------


def test_domain_specialist_predicate_matches_only_domain_caps() -> None:
    assert should_enter_domain_specialist({"required_capabilities": ["regulated"]})
    assert should_enter_domain_specialist({"required_capabilities": ["ml", "ui"]})
    assert not should_enter_domain_specialist({"required_capabilities": ["ui"]})
    assert not should_enter_domain_specialist({})


def test_ux_predicate_matches_only_ui_caps() -> None:
    assert should_enter_ux_design({"required_capabilities": ["frontend"]})
    assert should_enter_ux_design({"required_capabilities": ["ui"]})
    assert not should_enter_ux_design({"required_capabilities": ["regulated"]})
    assert not should_enter_ux_design({})


def test_domain_specialist_node_is_noop_without_matching_capability() -> None:
    rt = make_runtime()
    node = make_domain_specialist_node(rt)
    assert node({"required_capabilities": ["ui"]}, config_for("run-A")) == {}
    assert node({}, config_for("run-A")) == {}


def test_domain_specialist_node_contributes_on_match() -> None:
    rt = make_runtime()
    node = make_domain_specialist_node(rt)
    writes = node({"required_capabilities": ["regulated"]}, config_for("run-A"))
    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"domain_addendum"}
    assert artifacts[0].produced_by == DOMAIN_SPECIALIST_NODE_ID


def test_ux_node_is_noop_without_ui_capability() -> None:
    rt = make_runtime()
    node = make_ux_design_node(rt)
    assert node({"required_capabilities": ["regulated"]}, config_for("run-A")) == {}


def test_ux_node_contributes_on_match() -> None:
    rt = make_runtime()
    node = make_ux_design_node(rt)
    writes = node({"required_capabilities": ["frontend"]}, config_for("run-A"))
    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"ux_spec"}
    assert artifacts[0].produced_by == UX_DESIGN_NODE_ID


# ---- graph-compatibility --------------------------------------------------


def test_conditional_nodes_run_inside_a_state_graph() -> None:
    rt = make_runtime()
    g = StateGraph(FactoryState)
    g.add_node("domain", make_domain_specialist_node(rt))
    g.add_node("ux", make_ux_design_node(rt))
    g.add_edge(START, "domain")
    g.add_edge("domain", "ux")
    g.add_edge("ux", END)
    app = g.compile()

    # UI + domain caps: both contribute.
    final: FactoryState = app.invoke(
        {"request": "regulated UI app", "required_capabilities": ["regulated", "ui"]},
        config_for("run-A"),
    )
    assert {a.kind for a in final["artifacts"]} == {"domain_addendum", "ux_spec"}

    # No matching caps: both no-op, no artifacts written.
    final2: FactoryState = app.invoke(
        {"request": "plain backend"}, config_for("run-B")
    )
    assert not final2.get("artifacts")
