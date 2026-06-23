"""Released-artifact provenance lifecycle — queryable + tamper-evident. (lifecycle.md §5)

Every released artifact MUST trace to its full provenance: the RUN it was built
in, the attested gate VERDICTS that cleared it, the release EVIDENCE PACK, the
human SIGN-OFFS, and the SLSA/in-toto PROVENANCE predicate. The trace is
**tamper-evident** and **fail-closed**: a swapped/dropped gate verdict or
sign-off breaks the trace, and an artifact whose provenance is incomplete is
FLAGGED — never trusted as released (governance.md §6, lifecycle.md §5).

Reuse, not reinvention. The tamper-evidence is the SAME evidence-pack hash-chain
the WORM release pack already provides
(:func:`foundry.durability.evidence_pack.verify_evidence_pack`): a tampered link
re-keys a content-addressed reference and breaks the chain head. We do NOT build
a parallel integrity mechanism — :func:`trace` folds the released artifact's
recorded references through the EXISTING verifier and surfaces the result.

Determinism (L7): a :class:`ProvenanceRecord` is a frozen, content-addressed
record; :func:`trace` is a pure function of the record + the re-presented members
(no clock, no network, no agent code), so an equal record re-traced anywhere
yields an equal :class:`ProvenanceChain`.
"""

from __future__ import annotations

from foundry.durability.evidence_pack import (
    build_evidence_pack,
    pack_evidence_hash,
    verify_evidence_pack,
)

# Runtime imports: these are FIELD types on the frozen pydantic models below, so
# pydantic needs the concrete classes (a TYPE_CHECKING-only import leaves the
# forward refs unresolved at model-build time). The schemas are import-cheap.
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.governance import EvidencePack, SignOffRecord
from foundry.schemas.security import SLSAProvenance
from foundry.state.seq import Seq

__all__ = [
    "IncompleteProvenanceError",
    "ProvenanceChain",
    "ProvenanceRecord",
    "assert_released",
    "trace",
]


class IncompleteProvenanceError(RuntimeError):
    """A released artifact's provenance is broken / incomplete — fail-closed.

    Raised by :func:`assert_released` (the release-path variant that must refuse,
    not branch): a tampered evidence pack, a missing required link, or a
    non-PASS / unbound gate verdict means the artifact is NOT trustworthy as
    released (lifecycle.md §5, governance.md §6).
    """


class ProvenanceRecord(FrozenModel):
    """The full, queryable provenance of ONE released artifact (lifecycle.md §5).

    Links the released ``subject_artifact`` to every grounding the release rested
    on, all keyed to ONE integrated ``run_against`` tree_sha:

    * ``run_id`` — the run the artifact was produced in (causal anchor);
    * ``evidence_pack`` — the WORM-anchored, hash-chained release evidence pack
      (the provenance ANCHOR — it already references the gate results + artifact
      handles + sign-offs, tamper-evidently);
    * ``gate_results`` — the attested gate verdicts that cleared the release;
    * ``sign_offs`` — the approver-of-record human sign-offs;
    * ``slsa_provenance`` — the input-bound SLSA/in-toto provenance predicate.

    Bodies are content-addressed handles (DS9/L4); this record carries the
    members it traces over plus the integrated digest they key to. A maintained
    version's record links its prior released version via ``prior_uri`` (the
    maintenance chain — :mod:`foundry.lifecycle.maintenance`).
    """

    subject_artifact: Artifact  # the released artifact this provenance is for
    run_id: str  # the run the artifact was produced in
    run_against: str  # the integrated tree_sha every member keys to
    evidence_pack: EvidencePack  # the WORM-anchored hash-chained release pack
    gate_results: tuple[GateResult, ...] = ()  # attested verdicts that cleared release
    sign_offs: tuple[SignOffRecord, ...] = ()  # approver-of-record sign-offs
    slsa_provenance: SLSAProvenance | None = None  # input-bound SLSA predicate
    prior_uri: str | None = None  # the prior released version (maintenance chain)
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class ProvenanceChain(FrozenModel):
    """The result of tracing a released artifact's provenance (lifecycle.md §5).

    ``complete`` is the load-bearing bit: True iff the artifact has a FULL,
    INTACT, tamper-evident provenance — the evidence-pack hash-chain re-verifies
    over the re-presented members, every required link is present, and every gate
    verdict is an attested PASS keyed to this integrated tree. A False result
    means the artifact must NOT be trusted as released; ``flags`` names every
    reason (so the caller can surface, not just reject).

    Sub-bits are exposed so a caller can distinguish a TAMPERED pack
    (``tamper_evident is False``) from a MISSING link (``has_*`` False) — both
    flag the artifact, but the remediation differs.
    """

    subject_uri: str  # the artifact whose provenance was traced
    run_against: str  # the integrated tree_sha the trace keyed to
    complete: bool  # True iff full + intact + tamper-evident (trustworthy)
    tamper_evident: bool  # the evidence-pack hash-chain re-verifies
    has_run: bool  # a run anchor is recorded
    has_gate_verdicts: bool  # attested gate verdicts present
    has_evidence_pack: bool  # the release evidence pack is present
    has_sign_offs: bool  # approver-of-record sign-offs present
    has_slsa_provenance: bool  # the SLSA predicate is present
    all_gates_pass: bool  # every recorded gate verdict is PASS (keyed to this tree)
    prior_uri: str | None = None  # the prior released version, if a maintenance chain
    flags: tuple[str, ...] = ()  # the reasons completeness failed (empty iff complete)

    def trusted_as_released(self) -> bool:
        """A released artifact is trustworthy iff its provenance is complete +
        intact + tamper-evident. Fail-closed: any flag ⇒ not trusted."""
        return self.complete


# Stable flag strings — the deciding grounds recorded on the chain.
_F_NO_RUN = "missing_run_anchor"
_F_NO_EVIDENCE = "missing_evidence_pack"
_F_NO_GATES = "missing_gate_verdicts"
_F_NO_SIGN_OFFS = "missing_sign_offs"
_F_NO_SLSA = "missing_slsa_provenance"
_F_TAMPERED = "evidence_pack_tampered_or_incomplete"
_F_GATE_NOT_PASS = "gate_verdict_not_pass"
_F_GATE_WRONG_TREE = "gate_verdict_wrong_tree"
_F_SUBJECT_UNLINKED = "subject_artifact_not_in_evidence_pack"
_F_SIGNOFF_EVIDENCE_MISMATCH = "sign_off_evidence_hash_mismatch"


def trace(
    record: ProvenanceRecord,
    *,
    require_sign_off: bool = True,
    require_slsa: bool = False,
) -> ProvenanceChain:
    """Trace a released artifact's full provenance — tamper-evidently (§5).

    Returns a :class:`ProvenanceChain` reporting whether the artifact has a
    COMPLETE, INTACT provenance: the run anchor, the attested gate verdicts, the
    evidence pack, the sign-offs, and (optionally) the SLSA predicate, with the
    evidence-pack hash-chain RE-VERIFIED over the re-presented members
    (tamper-evidence — reuses
    :func:`foundry.durability.evidence_pack.verify_evidence_pack`). Fail-closed:
    a tampered/incomplete pack, a missing required link, a non-PASS gate verdict,
    a gate bound to a DIFFERENT tree, or a sign-off whose presented-evidence hash
    does not match the pack ⇒ ``complete=False`` with the reasons in ``flags`` —
    the artifact is NOT trusted as released.

    ``require_sign_off`` (default True) demands at least one approver-of-record
    sign-off (a Gated release). ``require_slsa`` (default False) additionally
    demands the SLSA predicate (Autonomous-mode / SLSA-L3 release).
    """
    flags: list[str] = []
    pack = record.evidence_pack

    has_run = bool(record.run_id)
    has_evidence_pack = bool(pack.pack_uri) and bool(pack.chain_head)
    has_gate_verdicts = bool(record.gate_results)
    has_sign_offs = bool(record.sign_offs)
    has_slsa = record.slsa_provenance is not None

    if not has_run:
        flags.append(_F_NO_RUN)
    if not has_evidence_pack:
        flags.append(_F_NO_EVIDENCE)
    if not has_gate_verdicts:
        flags.append(_F_NO_GATES)
    if require_sign_off and not has_sign_offs:
        flags.append(_F_NO_SIGN_OFFS)
    if require_slsa and not has_slsa:
        flags.append(_F_NO_SLSA)

    # Tamper-evidence: re-verify the evidence-pack hash-chain over the EXACT
    # re-presented members. A swapped/dropped gate verdict or sign-off re-keys a
    # content-addressed reference and breaks the chain head -> False (reuses the
    # WORM-chain verifier verbatim; we never re-implement the chain check).
    tamper_evident = verify_evidence_pack(
        pack,
        gate_results=record.gate_results,
        artifacts=(record.subject_artifact,),
        sign_offs=record.sign_offs,
        require_sign_off=require_sign_off,
    )
    if not tamper_evident:
        flags.append(_F_TAMPERED)

    # The released subject MUST be one of the artifacts the pack references (the
    # pack is what the human signed over — an artifact absent from it has no
    # provenance ANCHOR even if the chain is internally intact).
    if record.subject_artifact.uri not in pack.artifact_uris:
        flags.append(_F_SUBJECT_UNLINKED)

    # Every recorded gate verdict must be an attested PASS keyed to THIS
    # integrated tree — a non-PASS or a verdict bound to a different tree means
    # the release did not actually clear the gates for this state.
    all_gates_pass = has_gate_verdicts
    for g in record.gate_results:
        if g.run_against != record.run_against:
            flags.append(_F_GATE_WRONG_TREE)
            all_gates_pass = False
            break
        if g.status is not GateStatus.PASS:
            flags.append(_F_GATE_NOT_PASS)
            all_gates_pass = False
            break

    # Every sign-off must have signed over the evidence pack AS PRESENTED — the
    # what-you-see-is-what-you-sign binding (gov §6). A human signs over the
    # evidence BEFORE the sign-off itself is folded into the final pack, so the
    # presented hash is the pack assembled over the SAME gate results + artifacts
    # WITHOUT the sign-off references (rebuilt deterministically here, keyed to
    # the same run_against/seq). A sign-off binding a different hash did not sign
    # over THIS release's evidence.
    presented = build_evidence_pack(
        run_against=pack.run_against,
        gate_results=record.gate_results,
        artifacts=(record.subject_artifact,),
        sign_offs=(),
        seq=pack.seq,
        scanner_config_rev=pack.scanner_config_rev,
        provenance_predicate_uri=pack.provenance_predicate_uri,
        version=pack.version,
        supersedes=pack.supersedes,
    )
    expected_evidence_hash = pack_evidence_hash(presented)
    for s in record.sign_offs:
        if s.presented_evidence_hash != expected_evidence_hash:
            flags.append(_F_SIGNOFF_EVIDENCE_MISMATCH)
            break

    complete = not flags
    return ProvenanceChain(
        subject_uri=record.subject_artifact.uri,
        run_against=record.run_against,
        complete=complete,
        tamper_evident=tamper_evident,
        has_run=has_run,
        has_gate_verdicts=has_gate_verdicts,
        has_evidence_pack=has_evidence_pack,
        has_sign_offs=has_sign_offs,
        has_slsa_provenance=has_slsa,
        all_gates_pass=all_gates_pass,
        prior_uri=record.prior_uri,
        flags=tuple(flags),
    )


def assert_released(
    record: ProvenanceRecord,
    *,
    require_sign_off: bool = True,
    require_slsa: bool = False,
) -> ProvenanceChain:
    """Fail-closed trace: raise :class:`IncompleteProvenanceError` if the
    released artifact's provenance is not complete + intact + tamper-evident
    (for the release/admission path that must REFUSE, not branch — §5)."""
    chain = trace(record, require_sign_off=require_sign_off, require_slsa=require_slsa)
    if not chain.complete:
        raise IncompleteProvenanceError(
            f"released artifact {chain.subject_uri!r} has a broken/incomplete "
            f"provenance chain {chain.flags!r} — fail-closed, never trusted as "
            "released (lifecycle.md §5, governance.md §6)"
        )
    return chain
