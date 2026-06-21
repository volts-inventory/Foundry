"""Tests for the WORM-backed release evidence pack.

Contract under test (governance.md §4–§6):

* build over a set of attested gate results + released artifacts + sign-offs,
  keyed to ONE integrated tree_sha;
* verify PASSES on an intact pack;
* verify FAILS if a gate result is swapped / removed, or a sign-off is missing
  (the hash-chain breaks) — tamper-evident, reusing the WORM chain;
* the chain head + pack_uri are deterministic (L7).
"""

from __future__ import annotations

import pytest

from foundry.canonical import canonical_bytes
from foundry.durability.evidence_pack import (
    EvidencePackError,
    assert_evidence_pack,
    build_evidence_pack,
    chain_over,
    pack_evidence_hash,
    verify_evidence_pack,
)
from foundry.durability.outbox import _GENESIS, _chain_hash
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.governance import SignOffRecord
from foundry.state.seq import Seq

RUN_AGAINST = "d" * 40
SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=5, node_id="release", local=0)


def _gate(name: str, status: GateStatus = GateStatus.PASS) -> GateResult:
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


def _artifact(name: str) -> Artifact:
    return Artifact(kind="feature_diff", uri=f"cas://sha256/{name}", produced_by="senior", seq=SEQ)


def _sign_off() -> SignOffRecord:
    return SignOffRecord(
        subject_artifact_uri="cas://artifact/rc",
        state_sha=RUN_AGAINST,
        approver="alice@org",
        role="release-manager",
        approved=True,
        server_nonce="nonce-1",
        presented_evidence_hash="e" * 64,
        judgment_provenance=JudgmentProvenance.SELF,
        seq=SEQ,
    )


def _full_pack():
    gates = (_gate("build"), _gate("test"))
    artifacts = (_artifact("aa"), _artifact("bb"))
    sign_offs = (_sign_off(),)
    pack = build_evidence_pack(
        run_against=RUN_AGAINST,
        gate_results=gates,
        artifacts=artifacts,
        sign_offs=sign_offs,
        seq=SEQ,
    )
    return pack, gates, artifacts, sign_offs


# -- build + verify intact ---------------------------------------------------


def test_build_and_verify_intact() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    assert pack.run_against == RUN_AGAINST
    assert pack.chain_head and pack.chain_head != _GENESIS
    assert pack.pack_uri.startswith("cas://sha256/")
    assert len(pack.gate_result_uris) == 2
    assert len(pack.sign_off_uris) == 1
    assert verify_evidence_pack(
        pack, gate_results=gates, artifacts=artifacts, sign_offs=sign_offs
    )
    # raising variant does not raise on an intact pack
    assert_evidence_pack(pack, gate_results=gates, artifacts=artifacts, sign_offs=sign_offs)


def test_chain_reuses_worm_link_function() -> None:
    """chain_over folds references through the SAME _chain_hash / _GENESIS the
    outbox WORM audit chain uses (reuse, not reinvention)."""
    head = chain_over(("u0", "u1"))
    # Reproduce by hand with the outbox primitive.
    manual = _GENESIS
    for i, ref in enumerate(("u0", "u1")):
        manual = _chain_hash(
            manual, canonical_bytes({"index": i, "ref": ref}).decode("utf-8")
        )
    assert head == manual


# -- determinism (L7) --------------------------------------------------------


def test_determinism() -> None:
    pack_a, *_ = _full_pack()
    pack_b, *_ = _full_pack()
    assert pack_a.chain_head == pack_b.chain_head
    assert pack_a.pack_uri == pack_b.pack_uri
    assert pack_evidence_hash(pack_a) == pack_evidence_hash(pack_b)


# -- tamper detection: gate result swapped / removed -------------------------


def test_swapped_gate_result_breaks_verification() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    # A changed gate result (FAIL instead of PASS) re-keys its content identity.
    tampered = (_gate("build", status=GateStatus.FAIL), gates[1])
    assert not verify_evidence_pack(
        pack, gate_results=tampered, artifacts=artifacts, sign_offs=sign_offs
    )
    with pytest.raises(EvidencePackError):
        assert_evidence_pack(
            pack, gate_results=tampered, artifacts=artifacts, sign_offs=sign_offs
        )


def test_removed_gate_result_breaks_verification() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    assert not verify_evidence_pack(
        pack, gate_results=(gates[0],), artifacts=artifacts, sign_offs=sign_offs
    )


def test_reordered_gate_results_break_verification() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    assert not verify_evidence_pack(
        pack, gate_results=(gates[1], gates[0]), artifacts=artifacts, sign_offs=sign_offs
    )


def test_swapped_artifact_breaks_verification() -> None:
    pack, gates, _artifacts, sign_offs = _full_pack()
    tampered = (_artifact("aa"), _artifact("ZZ"))
    assert not verify_evidence_pack(
        pack, gate_results=gates, artifacts=tampered, sign_offs=sign_offs
    )


# -- tamper detection: missing sign-off --------------------------------------


def test_missing_sign_off_fails_verification() -> None:
    """A pack assembled with NO sign-off fails fail-closed verification."""
    gates = (_gate("build"),)
    pack = build_evidence_pack(run_against=RUN_AGAINST, gate_results=gates, seq=SEQ)
    assert not verify_evidence_pack(pack, gate_results=gates, require_sign_off=True)
    with pytest.raises(EvidencePackError):
        assert_evidence_pack(pack, gate_results=gates, require_sign_off=True)


def test_supplied_sign_off_missing_against_recorded_ref_fails() -> None:
    pack, gates, artifacts, _sign_offs = _full_pack()
    # Recorded a sign-off ref, but verification supplies none → mismatch.
    assert not verify_evidence_pack(
        pack, gate_results=gates, artifacts=artifacts, sign_offs=()
    )


def test_swapped_sign_off_breaks_verification() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    other = sign_offs[0].model_copy(update={"server_nonce": "different-nonce"})
    assert not verify_evidence_pack(
        pack, gate_results=gates, artifacts=artifacts, sign_offs=(other,)
    )


# -- tamper detection: mutated chain head / pack_uri -------------------------


def test_mutated_chain_head_fails() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    tampered = pack.model_copy(update={"chain_head": "0" * 64})
    assert not verify_evidence_pack(
        tampered, gate_results=gates, artifacts=artifacts, sign_offs=sign_offs
    )


def test_mutated_pack_uri_fails() -> None:
    pack, gates, artifacts, sign_offs = _full_pack()
    tampered = pack.model_copy(update={"pack_uri": "cas://sha256/forged"})
    assert not verify_evidence_pack(
        tampered, gate_results=gates, artifacts=artifacts, sign_offs=sign_offs
    )


# -- the sign-off binds the pack hash (what-you-see-is-what-you-sign) ---------


def test_sign_off_can_bind_pack_evidence_hash() -> None:
    pack, *_ = _full_pack()
    digest = pack_evidence_hash(pack)
    so = SignOffRecord(
        subject_artifact_uri="cas://artifact/rc",
        state_sha=RUN_AGAINST,
        approver="a",
        role="r",
        approved=True,
        server_nonce="n",
        presented_evidence_hash=digest,
        judgment_provenance=JudgmentProvenance.SELF,
        seq=SEQ,
    )
    assert so.presented_evidence_hash == pack_evidence_hash(pack)
