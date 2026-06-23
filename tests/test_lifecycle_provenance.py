"""Released-artifact provenance lifecycle — queryable + tamper-evident (T-M6-09).

Exercises ``foundry/lifecycle/provenance.py``: every released artifact traces to
its run + attested gate verdicts + evidence pack + sign-offs + SLSA provenance,
and the trace is tamper-evident (reuses the evidence-pack hash-chain).

* a released artifact traces to a COMPLETE chain (run/gates/evidence/sign-offs);
* TAMPER-EVIDENT: swap / drop a gate verdict or sign-off -> the trace breaks/flags;
* an artifact with an INCOMPLETE provenance is flagged (never trusted as released);
* the maintenance-chain link is carried through; determinism.
"""

from __future__ import annotations

import pytest

from foundry.durability.evidence_pack import build_evidence_pack, pack_evidence_hash
from foundry.lifecycle.provenance import (
    IncompleteProvenanceError,
    ProvenanceRecord,
    assert_released,
    trace,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import (
    GateResult,
    GateStatus,
    JudgmentProvenance,
    VerdictClass,
)
from foundry.schemas.governance import SignOffRecord
from foundry.schemas.security import Material, SLSAProvenance
from foundry.state.seq import Seq

RUN_AGAINST = "d" * 64
RUN_ID = "rel-run-1"
SEQ = Seq(run_id=RUN_ID, emission_epoch=1, superstep_index=5, node_id="release", local=0)


def _gate(
    name: str, *, status: GateStatus = GateStatus.PASS, run_against: str = RUN_AGAINST
) -> GateResult:
    return GateResult(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        runner_attestation="zoneB-sig",
        run_against=run_against,
        results_digest="r" * 64,
        seq=SEQ,
    )


def _artifact(name: str = "rc") -> Artifact:
    return Artifact(kind="release", uri=f"cas://sha256/{name}", produced_by="release", seq=SEQ)


def _slsa() -> SLSAProvenance:
    return SLSAProvenance(
        build_type="dev-build",
        builder_id="dev-trusted-builder-1",
        invocation_uri="cas://invocation/1",
        source_uri="git://repo",
        source_digest="sha256:" + "c" * 64,
        tree_sha=RUN_AGAINST,
        subject_name="rc",
        subject_digest="sha256:" + "a" * 64,
        materials=(Material(uri="git://repo", digest="sha256:" + "b" * 64),),
        seq=SEQ,
    )


def _record(
    *,
    gates: tuple[GateResult, ...] | None = None,
    sign_off_evidence_hash: str | None = None,
    include_sign_off: bool = True,
    slsa: SLSAProvenance | None = None,
    artifact: Artifact | None = None,
    prior_uri: str | None = None,
) -> ProvenanceRecord:
    """Build a full, intact provenance record over a real evidence pack."""
    art = artifact or _artifact()
    gate_results = gates if gates is not None else (_gate("build"), _gate("test"))

    # The evidence PRESENTED to the signer is the pack over the gate results +
    # artifacts WITHOUT any sign-off (what-you-see-is-what-you-sign, gov §6); the
    # signer binds its hash, then the sign-off is folded into the FINAL pack.
    presented = build_evidence_pack(
        run_against=RUN_AGAINST,
        gate_results=gate_results,
        artifacts=(art,),
        sign_offs=(),
        seq=SEQ,
    )
    presented_hash = sign_off_evidence_hash or pack_evidence_hash(presented)

    sign_offs: tuple[SignOffRecord, ...] = ()
    if include_sign_off:
        sign_offs = (
            SignOffRecord(
                subject_artifact_uri=art.uri,
                state_sha=RUN_AGAINST,
                approver="alice@org",
                role="release-manager",
                approved=True,
                server_nonce="nonce-1",
                presented_evidence_hash=presented_hash,
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
        slsa_provenance=slsa,
        prior_uri=prior_uri,
        seq=SEQ,
    )


# ---------------------------------------------------------------------------
# A released artifact traces to a COMPLETE chain
# ---------------------------------------------------------------------------


def test_released_artifact_traces_to_complete_chain():
    record = _record(slsa=_slsa())
    chain = trace(record)
    assert chain.complete
    assert chain.trusted_as_released()
    assert chain.tamper_evident
    assert chain.has_run
    assert chain.has_gate_verdicts
    assert chain.has_evidence_pack
    assert chain.has_sign_offs
    assert chain.has_slsa_provenance
    assert chain.all_gates_pass
    assert chain.flags == ()
    assert chain.subject_uri == record.subject_artifact.uri
    # The fail-closed variant does not raise on a complete chain.
    assert assert_released(record).complete


def test_require_slsa_complete_when_predicate_present():
    record = _record(slsa=_slsa())
    chain = trace(record, require_slsa=True)
    assert chain.complete


# ---------------------------------------------------------------------------
# TAMPER-EVIDENT: swap / drop a gate verdict or sign-off -> the trace breaks
# ---------------------------------------------------------------------------


def test_swapped_gate_verdict_breaks_the_trace():
    """Swapping a recorded gate verdict re-keys its content-addressed reference;
    the evidence-pack hash-chain no longer matches -> tamper flagged."""
    record = _record()
    # Tamper a recorded gate verdict's status (the pack's chain was folded over
    # the ORIGINAL verdict's identity).
    tampered = tuple(
        g.model_copy(update={"status": GateStatus.FAIL}) if g.name == "build" else g
        for g in record.gate_results
    )
    bad = record.model_copy(update={"gate_results": tampered})
    chain = trace(bad)
    assert not chain.complete
    assert not chain.tamper_evident
    assert "evidence_pack_tampered_or_incomplete" in chain.flags
    # ...and the non-PASS verdict is independently flagged.
    assert not chain.all_gates_pass


def test_dropped_gate_verdict_breaks_the_trace():
    record = _record()  # pack folded over (build, test)
    dropped = record.model_copy(update={"gate_results": record.gate_results[:1]})
    chain = trace(dropped)
    assert not chain.complete
    assert not chain.tamper_evident  # the reference list no longer reproduces


def test_dropped_sign_off_flags_incomplete():
    """A released artifact with NO sign-off (Gated release demands one) -> flagged."""
    record = _record(include_sign_off=False)
    chain = trace(record)
    assert not chain.complete
    assert not chain.has_sign_offs
    assert "missing_sign_offs" in chain.flags
    # The pack itself has no sign-off reference, so the chain is incomplete too.
    assert not chain.tamper_evident


def test_sign_off_over_different_evidence_hash_flags():
    """A sign-off whose presented_evidence_hash is NOT this pack's hash did not
    sign over this release (what-you-see-is-what-you-sign, gov §6) -> flagged."""
    record = _record(sign_off_evidence_hash="0" * 64)
    chain = trace(record)
    assert not chain.complete
    assert "sign_off_evidence_hash_mismatch" in chain.flags


# ---------------------------------------------------------------------------
# An artifact with an INCOMPLETE provenance is flagged (never trusted)
# ---------------------------------------------------------------------------


def test_missing_gate_verdicts_flags_incomplete():
    record = _record(gates=())
    chain = trace(record, require_sign_off=False)
    assert not chain.complete
    assert "missing_gate_verdicts" in chain.flags
    assert not chain.trusted_as_released()


def test_gate_verdict_for_wrong_tree_flags():
    """A gate verdict keyed to a DIFFERENT integrated tree -> the release did not
    clear the gates for THIS state -> flagged."""
    record = _record(
        gates=(_gate("build"), _gate("test", run_against="e" * 64)),
    )
    chain = trace(record)
    assert not chain.complete
    assert "gate_verdict_wrong_tree" in chain.flags
    assert not chain.all_gates_pass


def test_non_pass_gate_verdict_flags():
    record = _record(gates=(_gate("build"), _gate("test", status=GateStatus.FAIL)))
    chain = trace(record)
    assert not chain.complete
    assert "gate_verdict_not_pass" in chain.flags


def test_subject_not_in_evidence_pack_flags():
    """A released artifact absent from the pack it claims has no provenance anchor."""
    record = _record()
    other = Artifact(kind="release", uri="cas://sha256/other", produced_by="x", seq=SEQ)
    bad = record.model_copy(update={"subject_artifact": other})
    chain = trace(bad)
    assert not chain.complete
    assert "subject_artifact_not_in_evidence_pack" in chain.flags


def test_require_slsa_flags_when_predicate_absent():
    record = _record(slsa=None)
    chain = trace(record, require_slsa=True)
    assert not chain.complete
    assert "missing_slsa_provenance" in chain.flags
    # Without require_slsa it is complete (SLSA is Autonomous-mode only).
    assert trace(record, require_slsa=False).complete


def test_assert_released_raises_on_incomplete_chain():
    record = _record(gates=())
    with pytest.raises(IncompleteProvenanceError):
        assert_released(record, require_sign_off=False)


# ---------------------------------------------------------------------------
# Maintenance-chain link carried through
# ---------------------------------------------------------------------------


def test_provenance_carries_maintenance_chain_link():
    record = _record(prior_uri="cas://sha256/v1")
    chain = trace(record)
    assert chain.complete
    assert chain.prior_uri == "cas://sha256/v1"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_trace_is_deterministic():
    record = _record(slsa=_slsa())
    assert trace(record) == trace(record)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
