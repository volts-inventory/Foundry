"""Deployment-trust modes + AI-code licensing/liability (T-M6-10).

Exercises ``foundry/lifecycle/modes.py`` and ``foundry/lifecycle/licensing.py``:

* graduation ASSISTANT->GATED->AUTONOMOUS only with justifying evidence
  (autonomous needs the trust root green + a named accountable party);
* fail-closed: no evidence -> no graduation;
* a released artifact WITH complete provenance -> a clean license/liability record;
* no/incomplete provenance -> no clean license (fail-closed);
* determinism.
"""

from __future__ import annotations

import pytest

from foundry.durability.evidence_pack import build_evidence_pack, pack_evidence_hash
from foundry.graph.supervisor import MODE_ASSISTANT, MODE_AUTONOMOUS, MODE_GATED
from foundry.lifecycle.licensing import (
    DEFAULT_LICENSE,
    LicenseRecord,
    UnlicensableArtifactError,
    license_for,
)
from foundry.lifecycle.modes import (
    DeploymentTrustMode,
    GraduationEvidence,
    GraduationRefused,
    can_graduate,
    graduate,
)
from foundry.lifecycle.provenance import ProvenanceRecord
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.governance import SignOffRecord
from foundry.state.seq import Seq

RUN_AGAINST = "d" * 64
RUN_ID = "rel-run-1"
SEQ = Seq(run_id=RUN_ID, emission_epoch=1, superstep_index=5, node_id="release", local=0)


# ===========================================================================
# Deployment-trust modes — earned autonomy
# ===========================================================================


def _full_evidence() -> GraduationEvidence:
    return GraduationEvidence(
        gates_green=True,
        human_approval_recorded=True,
        trust_root_green=True,
        accountable_party="alice@org",
    )


def test_assistant_graduates_to_gated_with_gate_evidence():
    cur = DeploymentTrustMode(mode=MODE_ASSISTANT)
    ev = GraduationEvidence(gates_green=True, human_approval_recorded=True)
    assert can_graduate(cur, ev)
    nxt = graduate(cur, ev)
    assert nxt.mode == MODE_GATED
    assert nxt.prior_mode == MODE_ASSISTANT


def test_gated_graduates_to_autonomous_with_trust_root_green():
    cur = DeploymentTrustMode(mode=MODE_GATED)
    assert can_graduate(cur, _full_evidence())
    nxt = graduate(cur, _full_evidence())
    assert nxt.mode == MODE_AUTONOMOUS
    assert nxt.prior_mode == MODE_GATED


def test_autonomous_requires_trust_root_green():
    """Gated-level evidence alone does NOT justify Autonomous (needs trust root)."""
    cur = DeploymentTrustMode(mode=MODE_GATED)
    ev = GraduationEvidence(gates_green=True, human_approval_recorded=True)  # no trust root
    assert not can_graduate(cur, ev)
    with pytest.raises(GraduationRefused):
        graduate(cur, ev)


def test_autonomous_requires_named_accountable_party():
    cur = DeploymentTrustMode(mode=MODE_GATED)
    ev = GraduationEvidence(
        gates_green=True, human_approval_recorded=True, trust_root_green=True
    )  # no accountable party
    assert not can_graduate(cur, ev)


# ---- fail-closed: no evidence -> no graduation ----------------------------


def test_no_evidence_no_graduation_from_assistant():
    cur = DeploymentTrustMode(mode=MODE_ASSISTANT)
    assert not can_graduate(cur, GraduationEvidence())
    with pytest.raises(GraduationRefused):
        graduate(cur, GraduationEvidence())
    # Non-strict is a no-op (stays put), never an un-earned graduation.
    assert graduate(cur, GraduationEvidence(), strict=False) == cur


def test_no_evidence_no_graduation_from_gated():
    cur = DeploymentTrustMode(mode=MODE_GATED)
    assert not can_graduate(cur, GraduationEvidence())


def test_cannot_skip_a_rung():
    """Graduation moves ONE rung — Assistant cannot jump straight to Autonomous."""
    cur = DeploymentTrustMode(mode=MODE_ASSISTANT)
    # Even with full evidence, the single step is Assistant->Gated, not Autonomous.
    nxt = graduate(cur, _full_evidence())
    assert nxt.mode == MODE_GATED


def test_autonomous_is_the_top_no_further_graduation():
    cur = DeploymentTrustMode(mode=MODE_AUTONOMOUS)
    assert not can_graduate(cur, _full_evidence())
    assert graduate(cur, _full_evidence(), strict=False) == cur


def test_unknown_mode_cannot_graduate():
    cur = DeploymentTrustMode(mode="bogus")
    assert not can_graduate(cur, _full_evidence())


def test_modes_mirror_supervisor_constants():
    assert (MODE_ASSISTANT, MODE_GATED, MODE_AUTONOMOUS) == ("assistant", "gated", "autonomous")


def test_graduation_is_deterministic():
    cur = DeploymentTrustMode(mode=MODE_GATED)
    assert graduate(cur, _full_evidence()) == graduate(cur, _full_evidence())


# ===========================================================================
# AI-code licensing / liability — provenance-backed, fail-closed
# ===========================================================================


def _artifact(name: str = "rc") -> Artifact:
    return Artifact(kind="release", uri=f"cas://sha256/{name}", produced_by="release", seq=SEQ)


def _gate(name: str, *, status: GateStatus = GateStatus.PASS) -> GateResult:
    return GateResult(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        runner_attestation="zoneB-sig",
        run_against=RUN_AGAINST,
        results_digest="r" * 64,
        seq=SEQ,
    )


def _complete_record(
    *, artifact: Artifact | None = None, approver: str = "alice@org"
) -> ProvenanceRecord:
    """A full, intact provenance record (mirrors test_lifecycle_provenance)."""
    art = artifact or _artifact()
    gate_results = (_gate("build"), _gate("test"))
    presented = build_evidence_pack(
        run_against=RUN_AGAINST, gate_results=gate_results, artifacts=(art,), sign_offs=(), seq=SEQ
    )
    sign_offs = (
        SignOffRecord(
            subject_artifact_uri=art.uri,
            state_sha=RUN_AGAINST,
            approver=approver,
            role="release-manager",
            approved=True,
            server_nonce="nonce-1",
            presented_evidence_hash=pack_evidence_hash(presented),
            judgment_provenance=JudgmentProvenance.SELF,
            seq=SEQ,
        ),
    )
    pack = build_evidence_pack(
        run_against=RUN_AGAINST,
        gate_results=gate_results,
        artifacts=(art,),
        sign_offs=sign_offs,
        seq=SEQ,
    )
    return ProvenanceRecord(
        subject_artifact=art,
        run_id=RUN_ID,
        run_against=RUN_AGAINST,
        evidence_pack=pack,
        gate_results=gate_results,
        sign_offs=sign_offs,
        seq=SEQ,
    )


def test_complete_provenance_yields_clean_license():
    art = _artifact()
    rec = _complete_record(artifact=art)
    lic = license_for(art, rec, provenance_uri="cas://prov/v1")
    assert isinstance(lic, LicenseRecord)
    assert lic.clean
    assert lic.is_clean()
    assert lic.license_id == DEFAULT_LICENSE
    assert lic.ai_generated is True
    # The liability anchor is the Release/Admission human sign-off.
    assert lic.accountable_party == "alice@org"
    assert lic.run_against == RUN_AGAINST
    assert lic.provenance_uri == "cas://prov/v1"
    assert lic.flags == ()


def test_no_provenance_no_clean_license():
    art = _artifact()
    lic = license_for(art, None)
    assert not lic.clean
    assert lic.license_id == ""
    assert lic.accountable_party is None
    assert "no_provenance" in lic.flags


def test_no_provenance_strict_raises():
    art = _artifact()
    with pytest.raises(UnlicensableArtifactError):
        license_for(art, None, strict=True)


def test_incomplete_provenance_no_clean_license():
    """A broken provenance chain (e.g. a dropped gate verdict) -> no clean license."""
    art = _artifact()
    rec = _complete_record(artifact=art)
    # Dropping a gate verdict means the pack chain no longer reproduces.
    broken = rec.model_copy(update={"gate_results": rec.gate_results[:1]})
    lic = license_for(art, broken)
    assert not lic.clean
    assert lic.license_id == ""
    assert any(f.startswith("provenance:") for f in lic.flags)


def test_provenance_for_different_artifact_no_clean_license():
    art = _artifact("rc")
    rec = _complete_record(artifact=_artifact("other"))
    lic = license_for(art, rec)
    assert not lic.clean
    assert "provenance_subject_mismatch" in lic.flags


def test_custom_license_id_carried_when_clean():
    art = _artifact()
    rec = _complete_record(artifact=art)
    lic = license_for(art, rec, license_id="MIT")
    assert lic.clean
    assert lic.license_id == "MIT"


def test_license_for_is_deterministic():
    art = _artifact()
    rec = _complete_record(artifact=art)
    assert license_for(art, rec) == license_for(art, rec)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
