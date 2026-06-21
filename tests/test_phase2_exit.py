"""Phase-2 (Gated mode) EXIT integration test — the milestone audit witness (T-M2-11).

This is the testable witness for the docs/roadmap.md **Phase 2** exit criteria:

    (a) a known-vulnerable change is caught;
    (b) Design/Release require recorded sign-off;
    (c) re-verification is change-proportional;
    (d) rework loops terminate within caps;
    (e) HITL volume is measured.

It drives the WHOLE Gated pipeline END-TO-END over the SHIPPED dev wiring the
Phase-2 supervisor / factory_graph tests use (LEARNINGS D1/D3/D4/SUP7/SP2-5/FG4-8):
a REAL :class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API
key), a dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner` whose verdicts
are admitted ONLY by the REAL ``merge_gates`` path (a :class:`DevGateWriteVerifier`
on the reducer contextvar), a :class:`StubAdapter`, in-memory ``resolve`` /
``resolve_blob`` materialization seams, an :class:`ArtifactStore`, and a durable
:class:`FoundryCheckpointer`. The harness deliberately MIRRORS
``tests/test_factory_graph_gated.py`` so the witness exercises the same assembly a
real Gated run uses, not a parallel copy.

--------------------------------------------------------------------------------
HONESTY MAP — what is GENUINELY end-to-end vs a dev-mode stand-in (milestone audit)
--------------------------------------------------------------------------------
GENUINELY end-to-end (no stand-in beyond the mock-model + dev-HMAC trust roots):
  * The GATE machinery for ALL of criterion (a): the assurance Security /
    Supply-chain gates are run by the REAL ``TwoZoneGateRunner``, signed in zone B,
    admitted ONLY by the REAL ``merge_gates``; a hardcoded secret produces a
    runner-ATTESTED Security FAIL (``runner_attestation is not None``,
    ``judgment_binding is None``) that latches a stable-FAIL on the integrated
    digest and HALTS the run — fully end-to-end through the compiled graph.
  * The fail-closed RELEASE admission for criterion (b): the clean run records a
    real Design AND Release ``SignOffRecord`` on the ``sign_offs`` channel and the
    Gated emit's ``gated_release_admitted`` (= terminal+assurance PASS AND
    ``release_allowed``) genuinely admits / refuses the open_pr — proven against a
    live run AND against the pure ``release_allowed`` predicate.
  * The change-proportional re-verification for criterion (c): the
    ``reverification_plan`` is fed REAL runner-attested priors from the live runner
    over a materialized tree (a cosmetic change re-runs a STRICT subset; a lockfile
    change re-runs the bound gates) — a genuine producer→planner contract.
  * The bounded-rework loop for criterion (d): a real Gated run on a diff that makes
    the assurance Audit gate ERROR routes to the bounded fix loop and TERMINATES
    within ``max_fix_attempts`` (reaches a terminal phase, no ``GraphRecursionError``,
    no PR) — the bound is the SHIPPED supervisor/assurance mechanism, run live.
  * The HITL-volume measure for criterion (e): the count of ``SignOffRecord``s on
    the live ``sign_offs`` channel is the observable, measurable HITL volume.
  * The verifiable evidence pack for criterion 6: the emitted open_pr carries an
    ``EvidencePack`` that the REAL ``verify_evidence_pack`` validates, and tampering
    a member / dropping the sign-off / mutating the chain breaks it.

DEV-MODE STAND-INS (declared, NOT overclaimed — plan §0.3):
  * StubAdapter CODEGEN: the StubAdapter authors a deterministic no-op blob, so the
    integrated tree the gates run over is INJECTED through ``resolve`` /
    ``resolve_blob`` (the SUP7 synthetic KIND-dispatching doubles), not authored by
    the (scripted) producing agents. So criterion (a) proves the gate MACHINERY
    catches an injected vulnerable diff — NOT that the stub wrote vulnerable code.
    The CLEAN-vs-VULN control (test 1) proves the catch is LOAD-BEARING (same
    pipeline, same wiring, only the diff body differs → release vs halt).
  * MockTransport / dev-HMAC: model outputs are scripted (no live Anthropic call)
    and the trust roots are dev HMAC keys (``ZoneBSigner`` / ``DevGateWriteVerifier``)
    — production hardening (real KMS, SLSA-L3) is the Phase-4 trust root, an ops
    deployment task. The signing/attestation DISCIPLINE is real; only the key
    custody is dev-grade.
  * SELF-JUDGE HITL: the chosen profile (plan §0.2) is single-lineage self-judge,
    zero-HITL — the sign-off RECORD is always made but the human path is a no-op
    (provenance=SELF, recorded never hidden). So criterion (e)'s "HITL volume" is
    the count of RECORDED-but-no-op self-judge sign-offs, which is the honest
    measurable in this profile (an operator flipping to a real-HITL profile would
    measure the same channel with blocking delivery).
  * SIMULATED single-agent A/B: N/A here — that is the Phase-1.5 decision gate, not
    the Phase-2 assurance pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.evidence_pack import build_evidence_pack, verify_evidence_pack
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.gates.audit import AuditGate
from foundry.gates.build import BuildGate
from foundry.gates.security import SecurityGate
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.governance.sign_off import release_allowed
from foundry.graph.factory_graph import (
    _attested_terminal_results,
    _release_sign_offs,
    _released_artifacts,
    build_factory_graph,
    gated_release_admitted,
    terminal_gates_pass,
)
from foundry.graph.reverification import changed_scope, reverification_plan
from foundry.graph.supervisor import MODE_GATED
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.schemas.governance import EvidencePack, SignOffRecord
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha

KEY = b"dev-zone-b-phase2-exit-key-0123456789abcd"
KEY_ID = "dev-key-phase2-exit"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
TOOLCHAIN = "foundry-dev-toolchain-v1"

_DIFF_BODY_URI = "cas://test/diff-body"


# ---- scripted per-node payloads (free-text completions, SOP-keyed; SUP7/SP2-5) ----

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


# ---- in-memory materialization doubles (SUP7 KIND-dispatching synthetic) -----

# A clean unified-diff ADDITION: app.py + a passing test + a lockfile (the Audit
# gate reads the lockfile inventory; AW2). Build+Test PASS; Security/Audit PASS.
_CLEAN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)
# A KNOWN-VULNERABLE change: a hardcoded secret trips the SecurityGate dev rule
# (hardcoded-password). Build+Test still PASS (it compiles + the test passes); the
# assurance Security gate catches it -> attested FAIL -> stable-FAIL latch -> halt.
_VULN_DIFF = (
    '--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+API_KEY = "supersecrethardcodedtoken123"\n'
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)
# A diff with NO lockfile: the assurance Audit gate cannot rederive its inventory
# -> attested ERROR (infra, NOT a verdict; never latches) -> the assurance boundary
# routes to a BOUNDED fix loop, then HALTs once max_fix_attempts is spent (AW4/SUP5).
_NO_LOCKFILE_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
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
    return AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=ArtifactStore())


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


def _open_pr_actions(state: FactoryState) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "open_pr"]


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


def _sign_offs_by_role(state: FactoryState) -> dict[str, SignOffRecord]:
    out: dict[str, SignOffRecord] = {}
    for s in state.get("sign_offs") or ():
        out.setdefault(s.role, s)
    return out


def _src(tmp_path: Any) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    return str(src)


def _run_gated(
    tmp_path: Any, run_id: str, *, diff_body: str, max_fix_attempts: int = 3
) -> tuple[FactoryState, GlobalRecords]:
    """Drive a full Gated run end-to-end; return the final state + the records.

    Mirrors the shipped Gated wiring exactly (test_factory_graph_gated); the
    DevGateWriteVerifier contextvar token is always reset + the checkpointer
    closed in ``finally``.
    """
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_gated(
            records,
            _src(tmp_path),
            diff_body=diff_body,
            checkpointer=ckpt.saver,
            max_fix_attempts=max_fix_attempts,
        )
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config(run_id))
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()
    return final, records


# =============================================================================
# Criterion (a) — a KNOWN-VULNERABLE change is CAUGHT (+ a clean CONTROL).
# =============================================================================


def test_exit_a_known_vulnerable_change_is_caught_and_halts(tmp_path: Any) -> None:
    """A hardcoded secret -> attested Security/Supply-chain FAIL -> the run HALTS
    -> NO open_pr, NO evidence-pack release (fail-closed).

    GENUINELY end-to-end: the FAIL is runner-attested (no agent could forge it),
    latches a stable-FAIL on the integrated digest, and the Gated emit fail-closes.
    The vulnerable DIFF is injected via ``resolve_blob`` (StubAdapter is a no-op
    codegen stand-in) — so this proves the gate MACHINERY catches a vulnerable
    change, not that the stub authored one. The clean control below proves the
    catch is load-bearing.
    """
    final, records = _run_gated(tmp_path, "run-vuln", diff_body=_VULN_DIFF, max_fix_attempts=2)

    # The M1 boundaries still PASS (it compiles + the test passes); the vuln is a
    # SECURITY concern caught ONLY in the assurance phase.
    statuses = _gates_by_name(final)
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS

    # The assurance Security gate caught the hardcoded secret -> attested FAIL.
    security = next(g for g in final["gates"] if g.name == "security")
    assert security.status is GateStatus.FAIL
    assert any("hardcoded-password" in f for f in security.findings)
    # Runner-attested (zone B), NOT an agent judgment — no agent could forge it.
    assert security.judgment_binding is None and security.runner_attestation is not None
    # A FAIL on the integrated digest latched a stable-FAIL (terminal) -> the run HALTED.
    assert records.stable_fail_latched(security.run_against, "security")

    # Fail-closed: the release was NOT admitted -> NO open_pr, NO evidence pack,
    # NO release sign-off path reached.
    assert gated_release_admitted(final, _config("run-vuln")) is False
    assert _open_pr_actions(final) == []
    assert "release-approver" not in _sign_offs_by_role(final)


def test_exit_a_clean_control_releases_proving_the_catch_is_load_bearing(tmp_path: Any) -> None:
    """CONTROL: the SAME pipeline + wiring on a CLEAN diff RELEASES (one open_pr
    carrying a valid evidence pack).

    Only the diff body differs from the vuln run above — so the halt in that test
    is caused by the vulnerability, NOT a hard-wired fail. The catch is load-bearing.
    """
    final, _ = _run_gated(tmp_path, "run-clean", diff_body=_CLEAN_DIFF)

    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    assert terminal_gates_pass(final) is True
    assert gated_release_admitted(final, _config("run-clean")) is True

    prs = _open_pr_actions(final)
    assert len(prs) == 1
    assert prs[0].gate_bearing is True
    assert prs[0].payload_uri.startswith("cas://sha256/")  # the evidence-pack handle


# =============================================================================
# Criterion (b) — Design AND Release require recorded sign-off.
# =============================================================================


def test_exit_b_clean_release_records_both_design_and_release_sign_offs(tmp_path: Any) -> None:
    """A clean Gated release records BOTH a Design and a Release SignOffRecord (the
    always-recorded invariant), each carrying the self-judge stamp (provenance=SELF).

    The records are ALWAYS made; in the self-judge profile the human path is a no-op
    (recorded never hidden, plan §0.2/gov §1e)."""
    final, _ = _run_gated(tmp_path, "run-signoffs", diff_body=_CLEAN_DIFF)

    by_role = _sign_offs_by_role(final)
    design = by_role.get("design-approver")
    release = by_role.get("release-approver")
    assert design is not None and release is not None
    assert design.judgment_provenance is JudgmentProvenance.SELF
    assert release.judgment_provenance is JudgmentProvenance.SELF
    assert release.approved is True
    # Seq monotonicity proves the boundary ORDER: Design BEFORE Release.
    assert design.seq < release.seq

    # The recorded Release sign-off binds the EXACT decision point, so it admits.
    assert release_allowed(
        release,
        subject_artifact_uri=release.subject_artifact_uri,
        state_sha=release.state_sha,
        presented_evidence_hash=release.presented_evidence_hash,
        server_nonce=release.server_nonce,
    )


def test_exit_b_release_allowed_fail_closes_when_sign_off_absent_or_mismatched() -> None:
    """``release_allowed`` fail-closes: a missing / non-approved / anchor-mismatched
    release sign-off can never advance THIS release (governance.md §1/§5).

    This is the pure predicate the Gated emit's ``gated_release_admitted`` re-derives
    — a sign-off for a different artifact/state/evidence/nonce is rejected, so no PR."""
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
    # NOT approved -> blocked.
    assert release_allowed(approved.model_copy(update={"approved": False}), **anchors) is False
    # Anchor (nonce) mismatch -> blocked (replay/forge can't advance THIS release).
    assert release_allowed(approved, **{**anchors, "server_nonce": "nonce-other"}) is False
    # Evidence-hash mismatch -> blocked (what-you-see-is-what-you-sign).
    assert release_allowed(approved, **{**anchors, "presented_evidence_hash": "evid-2"}) is False


# =============================================================================
# Criterion (c) — re-verification is change-proportional.
# =============================================================================

_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'
_CRITERIA = [
    {"id": "c1", "text": "do a thing", "oracle": "test_c1", "security_abuse": False},
    {"id": "c2", "text": "no abuse", "oracle": "test_c2", "security_abuse": True},
]
_REV_GATES = (
    SpecGate(),
    BuildGate(),
    TestGate(min_passed=1),
    AuditGate(),
    SecurityGate(use_semgrep=False),
)
_REV_GATE_NAMES = {g.name for g in _REV_GATES}


def _materialize_tree(dest: Path, files: dict[str, str]) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "criteria.json").write_text(json.dumps(_CRITERIA), encoding="utf-8")
    for rel, body in files.items():
        (dest / rel).write_text(body, encoding="utf-8")
    return tree_sha(dest, toolchain_digest=TOOLCHAIN, prefer_git=False)


def _run_all_gates(runner: TwoZoneGateRunner, workdir: Path, sha: str) -> list[GateResult]:
    out: list[GateResult] = []
    for gate in _REV_GATES:
        seq = Seq(
            run_id="rev", emission_epoch=1, superstep_index=0,
            node_id=f"{gate.name}_gate", local=0,
        )
        out.append(runner.run(gate, sha, seq, workdir))
    return out


def _clean_files() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


def test_exit_c_cosmetic_change_reverifies_a_strict_subset(tmp_path: Any) -> None:
    """A cosmetic 1-file change re-runs only a STRICT subset of gates (re-run ⊂
    full); the untouched-scope gates REUSE their REAL runner-attested prior verdict.

    GENUINELY end-to-end: the priors are produced by the LIVE ``TwoZoneGateRunner``
    over a materialized tree (a genuine producer->planner contract, RV5)."""
    runner = _runner(GlobalRecords())
    old_files = _clean_files()
    old_sha = _materialize_tree(tmp_path / "old", old_files)
    prior = _run_all_gates(runner, tmp_path / "old", old_sha)

    new_files = {**old_files, "app.py": "VALUE = 43\n"}  # cosmetic value change
    new_sha = _materialize_tree(tmp_path / "new", new_files)

    scope = changed_scope(
        old_files | {"criteria.json": json.dumps(_CRITERIA)},
        new_files | {"criteria.json": json.dumps(_CRITERIA)},
        old_toolchain_digest=TOOLCHAIN,
        new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV,
        new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.changed_files == {"app.py"}

    plan = reverification_plan(
        prior, scope, gates=_REV_GATES, runner=runner,
        new_state_sha=new_sha, scanner_config_rev=SCANNER_REV,
    )
    rerun = set(plan.rerun_gates)
    # CHANGE-PROPORTIONAL: the re-run set is a STRICT subset of the full set.
    assert rerun < _REV_GATE_NAMES
    assert rerun  # something re-runs (app.py changed)
    # Criteria-only Spec + lockfile-only Audit are unaffected -> REUSE (their REAL
    # attested prior is carried forward verbatim).
    assert {"spec", "audit"} <= set(plan.reuse_gates)
    spec = plan.decision_for("spec")
    assert spec is not None and spec.reused_verdict is not None
    assert spec.reused_verdict.runner_attestation is not None


def test_exit_c_lockfile_change_invalidates_the_bound_gates(tmp_path: Any) -> None:
    """A lockfile (dependency) change re-keys + RE-RUNs the lockfile-bound gates
    (Audit + Security), while the pure code gates REUSE — proportional invalidation."""
    runner = _runner(GlobalRecords())
    old_files = _clean_files()
    old_sha = _materialize_tree(tmp_path / "old", old_files)
    prior = _run_all_gates(runner, tmp_path / "old", old_sha)

    new_files = {**old_files, "uv.lock": 'name = "pkg"\nlicense = "MIT"\nx = 1\n'}
    new_sha = _materialize_tree(tmp_path / "new", new_files)

    scope = changed_scope(
        old_files, new_files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.lockfile_changed and scope.changed_files == {"uv.lock"}

    plan = reverification_plan(
        prior, scope, gates=_REV_GATES, runner=runner,
        new_state_sha=new_sha, scanner_config_rev=SCANNER_REV,
    )
    rerun = set(plan.rerun_gates)
    # The lockfile-bound gates re-run; the pure code/criteria gates reuse.
    assert {"audit", "security"} <= rerun
    assert {"spec", "build", "test"} <= set(plan.reuse_gates)
    assert rerun < _REV_GATE_NAMES  # still proportional, not a full re-run


# =============================================================================
# Criterion (d) — rework loops terminate within caps.
# =============================================================================


def test_exit_d_persistently_failing_run_halts_within_caps_no_pr(tmp_path: Any) -> None:
    """A persistently-ERRORing assurance run HALTS within ``max_fix_attempts`` — it
    TERMINATES (reaches a terminal state, no GraphRecursionError / hang) and emits
    no PR.

    A diff with NO lockfile makes the assurance Audit gate ERROR (inventory not
    rederivable). An ERROR never latches (infra, DS7) so it routes to the BOUNDED
    fix loop; the bound (max_fix_attempts=1, raised internally by the supervisor)
    is exhausted -> the boundary HALTs. The proof it terminates is that ``invoke``
    RETURNS a terminal state rather than raising ``GraphRecursionError`` on an
    infinite loop. GENUINELY end-to-end (the SHIPPED bounded mechanism, run live)."""
    final, records = _run_gated(
        tmp_path, "run-rework", diff_body=_NO_LOCKFILE_DIFF, max_fix_attempts=1
    )

    # The assurance Audit gate ERRORed (no lockfile inventory) — runner-attested.
    audit = next((g for g in final["gates"] if g.name == "audit"), None)
    assert audit is not None
    assert audit.status is GateStatus.ERROR
    assert audit.runner_attestation is not None
    # ERROR never latches (infra, not a verdict) — so this halted on the BOUND, not
    # a stable-FAIL latch. The bounded loop terminated rather than looping forever.
    assert not records.stable_fail_latched(audit.run_against, "audit")

    # Terminated (no PR; the release was never admitted).
    assert gated_release_admitted(final, _config("run-rework")) is False
    assert _open_pr_actions(final) == []
    assert "release-approver" not in _sign_offs_by_role(final)


# =============================================================================
# Criterion (e) — HITL volume is measured.
# =============================================================================


def test_exit_e_hitl_volume_is_observable_and_countable(tmp_path: Any) -> None:
    """The run records a countable HITL / sign-off VOLUME on the ``sign_offs``
    channel — an observable, measurable quantity.

    In the self-judge profile the sign-offs are RECORDED-but-no-op (provenance=SELF;
    the human path is a no-op, recorded never hidden — plan §0.2). So the HITL volume
    measure is the COUNT of recorded sign-offs. A clean Gated release records exactly
    TWO (Design + Release); a halted run records FEWER (it never reaches Release) —
    the measure varies with the run, so it is genuinely observed, not a constant."""
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    vuln_dir = tmp_path / "vuln"
    vuln_dir.mkdir()
    clean, _ = _run_gated(clean_dir, "run-hitl-clean", diff_body=_CLEAN_DIFF)
    halted, _ = _run_gated(
        vuln_dir, "run-hitl-vuln", diff_body=_VULN_DIFF, max_fix_attempts=2
    )

    clean_volume = len(clean.get("sign_offs") or ())
    halted_volume = len(halted.get("sign_offs") or ())

    # The clean release recorded a measurable, non-zero HITL volume = Design + Release.
    assert clean_volume == 2
    roles = {s.role for s in clean["sign_offs"]}
    assert roles == {"design-approver", "release-approver"}
    # Every recorded sign-off is auditable as a self-judge (recorded, no-op) decision.
    assert all(s.judgment_provenance is JudgmentProvenance.SELF for s in clean["sign_offs"])

    # The halted run recorded a SMALLER volume (it never reached the Release seam),
    # so the measure is genuinely run-dependent / observable, not hard-wired.
    assert halted_volume < clean_volume
    assert "release-approver" not in {s.role for s in (halted.get("sign_offs") or ())}


# =============================================================================
# Criterion 6 — Gated release carries a verifiable evidence pack (tamper-evident).
# =============================================================================


def _carried_pack(final: FactoryState, pr_payload_uri: str) -> EvidencePack:
    """Rebuild the EvidencePack the gated emit carried, from the final state, and
    assert its handle == the PR payload (the release travels WITH its evidence; FG8)."""
    sentinel = Seq(run_id="x", emission_epoch=0, superstep_index=0, node_id="emit", local=0)
    pack = build_evidence_pack(
        run_against="integrated-sha",
        gate_results=_attested_terminal_results(final),
        artifacts=_released_artifacts(final),
        sign_offs=_release_sign_offs(final),
        scanner_config_rev=SCANNER_REV,
        seq=sentinel,  # seq is excluded from the pack hash (it's the handle)
    )
    assert pack.pack_uri == pr_payload_uri
    return pack


def test_exit_6_release_pack_verifies_and_tampering_breaks_it(tmp_path: Any) -> None:
    """The emitted open_pr references an EvidencePack that ``verify_evidence_pack``
    validates, and tampering (swapped gate result / dropped sign-off / mutated chain)
    breaks it. GENUINELY end-to-end: the pack chains over the REAL attested terminal
    + assurance verdicts, the released FeatureDiff, and the recorded Release sign-off."""
    final, _ = _run_gated(tmp_path, "run-pack", diff_body=_CLEAN_DIFF)

    pr = _open_pr_actions(final)[0]
    pack = _carried_pack(final, pr.payload_uri)
    gate_results = _attested_terminal_results(final)
    artifacts = _released_artifacts(final)
    sign_offs = _release_sign_offs(final)

    # The genuine members verify; the pack carries every required gate + the sign-off.
    assert verify_evidence_pack(
        pack, gate_results=gate_results, artifacts=artifacts, sign_offs=sign_offs
    )
    assert len(pack.gate_result_uris) == len(gate_results) >= 5
    assert len(pack.sign_off_uris) == 1

    # TAMPER 1 — swap a gate result's status: its content-identity re-keys -> verify False.
    tampered: tuple[GateResult, ...] = (
        gate_results[0].model_copy(update={"status": GateStatus.FAIL}),
        *gate_results[1:],
    )
    assert not verify_evidence_pack(
        pack, gate_results=tampered, artifacts=artifacts, sign_offs=sign_offs
    )
    # TAMPER 2 — drop the Release sign-off -> verify False.
    assert not verify_evidence_pack(
        pack, gate_results=gate_results, artifacts=artifacts, sign_offs=()
    )
    # TAMPER 3 — mutate the chain head out-of-band -> content-addressing fails.
    forged = pack.model_copy(update={"chain_head": "deadbeef"})
    assert not verify_evidence_pack(
        forged, gate_results=gate_results, artifacts=artifacts, sign_offs=sign_offs
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
