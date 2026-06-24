"""AI-code licensing & liability — provenance-backed, fail-closed. (lifecycle.md §5)

The Audit gate checks *dependency* licenses/SBOM; the GENERATED CODE ITSELF needs
its own provenance-backed licensing + liability posture. A released artifact gets
a CLEAN license/liability record ONLY when it TRACES to a complete provenance
chain (the run it was built in, the attested gate verdicts, the evidence pack,
the sign-offs, the SLSA predicate — :mod:`foundry.lifecycle.provenance`). An
artifact with NO provenance (or a broken/incomplete one) gets NO clean license
(fail-closed): you cannot license code you cannot prove the origin of.

The accountable party (the Release/Admission human sign-off — the accountability
anchor, lifecycle.md §5) is carried onto the record as the LIABILITY anchor, so
the record states *who is accountable* for the AI-generated code, not just *what
license it carries*.

Reuse, not reinvention: licensing does NOT re-implement provenance checking — it
folds the released artifact's :class:`~foundry.lifecycle.provenance.ProvenanceRecord`
through the EXISTING :func:`foundry.lifecycle.provenance.trace` and licenses iff
the resulting chain is complete.

Determinism (L7): :func:`license_for` is a pure function of the artifact + the
provenance record + the declared license (no clock, no network, no agent code) —
equal inputs yield an equal :class:`LicenseRecord`.
"""

from __future__ import annotations

from foundry.lifecycle.provenance import ProvenanceChain, ProvenanceRecord, trace
from foundry.schemas.base import Artifact, FrozenModel

__all__ = [
    "LicenseRecord",
    "UnlicensableArtifactError",
    "license_for",
]

# The default license declared for clean-provenance generated code. The adopter
# may pass a different SPDX id; what is NON-negotiable is that NO license is
# clean without complete provenance (fail-closed).
DEFAULT_LICENSE = "Apache-2.0"


class UnlicensableArtifactError(RuntimeError):
    """A released artifact has no clean provenance, so it gets no clean license.

    Raised by :func:`license_for` when ``strict`` is set: an artifact whose
    provenance does not trace to a complete chain cannot carry a clean
    license/liability record (fail-closed; lifecycle.md §5).
    """


class LicenseRecord(FrozenModel):
    """A provenance-backed licensing + liability record for generated code (§5).

    ``clean`` is the load-bearing bit: True iff the artifact TRACES to a complete
    provenance chain (so the license is provenance-backed and the liability
    anchor is real). A False record names the missing/broken provenance grounds
    in ``flags`` and carries ``license_id=""`` — there is NO clean license for
    un-provable code (fail-closed).

    * ``provenance_uri`` / ``run_against`` — the provenance anchor the license
      rests on (the run + tree it traces to);
    * ``accountable_party`` — the liability anchor (the Release/Admission human
      sign-off — who is accountable for the AI-generated code; lifecycle.md §5);
    * ``ai_generated`` — recorded TRUE: this is generated code, stated honestly
      in the licensing metadata (lifecycle.md §5).
    """

    subject_uri: str  # the released artifact this license is for
    license_id: str  # the SPDX license id — "" iff not cleanly licensable
    clean: bool  # True iff provenance-backed (a complete chain)
    provenance_uri: str  # handle to the ProvenanceRecord the license rests on
    run_against: str  # the integrated tree the provenance keyed to
    accountable_party: str | None = None  # the liability anchor (human sign-off)
    ai_generated: bool = True  # recorded honestly: this is generated code
    flags: tuple[str, ...] = ()  # provenance grounds the license failed (empty iff clean)

    def is_clean(self) -> bool:
        """A license is clean iff it is provenance-backed. Fail-closed: any
        provenance flag ⇒ not clean (no clean license for un-provable code)."""
        return self.clean


def _accountable_party(record: ProvenanceRecord) -> str | None:
    """The liability anchor: the first recorded approver-of-record sign-off, if
    any (the Release/Admission human is the accountability anchor; §5)."""
    for s in record.sign_offs:
        if s.approved:
            return s.approver
    return None


def license_for(
    released_artifact: Artifact,
    provenance: ProvenanceRecord | None,
    *,
    license_id: str = DEFAULT_LICENSE,
    provenance_uri: str = "",
    require_slsa: bool = False,
    strict: bool = False,
) -> LicenseRecord:
    """Issue a provenance-backed license/liability record — fail-closed (§5).

    A CLEAN license is issued ONLY when:

    * ``provenance`` is present (an artifact with NO provenance gets NO clean
      license — the most basic fail-closed case), AND
    * its ``subject_artifact`` is the artifact being licensed (the provenance
      must be FOR this artifact), AND
    * it TRACES to a complete chain via :func:`foundry.lifecycle.provenance.trace`
      (run + attested gate verdicts + evidence pack + sign-offs, tamper-evident).

    On success the record carries ``clean=True``, the declared ``license_id``,
    the provenance anchor, and the ``accountable_party`` liability anchor (the
    Release/Admission human sign-off; §5). On ANY failure the record is
    ``clean=False`` with ``license_id=""`` and the grounds in ``flags`` — no
    clean license for code whose origin cannot be proved. ``strict=True`` raises
    :class:`UnlicensableArtifactError` instead of returning a non-clean record.
    """
    flags: list[str] = []
    chain: ProvenanceChain | None = None
    run_against = ""
    accountable: str | None = None
    prov_uri = provenance_uri

    if provenance is None:
        flags.append("no_provenance")
    elif provenance.subject_artifact.uri != released_artifact.uri:
        # The provenance is for a DIFFERENT artifact — it does not back THIS one.
        flags.append("provenance_subject_mismatch")
        run_against = provenance.run_against
    else:
        run_against = provenance.run_against
        accountable = _accountable_party(provenance)
        chain = trace(provenance, require_slsa=require_slsa)
        if not chain.complete:
            # Carry the underlying provenance flags through so the remediation is
            # legible (e.g. tampered pack vs missing sign-off).
            flags.extend(f"provenance:{f}" for f in chain.flags)

    clean = not flags
    record = LicenseRecord(
        subject_uri=released_artifact.uri,
        license_id=license_id if clean else "",
        clean=clean,
        provenance_uri=prov_uri,
        run_against=run_against,
        accountable_party=accountable if clean else None,
        ai_generated=True,
        flags=tuple(flags),
    )
    if strict and not clean:
        raise UnlicensableArtifactError(
            f"released artifact {released_artifact.uri!r} has no clean provenance "
            f"{record.flags!r} — no clean license (fail-closed; lifecycle.md §5)"
        )
    return record
