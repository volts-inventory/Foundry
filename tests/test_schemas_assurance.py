"""Conformance tests for the Phase-2 (Gated mode) assurance + governance schemas.

Contract under test (docs/agents.md Layer 5/6; governance.md §1c/§1e/§2/§6):
every model is a FrozenModel (frozen, extra="forbid"), carries `seq: Seq`, has a
content_identity() stable across construction order and sensitive to any field
change. Large bodies are `*_uri` CAS handles, never inline (DS9). Tiering scales
assurance depth/HITL but never relaxes integrity; sign-off carries the self-judge
stamp; the evidence pack carries hash-chained content-addressed references.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import (
    ChangeRiskTier,
    EvidencePack,
    RiskTier,
    SignOffRecord,
    TierDecision,
)
from foundry.schemas.security import AuditFinding, Severity
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="assurance", local=0)

# (model, valid kwargs, field to change, changed value) — one row per model.
CASES: list[tuple[type[FrozenModel], dict, str, object]] = [
    (
        AuditFinding,
        {
            "run_against": "c" * 40,
            "sbom_uri": "cas://sbom/1",
            "severity": Severity.HIGH,
            "findings": ("dependency foo@1.2 has CVE-2026-0001",),
            "license_report_uri": "cas://license/1",
            "compliance_map_uri": "cas://controls/1",
            "seq": SEQ,
        },
        "severity",
        Severity.CRITICAL,
    ),
    (
        TierDecision,
        {
            "tier": RiskTier.HIGH,
            "rationale": "touches CI workflow + lockfile -> forced HIGH",
            "run_against": "c" * 40,
            "gate_set": ("build", "test", "supply_chain", "threat_review"),
            "two_person_release": True,
            "forced_high": True,
            "seq": SEQ,
        },
        "rationale",
        "ordinary app-code feature",
    ),
    (
        SignOffRecord,
        {
            "subject_artifact_uri": "cas://release/1",
            "state_sha": "c" * 40,
            "approver": "alice@example.com",
            "role": "release-manager",
            "approved": True,
            "server_nonce": "sn-signoff-1",
            "presented_evidence_hash": "sha256:" + "a" * 64,
            "judgment_provenance": JudgmentProvenance.INDEPENDENT,
            "meaning_of_signing": "approve release for deploy",
            "seq": SEQ,
        },
        "approver",
        "bob@example.com",
    ),
    (
        EvidencePack,
        {
            "pack_uri": "cas://evidence/1",
            "run_against": "c" * 40,
            "chain_head": "sha256:" + "b" * 64,
            "gate_result_uris": ("cas://gate/build", "cas://gate/test"),
            "artifact_uris": ("cas://diff/1",),
            "sign_off_uris": ("cas://signoff/1",),
            "scanner_config_rev": "scan-rev-9",
            "provenance_predicate_uri": "cas://slsa/1",
            "seq": SEQ,
        },
        "chain_head",
        "sha256:" + "c" * 64,
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_constructs_and_is_frozen(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_extra_fields_rejected(cls, kwargs, field, changed):
    with pytest.raises(ValidationError):
        cls(**kwargs, not_a_declared_field="boom")


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_content_identity_stable_and_field_sensitive(cls, kwargs, field, changed):
    a = cls(**kwargs)
    b = cls(**dict(reversed(list(kwargs.items()))))  # construction order irrelevant
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic
    mutated = a.model_copy(update={field: changed})
    assert mutated.content_identity() != a.content_identity()


# ---------------------------------------------------------------------------
# Spot checks on key invariants
# ---------------------------------------------------------------------------


def test_risk_tier_is_the_governance_1e_enum_alias():
    assert RiskTier is ChangeRiskTier
    assert {t.value for t in RiskTier} == {"low", "standard", "high"}


def test_tier_decision_never_relaxes_integrity():
    """Tiering scales depth/HITL only — admission stays fail-closed (gov §1e)."""
    for tier in RiskTier:
        d = TierDecision(tier=tier, rationale="r", run_against="c" * 40, seq=SEQ)
        assert d.relaxes_integrity() is False
    with pytest.raises(ValidationError):
        TierDecision(tier="medium", rationale="r", run_against="x", seq=SEQ)


def test_sign_off_carries_self_judge_stamp():
    """The self-judge profile stamp is a first-class field (gov §1e)."""
    base = {
        "subject_artifact_uri": "cas://release/1",
        "state_sha": "c" * 40,
        "approver": "alice@example.com",
        "role": "release-manager",
        "approved": True,
        "server_nonce": "sn-1",
        "presented_evidence_hash": "sha256:" + "a" * 64,
        "seq": SEQ,
    }
    self_judged = SignOffRecord(**base, judgment_provenance=JudgmentProvenance.SELF)
    assert self_judged.judgment_provenance is JudgmentProvenance.SELF
    indep = SignOffRecord(**base, judgment_provenance=JudgmentProvenance.INDEPENDENT)
    # the stamp is part of identity — a self-judge sign-off is not the same record
    assert self_judged.content_identity() != indep.content_identity()
    with pytest.raises(ValidationError):
        SignOffRecord(**base)  # judgment_provenance is required, never defaulted


def test_evidence_pack_carries_content_addressed_references():
    """Hash-chained handles travel with the release; bodies stay out-of-band."""
    pack = EvidencePack(
        pack_uri="cas://evidence/1",
        run_against="c" * 40,
        chain_head="sha256:" + "b" * 64,
        gate_result_uris=["cas://gate/build", "cas://gate/test"],  # list coerces
        sign_off_uris=("cas://signoff/1",),
        seq=SEQ,
    )
    assert pack.gate_result_uris == ("cas://gate/build", "cas://gate/test")
    assert pack.sign_off_uris == ("cas://signoff/1",)
    assert pack.artifact_uris == ()  # default_factory empty tuple
    # the pack's own handle is what a sign-off binds via presented_evidence_hash
    assert pack.pack_uri.startswith("cas://")


def test_versioned_supersession_fields_present():
    """Phase-2 records supersede, never mutate (L4)."""
    f = AuditFinding(
        run_against="c" * 40, sbom_uri="cas://sbom/2", severity=Severity.LOW,
        version=2, supersedes="cas://audit/1", seq=SEQ,
    )
    assert f.version == 2 and f.supersedes == "cas://audit/1"
    t = TierDecision(
        tier=RiskTier.STANDARD, rationale="r", run_against="c" * 40,
        version=2, supersedes="cas://tier/1", seq=SEQ,
    )
    assert t.supersedes == "cas://tier/1"
