"""Cross-fork digest-disqualification latch + admission_look query.

This is the **fork-divergence** defense (gov §5, plan §2.4): once a build/tree
digest is DISQUALIFIED — because it carried a forged/divergent verdict, or a
fork tried to launder a stable-FAIL'd digest — that digest is permanently
disqualified ACROSS forks. The latch is keyed on the digest (never the
`run_id`), monotonic (never un-disqualifies), and written atomically
insert-if-absent under the lock, so a malicious fork cannot escape it by
minting a fresh `run_id`. The deploy-time admission controller (T-M4-07) reads
admissibility through :meth:`GlobalRecords.admission_look`, which folds
disqualification + stable-FAIL and is fail-closed.
"""

from __future__ import annotations

from foundry.state.global_records import AdmissionState, GlobalRecords

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32


def test_disqualify_then_disqualified() -> None:
    records = GlobalRecords()
    assert records.digest_disqualified(DIGEST) is False
    assert records.disqualify_digest(DIGEST, "forged-verdict", "run-1") is True
    assert records.digest_disqualified(DIGEST) is True


def test_disqualification_survives_a_fork() -> None:
    # A fork mints a NEW run_id but presents the SAME digest -> still disqualified.
    records = GlobalRecords()
    records.disqualify_digest(DIGEST, "stable-fail-launder", "run-parent")
    # The fork: different run_id, same digest.
    assert records.digest_disqualified(DIGEST) is True
    assert records.admission_look(DIGEST) is AdmissionState.DISQUALIFIED
    # A fork re-presenting it to disqualify again is a no-op (already latched).
    assert records.disqualify_digest(DIGEST, "second-try", "run-fork") is False
    assert records.digest_disqualified(DIGEST) is True


def test_disqualification_is_monotonic_cannot_be_cleared() -> None:
    records = GlobalRecords()
    assert records.disqualify_digest(DIGEST, "forged-verdict", "run-1") is True
    # No API un-disqualifies; re-presenting the digest never clears it. The
    # second call collapses to the SAME record (FIRST disqualification wins).
    assert records.disqualify_digest(DIGEST, "benign-relabel", "run-2") is False
    assert records.digest_disqualified(DIGEST) is True
    assert records.admission_look(DIGEST) is AdmissionState.DISQUALIFIED


def test_atomic_disqualify_is_idempotent() -> None:
    # Insert-if-absent: exactly one caller records it; the rest are no-ops.
    records = GlobalRecords()
    first = records.disqualify_digest(DIGEST, "r", "run-a")
    second = records.disqualify_digest(DIGEST, "r", "run-b")
    third = records.disqualify_digest(DIGEST, "r", "run-c")
    assert [first, second, third] == [True, False, False]
    assert records.digest_disqualified(DIGEST) is True


def test_admission_look_admissible_for_clean_digest() -> None:
    records = GlobalRecords()
    assert records.admission_look(DIGEST) is AdmissionState.ADMISSIBLE
    assert records.digest_disqualified(DIGEST) is False


def test_admission_look_disqualified_via_stable_fail_latch() -> None:
    # A stable-FAIL'd digest reads as not-admissible WITHOUT an explicit
    # disqualification — admission_look folds the stable-FAIL latch (any gate).
    records = GlobalRecords()
    records.latch_stable_fail(DIGEST, "build", "run-1")
    assert records.digest_disqualified(DIGEST) is False  # not EXPLICITLY DQ'd
    assert records.admission_look(DIGEST) is AdmissionState.DISQUALIFIED


def test_admission_look_disqualified_via_explicit_disqualification() -> None:
    records = GlobalRecords()
    records.disqualify_digest(DIGEST, "forged-verdict", "run-1")
    assert records.admission_look(DIGEST) is AdmissionState.DISQUALIFIED


def test_disqualification_is_per_digest_scoped() -> None:
    # Disqualifying one digest must not contaminate a different clean digest.
    records = GlobalRecords()
    records.disqualify_digest(DIGEST, "forged-verdict", "run-1")
    assert records.admission_look(OTHER) is AdmissionState.ADMISSIBLE
    assert records.digest_disqualified(OTHER) is False
