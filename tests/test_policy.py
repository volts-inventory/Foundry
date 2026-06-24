"""Five-plane policy package. (governance §2, implementation-plan §5.3, §9)

Asserts the load-bearing seams:
- the five-plane model enumerates ALL five planes and maps each to its controls
  + implementing modules (completeness is assertable, not hoped-for);
- the OPA-style policy evaluator is fail-closed — denies on a deny rule AND on
  an unknown/missing policy (default-deny), allows only on an explicit match;
- the scanner config is run-immutable + content-addressed (mutation → different
  identity, tampering detectable, fail-closed verify);
- segregation of duties denies a self-produce-and-sign attempt;
- determinism (same inputs → same identity / same decision).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.policy import (
    DENY_RULES,
    FIVE_PLANE_MODEL,
    AuthorizationDecision,
    Decision,
    DutyRequest,
    FivePlaneModel,
    PinnedScannerConfig,
    Plane,
    PolicyEvaluator,
    ScannerConfigRecord,
    ScannerConfigTamperError,
    ScannerRule,
    Zone,
    authorize_segregation_of_duties,
)
from foundry.policy.planes import PlaneSpec


# --------------------------------------------------------------------------- #
# Five-plane model: completeness + control/module mapping
# --------------------------------------------------------------------------- #
def test_five_plane_model_enumerates_all_five_planes() -> None:
    assert FIVE_PLANE_MODEL.planes() == frozenset(Plane)
    assert len(FIVE_PLANE_MODEL.specs) == 5
    assert FIVE_PLANE_MODEL.missing_planes() == frozenset()


def test_five_plane_model_is_complete() -> None:
    assert FIVE_PLANE_MODEL.is_complete()
    mapping = FIVE_PLANE_MODEL.by_plane()
    for plane in Plane:
        spec = mapping[plane]
        # Each plane maps to at least one control AND one implementing module.
        assert spec.controls, f"{plane} has no controls"
        assert spec.modules, f"{plane} has no implementing modules"


def test_five_plane_policy_plane_wires_the_three_policy_controls() -> None:
    policy = FIVE_PLANE_MODEL.by_plane()[Plane.POLICY]
    assert "run-immutable-scanner-config" in policy.controls
    assert "segregation-of-duties" in policy.controls
    assert "opa-style-deny-evaluation" in policy.controls
    assert "foundry.policy.scanner_config" in policy.modules
    assert "foundry.policy.authorization" in policy.modules


def test_five_plane_model_incomplete_when_a_plane_is_missing() -> None:
    # Drop the Assurance plane → completeness must report false + the gap.
    pruned = FivePlaneModel(
        specs=tuple(s for s in FIVE_PLANE_MODEL.specs if s.plane is not Plane.ASSURANCE)
    )
    assert not pruned.is_complete()
    assert Plane.ASSURANCE in pruned.missing_planes()


def test_five_plane_model_incomplete_when_a_plane_has_no_controls() -> None:
    empty = PlaneSpec(plane=Plane.DATA, summary="x", controls=(), modules=("m",))
    specs = tuple(s if s.plane is not Plane.DATA else empty for s in FIVE_PLANE_MODEL.specs)
    assert not FivePlaneModel(specs=specs).is_complete()


# --------------------------------------------------------------------------- #
# OPA-style policy evaluator: fail-closed deny
# --------------------------------------------------------------------------- #
def test_policy_allows_an_explicit_known_subject_with_no_deny() -> None:
    ev = PolicyEvaluator()
    decision = ev.evaluate({"policy": "tool-call", "action": "read"})
    assert decision.allowed
    assert decision.decision is Decision.ALLOW


def test_policy_denies_unknown_or_missing_policy_fail_closed() -> None:
    ev = PolicyEvaluator()
    # No policy id at all → default-deny.
    missing = ev.evaluate({"action": "read"})
    assert not missing.allowed
    assert missing.matched_rule == "unknown-policy"
    # Empty mapping → also denied (never allowed by absence of a rule).
    assert not ev.evaluate({}).allowed


def test_policy_denies_on_a_deny_rule() -> None:
    ev = PolicyEvaluator()
    # Egress-widening output requires HITL → denied here.
    d1 = ev.evaluate({"policy": "iac-plan", "widens_egress": True})
    assert not d1.allowed
    assert d1.matched_rule == "egress-widening"
    # Writing the run-immutable scanner config is denied.
    d2 = ev.evaluate({"policy": "fs-write", "writes_scanner_config": True})
    assert not d2.allowed
    assert d2.matched_rule == "scanner-config-write"
    # Signing from a non-attestor zone is denied.
    d3 = ev.evaluate({"policy": "sign", "action": "sign", "zone": "zone-a-producer"})
    assert not d3.allowed
    assert d3.matched_rule == "signing-outside-zone-b"


def test_policy_evaluator_error_fails_closed() -> None:
    from foundry.policy.planes import DenyRule

    def _boom(_: object) -> bool:
        raise RuntimeError("rule blew up")

    ev = PolicyEvaluator(rules=(DenyRule(name="boom", predicate=_boom),))
    decision = ev.evaluate({"policy": "anything"})
    assert not decision.allowed
    assert decision.matched_rule == "boom"


def test_policy_evaluation_is_deterministic() -> None:
    ev = PolicyEvaluator()
    subject = {"policy": "iac-plan", "widens_egress": True}
    first = ev.evaluate(subject)
    second = ev.evaluate(subject)
    assert first == second
    # Rule set is pinned/run-immutable: the default evaluator carries DENY_RULES.
    assert ev.rules == DENY_RULES


# --------------------------------------------------------------------------- #
# Run-immutable, content-addressed scanner config
# --------------------------------------------------------------------------- #
def _record() -> ScannerConfigRecord:
    return ScannerConfigRecord(
        rules=(
            ScannerRule(id="py-sast-001", family="sast", version="v1"),
            ScannerRule(id="secret-aws-key", family="secret", version="v1"),
        ),
        severity_floors=(("sast", "high"),),
        license_allowlist=("MIT", "Apache-2.0"),
    )


def test_scanner_config_is_content_addressed_and_deterministic() -> None:
    a = _record()
    b = _record()
    assert a.revision_hash == b.revision_hash  # same content → same identity
    assert len(a.revision_hash) == 64  # sha256 hex


def test_scanner_config_mutation_yields_different_identity() -> None:
    base = _record()
    # A weakened severity floor is a DIFFERENT record → different content address.
    weakened = base.model_copy(update={"severity_floors": (("sast", "low"),)})
    assert weakened.revision_hash != base.revision_hash
    # A new suppression allowlist entry likewise changes identity (detectable).
    suppressed = base.model_copy(update={"suppression_allowlist": ("nosec:py-sast-001",)})
    assert suppressed.revision_hash != base.revision_hash


def test_scanner_config_record_is_frozen() -> None:
    rec = _record()
    with pytest.raises(ValidationError):
        rec.severity_floors = (("sast", "low"),)  # type: ignore[misc]


def test_pinned_scanner_config_verifies_intact() -> None:
    pinned = PinnedScannerConfig.pin(_record())
    assert pinned.is_intact()
    assert pinned.verify() == _record().revision_hash


def test_pinned_scanner_config_detects_tampering_fail_closed() -> None:
    record = _record()
    # Pin to the genuine revision, then bind a TAMPERED record under that pin.
    tampered = record.model_copy(update={"severity_floors": (("sast", "low"),)})
    pinned = PinnedScannerConfig(record=tampered, pinned_revision=record.revision_hash)
    assert not pinned.is_intact()
    with pytest.raises(ScannerConfigTamperError):
        pinned.verify()


def test_scanner_config_rule_versions_feed_reuse_key() -> None:
    rec = _record()
    assert rec.rule_versions() == ("v1",)


# --------------------------------------------------------------------------- #
# Segregation of duties
# --------------------------------------------------------------------------- #
def test_self_produce_and_sign_is_denied() -> None:
    req = DutyRequest(
        artifact_uri="cas://sha256/abc",
        authors=("agent-7",),
        approver="agent-7",  # the producer trying to sign its own output
        approver_zone=Zone.B_ATTESTOR,
        approver_role="attestor",
    )
    decision = authorize_segregation_of_duties(req)
    assert not decision.allowed
    assert "author set" in decision.reason


def test_distinct_approver_in_attestor_zone_is_allowed() -> None:
    req = DutyRequest(
        artifact_uri="cas://sha256/abc",
        authors=("agent-7",),
        approver="runner-zone-b",
        approver_zone=Zone.B_ATTESTOR,
        approver_role="attestor",
        required_distinct_role="attestor",
    )
    decision = authorize_segregation_of_duties(req)
    assert isinstance(decision, AuthorizationDecision)
    assert decision.allowed


def test_signing_from_producer_zone_is_denied() -> None:
    req = DutyRequest(
        artifact_uri="cas://sha256/abc",
        authors=("agent-7",),
        approver="runner",
        approver_zone=Zone.A_PRODUCER,  # producer zone cannot sign/admit
        approver_role="attestor",
    )
    assert not authorize_segregation_of_duties(req).allowed


def test_missing_distinct_role_is_denied() -> None:
    req = DutyRequest(
        artifact_uri="cas://sha256/abc",
        authors=("agent-7",),
        approver="runner",
        approver_zone=Zone.B_ATTESTOR,
        approver_role="reviewer",
        required_distinct_role="attestor",
    )
    assert not authorize_segregation_of_duties(req).allowed


def test_empty_author_or_approver_fails_closed() -> None:
    no_authors = DutyRequest(
        artifact_uri="u", authors=(), approver="x", approver_zone=Zone.B_ATTESTOR, approver_role="r"
    )
    assert not authorize_segregation_of_duties(no_authors).allowed
    no_approver = DutyRequest(
        artifact_uri="u",
        authors=("a",),
        approver="",
        approver_zone=Zone.B_ATTESTOR,
        approver_role="r",
    )
    assert not authorize_segregation_of_duties(no_approver).allowed


def test_segregation_of_duties_is_deterministic() -> None:
    req = DutyRequest(
        artifact_uri="u",
        authors=("agent-7",),
        approver="agent-7",
        approver_zone=Zone.B_ATTESTOR,
        approver_role="attestor",
    )
    assert authorize_segregation_of_duties(req) == authorize_segregation_of_duties(req)


def test_evaluator_exposes_segregation_of_duties() -> None:
    ev = PolicyEvaluator()
    req = DutyRequest(
        artifact_uri="u",
        authors=("a",),
        approver="a",
        approver_zone=Zone.B_ATTESTOR,
        approver_role="attestor",
    )
    assert not ev.authorize_duties(req).allowed
