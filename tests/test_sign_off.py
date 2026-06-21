"""Tests for HITL sign-off at the Design / Release seams.

Contract under test (governance.md §1/§5/§6, plan §0.2):

* In the self-judge profile the sign-off RECORD IS ALWAYS MADE, carrying
  `judgment_provenance = SELF` (the no-op HITL path does not suppress it) —
  the load-bearing invariant.
* The record binds the four what-you-sign anchors: subject, state_sha,
  presented_evidence_hash, server_nonce.
* A missing sign-off FAILS the release path (fail-closed: no record ⇒ not
  released).
* The record is tamper-evident (content-addressed) — any bound-field change
  re-keys its content_identity.
"""

from __future__ import annotations

import pytest

from foundry.governance.human_liaison import HumanLiaison
from foundry.governance.sign_off import (
    SignOffError,
    SignOffPoint,
    provenance_for_profile,
    record_design_sign_off,
    record_release_sign_off,
    record_sign_off,
    release_allowed,
    require_release_sign_off,
)
from foundry.schemas.gates import JudgmentProvenance
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=4, node_id="release", local=0)

_ANCHORS = {
    "subject_artifact_uri": "cas://artifact/release-candidate",
    "state_sha": "a" * 40,
    "presented_evidence_hash": "b" * 64,
    "server_nonce": "nonce-xyz",
}


def _release(profile: str, *, liaison: HumanLiaison | None = None, approved: bool = True):
    return record_release_sign_off(
        approver="alice@org",
        role="release-manager",
        profile=profile,
        seq=SEQ,
        approved=approved,
        liaison=liaison,
        **_ANCHORS,
    )


# -- always-recorded / self-judge invariant ---------------------------------


def test_self_judge_record_is_always_made_with_self_provenance() -> None:
    liaison = HumanLiaison(self_judge=True)  # zero-HITL profile
    record = _release("self_judge", liaison=liaison)
    assert record is not None
    assert record.judgment_provenance is JudgmentProvenance.SELF
    # The record is made REGARDLESS of the no-op HITL delivery path.
    assert record.approved is True


def test_design_sign_off_also_self_stamped_in_self_judge() -> None:
    record = record_design_sign_off(
        approver="bob@org",
        role="architect-approver",
        profile="self_judge",
        seq=SEQ,
        **_ANCHORS,
    )
    assert record.judgment_provenance is JudgmentProvenance.SELF
    assert record.meaning_of_signing == "design sign-off"


def test_decorrelated_profile_stamps_independent() -> None:
    record = _release("decorrelated")
    assert record.judgment_provenance is JudgmentProvenance.INDEPENDENT


def test_provenance_for_profile() -> None:
    assert provenance_for_profile("self_judge") is JudgmentProvenance.SELF
    assert provenance_for_profile("decorrelated") is JudgmentProvenance.INDEPENDENT
    assert provenance_for_profile("anything-else") is JudgmentProvenance.INDEPENDENT


def test_no_op_liaison_does_not_suppress_record() -> None:
    """Even with no liaison wired at all, the record is still produced."""
    record = _release("self_judge", liaison=None)
    assert record.judgment_provenance is JudgmentProvenance.SELF


# -- binding the anchors -----------------------------------------------------


def test_binds_all_four_anchors() -> None:
    record = _release("self_judge")
    assert record.subject_artifact_uri == _ANCHORS["subject_artifact_uri"]
    assert record.state_sha == _ANCHORS["state_sha"]
    assert record.presented_evidence_hash == _ANCHORS["presented_evidence_hash"]
    assert record.server_nonce == _ANCHORS["server_nonce"]


def test_unified_record_sign_off_point_stamped() -> None:
    record = record_sign_off(
        SignOffPoint.RELEASE,
        approver="a",
        role="r",
        profile="self_judge",
        seq=SEQ,
        **_ANCHORS,
    )
    assert record.meaning_of_signing == "release sign-off"


# -- fail-closed release path ------------------------------------------------


def test_missing_sign_off_fails_release_closed() -> None:
    assert release_allowed(None, **_ANCHORS) is False
    with pytest.raises(SignOffError):
        require_release_sign_off(None, **_ANCHORS)


def test_present_matching_sign_off_allows_release() -> None:
    record = _release("self_judge")
    assert release_allowed(record, **_ANCHORS) is True
    assert require_release_sign_off(record, **_ANCHORS) is record


def test_not_approved_blocks_release() -> None:
    record = _release("self_judge", approved=False)
    assert release_allowed(record, **_ANCHORS) is False


@pytest.mark.parametrize(
    "field",
    ["subject_artifact_uri", "state_sha", "presented_evidence_hash", "server_nonce"],
)
def test_anchor_mismatch_blocks_release(field: str) -> None:
    """A sign-off bound to a DIFFERENT anchor cannot advance THIS release."""
    record = _release("self_judge")
    bad = dict(_ANCHORS)
    bad[field] = "tampered-" + str(bad[field])
    assert release_allowed(record, **bad) is False
    with pytest.raises(SignOffError):
        require_release_sign_off(record, **bad)


# -- tamper-evidence (content-addressed) -------------------------------------


def test_record_is_content_addressed_and_tamper_evident() -> None:
    record = _release("self_judge")
    identity = record.content_identity()
    # Re-deriving the same record yields the same identity (determinism).
    assert _release("self_judge").content_identity() == identity
    # Any bound-field change re-keys the content identity.
    flipped = record.model_copy(update={"server_nonce": "different-nonce"})
    assert flipped.content_identity() != identity
    flipped2 = record.model_copy(update={"presented_evidence_hash": "c" * 64})
    assert flipped2.content_identity() != identity
    # The provenance stamp is part of identity too (can't be silently flipped).
    flipped3 = record.model_copy(
        update={"judgment_provenance": JudgmentProvenance.INDEPENDENT}
    )
    assert flipped3.content_identity() != identity
