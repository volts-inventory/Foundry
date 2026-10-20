"""Supervisor Phase-2 (Gated mode) wiring — tiering + design/release sign-off
boundaries + the assurance phase, with Phase-1 (Assistant) back-compat (T-M2-09).

Drives :func:`build_supervisor_graph` END-TO-END in ``mode="gated"`` over the
SAME doubles the Phase-1 supervisor test uses (LEARNINGS D1/D3/D4/SUP7): a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API key) +
a dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner`, the REAL
``merge_gates`` admission path (a :class:`DevGateWriteVerifier` on the reducer
contextvar), a :class:`StubAdapter`, and in-memory ``resolve`` / ``resolve_blob``
materialization seams.

Asserts (the Phase-2 EXIT criteria — docs/roadmap.md Phase 2):

* GATED run: triage sets ``mode=gated`` + a ``risk_tier``; a Design sign-off AND
  a Release sign-off record are BOTH made (the always-recorded invariant); the
  assurance phase runs; advancement to emit requires ``release_allowed``;
* a KNOWN-VULNERABLE change is caught in the assurance phase → the run HALTS →
  NO Release sign-off path to emit;
* ASSISTANT mode (default) still produces the Phase-1 flow (back-compat — no
  ``mode``, no ``risk_tier``, no sign-offs);
* the full Gated phase order: triage → planner → [design
  sign-off] → implementation → quality → [build+test] → assurance →
  [release sign-off] → END.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.governance.sign_off import release_allowed
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_GATED,
    build_supervisor_graph,
)
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
from tests.planner_payload import PLANNER_SOP_KEY, plan_payload

KEY = b"dev-zone-b-supervisor-p2-key-01234"
KEY_ID = "dev-key-supervisor-p2"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"

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
# Layer-5 assurance agents (advisory triage authoring; AS1). Schema-constrained
# IDEATION rides the free-text completions path (AC3/DT1), keyed by SOP marker.
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
    PLANNER_SOP_KEY: json.dumps(
        plan_payload(
            intent=INTENT_PAYLOAD,
            prd=PRD_PAYLOAD,
            critique=CRITIQUE_PAYLOAD,
            sprint=SPRINT_PAYLOAD,
            design=DESIGN_PAYLOAD,
            decomposition=DECOMPOSITION_PAYLOAD,
        )
    ),
    "QA Engineer": json.dumps(QA_PAYLOAD),
    "SDET": json.dumps(SDET_PAYLOAD),
    "You are the Security / AppSec Engineer": json.dumps(APPSEC_PAYLOAD),
    "You are the Audit / Compliance agent": json.dumps(AUDIT_PAYLOAD),
    "You are the Supply-Chain Reviewer": json.dumps(SUPPLY_CHAIN_PAYLOAD),
}


# ---- in-memory materialization doubles --------------------------------------

# A unified-diff ADDITION blob carrying app.py + a passing test + a clean lockfile
# (the assurance Audit gate reads the lockfile inventory; AW2). Build+Test PASS;
# Security/Audit/Supply-chain PASS -> the assurance phase advances.
_CLEAN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)
# A KNOWN-VULNERABLE change: a hardcoded secret in app.py trips the SecurityGate
# dev rule (hardcoded-password). Build+Test still PASS (it compiles + the test
# passes); the assurance Security gate catches it -> attested FAIL -> latch -> halt.
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


def _build_gated(
    records: GlobalRecords, source_dir: str, *, diff_body: str, max_fix_attempts: int = 3
) -> Any:
    return build_supervisor_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        max_fix_attempts=max_fix_attempts,
        mode=MODE_GATED,
    ).compile(checkpointer=MemorySaver())


def _build_assistant(records: GlobalRecords, source_dir: str, *, diff_body: str) -> Any:
    return build_supervisor_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
    ).compile(checkpointer=MemorySaver())


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        if g.name not in out:
            out[g.name] = g.status
    return out


def _sign_offs_by_role(state: FactoryState) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in state.get("sign_offs") or ():
        out.setdefault(s.role, s)
    return out


def _src(tmp_path: Any) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    return str(src)


# ---- GATED end-to-end: tiering + both sign-offs + assurance + release --------


def test_gated_run_tiers_records_both_signoffs_runs_assurance_and_releases(tmp_path: Any) -> None:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(records, _src(tmp_path), diff_body=_CLEAN_DIFF)
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-gated-pass")
        )
    finally:
        reset_gate_write_verifier(token)

    # Triage stamped Gated mode + a risk_tier (TierDecision; gov §1e). The tier
    # never relaxes integrity.
    assert final["mode"] == MODE_GATED
    tier = final.get("risk_tier")
    assert tier is not None
    assert tier.relaxes_integrity() is False

    # The full phase order ran: every layer's artifacts landed.
    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert {"prd", "acceptance_criterion", "system_design", "feature_diff"} <= kinds

    # The M1 integrity boundaries still gate on attested PASS.
    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS
    # The assurance phase ran: its attested gate verdicts are present + PASS.
    for name in ("security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name

    # BOTH sign-off records were made (the always-recorded invariant; gov §1/§6),
    # each carrying the self-judge stamp (provenance=SELF).
    by_role = _sign_offs_by_role(final)
    design = by_role.get("design-approver")
    release = by_role.get("release-approver")
    assert design is not None and release is not None
    assert design.judgment_provenance is JudgmentProvenance.SELF
    assert release.judgment_provenance is JudgmentProvenance.SELF
    assert release.approved is True

    # Advancement to emit required release_allowed: the recorded Release sign-off
    # binds the exact decision point (subject / state_sha / evidence / nonce), so
    # it admits the release. (A missing/!approved record would have blocked it —
    # see the fail-closed test below.)
    assert release_allowed(
        release,
        subject_artifact_uri=release.subject_artifact_uri,
        state_sha=release.state_sha,
        presented_evidence_hash=release.presented_evidence_hash,
        server_nonce=release.server_nonce,
    )


# ---- release_allowed FAIL-CLOSES emit (no/!approved record => no release) -----


def test_release_allowed_fail_closes_emit() -> None:
    """A missing record, a non-approved record, or a record bound to a DIFFERENT
    decision point can never advance THIS release (governance.md §1/§5)."""
    from foundry.schemas.governance import SignOffRecord
    from foundry.state.seq import Seq

    anchors = {
        "subject_artifact_uri": "cas://design/x",
        "state_sha": "integrated-sha",
        "presented_evidence_hash": "evid-1",
        "server_nonce": "nonce-1",
    }
    # No record -> not released.
    assert release_allowed(None, **anchors) is False

    seq = Seq(run_id="r", emission_epoch=1, superstep_index=0, node_id="release_sign_off", local=0)
    approved = SignOffRecord(
        approver="self-judge",
        role="release-approver",
        approved=True,
        judgment_provenance=JudgmentProvenance.SELF,
        seq=seq,
        **anchors,
    )
    assert release_allowed(approved, **anchors) is True

    # A NON-approved record -> blocked.
    rejected = approved.model_copy(update={"approved": False})
    assert release_allowed(rejected, **anchors) is False

    # A record bound to a DIFFERENT nonce (replay) -> blocked.
    assert release_allowed(approved, **{**anchors, "server_nonce": "nonce-other"}) is False


# ---- a KNOWN-VULNERABLE change is caught in the assurance phase -> halt -------


def test_known_vulnerable_change_caught_in_assurance_no_release(tmp_path: Any) -> None:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(
            records, _src(tmp_path), diff_body=_VULN_DIFF, max_fix_attempts=2
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-gated-vuln")
        )
    finally:
        reset_gate_write_verifier(token)

    # The M1 boundaries still PASS (the module compiles + the test passes); the
    # vulnerability is a SECURITY concern caught only in the assurance phase.
    statuses = _gates_by_name(final)
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS

    # The assurance Security gate caught the hardcoded secret -> attested FAIL.
    security = next(g for g in final["gates"] if g.name == "security")
    assert security.status is GateStatus.FAIL
    assert any("hardcoded-password" in f for f in security.findings)
    # Runner-attested (no agent could forge it); a FAIL on the integrated digest
    # latches stable-FAIL (terminal) -> the run HALTED in the assurance phase.
    assert security.judgment_binding is None and security.runner_attestation is not None
    assert records.stable_fail_latched(security.run_against, "security")

    # NO Release sign-off path to emit: the run halted at the assurance boundary,
    # so the release_sign_off node never ran (no release-approver record).
    by_role = _sign_offs_by_role(final)
    assert "release-approver" not in by_role


# ---- ASSISTANT mode (default) still produces the Phase-1 flow (back-compat) ---


def test_assistant_mode_default_is_phase1_flow(tmp_path: Any) -> None:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_assistant(records, _src(tmp_path), diff_body=_CLEAN_DIFF)
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-assistant")
        )
    finally:
        reset_gate_write_verifier(token)

    # Phase-1 flow: the four layers ran + both M1 boundaries advanced on PASS...
    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS

    # ...and NONE of the Phase-2 boundaries fired: no risk_tier, no sign-offs,
    # no assurance-only gate (security/audit/supply_chain) verdicts.
    assert final.get("risk_tier") is None
    assert not (final.get("sign_offs") or [])
    assert "security" not in statuses
    assert "audit" not in statuses
    assert "supply_chain" not in statuses
    # mode defaults to assistant (explicitly stamped by triage).
    assert final.get("mode") in (None, MODE_ASSISTANT)


# ---- the full Gated phase order ---------------------------------------------


def test_gated_phase_order(tmp_path: Any) -> None:
    """triage -> product -> architecture -> [design sign-off] -> implementation
    -> quality -> [build+test] -> assurance -> [release sign-off] -> END.

    Verified by the observable side-effects of each phase landing in the final
    state (artifacts per layer, both M1 gate verdicts, the assurance verdicts,
    and BOTH sign-off records in the right order by Seq)."""
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(records, _src(tmp_path), diff_body=_CLEAN_DIFF)
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-gated-order")
        )
    finally:
        reset_gate_write_verifier(token)

    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert "prd" in kinds  # product
    assert "system_design" in kinds  # architecture
    assert "feature_diff" in kinds  # implementation
    assert {"test_plan", "e2e_report"} <= kinds  # quality

    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS  # spec boundary (after design)
    assert {"build", "test"} <= statuses.keys()  # build+test boundary
    assert {"security", "audit"} <= statuses.keys()  # assurance phase

    by_role = _sign_offs_by_role(final)
    design = by_role["design-approver"]
    release = by_role["release-approver"]
    # The Design sign-off precedes the Release sign-off (Seq monotonicity proves
    # the boundary order: design BEFORE the assurance/release stages).
    assert design.seq < release.seq
