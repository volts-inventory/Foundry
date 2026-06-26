"""Generated-code maintenance lifecycle — re-verify + re-gate, no bypass (T-M6-09).

Exercises ``foundry/lifecycle/maintenance.py`` against the REAL trust seams (the
two-zone runner, the change-proportional re-verification engine, the fail-closed
admission controller):

* a maintained change RE-VERIFIES change-proportionally (only the touched scope
  re-runs; the rest REUSE the prior attested verdict) and must RE-PASS every
  required gate + RE-ADMIT to be released;
* FAIL-CLOSED: a maintained change that fails a gate is NOT released
  (maintenance never bypasses the gates) — and ``strict=True`` raises;
* the maintenance chain links the maintained version to the prior;
* determinism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.gates import (
    AuditGate,
    BuildGate,
    DevGateWriteVerifier,
    SecurityGate,
    SpecGate,
    TestGate,
    TwoZoneGateRunner,
    ZoneBSigner,
)
from foundry.graph.reverification import changed_scope
from foundry.lifecycle.maintenance import (
    MaintenanceRefused,
    maintain,
    plan_maintenance,
)
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.security import SBOMComponent
from foundry.schemas.trust_root import (
    AdmissionVerdict,
    BuildRequest,
)
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha
from foundry.trust_root import AdmissionController, TrustedBuilder

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
RUN_ID = "maint-run-1"
TOOLCHAIN = "foundry-dev-toolchain-v1"
BUILDER_ID = "dev-trusted-builder-1"

_SRC = "sha256:" + "c" * 64
_BASE = "sha256:" + "d" * 64
_MAT_A = "sha256:" + "a" * 64

_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'
_SECRET_MODULE = 'API_KEY = "AKIAIOSFODNN7EXAMPLEsecrettoken1234567890"\n'

_CRITERIA = [
    {"id": "c1", "text": "do a thing", "oracle": "test_c1", "security_abuse": False},
    {"id": "c2", "text": "no abuse", "oracle": "test_c2", "security_abuse": True},
]

_GATES = (
    SpecGate(),
    BuildGate(),
    TestGate(min_passed=1),
    AuditGate(),
    SecurityGate(use_semgrep=False),
)
_REQUIRED = tuple(g.name for g in _GATES)
_COMPONENTS = (SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT_A, license="MIT"),)


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


def _seq(node: str = "maintain", epoch: int = 1) -> Seq:
    return Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _materialize(dest: Path, files: dict[str, str]) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "criteria.json").write_text(json.dumps(_CRITERIA), encoding="utf-8")
    for rel, body in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tree_sha(dest, toolchain_digest=TOOLCHAIN, prefer_git=False)


def _run_all(runner: TwoZoneGateRunner, workdir: Path, sha: str) -> list[GateResult]:
    return [runner.run(g, sha, _seq(f"{g.name}_gate"), workdir) for g in _GATES]


def _clean() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


def _with_criteria(files: dict[str, str]) -> dict[str, str]:
    return files | {"criteria.json": json.dumps(_CRITERIA)}


def _build_provenance(signer: ZoneBSigner, *, tree: str):
    """A genuine hermetic build keyed to the maintained tree_sha."""
    builder = TrustedBuilder(signer, builder_id=BUILDER_ID)
    request = BuildRequest(
        build_type="dev-build",
        source_uri="git://repo",
        source_digest=_SRC,
        tree_sha=tree,
        base_image_digest=_BASE,
        build_config_uri="cas://invocation/1",
        material_digests=(_MAT_A,),
        hermetic=True,
        seq=_seq("build"),
    )
    return builder.build(request, components=_COMPONENTS)


# ---------------------------------------------------------------------------
# A maintained change RE-VERIFIES change-proportionally + re-passes -> released
# ---------------------------------------------------------------------------


def test_maintained_change_reverifies_change_proportionally_and_releases(tmp_path):
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)

    # Prior released version.
    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    # A maintained change to ONE source file (a benign value tweak).
    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": "VALUE = 43\n"}
    new_sha = _materialize(new_dir, new_files)

    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.changed_files == {"app.py"}

    plan = plan_maintenance(
        prior, scope,
        prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )
    # CHANGE-PROPORTIONAL: only the touched scope re-runs; Spec/Audit reuse.
    assert set(plan.rerun_gates) < set(_REQUIRED)
    assert {"build", "test", "security"} <= set(plan.rerun_gates)
    assert {"spec", "audit"} <= set(plan.reuse_gates)

    build = _build_provenance(signer, tree=new_sha)

    outcome = maintain(
        plan,
        gates=_GATES, runner=runner, workdir=new_dir,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier,
        seq=_seq(),
    )
    assert outcome.released
    assert outcome.admission_admitted
    assert outcome.admission_reason == "ok"
    assert not outcome.flags
    # The re-verified integrated gate-state covers EVERY required gate, attested.
    by_name = {g.name: g for g in outcome.gate_results}
    assert set(by_name) >= set(_REQUIRED)
    for g in outcome.gate_results:
        assert g.runner_attestation is not None
        assert g.status is GateStatus.PASS
    # Reused gates carry the genuine prior attested verdict forward.
    assert by_name["spec"] in prior


# ---------------------------------------------------------------------------
# FAIL-CLOSED: a maintained change that fails a gate is NOT released
# ---------------------------------------------------------------------------


def test_maintained_change_failing_a_gate_is_not_released(tmp_path):
    """Maintenance never bypasses the gates: a maintained change that introduces
    a hardcoded secret FAILs the Security gate -> NOT released (fail-closed)."""
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)

    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    # The maintained change adds a hardcoded secret -> Security gate FAILs.
    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": _SECRET_MODULE}
    new_sha = _materialize(new_dir, new_files)

    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope,
        prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )
    assert "security" in plan.rerun_gates  # the touched scope re-runs

    build = _build_provenance(signer, tree=new_sha)
    outcome = maintain(
        plan,
        gates=_GATES, runner=runner, workdir=new_dir,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier,
        seq=_seq(),
    )
    # FAIL-CLOSED: the Security gate re-ran and FAILed -> NOT released.
    sec = {g.name: g for g in outcome.gate_results}["security"]
    assert sec.status is GateStatus.FAIL
    assert not outcome.released
    assert any(f.startswith("required_gate_not_pass:security") for f in outcome.flags)
    # Admission also DENYs the maintained subject (stable-FAIL latched on the
    # integrated digest by the runner) — maintenance cannot ship it.
    assert not outcome.admission_admitted
    assert outcome.admission_reason != "ok"


def test_strict_maintain_raises_when_not_released(tmp_path):
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)

    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": _SECRET_MODULE}
    new_sha = _materialize(new_dir, new_files)
    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope, prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=new_sha, scanner_config_rev=SCANNER_REV,
    )
    build = _build_provenance(signer, tree=new_sha)
    with pytest.raises(MaintenanceRefused):
        maintain(
            plan, gates=_GATES, runner=runner, workdir=new_dir,
            new_subject_digest=build.result.subject_digest,
            provenance=build.provenance, build_verdict=build.signed_verdict,
            admission=admission, records=records, verifier=verifier,
            seq=_seq(), strict=True,
        )


def test_forged_reused_verdict_cannot_launder_a_release(tmp_path):
    """A reused 'prior' verdict that is UNSIGNED can never ground a release —
    admission re-verifies every required gate's attestation (no bypass)."""
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)

    work = tmp_path / "wd"
    sha = _materialize(work, _clean())
    prior = _run_all(runner, work, sha)
    # Forge the prior 'spec' verdict: strip its attestation (an agent-forged PASS).
    prior = [
        g.model_copy(update={"runner_attestation": None}) if g.name == "spec" else g
        for g in prior
    ]

    # No change at all -> every gate would REUSE the prior verdict.
    scope = changed_scope(
        _clean(), _clean(),
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope, prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=sha, scanner_config_rev=SCANNER_REV,
    )
    build = _build_provenance(signer, tree=sha)
    outcome = maintain(
        plan, gates=_GATES, runner=runner, workdir=work,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier, seq=_seq(),
    )
    # The forged 'spec' verdict fails admission's signature re-check -> DENY.
    assert not outcome.released
    assert outcome.admission_reason == "required_gate_signature_invalid"


# ---------------------------------------------------------------------------
# The maintenance chain links to the prior version
# ---------------------------------------------------------------------------


def test_maintenance_chain_links_to_prior_version(tmp_path):
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)

    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)
    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": "VALUE = 7\n"}
    new_sha = _materialize(new_dir, new_files)
    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope, prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=new_sha, scanner_config_rev=SCANNER_REV,
    )
    build = _build_provenance(signer, tree=new_sha)
    outcome = maintain(
        plan, gates=_GATES, runner=runner, workdir=new_dir,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier, seq=_seq(),
    )
    assert outcome.released
    # The maintained version's provenance links to the prior released version.
    assert outcome.prior_uri == "cas://artifact/v1"
    assert outcome.prior_subject_uri == "cas://artifact/v1"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_maintain_is_deterministic(tmp_path):
    def run_once() -> tuple[bool, tuple[str, ...]]:
        records = GlobalRecords()
        signer = ZoneBSigner(KEY, KEY_ID)
        runner = _runner(records)
        verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
        admission = AdmissionController(builder_id=BUILDER_ID)
        old_dir = tmp_path / "old"
        old_files = _clean()
        old_sha = _materialize(old_dir, old_files)
        prior = _run_all(runner, old_dir, old_sha)
        new_dir = tmp_path / "new"
        new_files = {**old_files, "app.py": "VALUE = 9\n"}
        new_sha = _materialize(new_dir, new_files)
        scope = changed_scope(
            _with_criteria(old_files), _with_criteria(new_files),
            old_state_sha=old_sha, new_state_sha=new_sha,
            old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
            old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
        )
        plan = plan_maintenance(
            prior, scope, prior_subject_uri="cas://artifact/v1",
            gates=_GATES, runner=runner, new_state_sha=new_sha, scanner_config_rev=SCANNER_REV,
        )
        build = _build_provenance(signer, tree=new_sha)
        outcome = maintain(
            plan, gates=_GATES, runner=runner, workdir=new_dir,
            new_subject_digest=build.result.subject_digest,
            provenance=build.provenance, build_verdict=build.signed_verdict,
            admission=admission, records=records, verifier=verifier, seq=_seq(),
        )
        return outcome.released, tuple(sorted(outcome.rerun_gates))

    assert run_once() == run_once()


def test_outcome_admission_verdict_is_admit_on_release(tmp_path):
    records = GlobalRecords()
    signer = ZoneBSigner(KEY, KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    admission = AdmissionController(builder_id=BUILDER_ID)
    work = tmp_path / "wd"
    sha = _materialize(work, _clean())
    prior = _run_all(runner, work, sha)
    scope = changed_scope(
        _clean(), _clean(),
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope, prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=sha, scanner_config_rev=SCANNER_REV,
    )
    # A no-op change reuses EVERY gate (max reuse) yet still must re-admit.
    assert set(plan.reuse_gates) == set(_REQUIRED)
    build = _build_provenance(signer, tree=sha)
    outcome = maintain(
        plan, gates=_GATES, runner=runner, workdir=work,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier, seq=_seq(),
    )
    assert outcome.released
    assert outcome.admission_admitted is (AdmissionVerdict.ADMIT is AdmissionVerdict.ADMIT)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
