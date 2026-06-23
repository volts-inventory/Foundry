"""Core enforcement of the composed governance hooks (T-M5-04, domains.md §4).

Contract under test (the pack DECLARES via :mod:`foundry.domains.contract`; this
ENFORCES). Each hook is fail-closed + tighten-only:

* **(d) mode/tier veto + regulatory-significance axis.** A vetoed Autonomous → the
  effective mode is Gated (Autonomous UNREACHABLE); ``min_tier`` raises the tier;
  a regulatory-significant request forces the strict path (Gated + human, HIGH
  tier) independent of diff size; the veto can only TIGHTEN (never widen Gated to
  Autonomous).
* **(e) approver-of-record.** A class-4/regulated artifact REQUIRES a recorded
  human approver (no approver ⇒ no release, fail-closed); an Autonomous/agent
  auto-approve is rejected; two-person where required.
* **(f) data-egress profile.** A third-party-egress / real-PHI violation is denied
  fail-closed; a within-profile request is allowed.
* **(g) mandatory perceptual HITL.** A class-5 / UI request is HITL-required and
  NEVER auto-approved; a non-UI request is a no-op.
* Determinism: the resolvers are pure functions of (policy + request).
"""

from __future__ import annotations

import pytest

from foundry.domains.contract import (
    ApproverPolicy,
    EgressPolicy,
    ModeTierPolicy,
    PerceptualHITLPolicy,
)
from foundry.domains.governance_hooks import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    ApproverRequirement,
    EgressRequest,
    EgressVerdict,
    GovernanceHookError,
    PerceptualHITLRequirement,
    assert_egress_within_profile,
    bind_approver_of_record,
    egress_within_profile,
    mark_perceptual_hitl,
    perceptual_hitl_required,
    release_admitted,
    resolve_effective_mode_tier,
)
from foundry.governance.sign_off import SignOffError, SignOffPoint, record_sign_off
from foundry.schemas.governance import RiskTier, SignOffRecord
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=0, node_id="gov", local=0)
SEQ2 = Seq(run_id="r1", emission_epoch=2, superstep_index=0, node_id="gov", local=1)


def _human_release(
    *,
    approver: str = "dr.alice",
    role: str = "validator",
    subject: str = "cas://design",
    state_sha: str = "sha-1",
    nonce: str = "n-1",
    approved: bool = True,
    seq: Seq = SEQ,
) -> SignOffRecord:
    return record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=subject,
        state_sha=state_sha,
        presented_evidence_hash="ev-1",
        server_nonce=nonce,
        approver=approver,
        role=role,
        profile="independent",
        seq=seq,
        approved=approved,
    )


# --- (d) mode/tier veto + regulatory-significance axis -----------------------


def test_vetoed_autonomous_is_unreachable_effective_mode_is_gated() -> None:
    policy = ModeTierPolicy(vetoed_modes=frozenset({MODE_AUTONOMOUS, MODE_ASSISTANT}))
    eff = resolve_effective_mode_tier(
        policy, requested_mode=MODE_AUTONOMOUS, request={"request": "ship it"}, seq=SEQ
    )
    assert MODE_AUTONOMOUS not in eff.reachable_modes
    assert eff.effective_mode == MODE_GATED  # falls back to the strictest still-allowed
    assert eff.autonomous_reachable is False
    assert eff.force_human_release is True  # no hands-off path ⇒ a human is forced


def test_min_tier_raises_effective_tier_tighten_only() -> None:
    # The classifier would call a plain "tweak the button label" change LOW-ish; a
    # pack min_tier=HIGH must RAISE it (tighten-only: the stricter side wins).
    policy = ModeTierPolicy(min_tier=RiskTier.HIGH)
    eff = resolve_effective_mode_tier(
        policy, request={"request": "rename a local variable typo"}, seq=SEQ
    )
    assert eff.classifier_tier == RiskTier.LOW
    assert eff.effective_tier == RiskTier.HIGH  # min_tier raised it


def test_min_tier_never_lowers_a_stricter_classifier_tier() -> None:
    # The classifier force-escalates an auth change to HIGH; a pack min_tier=LOW
    # must NOT lower it — the effective tier stays the STRICTER classifier verdict.
    policy = ModeTierPolicy(min_tier=RiskTier.LOW)
    eff = resolve_effective_mode_tier(
        policy, request={"request": "change the oauth login crypto"}, seq=SEQ
    )
    assert eff.classifier_tier == RiskTier.HIGH
    assert eff.effective_tier == RiskTier.HIGH  # min_tier=LOW can't relax it


def test_regulatory_significance_forces_strict_path_independent_of_diff_size() -> None:
    # A one-line change, no security keywords → classifier LOW; the regulatory-
    # significance axis forces the strict path: Autonomous + auto-approve Assistant
    # unreachable, Gated, HIGH tier, human + two-person — independent of diff size.
    policy = ModeTierPolicy.identity()
    eff = resolve_effective_mode_tier(
        policy,
        requested_mode=MODE_AUTONOMOUS,
        request={"request": "change the APR by one basis point"},
        regulatory_significant=True,
        seq=SEQ,
    )
    assert eff.regulatory_significant is True
    assert eff.classifier_tier == RiskTier.STANDARD  # nothing flagged it
    assert eff.effective_tier == RiskTier.HIGH  # reg-sig floors HIGH
    assert eff.reachable_modes == frozenset({MODE_GATED})
    assert eff.effective_mode == MODE_GATED
    assert eff.force_human_release is True
    assert eff.two_person_release is True


def test_regulatory_significance_read_from_request_view() -> None:
    eff = resolve_effective_mode_tier(
        ModeTierPolicy.identity(),
        request={"request": "x", "regulatory_significant": True},
        seq=SEQ,
    )
    assert eff.regulatory_significant is True
    assert eff.effective_mode == MODE_GATED


def test_veto_cannot_relax_a_gated_request_to_autonomous() -> None:
    # The resolver is tighten-only: a Gated request can never be WIDENED to a more
    # permissive mode even when nothing is vetoed.
    eff = resolve_effective_mode_tier(
        ModeTierPolicy.identity(), requested_mode=MODE_GATED, request={"request": "x"}, seq=SEQ
    )
    assert eff.effective_mode == MODE_GATED


def test_no_veto_keeps_requested_autonomous_reachable() -> None:
    eff = resolve_effective_mode_tier(
        ModeTierPolicy.identity(),
        requested_mode=MODE_AUTONOMOUS,
        request={"request": "ordinary app feature"},
        seq=SEQ,
    )
    assert eff.autonomous_reachable is True
    assert eff.effective_mode == MODE_AUTONOMOUS
    assert eff.force_human_release is False


def test_mode_tier_resolution_is_deterministic() -> None:
    policy = ModeTierPolicy(vetoed_modes=frozenset({MODE_AUTONOMOUS}), min_tier=RiskTier.STANDARD)
    req = {"request": "build a payment feature", "regulatory_significant": True}
    a = resolve_effective_mode_tier(policy, request=req, seq=SEQ)
    b = resolve_effective_mode_tier(policy, request=req, seq=SEQ)
    assert a == b


# --- (e) approver-of-record --------------------------------------------------

_APPROVER_POLICY = ApproverPolicy(
    bound_artifact_classes=frozenset({"design", "validation_report"})
)


def test_class4_artifact_requires_recorded_human_approver_no_record_no_release() -> None:
    # No sign-off recorded ⇒ release NOT admitted (fail-closed).
    assert (
        release_admitted(
            _APPROVER_POLICY,
            None,
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
        )
        is False
    )
    # A recorded, approved HUMAN sign-off binding this artifact ⇒ admitted.
    rec = _human_release()
    assert (
        release_admitted(
            _APPROVER_POLICY,
            [rec],
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
        )
        is True
    )


def test_autonomous_auto_approve_is_unreachable_for_class4() -> None:
    # An "autonomous" auto-approver identity is NOT a human ⇒ binding rejected.
    with pytest.raises(SignOffError):
        bind_approver_of_record(
            _APPROVER_POLICY,
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
            presented_evidence_hash="ev-1",
            server_nonce="n-1",
            approver="autonomous",
            role="release-approver",
            profile="self_judge",
            seq=SEQ,
        )
    # And an agent-identity sign-off does not admit a release even if "recorded".
    agent_rec = _human_release(approver="agent")
    assert (
        release_admitted(
            _APPROVER_POLICY,
            [agent_rec],
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
        )
        is False
    )


def test_bind_records_a_human_approver_of_record() -> None:
    recs = bind_approver_of_record(
        _APPROVER_POLICY,
        artifact_class="validation_report",
        subject_artifact_uri="cas://vr",
        state_sha="sha-2",
        presented_evidence_hash="ev-2",
        server_nonce="n-2",
        approver="dr.alice",
        role="validator",
        profile="independent",
        seq=SEQ,
    )
    assert len(recs) == 1
    assert recs[0].approver == "dr.alice"
    assert recs[0].approved is True


def test_binding_an_unbound_artifact_class_is_refused() -> None:
    with pytest.raises(SignOffError):
        bind_approver_of_record(
            _APPROVER_POLICY,
            artifact_class="readme",  # not in the bound set
            subject_artifact_uri="cas://r",
            state_sha="sha",
            presented_evidence_hash="ev",
            server_nonce="n",
            approver="dr.alice",
            role="validator",
            profile="independent",
            seq=SEQ,
        )


def test_two_person_requires_distinct_human_distinct_role() -> None:
    recs = bind_approver_of_record(
        _APPROVER_POLICY,
        artifact_class="design",
        subject_artifact_uri="cas://design",
        state_sha="sha-1",
        presented_evidence_hash="ev-1",
        server_nonce="n-1",
        approver="dr.alice",
        role="preparer",
        profile="independent",
        seq=SEQ,
        second_approver="dr.bob",
        second_role="validator",
        second_nonce="n-2",
        second_seq=SEQ2,
    )
    assert len(recs) == 2
    assert release_admitted(
        _APPROVER_POLICY,
        recs,
        artifact_class="design",
        subject_artifact_uri="cas://design",
        state_sha="sha-1",
        require_two_person=True,
    )
    # The SAME human in the same role does NOT satisfy two-person.
    with pytest.raises(SignOffError):
        bind_approver_of_record(
            _APPROVER_POLICY,
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
            presented_evidence_hash="ev-1",
            server_nonce="n-1",
            approver="dr.alice",
            role="preparer",
            profile="independent",
            seq=SEQ,
            second_approver="dr.alice",
            second_role="preparer",
        )


def test_two_person_not_satisfied_by_single_approver() -> None:
    rec = _human_release(approver="dr.alice", role="validator")
    assert (
        release_admitted(
            _APPROVER_POLICY,
            [rec],
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
            require_two_person=True,
        )
        is False
    )


def test_unbound_class_needs_no_approver_of_record() -> None:
    # A class outside the bound set is a no-op for this hook → admitted.
    assert (
        release_admitted(
            _APPROVER_POLICY,
            None,
            artifact_class="readme",
            subject_artifact_uri="cas://r",
            state_sha="sha",
        )
        is True
    )
    assert approver_requirement_unbound().required is False


def approver_requirement_unbound() -> ApproverRequirement:
    from foundry.domains.governance_hooks import approver_required

    return approver_required(_APPROVER_POLICY, artifact_class="readme")


def test_release_admission_rejects_mismatched_state_sha() -> None:
    rec = _human_release(state_sha="other-sha")
    assert (
        release_admitted(
            _APPROVER_POLICY,
            [rec],
            artifact_class="design",
            subject_artifact_uri="cas://design",
            state_sha="sha-1",
        )
        is False
    )


# --- (f) data-egress profile -------------------------------------------------


def test_third_party_egress_violation_denied_fail_closed() -> None:
    policy = EgressPolicy(no_third_party_model_egress=True, no_third_party_trace_egress=True)
    verdict = egress_within_profile(
        policy, EgressRequest(third_party_model_egress=True)
    )
    assert isinstance(verdict, EgressVerdict)
    assert verdict.allowed is False
    assert "third_party_model_egress" in verdict.violations
    with pytest.raises(GovernanceHookError):
        assert_egress_within_profile(policy, EgressRequest(third_party_trace_egress=True))


def test_real_phi_violation_denied_fail_closed() -> None:
    policy = EgressPolicy(no_real_phi_pan=True)
    with pytest.raises(GovernanceHookError):
        assert_egress_within_profile(policy, EgressRequest(real_phi_pan=True))


def test_within_profile_egress_allowed() -> None:
    policy = EgressPolicy(no_third_party_model_egress=True, no_real_phi_pan=True)
    # In-boundary (no third-party egress, synthetic data) → allowed, no raise.
    egress = EgressRequest(
        third_party_model_egress=False, third_party_trace_egress=False, real_phi_pan=False
    )
    assert egress_within_profile(policy, egress).allowed is True
    assert_egress_within_profile(policy, egress)  # does not raise


def test_identity_egress_profile_forbids_nothing() -> None:
    # An identity (no-opinion) profile permits any egress — the hook only tightens.
    assert (
        egress_within_profile(
            EgressPolicy.identity(),
            EgressRequest(third_party_model_egress=True, real_phi_pan=True),
        ).allowed
        is True
    )


def test_egress_enumerates_all_violations() -> None:
    policy = EgressPolicy(
        no_third_party_model_egress=True,
        no_third_party_trace_egress=True,
        no_real_phi_pan=True,
    )
    verdict = egress_within_profile(
        policy,
        EgressRequest(
            third_party_model_egress=True, third_party_trace_egress=True, real_phi_pan=True
        ),
    )
    assert set(verdict.violations) == {
        "third_party_model_egress",
        "third_party_trace_egress",
        "real_phi_pan",
    }


# --- (g) mandatory perceptual HITL -------------------------------------------

_HITL_POLICY = PerceptualHITLPolicy(hitl_required_capabilities=frozenset({"ui"}))


def test_ui_request_is_hitl_required_and_never_auto_approved() -> None:
    req = perceptual_hitl_required(_HITL_POLICY, request={"required_capabilities": ["ui"]})
    assert isinstance(req, PerceptualHITLRequirement)
    assert req.required is True
    assert req.auto_approvable is False
    assert req.require_rendered_evidence is True
    # No human sign-off ⇒ the gate can NEVER be cleared automatically.
    with pytest.raises(SignOffError):
        mark_perceptual_hitl(_HITL_POLICY, request={"required_capabilities": ["ui"]})


def test_ui_request_cleared_only_by_recorded_human_sign_off() -> None:
    human = _human_release(role="ux-approver")
    out = mark_perceptual_hitl(
        _HITL_POLICY,
        request={"required_capabilities": ["ui"]},
        human_sign_off=human,
    )
    assert out is human
    # An un-approved sign-off does NOT clear it.
    with pytest.raises(SignOffError):
        mark_perceptual_hitl(
            _HITL_POLICY,
            request={"required_capabilities": ["ui"]},
            human_sign_off=_human_release(approved=False),
        )


def test_non_ui_request_is_a_noop_for_perceptual_hitl() -> None:
    req = perceptual_hitl_required(
        _HITL_POLICY, request={"required_capabilities": ["backend"]}
    )
    assert req.required is False
    assert req.auto_approvable is True
    assert (
        mark_perceptual_hitl(_HITL_POLICY, request={"required_capabilities": ["backend"]})
        is None
    )


def test_perceptual_hitl_is_deterministic() -> None:
    req = {"required_capabilities": ["ui", "frontend"]}
    a = perceptual_hitl_required(_HITL_POLICY, request=req)
    b = perceptual_hitl_required(_HITL_POLICY, request=req)
    assert a == b
    assert a.matched_capabilities == frozenset({"ui"})
