"""Factory graph — Gated-mode emit (the WORM-evidence-carrying release; T-M2-10).

Drives :func:`build_factory_graph` in ``mode="gated"`` over the SAME end-to-end
doubles the Phase-1/Phase-2 supervisor tests use (LEARNINGS D1/D3/D4/SUP7): a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API key) + a
dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner` admitted through the
REAL ``merge_gates`` path (a :class:`DevGateWriteVerifier` on the reducer
contextvar), a :class:`StubAdapter`, in-memory ``resolve`` / ``resolve_blob``
materialization seams, and a durable :class:`FoundryCheckpointer`.

Asserts (the T-M2-10 exit criteria):

* a CLEAN Gated run completes — terminal (Spec+Build+Test) + assurance attested
  PASS, a Release sign-off recorded + ``release_allowed`` True — and emits EXACTLY
  ONE ``open_pr`` action carrying a valid :class:`EvidencePack`; ``verify_evidence_pack``
  passes on the carried pack;
* a KNOWN-VULNERABLE / halted run emits NO ``open_pr`` (fail-closed) and releases
  NO evidence pack;
* kill-then-resume: re-invoking the SAME ``thread_id`` does NOT duplicate the PR
  (same deterministic idempotency key);
* the carried evidence pack is TAMPER-EVIDENT — swapping a gate result / dropping
  a sign-off breaks ``verify_evidence_pack``;
* ASSISTANT mode (default) still emits the Phase-1 bare ``open_pr`` (back-compat).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.evidence_pack import verify_evidence_pack
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import (
    _attested_terminal_results,
    _release_sign_offs,
    _released_artifacts,
    build_factory_graph,
    gated_release_admitted,
    terminal_gates_pass,
)
from foundry.graph.supervisor import MODE_GATED
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.schemas.governance import EvidencePack
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier

KEY = b"dev-zone-b-factory-graph-gated-key-0123456789"
KEY_ID = "dev-key-factory-graph-gated"
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

# A clean unified-diff ADDITION: app.py + a passing test + a lockfile (the Audit
# gate reads the lockfile inventory; AW2). Build+Test PASS; Security/Audit PASS.
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


def _build_gated(
    records: GlobalRecords,
    source_dir: str,
    *,
    diff_body: str,
    checkpointer: Any,
    max_fix_attempts: int = 3,
) -> Any:
    return build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        checkpointer=checkpointer,
        max_fix_attempts=max_fix_attempts,
        mode=MODE_GATED,
    )


def _build_assistant(
    records: GlobalRecords, source_dir: str, *, diff_body: str, checkpointer: Any
) -> Any:
    return build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        checkpointer=checkpointer,
    )


def _open_pr_actions(state: FactoryState) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "open_pr"]


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


def _carried_pack(state: FactoryState, pr_payload_uri: str) -> EvidencePack:
    """Rebuild the EvidencePack the gated emit carried, from the final state, and
    assert its handle == the PR payload (the release travels WITH its evidence).

    The pack is a pure function of the attested members + the integrated
    tree_sha, so reconstructing it here yields the SAME ``pack_uri`` the emit
    embedded as the PR payload. ``run_against`` is read off an attested verdict
    (the ACTUAL materialized tree the gates ran over; M2-R1-02), mirroring the
    gated emit — NOT a config state_sha label."""
    from foundry.durability.evidence_pack import build_evidence_pack
    from foundry.state.seq import Seq

    gate_results = _attested_terminal_results(state)
    run_against = next((g.run_against for g in gate_results if g.run_against), "")
    sentinel = Seq(run_id="x", emission_epoch=0, superstep_index=0, node_id="emit", local=0)
    pack = build_evidence_pack(
        run_against=run_against,
        gate_results=gate_results,
        artifacts=_released_artifacts(state),
        sign_offs=_release_sign_offs(state),
        scanner_config_rev=SCANNER_REV,
        seq=sentinel,  # seq is excluded from pack_evidence_hash (it's the handle)
    )
    assert pack.pack_uri == pr_payload_uri
    return pack


def _src(tmp_path: Any) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    return str(src)


# ---- CLEAN gated run -> ONE open_pr carrying a valid EvidencePack ------------


def test_gated_clean_run_emits_one_pr_carrying_a_valid_evidence_pack(tmp_path: Any) -> None:
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver)
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-gated-pass"))
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # Terminal + assurance gates attested PASS; the release was admitted.
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    assert terminal_gates_pass(final) is True
    assert gated_release_admitted(final, _config("run-gated-pass")) is True
    assert final["phase"] == "done"

    # EXACTLY ONE open_pr — gate-bearing, carrying the evidence-pack handle.
    prs = _open_pr_actions(final)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.kind == "open_pr"
    assert pr.gate_bearing is True
    assert pr.payload_uri.startswith("cas://sha256/")  # the pack handle, not the diff

    # The carried evidence pack verifies (hash-chained + content-addressed); it
    # chains over the attested terminal+assurance verdicts, the released
    # FeatureDiff, and the recorded Release sign-off.
    pack = _carried_pack(final, pr.payload_uri)
    assert verify_evidence_pack(
        pack,
        gate_results=_attested_terminal_results(final),
        artifacts=_released_artifacts(final),
        sign_offs=_release_sign_offs(final),
    )
    # The pack actually carries every required gate + the release sign-off.
    assert len(pack.gate_result_uris) == len(_attested_terminal_results(final)) >= 5
    assert len(pack.sign_off_uris) == 1


# ---- a KNOWN-VULNERABLE / halted run -> NO PR, NO evidence-pack release ------


def test_gated_vulnerable_run_emits_no_pr_and_no_pack(tmp_path: Any) -> None:
    """A hardcoded secret is caught in the assurance phase (attested Security
    FAIL -> latch -> halt); the gated emit fail-closes: no open_pr, no release."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(
            records,
            _src(tmp_path),
            diff_body=_VULN_DIFF,
            checkpointer=ckpt.saver,
            max_fix_attempts=2,
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-gated-vuln")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS
    # The assurance Security gate caught the secret -> attested FAIL (runner-signed).
    security = next(g for g in final["gates"] if g.name == "security")
    assert security.status is GateStatus.FAIL
    assert security.runner_attestation is not None
    assert records.stable_fail_latched(security.run_against, "security")

    # Fail-closed: the release was NOT admitted -> NO open_pr, NO sign-off-bound
    # release path -> NO evidence pack released.
    assert gated_release_admitted(final, _config("run-gated-vuln")) is False
    assert _open_pr_actions(final) == []
    by_role = {s.role for s in (final.get("sign_offs") or ())}
    assert "release-approver" not in by_role


# ---- kill-then-resume: no duplicate open_pr (same idempotency key) -----------


def test_gated_kill_then_resume_does_not_duplicate_pr(tmp_path: Any) -> None:
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver)
        cfg = _config("run-gated-resume")
        first: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
        assert len(_open_pr_actions(first)) == 1
        first_key = _open_pr_actions(first)[0].idempotency_key

        # Re-invoke the SAME thread_id: LangGraph resumes + re-folds; the gated
        # emit re-derives the SAME (content_digest) key -> union_actions coalesces.
        second: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    prs = _open_pr_actions(second)
    assert len(prs) == 1  # coalesced — NOT a duplicate
    assert prs[0].idempotency_key == first_key


# ---- the carried evidence pack is TAMPER-EVIDENT ----------------------------


def test_gated_evidence_pack_is_tamper_evident(tmp_path: Any) -> None:
    """Swapping a gate result or dropping a sign-off breaks verify_evidence_pack."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver)
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-gated-tamper")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    pr = _open_pr_actions(final)[0]
    pack = _carried_pack(final, pr.payload_uri)
    gate_results = _attested_terminal_results(final)
    artifacts = _released_artifacts(final)
    sign_offs = _release_sign_offs(final)

    # Baseline: the genuine members verify.
    assert verify_evidence_pack(
        pack, gate_results=gate_results, artifacts=artifacts, sign_offs=sign_offs
    )

    # TAMPER 1 — swap a gate result's status: its content-identity re-keys, so the
    # supplied references no longer reproduce the recorded ones.
    tampered: tuple[GateResult, ...] = (
        gate_results[0].model_copy(update={"status": GateStatus.FAIL}),
        *gate_results[1:],
    )
    assert not verify_evidence_pack(
        pack, gate_results=tampered, artifacts=artifacts, sign_offs=sign_offs
    )

    # TAMPER 2 — drop the Release sign-off: a missing sign-off breaks verify.
    assert not verify_evidence_pack(
        pack, gate_results=gate_results, artifacts=artifacts, sign_offs=()
    )

    # TAMPER 3 — mutate the pack's chain_head out-of-band: content-addressing fails.
    forged = pack.model_copy(update={"chain_head": "deadbeef"})
    assert not verify_evidence_pack(
        forged, gate_results=gate_results, artifacts=artifacts, sign_offs=sign_offs
    )


# ---- ASSISTANT mode (default) still emits the Phase-1 bare PR (back-compat) --


def test_assistant_mode_default_emits_phase1_bare_pr(tmp_path: Any) -> None:
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_assistant(
            records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-assistant")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # Phase-1 emit: exactly one open_pr against the FeatureDiff (NOT a pack), no
    # assurance/sign-off machinery.
    prs = _open_pr_actions(final)
    assert len(prs) == 1
    pr = prs[0]
    feature_diff_uris = {a.uri for a in final["artifacts"] if a.kind == "feature_diff"}
    assert pr.payload_uri in feature_diff_uris
    assert pr.gate_bearing is False
    assert final.get("risk_tier") is None
    assert not (final.get("sign_offs") or [])
    assert final["phase"] == "done"
