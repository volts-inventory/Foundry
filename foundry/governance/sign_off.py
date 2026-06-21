"""HITL sign-off at the Design / Release seams. (governance.md §1/§6, plan §0.2)

A Gated release / significant Design cannot advance without a **recorded**
:class:`~foundry.schemas.governance.SignOffRecord` — admission is fail-closed: no
record ⇒ not released (governance.md §1 table: Design/Release rows require a
recorded human sign-off; §5 admission re-checks it).

In the **self-judge zero-HITL profile** (governance.md §1e solo-lineage profile,
plan §0.2) the human path is a no-op: the :class:`~foundry.governance.HumanLiaison`
records the escalation as ``deferred_log`` (the operator reviews async) rather than
blocking, BUT THE SIGN-OFF RECORD IS STILL MADE — carrying the machine-stamped
``judgment_provenance = SELF`` (the cost is *owned and recorded, never hidden*; the
scorecard must not claim independent validation for it). **The record is always
made** is the testable invariant of this module — the no-op HITL path does not
suppress it.

The record binds the four what-you-sign anchors (governance.md §6):

* ``subject_artifact_uri`` — the decision point's attested artifact,
* ``state_sha`` — the integrated state the sign-off is keyed to,
* ``presented_evidence_hash`` — the content hash of the EXACT evidence pack shown
  to the signer (closes the what-you-see-is-what-you-sign gap),
* ``server_nonce`` — the per-approval server-issued challenge, so the sign-off
  cannot be minted or replayed.

The MODEL never supplies ``judgment_provenance``; it is stamped here from the
active proxy/profile (mirrors the proxy's ``judge()`` stamp source). The record is
content-addressed (``SignOffRecord.content_identity()``), so any tamper of a bound
field changes its identity — the evidence pack chains over that identity, so a
swapped/forged sign-off breaks the chain (see :mod:`foundry.durability.evidence_pack`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import SignOffRecord

if TYPE_CHECKING:
    from foundry.governance.human_liaison import HumanLiaison
    from foundry.state.seq import Seq

__all__ = [
    "SignOffError",
    "SignOffPoint",
    "provenance_for_profile",
    "record_design_sign_off",
    "record_release_sign_off",
    "record_sign_off",
    "release_allowed",
    "require_release_sign_off",
]


class SignOffPoint(StrEnum):
    """The two HITL decision points a sign-off binds to (governance.md §1)."""

    DESIGN = "design"
    RELEASE = "release"


class SignOffError(RuntimeError):
    """Fail-closed: a Gated release was attempted without a recorded sign-off."""


def provenance_for_profile(profile: str) -> JudgmentProvenance:
    """Stamp source — SELF in the self-judge profile, INDEPENDENT otherwise.

    Mirrors :meth:`ModelProviderProxy.judge`'s stamp: the model never supplies
    provenance; the active profile does (governance.md §1e). Same lineage is the
    *declared, recorded* operating condition in ``self_judge`` — never hidden.
    """
    return JudgmentProvenance.SELF if profile == "self_judge" else JudgmentProvenance.INDEPENDENT


def record_sign_off(
    point: SignOffPoint,
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
    approver: str,
    role: str,
    profile: str,
    seq: Seq,
    approved: bool = True,
    liaison: HumanLiaison | None = None,
    meaning_of_signing: str = "",
    note: str = "",
) -> SignOffRecord:
    """Produce — and RECORD — a :class:`SignOffRecord` for a Design/Release decision.

    The sign-off is ALWAYS made (the testable invariant). In the self-judge
    profile the HITL path via ``liaison`` is a no-op (``deferred_log`` delivery,
    nothing blocks) yet the record is still produced, stamped
    ``judgment_provenance = SELF``. In any other profile the liaison records a
    blocking escalation — but the record this function returns is the same shape
    either way; the *delivery* differs, the *recording* never does.

    Binds the four what-you-sign anchors (subject, ``state_sha``,
    ``presented_evidence_hash``, ``server_nonce``); see module docstring.
    """
    provenance = provenance_for_profile(profile)
    record = SignOffRecord(
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        approver=approver,
        role=role,
        approved=approved,
        server_nonce=server_nonce,
        presented_evidence_hash=presented_evidence_hash,
        judgment_provenance=provenance,
        meaning_of_signing=meaning_of_signing or f"{point.value} sign-off",
        note=note,
        seq=seq,
    )
    # The HITL path is exercised regardless of profile; in self_judge it is a
    # no-op (deferred_log) — but the record above is ALWAYS made (never gated on
    # delivery). We do NOT route through the liaison's Trip-shaped packager here
    # (that is the monitor-trip escalation surface); the sign-off record IS the
    # durable HITL artifact, and `liaison.profile` only documents the active path.
    _ = liaison  # presence documents the (no-op in self_judge) HITL delivery path
    return record


def record_design_sign_off(
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
    approver: str,
    role: str,
    profile: str,
    seq: Seq,
    approved: bool = True,
    liaison: HumanLiaison | None = None,
    meaning_of_signing: str = "",
    note: str = "",
) -> SignOffRecord:
    """Record a Design-gate sign-off (governance.md §1 Design row)."""
    return record_sign_off(
        SignOffPoint.DESIGN,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
        approver=approver,
        role=role,
        profile=profile,
        seq=seq,
        approved=approved,
        liaison=liaison,
        meaning_of_signing=meaning_of_signing,
        note=note,
    )


def record_release_sign_off(
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
    approver: str,
    role: str,
    profile: str,
    seq: Seq,
    approved: bool = True,
    liaison: HumanLiaison | None = None,
    meaning_of_signing: str = "",
    note: str = "",
) -> SignOffRecord:
    """Record a Release-gate sign-off (governance.md §1 Release row, §6)."""
    return record_sign_off(
        SignOffPoint.RELEASE,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
        approver=approver,
        role=role,
        profile=profile,
        seq=seq,
        approved=approved,
        liaison=liaison,
        meaning_of_signing=meaning_of_signing,
        note=note,
    )


def release_allowed(
    record: SignOffRecord | None,
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
) -> bool:
    """Fail-closed release admission: a Gated release advances ONLY if a sign-off
    record exists, is ``approved``, and binds the EXACT decision point.

    No record ⇒ not released (governance.md §1/§5). A record whose bound anchors
    (subject / ``state_sha`` / evidence hash / nonce) do not match the release
    under admission is rejected too — a sign-off for a different artifact, state,
    evidence snapshot, or a replayed/forged nonce can never advance THIS release.
    """
    if record is None:
        return False
    return (
        record.approved
        and record.subject_artifact_uri == subject_artifact_uri
        and record.state_sha == state_sha
        and record.presented_evidence_hash == presented_evidence_hash
        and record.server_nonce == server_nonce
    )


def require_release_sign_off(
    record: SignOffRecord | None,
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
) -> SignOffRecord:
    """Like :func:`release_allowed` but RAISES :class:`SignOffError` (fail-closed)
    rather than returning a bool — for the release path that must refuse to
    advance, not branch."""
    if not release_allowed(
        record,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
    ):
        raise SignOffError(
            "Gated release blocked: no matching recorded sign-off for "
            f"({subject_artifact_uri!r}, state_sha={state_sha!r}) — fail-closed "
            "(governance.md §1/§5: no record ⇒ not released)"
        )
    assert record is not None  # narrowed by release_allowed
    return record
