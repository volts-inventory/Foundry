"""Security-layer node factories (T-M2-04): appsec / audit / supply_chain.

Drives the REAL nodes over a REAL :class:`ModelProviderProxy` backed by
:class:`MockTransport` + an :class:`ArtifactStore` on ``rt.cas`` (no live API
key, LEARNINGS D1). Asserts, for each node:

* the advisory body artifact is emitted as a content-addressed handle and is
  RESOLVABLE from the store;
* an advisory ``GateTriage`` is produced on the ``gate_triage`` channel — and
  AppSec flags a scripted HIGH-severity finding as BLOCKING;
* NO ``gates`` channel write from ANY security node (LEARNINGS L5 — agents
  emit triage, never a verdict);
* the nodes drop into a ``StateGraph(FactoryState)`` (graph-compatibility).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents.security import (
    make_appsec_engineer_node,
    make_audit_compliance_node,
    make_supply_chain_reviewer_node,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateTriage
from foundry.schemas.security import (
    AuditFinding,
    Severity,
    SupplyChainReport,
    ThreatModel,
)
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-security-key"
KEY_ID = "dev-key-security"


# ---- harness ----------------------------------------------------------------


def make_runtime(transport: MockTransport) -> tuple[AgentRuntime, ArtifactStore]:
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))
    cas = ArtifactStore()
    return AgentRuntime(proxy=proxy, cas=cas), cas


def config_for(run_id: str, *, step: int = 1, **configurable: Any) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id, **configurable},
        "metadata": {"langgraph_step": step},
    }


def _appsec_payload(severity: str = "high", findings: list[str] | None = None) -> str:
    return json.dumps(
        {
            "threat_model_body": "STRIDE: tampering of auth token; spoofing login.",
            "oracle_bindings": {"T-001": "test::auth_token_signed"},
            "residual_item_ids": ["T-002"],
            "severity": severity,
            "findings": findings if findings is not None else ["missing CSRF token"],
        }
    )


def _audit_payload(severity: str = "medium") -> str:
    return json.dumps(
        {
            "sbom_body": "pkg: left-pad@1.0.0; flask@3.0.0",
            "license_report_body": "all permissive",
            "compliance_map_body": "SOC2 CC6.1 -> auth tests",
            "severity": severity,
            "findings": ["GPL dependency 'foo' under disallowed license"],
        }
    )


def _supply_chain_payload(
    violations: list[str] | None = None, trust_root_touched: bool = False
) -> str:
    return json.dumps(
        {
            "report_body": "diff of Dockerfile + lockfile reviewed",
            "policy_violations": violations if violations is not None else [],
            "trust_root_touched": trust_root_touched,
        }
    )


def _no_gates_write(writes: dict[str, Any]) -> None:
    """L5: a security node must NEVER write the ``gates`` channel."""
    assert "gates" not in writes, "security node wrote to the gates channel (L5 breach)"


# ---- 1. AppSec: ThreatModel + BLOCKING security triage ----------------------


def test_advisory_security_authors_skip_the_model_call_in_autonomous_mode() -> None:
    """A headless autonomous run reads none of these authors' output (the gates
    re-scan the tree independently), so each skips its model call and writes
    nothing — while an assistant/gated run still authors normally."""
    cfg = config_for("run-auto", run_against="sha", base_tree_sha="base")
    cases = (
        (make_appsec_engineer_node, _appsec_payload(severity="low")),
        (make_audit_compliance_node, _audit_payload(severity="low")),
        (make_supply_chain_reviewer_node, _supply_chain_payload(violations=[])),
    )
    for make, payload in cases:
        transport = MockTransport(completions=[payload])
        rt, _cas = make_runtime(transport)
        assert make(rt)({"request": "x", "mode": "autonomous"}, cfg) == {}
        assert transport.requests == []  # no model call billed

        # control: an assistant run (mode unset / not autonomous) still authors.
        transport2 = MockTransport(completions=[payload])
        rt2, _cas2 = make_runtime(transport2)
        assert make(rt2)({"request": "x"}, cfg) != {}
        assert transport2.requests  # model called


def test_appsec_emits_threat_model_and_blocking_high_severity_triage() -> None:
    transport = MockTransport(completions=[_appsec_payload(severity="high")])
    rt, cas = make_runtime(transport)
    node = make_appsec_engineer_node(rt)

    writes = node({"request": "review auth flow"}, config_for("run-appsec", run_against="sha-X"))
    _no_gates_write(writes)

    [handle] = writes["artifacts"]
    assert handle.kind == "threat_model"
    body = cas.resolve(handle)  # resolvable from the store
    assert isinstance(body, ThreatModel)
    assert body.run_against == "sha-X"
    assert body.oracle_bindings == {"T-001": "test::auth_token_signed"}
    assert body.residual_item_ids == ("T-002",)
    assert body.has_residual()

    [triage] = writes["gate_triage"]
    assert isinstance(triage, GateTriage)
    assert triage.name == "security"
    assert triage.produced_by == "appsec_engineer"
    assert triage.explanation.startswith("BLOCKING")  # high-severity is blocking


def test_appsec_low_severity_is_not_blocking() -> None:
    transport = MockTransport(completions=[_appsec_payload(severity="low")])
    rt, _cas = make_runtime(transport)
    node = make_appsec_engineer_node(rt)

    writes = node({"request": "x"}, config_for("run-appsec-low"))
    _no_gates_write(writes)
    [triage] = writes["gate_triage"]
    assert not triage.explanation.startswith("BLOCKING")


def test_appsec_no_findings_yields_clean_triage() -> None:
    transport = MockTransport(completions=[_appsec_payload(severity="high", findings=[])])
    rt, _cas = make_runtime(transport)
    node = make_appsec_engineer_node(rt)

    writes = node({"request": "x"}, config_for("run-appsec-clean"))
    [triage] = writes["gate_triage"]
    # high severity but NO findings -> not blocking (nothing to block on)
    assert not triage.explanation.startswith("BLOCKING")


# ---- 2. Audit / Compliance: AuditFinding advisory ---------------------------


def test_audit_emits_finding_and_advisory_triage() -> None:
    transport = MockTransport(completions=[_audit_payload(severity="high")])
    rt, cas = make_runtime(transport)
    node = make_audit_compliance_node(rt)

    writes = node({"request": "audit deps"}, config_for("run-audit", run_against="sha-Y"))
    _no_gates_write(writes)

    [handle] = writes["artifacts"]
    assert handle.kind == "audit_finding"
    body = cas.resolve(handle)
    assert isinstance(body, AuditFinding)
    assert body.run_against == "sha-Y"
    assert body.severity is Severity.HIGH
    assert body.license_report_uri is not None
    assert body.compliance_map_uri is not None
    assert body.findings

    [triage] = writes["gate_triage"]
    assert triage.name == "audit"
    assert triage.produced_by == "audit_compliance"
    assert triage.explanation.startswith("BLOCKING")


# ---- 3. Supply-Chain Reviewer: SupplyChainReport advisory -------------------


def test_supply_chain_clean_diff_is_advisory_non_blocking() -> None:
    transport = MockTransport(completions=[_supply_chain_payload(violations=[])])
    rt, cas = make_runtime(transport)
    node = make_supply_chain_reviewer_node(rt)

    writes = node(
        {"request": "review build"},
        config_for("run-sc", run_against="sha-Z", base_tree_sha="base-Z"),
    )
    _no_gates_write(writes)

    [handle] = writes["artifacts"]
    assert handle.kind == "supply_chain_report"
    body = cas.resolve(handle)
    assert isinstance(body, SupplyChainReport)
    assert body.base_tree_sha == "base-Z"
    assert body.tree_sha == "sha-Z"
    assert body.policy_violations == ()

    [triage] = writes["gate_triage"]
    assert triage.name == "supply_chain"
    assert not triage.explanation.startswith("BLOCKING")


def test_supply_chain_policy_violation_is_blocking() -> None:
    transport = MockTransport(
        completions=[_supply_chain_payload(violations=["build-time network access"])]
    )
    rt, _cas = make_runtime(transport)
    node = make_supply_chain_reviewer_node(rt)

    writes = node({"request": "x"}, config_for("run-sc-bad"))
    _no_gates_write(writes)
    [triage] = writes["gate_triage"]
    assert triage.explanation.startswith("BLOCKING")


def test_supply_chain_trust_root_change_requires_hitl() -> None:
    transport = MockTransport(
        completions=[_supply_chain_payload(violations=[], trust_root_touched=True)]
    )
    rt, cas = make_runtime(transport)
    node = make_supply_chain_reviewer_node(rt)

    writes = node({"request": "x"}, config_for("run-sc-trust"))
    [handle] = writes["artifacts"]
    body = cas.resolve(handle)
    assert isinstance(body, SupplyChainReport)
    assert body.trust_root_touched
    [triage] = writes["gate_triage"]
    assert triage.explanation.startswith("BLOCKING")
    assert "HITL" in triage.explanation


# ---- 4. graph-compatibility: the three nodes in a StateGraph ----------------


def test_security_nodes_run_inside_a_state_graph() -> None:
    transport = MockTransport(
        completions=[
            _appsec_payload(severity="high"),
            _audit_payload(severity="medium"),
            _supply_chain_payload(violations=["egress widening"]),
        ]
    )
    rt, cas = make_runtime(transport)

    g: StateGraph = StateGraph(FactoryState)
    g.add_node("appsec", make_appsec_engineer_node(rt))
    g.add_node("audit", make_audit_compliance_node(rt))
    g.add_node("supply_chain", make_supply_chain_reviewer_node(rt))
    g.add_edge(START, "appsec")
    g.add_edge("appsec", "audit")
    g.add_edge("audit", "supply_chain")
    g.add_edge("supply_chain", END)
    app = g.compile()

    final = app.invoke(
        {"request": "ship securely"},
        config_for("run-graph", run_against="sha-G"),
    )

    # all three advisory bodies landed on the channel (folded by merge_by_uri).
    kinds = {a.kind for a in final["artifacts"]}
    assert {"threat_model", "audit_finding", "supply_chain_report"} <= kinds

    # all three advisory triages rode the gate_triage channel...
    triage_names = {t.name for t in final["gate_triage"]}
    assert {"security", "audit", "supply_chain"} <= triage_names

    # ...and NO security node wrote the gates channel (L5).
    assert not final.get("gates")

    # every body is resolvable end to end from the store.
    for handle in final["artifacts"]:
        assert cas.resolve(handle) is not None
