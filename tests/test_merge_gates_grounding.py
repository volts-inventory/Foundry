"""Gate-grounding suite (implementation-plan.md §9): merge_gates trust checks.

Folds untrusted/trusted GateResult writes through the real reducer with the
dev-grade HMAC verifier (foundry/gates/attestation.py) backed by an in-memory
GlobalRecords store. Covers: unsigned/tampered attestation, forged or
verdict-flipped judgment bindings, nonce replay vs idempotent re-fold, the
negative-verdict latch + release, Seq re-execution supersession, the
fail-closed no-verifier default, ERROR-status folding, and the memoization
split (signatures cached; nonce/latch reads consulted every fold).
"""

from __future__ import annotations

import contextvars

import pytest

from foundry.gates import attestation
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-test-key"
KEY_ID = "dev-key-1"


# ---- builders ----------------------------------------------------------------


def make_seq(
    node_id: str = "test_runner",
    *,
    epoch: int = 0,
    step: int = 1,
    local: int = 0,
    run_id: str = "run-1",
) -> Seq:
    return Seq(
        run_id=run_id,
        emission_epoch=epoch,
        superstep_index=step,
        node_id=node_id,
        local=local,
    )


def make_det_gate(
    name: str = "test",
    status: GateStatus = GateStatus.PASS,
    *,
    seq: Seq | None = None,
    **over: object,
) -> GateResult:
    """Deterministic-gate verdict shell (unsigned until zone B attests it)."""
    fields: dict[str, object] = dict(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        scanner_config_rev="scanrev-1",
        lineage_map_revision="lineage-1",
        results_digest="resdigest-1",
        run_against="task/T1",
        seq=seq or make_seq(),
    )
    fields.update(over)
    return GateResult(**fields)


def make_review(
    status: GateStatus = GateStatus.PASS,
    *,
    nonce: str | None = "nonce-1",
    seq: Seq | None = None,
    **over: object,
) -> GateResult:
    """Judgment verdict shell (no binding until the proxy stamps it)."""
    fields: dict[str, object] = dict(
        name="review",
        status=status,
        decision_deterministic=False,
        execution_deterministic=False,
        verdict_class=VerdictClass.PERCEPTUAL,
        verdict_nonce=nonce,
        served_family=frozenset({"family-a"}),
        subject_artifact_uri="cas://artifact/abc",
        lineage_map_revision="lineage-1",
        run_against="statesha-xyz",
        seq=seq or make_seq("review_node"),
    )
    fields.update(over)
    return GateResult(**fields)


class CountingRecords:
    """GlobalRecordsView proxy that counts the mutable committed reads."""

    def __init__(self, inner: GlobalRecords) -> None:
        self._inner = inner
        self.nonce_reads = 0
        self.latch_reads = 0

    def nonce_consumed_by_other(self, nonce: str, content_identity: str) -> bool:
        self.nonce_reads += 1
        return self._inner.nonce_consumed_by_other(nonce, content_identity)

    def negative_verdict_latched(self, key: tuple[str, str, str]) -> bool:
        self.latch_reads += 1
        return self._inner.negative_verdict_latched(key)

    def stable_fail_latched(self, digest: str, gate: str) -> bool:
        return self._inner.stable_fail_latched(digest, gate)


# ---- fixtures ------------------------------------------------------------------


@pytest.fixture
def signer() -> ZoneBSigner:
    return ZoneBSigner(KEY, KEY_ID)


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def verifier(records: GlobalRecords) -> DevGateWriteVerifier:
    return DevGateWriteVerifier(KEY, KEY_ID, records)


@pytest.fixture
def installed(verifier: DevGateWriteVerifier):
    """Install the verifier in this test's context, reset on teardown so the
    fail-closed-default test never sees a leaked verifier."""
    token = set_gate_write_verifier(verifier)
    try:
        yield verifier
    finally:
        token.var.reset(token)


# ---- 1. unsigned deterministic write dropped; signed kept ----------------------


def test_unsigned_deterministic_gate_dropped_signed_kept(signer, installed):
    unsigned = make_det_gate(status=GateStatus.PASS, seq=make_seq(step=2))
    signed = signer.with_attestation(make_det_gate(status=GateStatus.FAIL, seq=make_seq(step=1)))

    # The unsigned agent write alone is dropped — it cannot wedge the channel.
    assert merge_gates([unsigned], None) == []
    # Folded together (either side), only the runner-attested verdict survives,
    # even though the unsigned write carries a HIGHER seq.
    assert merge_gates([unsigned], [signed]) == [signed]
    assert merge_gates([signed], [unsigned]) == [signed]


# ---- 2. tampered attestation (status flip after signing) -----------------------


def test_tampered_attestation_rejected(signer, installed):
    genuine = signer.with_attestation(make_det_gate(status=GateStatus.PASS))
    flipped = genuine.model_copy(update={"status": GateStatus.FAIL})

    assert merge_gates([genuine], None) == [genuine]
    # Signature binds STATUS: the flipped copy fails verification.
    assert merge_gates([flipped], None) == []
    assert merge_gates([genuine, flipped], None) == [genuine]


# ---- 3. judgment verdict requires a valid binding ------------------------------


def test_judgment_verdict_requires_binding(signer, installed):
    unbound = make_review()
    assert merge_gates([unbound], None) == []

    bound = signer.with_judgment_binding(make_review())  # fresh nonce, never consumed
    assert merge_gates([bound], None) == [bound]


# ---- 4. verdict-FLIP replay of a genuine binding -------------------------------


def test_verdict_flip_replay_rejected(signer, installed):
    approve = signer.with_judgment_binding(make_review(status=GateStatus.PASS))
    # Adversary copies the genuine approve binding onto a flipped verdict.
    flipped = approve.model_copy(update={"status": GateStatus.FAIL})

    assert merge_gates([flipped], None) == []
    assert merge_gates([approve, flipped], None) == [approve]


# ---- 5. nonce replay vs idempotent re-fold --------------------------------------


def test_nonce_replay_dropped_but_refold_idempotent(signer, records, installed):
    a = signer.with_judgment_binding(make_review(status=GateStatus.PASS, nonce="N"))
    assert merge_gates([a], None) == [a]
    records.consume_nonce("N", a.content_identity())

    # A DIFFERENT verdict (status flipped, genuinely re-signed) re-using N:
    b = signer.with_judgment_binding(
        make_review(status=GateStatus.FAIL, nonce="N", seq=make_seq("review_node", step=9))
    )
    assert b.content_identity() != a.content_identity()
    assert merge_gates([b], None) == []

    # Re-folding A itself (resume) is idempotent — same content identity.
    assert merge_gates([a], None) == [a]
    assert merge_gates([a], [b]) == [a]


# ---- 6. negative-verdict latch + nonce-bound release ---------------------------


def test_negative_verdict_latch_blocks_older_approve_until_released(
    signer, records, installed
):
    request_changes = signer.with_judgment_binding(
        make_review(status=GateStatus.FAIL, nonce="n-fail", seq=make_seq("review_node", step=5))
    )
    older_approve = signer.with_judgment_binding(
        make_review(status=GateStatus.PASS, nonce="n-old", seq=make_seq("review_node", step=3))
    )
    assert older_approve.latch_key() == request_changes.latch_key()
    assert older_approve.seq < request_changes.seq

    records.latch_negative_verdict(request_changes.latch_key())
    # Latched: re-emitting the OLDER approve cannot supersede the rejection.
    assert merge_gates([older_approve], None) == []

    # Release only via a fresh server-issued nonce bound to a genuine re-review.
    records.release_negative_verdict(request_changes.latch_key(), "n-fresh")
    fresh_approve = signer.with_judgment_binding(
        make_review(status=GateStatus.PASS, nonce="n-fresh", seq=make_seq("review_node", step=8))
    )
    assert merge_gates([fresh_approve], None) == [fresh_approve]


# ---- 7. re-execution outranks stale (emission_epoch ahead of superstep_index) --


def test_reexecution_outranks_stale_record(signer, installed):
    stale = signer.with_attestation(
        make_det_gate(status=GateStatus.FAIL, seq=make_seq("test_runner", epoch=0, step=4))
    )
    # Re-executed node keeps its langgraph step; the bumped epoch must win.
    rerun = signer.with_attestation(
        make_det_gate(status=GateStatus.PASS, seq=make_seq("test_runner", epoch=1, step=4))
    )
    assert rerun.seq > stale.seq  # epoch ordered AHEAD of superstep_index
    assert merge_gates([stale], [rerun]) == [rerun]
    assert merge_gates([rerun], [stale]) == [rerun]


# ---- 8. fail-closed default: no verifier configured -----------------------------


def test_fail_closed_without_verifier(signer):
    signed_det = signer.with_attestation(make_det_gate())
    bound_review = signer.with_judgment_binding(make_review())

    # Run in a FRESH (empty) context so no verifier can leak in from other tests.
    out = contextvars.Context().run(merge_gates, [signed_det, bound_review], None)
    assert out == []


# ---- 9. ERROR is not a verdict but folds by the same rules -----------------------


def test_error_status_folds_by_same_rules(signer, installed):
    earlier_pass = signer.with_attestation(
        make_det_gate(status=GateStatus.PASS, seq=make_seq(step=2))
    )
    signed_error = signer.with_attestation(
        make_det_gate(status=GateStatus.ERROR, seq=make_seq(step=6))
    )
    unsigned_error = make_det_gate(status=GateStatus.ERROR, seq=make_seq(step=7))

    # Signed error record is kept as latest; an unsigned error is still dropped.
    assert merge_gates([earlier_pass, unsigned_error], [signed_error]) == [signed_error]


# ---- 10. memoization split: signatures cached, nonce/latch read every fold -------


def test_signature_memoized_but_records_read_every_fold(signer, records, monkeypatch):
    counting = CountingRecords(records)
    verifier = DevGateWriteVerifier(KEY, KEY_ID, counting)

    # Sign BEFORE installing the spy — the signer legitimately computes HMACs.
    det = signer.with_attestation(make_det_gate())
    review = signer.with_judgment_binding(make_review())

    calls = {"n": 0}
    real_mac = attestation._mac

    def spy(key: bytes, payload: bytes) -> str:
        calls["n"] += 1
        return real_mac(key, payload)

    monkeypatch.setattr(attestation, "_mac", spy)

    token = set_gate_write_verifier(verifier)
    try:
        first = merge_gates([det, review], None)
        assert set(first) == {det, review}
        after_first_fold = calls["n"]
        assert after_first_fold == 2  # one HMAC per record: attestation + binding
        assert counting.nonce_reads == 1
        assert counting.latch_reads == 1

        second = merge_gates([det, review], None)
        assert set(second) == {det, review}
        # Pure-in-content signature checks were served from the memo cache...
        assert calls["n"] == after_first_fold
        # ...while the MUTABLE committed reads were consulted again.
        assert counting.nonce_reads == 2
        assert counting.latch_reads == 2
    finally:
        token.var.reset(token)
