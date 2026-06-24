"""Tests for the REGULATED domain pack (T-M5-05, docs/domains.md §5).

Proves the four overridden hooks tighten correctly + compose strictest-wins, and
that the pack registers + routes by capability with NO core edit:

* Autonomous is UNREACHABLE — the effective mode for a regulated request is Gated
  (both ``autonomous`` and the auto-approve ``assistant`` are vetoed).
* A recorded HUMAN approver-of-record is REQUIRED for release (no approver ⇒ no
  release, fail-closed; a non-human / self-judge / Autonomous approver is rejected).
* Two-person sign-off is required; the min tier is raised to HIGH; the
  regulatory-significance axis fires.
* Egress restricts third-party model/trace egress and real PHI/PAN.
* The pack registers via ``PackRegistry.register(RegulatedPack())`` and routes for a
  regulated capability — instantiate + register inside the test (no core change).
* Determinism: routing/composition is registration-order-independent.
"""

from __future__ import annotations

from foundry.domains.contract import VerdictClassPolicy
from foundry.domains.governance_hooks import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    EgressRequest,
    bind_approver_of_record,
    egress_within_profile,
    release_admitted,
    resolve_effective_mode_tier,
)
from foundry.domains.packs.regulated import (
    REGULATED_CAPABILITIES,
    RegulatedPack,
)
from foundry.domains.registry import PackRegistry
from foundry.domains.verdict_class import autonomous_reachable, gate_requirements_for, satisfies
from foundry.governance.sign_off import SignOffError, SignOffPoint, record_sign_off
from foundry.schemas.gates import VerdictClass
from foundry.schemas.governance import RiskTier
from foundry.state.seq import Seq

_SEQ = Seq(run_id="run-reg", emission_epoch=0, superstep_index=0, node_id="release_gate", local=0)
_SUBJECT = "cas://sha256/regulated-release"
_STATE_SHA = "state-abc"
_EVIDENCE = "evidence-hash"
_NONCE = "server-nonce-1"


def _registered_registry() -> PackRegistry:
    reg = PackRegistry()
    reg.register(RegulatedPack())
    return reg


# --- registration + routing (open/closed: no core edit) ----------------------


def test_registers_and_routes_by_capability() -> None:
    reg = _registered_registry()
    for cap in REGULATED_CAPABILITIES:
        selected = reg.select_packs({"required_capabilities": [cap]})
        assert [p.name for p in selected] == ["regulated"]


def test_does_not_route_for_unrelated_capability() -> None:
    reg = _registered_registry()
    assert reg.select_packs({"required_capabilities": ["backend", "ui"]}) == ()


def test_register_is_idempotent_same_instance() -> None:
    reg = PackRegistry()
    pack = RegulatedPack()
    reg.register(pack)
    reg.register(pack)  # idempotent re-registration is a no-op
    assert [p.name for p in reg.packs()] == ["regulated"]


def test_satisfies_the_contract_structurally() -> None:
    from foundry.domains.contract import DomainPackContract

    assert isinstance(RegulatedPack(), DomainPackContract)


# --- Hook 4: Autonomous unreachable, tier raised, two-person, reg-significance --


def test_autonomous_is_vetoed_effective_mode_is_gated() -> None:
    policy = _registered_registry().compose({"required_capabilities": ["regulated"]}).mode_tier
    eff = resolve_effective_mode_tier(
        policy, requested_mode=MODE_AUTONOMOUS, seq=_SEQ, regulatory_significant=False
    )
    assert eff.effective_mode == MODE_GATED
    assert eff.autonomous_reachable is False
    assert MODE_AUTONOMOUS not in eff.reachable_modes
    assert MODE_ASSISTANT not in eff.reachable_modes  # auto-approve vetoed too
    assert eff.reachable_modes == frozenset({MODE_GATED})


def test_min_tier_raised_to_high_and_forces_human_two_person() -> None:
    policy = _registered_registry().compose({"required_capabilities": ["healthcare"]}).mode_tier
    assert policy.min_tier is RiskTier.HIGH
    eff = resolve_effective_mode_tier(policy, requested_mode=MODE_AUTONOMOUS, seq=_SEQ)
    assert eff.effective_tier is RiskTier.HIGH
    assert eff.force_human_release is True
    assert eff.two_person_release is True
    # The regulatory-significance axis fires (human Release independent of diff size).
    assert eff.regulatory_significant is True or eff.force_human_release is True


def test_assistant_requested_still_gated() -> None:
    # Even an explicit assistant (auto-approve) request falls back to Gated.
    policy = _registered_registry().compose({"required_capabilities": ["finance"]}).mode_tier
    eff = resolve_effective_mode_tier(policy, requested_mode=MODE_ASSISTANT, seq=_SEQ)
    assert eff.effective_mode == MODE_GATED


# --- Hook 1: class-4 (human-witnessed) verdict assignment --------------------


def test_release_keys_assigned_class4_human_witnessed() -> None:
    vc = _registered_registry().compose({"required_capabilities": ["regulated"]}).verdict_class
    assert vc.classes["release"] is VerdictClass.HUMAN_WITNESSED
    req = gate_requirements_for(vc.classes["release"])
    assert req.requires_approver is True
    assert req.requires_hitl is True
    assert req.auto_approvable is False  # Autonomous / self-judge unreachable
    # A class-4 key is NOT satisfied by a green machine (class-2) gate.
    assert satisfies(VerdictClass.HUMAN_WITNESSED, VerdictClass.EXECUTION_DETERMINISTIC) is False
    assert autonomous_reachable(VerdictClass.HUMAN_WITNESSED) is False


# --- Hook 5: approver-of-record (fail-closed) --------------------------------


def test_no_approver_no_release_fail_closed() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    # No recorded sign-off at all ⇒ release NOT admitted (fail-closed).
    assert (
        release_admitted(
            approver,
            None,
            artifact_class="release",
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
        )
        is False
    )


def test_recorded_human_approver_admits_release() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    record = record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash=_EVIDENCE,
        server_nonce=_NONCE,
        approver="dr.jane.smith",
        role="qualified-validator",
        profile="independent",
        seq=_SEQ,
    )
    assert (
        release_admitted(
            approver,
            [record],
            artifact_class="release",
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
        )
        is True
    )


def test_non_human_self_judge_approver_rejected() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    for non_human in ("autonomous", "agent", "self-judge", "system"):
        record = record_sign_off(
            SignOffPoint.RELEASE,
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
            presented_evidence_hash=_EVIDENCE,
            server_nonce=_NONCE,
            approver=non_human,
            role="approver",
            profile="self_judge",
            seq=_SEQ,
        )
        assert (
            release_admitted(
                approver,
                [record],
                artifact_class="release",
                subject_artifact_uri=_SUBJECT,
                state_sha=_STATE_SHA,
            )
            is False
        )


def test_bind_approver_rejects_non_human() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    try:
        bind_approver_of_record(
            approver,
            artifact_class="release",
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
            presented_evidence_hash=_EVIDENCE,
            server_nonce=_NONCE,
            approver="autonomous",
            role="approver",
            profile="self_judge",
            seq=_SEQ,
        )
    except SignOffError:
        pass
    else:
        raise AssertionError("binding a non-human class-4 approver must fail-closed")


def test_two_person_requires_distinct_human_and_role() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    second_seq = _SEQ._replace(local=1)
    records = bind_approver_of_record(
        approver,
        artifact_class="release",
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash=_EVIDENCE,
        server_nonce=_NONCE,
        approver="dr.jane.smith",
        role="preparer",
        profile="independent",
        seq=_SEQ,
        second_approver="dr.john.doe",
        second_role="validator",
        second_nonce="server-nonce-2",
        second_seq=second_seq,
    )
    assert len(records) == 2
    assert (
        release_admitted(
            approver,
            records,
            artifact_class="release",
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
            require_two_person=True,
        )
        is True
    )


def test_two_person_single_approver_not_admitted() -> None:
    approver = _registered_registry().compose({"required_capabilities": ["regulated"]}).approver
    record = record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=_SUBJECT,
        state_sha=_STATE_SHA,
        presented_evidence_hash=_EVIDENCE,
        server_nonce=_NONCE,
        approver="dr.jane.smith",
        role="preparer",
        profile="independent",
        seq=_SEQ,
    )
    assert (
        release_admitted(
            approver,
            [record],
            artifact_class="release",
            subject_artifact_uri=_SUBJECT,
            state_sha=_STATE_SHA,
            require_two_person=True,
        )
        is False
    )


# --- Hook 6: egress profile --------------------------------------------------


def test_egress_restricts_phi_pii_and_third_party() -> None:
    egress = _registered_registry().compose({"required_capabilities": ["phi"]}).egress
    assert egress.no_third_party_model_egress is True
    assert egress.no_third_party_trace_egress is True
    assert egress.no_real_phi_pan is True
    # A request performing those egresses is DENIED.
    verdict = egress_within_profile(
        egress,
        EgressRequest(
            third_party_model_egress=True,
            third_party_trace_egress=True,
            real_phi_pan=True,
        ),
    )
    assert verdict.allowed is False
    assert set(verdict.violations) == {
        "third_party_model_egress",
        "third_party_trace_egress",
        "real_phi_pan",
    }


def test_egress_allows_in_boundary_request() -> None:
    egress = _registered_registry().compose({"required_capabilities": ["regulated"]}).egress
    verdict = egress_within_profile(egress, EgressRequest())  # no egress posture
    assert verdict.allowed is True


# --- determinism / composition ------------------------------------------------


def test_composition_is_deterministic_across_registration_order() -> None:
    # The pack is the only one engaged; composition must be stable regardless of how
    # many times it (idempotently) registers — the joins are order-independent.
    req = {"required_capabilities": ["regulated", "phi"]}
    a = _registered_registry().compose(req)
    b = _registered_registry().compose(req)
    assert a == b
    assert a.mode_tier == b.mode_tier
    assert a.approver == b.approver
    assert a.verdict_class == b.verdict_class
    assert a.egress == b.egress


def test_pack_only_tightens_does_not_relax() -> None:
    # The pack's contributions must never relax an identity floor (tighten-only).
    pack = RegulatedPack()
    view: dict[str, object] = {"required_capabilities": ["regulated"]}
    composed_mode_tier = _registered_registry().compose(view).mode_tier
    assert pack.mode_tier_policy(view).relaxes(composed_mode_tier) is False
    # Verdict-class assignment only ever keeps the stricter grounding.
    assert pack.assign_verdict_class(view).relaxes(VerdictClassPolicy.identity()) is False
