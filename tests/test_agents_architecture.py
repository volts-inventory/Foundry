"""Conditional Layer-2 nodes (domain_specialist / ux_design).

The design itself is authored by the planner (tests/test_planner.py). These
deterministic addenda nodes enter only when ``required_capabilities`` match
(predicate + node guard agree) and are a no-op otherwise (``{}``).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

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


# ---- conditional entry predicates ----------------------------------------


@pytest.mark.parametrize(
    ("enter", "matching", "other"),
    [
        (should_enter_domain_specialist, [["regulated"], ["ml", "ui"]], ["ui"]),
        (should_enter_ux_design, [["frontend"], ["ui"]], ["regulated"]),
    ],
)
def test_entry_predicates(enter: Any, matching: list[list[str]], other: list[str]) -> None:
    for caps in matching:
        assert enter({"required_capabilities": caps})
    assert not enter({"required_capabilities": other})
    assert not enter({})


@pytest.mark.parametrize(
    ("make", "match", "other", "kind", "node_id"),
    [
        (make_domain_specialist_node, ["regulated"], ["ui"], "domain_addendum",
         DOMAIN_SPECIALIST_NODE_ID),
        (make_ux_design_node, ["frontend"], ["regulated"], "ux_spec", UX_DESIGN_NODE_ID),
    ],
)
def test_conditional_node_is_noop_without_matching_capability(
    make: Any, match: list[str], other: list[str], kind: str, node_id: str
) -> None:
    node = make(make_runtime())
    assert node({"required_capabilities": other}, config_for("run-A")) == {}
    assert node({}, config_for("run-A")) == {}
    artifacts = list(node({"required_capabilities": match}, config_for("run-A"))["artifacts"])
    assert {a.kind for a in artifacts} == {kind}
    assert artifacts[0].produced_by == node_id
