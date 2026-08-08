"""Factory graph — Autonomous-mode emit (hands-off build → admit → deploy; T-M4-10).

Drives :func:`build_factory_graph` in ``mode="autonomous"`` over the SAME
end-to-end doubles the Gated supervisor test uses (LEARNINGS D1/D3/D4/SUP7): a
REAL :class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API
key) + a dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner` admitted
through the REAL ``merge_gates`` path (a :class:`DevGateWriteVerifier` on the
reducer contextvar), a :class:`StubAdapter`, in-memory ``resolve`` /
``resolve_blob`` materialization seams, and a durable :class:`FoundryCheckpointer`.

The release boundary is NOT a human / recorded sign-off — it is the TRUST ROOT
(governance §4/§5): a REAL :class:`TrustedBuilder` (hermetic, signed provenance),
the fail-closed deploy-time :class:`AdmissionController` (ADMISSION IS THE SOLE
release gate), and the least-privileged :class:`Deployer`.

Asserts (the T-M4-10 exit criteria):

* a CLEAN autonomous run — terminal (Spec+Build+Test) + assurance attested PASS
  → build → admit (ADMITTED) → EXACTLY ONE deploy action; admission (NOT a human
  sign-off) is the gate (no ``release-approver`` record is needed);
* a DENIED artifact (the integrated subject digest disqualified via
  ``global_records``) deploys NOTHING (fail-closed) — no deploy action;
* a FAILED / halted run (the assurance Security gate catches a hardcoded secret
  → attested FAIL → latch → halt) → NO build, NO admit, NO deploy;
* kill-then-resume: re-invoking the SAME ``thread_id`` does NOT duplicate the
  deploy (same deterministic idempotency key);
* ASSISTANT + GATED modes still work (back-compat — those suites are unchanged).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import (
    _admission_tree_and_gates,
    _autonomous_attested_gates,
    build_factory_graph,
)
from foundry.graph.supervisor import MODE_AUTONOMOUS
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

KEY = b"dev-zone-b-factory-graph-autonomous-key-0123456789"
KEY_ID = "dev-key-factory-graph-autonomous"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
BUILDER_ID = "dev-trusted-builder-autonomous"
DEPLOY_TARGET = "prod"

_DIFF_BODY_URI = "cas://test/diff-body"


# ---- scripted per-node payloads (free-text completions, SOP-keyed) ----------

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user"],
    "unknowns": [],
    "success_criteria": ["a user can add a todo"],
    "must_haves": ["add todo"],
    "nice_to_haves": [],
}
PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos.",
    "required_capabilities": [],
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
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401; negative oracle",
            "security_abuse": True,
        },
    ],
}
CRITIQUE_PAYLOAD: dict[str, Any] = {"missing": [], "security_abuse_omissions": []}
SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "none",
    "done_definition": "all criteria green",
}
DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "design_body": "api -> store",
    "components": ["api"],
    "adrs": [],
    "interfaces": [],
    "ownership": {"app.py": "owner-a"},
    "pinned_constraints": [],
}
DECOMPOSITION_PAYLOAD: dict[str, Any] = {
    "tasks": [{"unit": "app.py", "title": "build app", "criterion_ids": ["AC-001"]}]
}
QA_PAYLOAD: dict[str, Any] = {
    "tests": [{"test_id": "t-1", "criterion_ids": ["AC-001", "AC-002"]}],
    "quarantined": [],
}
SDET_PAYLOAD: dict[str, Any] = {
    "scenarios": [{"scenario_id": "e2e-1", "criterion_ids": ["AC-001"]}],
    "quarantined": [],
}
APPSEC_PAYLOAD: dict[str, Any] = {
    "threat_model_body": "STRIDE model",
    "oracle_bindings": {"S1": "test_auth"},
    "residual_item_ids": [],
    "severity": "low",
    "findings": [],
}
AUDIT_PAYLOAD: dict[str, Any] = {"sbom_body": "sbom", "severity": "low", "findings": []}
SUPPLY_CHAIN_PAYLOAD: dict[str, Any] = {
    "report_body": "supply-chain review",
    "policy_violations": [],
    "trust_root_touched": False,
}

_SCRIPTS: dict[str, str] = {
    # Every node that can make a model call needs a script: an unscripted node
    # serves "" and dies as ProxyUnavailable after the retries.
    "You are the Scope Judge": json.dumps(
        {"tier": "standard", "rationale": "a full-scope build", "signals": []}
    ),
    "Stakeholder / Client Proxy": json.dumps(INTENT_PAYLOAD),
    "Product Manager": json.dumps(PRD_PAYLOAD),
    "Criteria-Completeness Critic": json.dumps(CRITIQUE_PAYLOAD),
    "Engineering / Program Manager": json.dumps(SPRINT_PAYLOAD),
    "Principal Architect": json.dumps(DESIGN_PAYLOAD),
    "Senior Engineer": json.dumps(DECOMPOSITION_PAYLOAD),
    "QA Engineer": json.dumps(QA_PAYLOAD),
    "SDET": json.dumps(SDET_PAYLOAD),
    "You are the Security / AppSec Engineer": json.dumps(APPSEC_PAYLOAD),
    "You are the Audit / Compliance agent": json.dumps(AUDIT_PAYLOAD),
    "You are the Supply-Chain Reviewer": json.dumps(SUPPLY_CHAIN_PAYLOAD),
}


# ---- in-memory materialization doubles --------------------------------------

# A clean unified-diff ADDITION: app.py + a passing test + a lockfile. Build+Test
# PASS; assurance Security/Audit PASS.
_CLEAN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)
# A KNOWN-VULNERABLE change: a hardcoded secret trips the SecurityGate dev rule.
# Build+Test still PASS; the assurance Security gate catches it -> FAIL -> latch.
_VULN_DIFF = (
    '--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+API_KEY = "supersecrethardcodedtoken123"\n'
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)


def _synthetic_resolve(handle: Artifact) -> FrozenModel:
    """KIND-dispatching typed resolver (an in-memory infra double; SUP7)."""
    if handle.kind in {SystemDesign.__name__, "system_design"}:
        return SystemDesign(
            title="Todo service",
            design_uri="cas://design/body",
            components=("api",),
            interface_ids=(),
            seq=handle.seq,
        )
    if handle.kind == "ownership_map":
        return OwnershipMap(owners={"app.py": "owner-a"}, seq=handle.seq)
    if handle.kind == "acceptance_criterion":
        cid = f"AC-{handle.seq.local:03d}-{handle.seq.emission_epoch}"
        return AcceptanceCriterion(
            id=cid,
            text=f"Given/When/Then for {cid}",
            criticality=Criticality.HIGH_RISK,
            oracle_hint=f"oracle for {cid}",
            security_abuse=True,
            seq=handle.seq,
        )
    if handle.kind == "criteria_findings":
        # The Criteria-Completeness Critic finding the Spec gate now consumes.
        # No security/abuse omission here -> does not block the clean run.
        return CriteriaCritique(
            prd_uri="cas://design/prd",
            security_abuse_omissions=(),
            judgment_provenance=JudgmentProvenance.SELF,
            seq=handle.seq,
        )
    if handle.kind == "feature_diff":
        return FeatureDiff(
            diff_uri=_DIFF_BODY_URI,
            task_id=handle.task_id or "T1",
            branch=f"task/{handle.task_id or 'T1'}",
            base_tree_sha="base-sha",
            seq=handle.seq,
        )
    raise AssertionError(f"unexpected artifact kind for resolve: {handle.kind!r}")


def _resolve_blob_factory(diff_body: str) -> Any:
    def _resolve_blob(uri: str) -> bytes:
        return diff_body.encode("utf-8")

    return _resolve_blob


# ---- harness ----------------------------------------------------------------


def _runtime() -> AgentRuntime:
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "verdict_nonce": f"N-{run_id}",
            "subject_artifact_uri": _DIFF_BODY_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
        "recursion_limit": 200,
    }


def _build_autonomous(
    records: GlobalRecords,
    source_dir: str,
    *,
    diff_body: str,
    checkpointer: Any,
    max_fix_attempts: int = 3,
    admission: AdmissionController | None = None,
) -> Any:
    """Wire the autonomous factory graph with the REAL trust-root chain.

    The verifier shares the runner's zone-B KEY/KEY_ID + the SAME cross-fork
    records, so admission re-verifies the runner-attested gate-state and reads
    the cross-fork disqualification latch."""
    signer = ZoneBSigner(KEY, KEY_ID)
    return build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        checkpointer=checkpointer,
        max_fix_attempts=max_fix_attempts,
        mode=MODE_AUTONOMOUS,
        builder=TrustedBuilder(signer, builder_id=BUILDER_ID),
        admission=admission or AdmissionController(builder_id=BUILDER_ID),
        deployer=Deployer(target=DEPLOY_TARGET),
        gate_verifier=DevGateWriteVerifier(KEY, KEY_ID, records),
    )


def _deploy_actions(state: FactoryState) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "deploy"]


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


def _src(tmp_path: Any) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    return str(src)


# ---- CLEAN autonomous run -> build -> admit -> ONE deploy --------------------


def test_autonomous_clean_run_builds_admits_and_deploys_once(tmp_path: Any) -> None:
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous(
            records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-auto-pass")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # Terminal + assurance gates attested PASS (the gated assurance spine).
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    assert final["phase"] == "done"
    assert final.get("mode") == MODE_AUTONOMOUS

    # EXACTLY ONE deploy action — gate-bearing, hands-off (no human sign-off).
    deploys = _deploy_actions(final)
    assert len(deploys) == 1
    deploy = deploys[0]
    assert deploy.kind == "deploy"
    assert deploy.gate_bearing is True
    assert deploy.payload_uri.startswith("cas://admission/")  # the admitting decision

    # ADMISSION was the gate, NOT a human sign-off: the deploy fired off the
    # admitting decision (payload starts cas://admission/), and the deploy
    # ExternalAction carries no sign-off binding. (The gated spine still RECORDS
    # a release sign-off, but it is NOT the release authority here — admission is.)
    assert deploy.payload_uri.startswith("cas://admission/")

    # The deploy really is keyed to an ADMITTED decision over the integrated tree.
    gate_results = _autonomous_attested_gates(final)
    bound = _admission_tree_and_gates(gate_results)
    assert bound is not None
    _tree_sha, bound_gates, _expected_trees = bound
    # The bound gate-state carries the always-on assurance floor AND the terminal
    # gates across BOTH trees (admission re-verifies all of them; M4-R1-01).
    bound_names = {g.name for g in bound_gates}
    assert {"spec", "build", "test", "security", "audit"} <= bound_names


# ---- a DENIED artifact deploys NOTHING (fail-closed) ------------------------


def test_autonomous_denied_artifact_deploys_nothing(tmp_path: Any) -> None:
    """ADMISSION is the SOLE release gate: force a DENY by disqualifying the
    integrated subject digest in global_records (a forged/laundered fork). The
    autonomous emit fail-closes — no deploy action."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))

    # First, run clean to discover the integrated subject digest the autonomous
    # build would produce, so we can pre-disqualify exactly THAT digest.
    probe_records = GlobalRecords()
    probe_ckpt = FoundryCheckpointer()
    probe_token = set_gate_write_verifier(
        DevGateWriteVerifier(KEY, KEY_ID, probe_records)
    )
    (tmp_path / "p").mkdir()
    try:
        probe = _build_autonomous(
            probe_records,
            _src(tmp_path / "p"),
            diff_body=_CLEAN_DIFF,
            checkpointer=probe_ckpt.saver,
        )
        probe_final: FactoryState = probe.invoke(
            {"request": "build a todo app"}, _config("run-auto-probe")
        )
    finally:
        reset_gate_write_verifier(probe_token)
        probe_ckpt.close()
    # The probe deployed once; recover the subject digest from the build the
    # autonomous emit would re-derive (a pure function of the integrated tree).
    probe_gates = _autonomous_attested_gates(probe_final)
    bound = _admission_tree_and_gates(probe_gates)
    assert bound is not None
    tree_sha, _, _ = bound
    signer = ZoneBSigner(KEY, KEY_ID)
    builder = TrustedBuilder(signer, builder_id=BUILDER_ID)
    from foundry.graph.factory_graph import _autonomous_build_request

    req = _autonomous_build_request(probe_final, tree_sha, probe_gates[0].seq)
    subject_digest = builder.build(req).result.subject_digest

    # Now run a SECOND, identical autonomous build but with that exact subject
    # digest cross-fork DISQUALIFIED — admission DENYs, deploy nothing.
    records.disqualify_digest(subject_digest, "forged-verdict fork", "fork-run-99")
    (tmp_path / "d").mkdir()
    try:
        app = _build_autonomous(
            records, _src(tmp_path / "d"), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-auto-deny")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # The gate-state itself is clean PASS, but admission DENYs the disqualified
    # digest -> NO deploy (fail-closed; the trust root is the only authority).
    statuses = _gates_by_name(final)
    assert statuses.get("security") is GateStatus.PASS
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"


# ---- a FAILED / halted run -> no build, no admit, no deploy ------------------


def test_autonomous_halted_run_does_not_build_admit_or_deploy(tmp_path: Any) -> None:
    """A hardcoded secret is caught in the assurance phase (attested Security
    FAIL -> latch -> halt). The autonomous emit guard fail-closes BEFORE the
    trust-root chain: no build, no admit, no deploy."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous(
            records,
            _src(tmp_path),
            diff_body=_VULN_DIFF,
            checkpointer=ckpt.saver,
            max_fix_attempts=2,
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-auto-vuln")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS
    # The assurance Security gate caught the secret -> attested FAIL -> latch.
    security = next(g for g in final["gates"] if g.name == "security")
    assert security.status is GateStatus.FAIL
    assert security.runner_attestation is not None
    assert records.stable_fail_latched(security.run_against, "security")

    # Fail-closed: the guard halted before the trust-root chain — NO deploy.
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"


# ---- kill-then-resume: no duplicate deploy (same idempotency key) -----------


def test_autonomous_kill_then_resume_does_not_duplicate_deploy(tmp_path: Any) -> None:
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous(
            records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver
        )
        cfg = _config("run-auto-resume")
        first: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
        assert len(_deploy_actions(first)) == 1
        first_key = _deploy_actions(first)[0].idempotency_key

        # Re-invoke the SAME thread_id: LangGraph resumes + re-folds; the deployer
        # re-derives the SAME (content_digest over subject_digest+target) key ->
        # union_actions coalesces.
        second: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    deploys = _deploy_actions(second)
    assert len(deploys) == 1  # coalesced — NOT a duplicate
    assert deploys[0].idempotency_key == first_key
