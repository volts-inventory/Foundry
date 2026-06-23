"""Verdict-class gating machinery (T-M5-02, domains.md §1).

Contract under test — the core machinery a pack's ``assign_verdict_class`` hook
drives (the packs T-M5-05/06/07 ASSIGN classes via the contract; THIS enforces
how each class is gated):

* Each class 1-5 maps to the right :class:`GateRequirement` (machine /
  alpha+state_sha / approver / perceptual-HITL).
* class 3 (statistical) is FAIL-CLOSED unless it binds a frozen alpha look
  (``(eval_set_identity, decision_id)`` ledger) + a ``state_sha``.
* class 4 (human-witnessed) requires a recorded approver-of-record; Autonomous is
  UNREACHABLE (a missing record blocks, never auto-approves).
* class 5 (perceptual) requires a HITL sign-off over the rendered-evidence
  bundle; never auto-approved.
* a HIGHER class can NEVER be satisfied by a LOWER-class gate (fail-closed).
* determinism: the requirement is a pure function of the class.
"""

from __future__ import annotations

import pytest

from foundry.domains.verdict_class import (
    Class3Binding,
    GateRequirement,
    GroundingKind,
    InvalidClass3BindingError,
    assert_class3_binds_ledger,
    autonomous_reachable,
    class4_attested,
    class5_hitl_satisfied,
    gate_requirements_for,
    satisfies,
)
from foundry.governance.sign_off import record_release_sign_off
from foundry.schemas.gates import VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq

# --- per-class requirements --------------------------------------------------


def _seq() -> Seq:
    return Seq("run", 1, 0, "node", 0)


def test_class1_and_2_are_machine_grounded_passthrough() -> None:
    for vc in (VerdictClass.CONFIG_EVALUABLE, VerdictClass.EXECUTION_DETERMINISTIC):
        req = gate_requirements_for(vc)
        assert req.grounding is GroundingKind.MACHINE
        assert req.requires_alpha_state_sha is False
        assert req.requires_approver is False
        assert req.requires_hitl is False
        assert req.auto_approvable is True
        assert req.in_signed_predicate is True


def test_class3_requires_alpha_and_state_sha() -> None:
    req = gate_requirements_for(VerdictClass.STATISTICAL)
    assert req.grounding is GroundingKind.ALPHA_STATE_SHA
    assert req.requires_alpha_state_sha is True
    assert req.requires_approver is False
    assert req.in_signed_predicate is True


def test_class4_requires_approver_and_is_not_auto_approvable() -> None:
    req = gate_requirements_for(VerdictClass.HUMAN_WITNESSED)
    assert req.grounding is GroundingKind.APPROVER_OF_RECORD
    assert req.requires_approver is True
    assert req.requires_hitl is True
    assert req.auto_approvable is False  # Autonomous unreachable
    assert req.in_signed_predicate is True


def test_class5_requires_perceptual_hitl_and_is_excluded_from_predicate() -> None:
    req = gate_requirements_for(VerdictClass.PERCEPTUAL)
    assert req.grounding is GroundingKind.PERCEPTUAL_HITL
    assert req.requires_hitl is True
    assert req.auto_approvable is False  # never auto-approved
    assert req.in_signed_predicate is False  # advisory model + human (§1 table)


def test_autonomous_unreachable_only_for_class_4_and_5() -> None:
    assert autonomous_reachable(VerdictClass.CONFIG_EVALUABLE) is True
    assert autonomous_reachable(VerdictClass.EXECUTION_DETERMINISTIC) is True
    assert autonomous_reachable(VerdictClass.STATISTICAL) is True
    assert autonomous_reachable(VerdictClass.HUMAN_WITNESSED) is False
    assert autonomous_reachable(VerdictClass.PERCEPTUAL) is False


def test_every_class_maps_to_a_requirement() -> None:
    for vc in VerdictClass:
        assert isinstance(gate_requirements_for(vc), GateRequirement)


# --- class 3: fail-closed unless it binds a frozen alpha look + state_sha ------


def _draw_look(ledger: GlobalRecords, *, eval_set: str, decision: str) -> None:
    assert ledger.try_first_alpha_look(eval_set, decision, "state-abc", "run-1") is True


def test_class3_valid_when_alpha_look_drawn_and_state_sha_present() -> None:
    ledger = GlobalRecords()
    _draw_look(ledger, eval_set="eval:v1", decision="dec:1")
    binding = Class3Binding(
        eval_set_identity="eval:v1", decision_id="dec:1", state_sha="state-abc"
    )
    assert assert_class3_binds_ledger(binding, ledger) is binding


def test_class3_fail_closed_when_no_binding() -> None:
    ledger = GlobalRecords()
    with pytest.raises(InvalidClass3BindingError, match="no alpha-look binding"):
        assert_class3_binds_ledger(None, ledger)


def test_class3_fail_closed_when_alpha_look_never_drawn() -> None:
    ledger = GlobalRecords()  # nothing drawn
    binding = Class3Binding(
        eval_set_identity="eval:v1", decision_id="dec:1", state_sha="state-abc"
    )
    with pytest.raises(InvalidClass3BindingError, match="UNDRAWN"):
        assert_class3_binds_ledger(binding, ledger)


def test_class3_fail_closed_when_missing_state_sha() -> None:
    ledger = GlobalRecords()
    _draw_look(ledger, eval_set="eval:v1", decision="dec:1")
    binding = Class3Binding(eval_set_identity="eval:v1", decision_id="dec:1", state_sha="")
    with pytest.raises(InvalidClass3BindingError, match="state_sha"):
        assert_class3_binds_ledger(binding, ledger)


def test_class3_fail_closed_when_missing_ledger_key() -> None:
    ledger = GlobalRecords()
    binding = Class3Binding(eval_set_identity="", decision_id="dec:1", state_sha="s")
    with pytest.raises(InvalidClass3BindingError, match="eval_set_identity"):
        assert_class3_binds_ledger(binding, ledger)


def test_class3_binding_is_keyed_on_eval_set_decision_not_state_sha() -> None:
    # The ledger key is (eval_set_identity, decision_id); a DIFFERENT decision_id
    # against the same eval set is NOT drawn even though the look exists for dec:1.
    ledger = GlobalRecords()
    _draw_look(ledger, eval_set="eval:v1", decision="dec:1")
    other = Class3Binding(
        eval_set_identity="eval:v1", decision_id="dec:2", state_sha="state-abc"
    )
    with pytest.raises(InvalidClass3BindingError, match="UNDRAWN"):
        assert_class3_binds_ledger(other, ledger)


# --- class 4: requires an approver-of-record; Autonomous unreachable ----------

_ANCHORS = {
    "subject_artifact_uri": "cas://design",
    "state_sha": "state-abc",
    "presented_evidence_hash": "evid-hash",
    "server_nonce": "nonce-1",
}


def _approved_record(**overrides: str) -> object:
    anchors = {**_ANCHORS, **overrides}
    return record_release_sign_off(
        **anchors,  # type: ignore[arg-type]
        approver="dr-jane",
        role="validator",
        profile="independent",
        seq=_seq(),
    )


def test_class4_attested_with_matching_recorded_approver() -> None:
    record = _approved_record()
    assert class4_attested(record, **_ANCHORS) is True  # type: ignore[arg-type]


def test_class4_blocked_when_no_record_not_auto_approved() -> None:
    # A missing approver-of-record is a BLOCK, never an auto-approve (§1 class 4).
    assert class4_attested(None, **_ANCHORS) is False  # type: ignore[arg-type]


def test_class4_rejects_record_bound_to_a_different_artifact() -> None:
    record = _approved_record(subject_artifact_uri="cas://OTHER")
    assert class4_attested(record, **_ANCHORS) is False  # type: ignore[arg-type]


def test_class4_rejects_replayed_nonce() -> None:
    record = _approved_record(server_nonce="STALE")
    assert class4_attested(record, **_ANCHORS) is False  # type: ignore[arg-type]


# --- class 5: requires perceptual HITL over the rendered-evidence bundle -------


def test_class5_satisfied_only_by_hitl_signoff_over_the_bundle() -> None:
    record = _approved_record()
    assert class5_hitl_satisfied(record, **_ANCHORS) is True  # type: ignore[arg-type]


def test_class5_never_auto_approved_without_signoff() -> None:
    assert class5_hitl_satisfied(None, **_ANCHORS) is False  # type: ignore[arg-type]


def test_class5_rejects_signoff_over_a_different_bundle() -> None:
    record = _approved_record(presented_evidence_hash="DIFFERENT-BUNDLE")
    assert class5_hitl_satisfied(record, **_ANCHORS) is False  # type: ignore[arg-type]


# --- a higher class can never be satisfied by a lower-class gate (fail-closed) -


def test_higher_class_not_satisfied_by_lower_class_gate() -> None:
    # class 4 required, class 2 machine gate -> NOT satisfied (even if green).
    assert satisfies(VerdictClass.HUMAN_WITNESSED, VerdictClass.EXECUTION_DETERMINISTIC) is False
    # class 3 required, class 1 gate -> NOT satisfied.
    assert satisfies(VerdictClass.STATISTICAL, VerdictClass.CONFIG_EVALUABLE) is False
    # class 5 required, class 3 gate -> NOT satisfied.
    assert satisfies(VerdictClass.PERCEPTUAL, VerdictClass.STATISTICAL) is False


def test_equal_or_stricter_class_satisfies() -> None:
    # equality satisfies
    assert satisfies(VerdictClass.STATISTICAL, VerdictClass.STATISTICAL) is True
    # a stricter gate satisfies a weaker requirement (rerouting only tightens)
    assert satisfies(VerdictClass.EXECUTION_DETERMINISTIC, VerdictClass.HUMAN_WITNESSED) is True
    assert satisfies(VerdictClass.CONFIG_EVALUABLE, VerdictClass.PERCEPTUAL) is True


def test_full_strictness_chain_is_fail_closed_downward() -> None:
    order = [
        VerdictClass.CONFIG_EVALUABLE,
        VerdictClass.EXECUTION_DETERMINISTIC,
        VerdictClass.STATISTICAL,
        VerdictClass.PERCEPTUAL,
        VerdictClass.HUMAN_WITNESSED,
    ]
    for i, required in enumerate(order):
        for j, gate in enumerate(order):
            # a gate satisfies iff it is AT LEAST as strict (j >= i)
            assert satisfies(required, gate) is (j >= i)


# --- determinism -------------------------------------------------------------


def test_requirements_are_deterministic() -> None:
    for vc in VerdictClass:
        assert gate_requirements_for(vc) == gate_requirements_for(vc)


def test_class3_binding_check_is_deterministic() -> None:
    ledger = GlobalRecords()
    _draw_look(ledger, eval_set="eval:v1", decision="dec:1")
    binding = Class3Binding(
        eval_set_identity="eval:v1", decision_id="dec:1", state_sha="state-abc"
    )
    # repeated checks are stable (pure read of the committed ledger view)
    assert assert_class3_binds_ledger(binding, ledger) == binding
    assert assert_class3_binds_ledger(binding, ledger) == binding
