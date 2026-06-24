"""WORM-backed release evidence pack. (governance.md §4–§6, schemas EvidencePack)

A Gated release travels with an **independent, non-agent-assembled** evidence pack
(governance.md §6): a HASH-CHAINED set of content-addressed references to the
WORM-anchored gate results + released artifact handles + sign-off records, all keyed
to ONE integrated ``tree_sha``. The pack is **content-addressed** and
**tamper-evident** — a changed gate result, a swapped artifact, or a missing /
forged sign-off breaks the chain.

Reuse, not reinvention (LEARNINGS — the WORM chain lives in
:mod:`foundry.durability.outbox`): the chaining primitive is the SAME
``_chain_hash(prev_hash, canonical_event_bytes)`` the WORM audit chain uses
(``sha256(prev_hash || canonical(event))``), seeded from the same ``_GENESIS``. We
do NOT rebuild a parallel chain mechanism — we fold the ordered references through
the identical link function, so the evidence pack's ``chain_head`` is computed by
the exact discipline ``verify_audit_chain`` validates row-by-row.

Determinism (L7): the chain head is a pure function of the ordered references via
``content_digest`` / canonical bytes — never Python's seed-randomized ``hash()`` —
so an equal pack rebuilt anywhere yields the identical ``chain_head`` and
``pack_uri``. Bodies stay out-of-band CAS handles (DS9/L4); this record carries only
the handles + the chain head.

The pack's content hash (``pack_uri`` / :func:`pack_evidence_hash`) is what a
:class:`~foundry.schemas.governance.SignOffRecord.presented_evidence_hash` binds to,
closing the what-you-see-is-what-you-sign gap (governance.md §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.canonical import canonical_bytes, content_digest
from foundry.durability.outbox import _GENESIS, _chain_hash
from foundry.schemas.governance import EvidencePack

if TYPE_CHECKING:
    from foundry.schemas.base import Artifact
    from foundry.schemas.gates import GateResult
    from foundry.schemas.governance import SignOffRecord
    from foundry.state.seq import Seq

__all__ = [
    "EvidencePackError",
    "assert_evidence_pack",
    "build_evidence_pack",
    "chain_over",
    "pack_evidence_hash",
    "verify_evidence_pack",
]

_CAS_SHA256_PREFIX = "cas://sha256/"


class EvidencePackError(RuntimeError):
    """An evidence pack failed integrity verification (tamper / missing ref)."""


def chain_over(references: tuple[str, ...]) -> str:
    """Fold ordered references through the WORM link function; return the head.

    Each reference is chained as its own canonical event
    (``{"ref": <uri>, "index": i}``) so the head depends on BOTH content AND
    position — a reorder, swap, insertion, or deletion changes the head. Reuses
    :func:`foundry.durability.outbox._chain_hash` (the exact audit-chain link) and
    its ``_GENESIS`` seed (LEARNINGS WORM-chain reuse; do not reinvent).
    """
    head = _GENESIS
    for index, ref in enumerate(references):
        event_text = canonical_bytes({"index": index, "ref": ref}).decode("utf-8")
        head = _chain_hash(head, event_text)
    return head


def _ordered_references(
    gate_result_uris: tuple[str, ...],
    artifact_uris: tuple[str, ...],
    sign_off_uris: tuple[str, ...],
) -> tuple[str, ...]:
    """The canonical reference order the chain folds over: gate results, then
    artifacts, then sign-offs (a stable, kind-segmented order so the head is
    deterministic across rebuilds — L7)."""
    return (*gate_result_uris, *artifact_uris, *sign_off_uris)


def _gate_result_uri(result: GateResult) -> str:
    return f"{_CAS_SHA256_PREFIX}{result.content_identity()}"


def _sign_off_uri(record: SignOffRecord) -> str:
    return f"{_CAS_SHA256_PREFIX}{record.content_identity()}"


def pack_evidence_hash(pack: EvidencePack) -> str:
    """Content hash of the assembled pack — what a sign-off's
    ``presented_evidence_hash`` binds to (governance.md §6).

    Computed over the pack EXCLUDING its own ``pack_uri`` and ``seq`` (those are
    the handle/causal-order of the assembled record, not part of what the human
    signs over), so the hash is stable and re-derivable from the references +
    chain head."""
    body = pack.model_dump(mode="json", exclude={"pack_uri", "seq"})
    return content_digest(body)


def build_evidence_pack(
    *,
    run_against: str,
    gate_results: tuple[GateResult, ...] = (),
    artifacts: tuple[Artifact, ...] = (),
    sign_offs: tuple[SignOffRecord, ...] = (),
    seq: Seq,
    scanner_config_rev: str | None = None,
    provenance_predicate_uri: str | None = None,
    version: int = 1,
    supersedes: str | None = None,
) -> EvidencePack:
    """Assemble a tamper-evident :class:`EvidencePack` over the attested gate
    results + released artifact handles + sign-off records, all keyed to one
    integrated ``run_against`` tree_sha.

    Gate results and sign-offs are referenced by their CONTENT IDENTITY (so a
    tampered field re-keys the reference and breaks the chain); artifacts are
    referenced by their existing content-addressed ``uri`` handle. The
    ``chain_head`` is folded over the ordered references via the WORM link
    function (:func:`chain_over`); the ``pack_uri`` is the content hash of the
    assembled pack (:func:`pack_evidence_hash`).
    """
    gate_result_uris = tuple(_gate_result_uri(g) for g in gate_results)
    artifact_uris = tuple(a.uri for a in artifacts)
    sign_off_uris = tuple(_sign_off_uri(s) for s in sign_offs)

    references = _ordered_references(gate_result_uris, artifact_uris, sign_off_uris)
    chain_head = chain_over(references)

    # Build once WITHOUT pack_uri to compute the content hash, then re-bind it.
    draft = EvidencePack(
        pack_uri="",
        run_against=run_against,
        chain_head=chain_head,
        gate_result_uris=gate_result_uris,
        artifact_uris=artifact_uris,
        sign_off_uris=sign_off_uris,
        scanner_config_rev=scanner_config_rev,
        provenance_predicate_uri=provenance_predicate_uri,
        version=version,
        supersedes=supersedes,
        seq=seq,
    )
    pack_uri = f"{_CAS_SHA256_PREFIX}{pack_evidence_hash(draft)}"
    return draft.model_copy(update={"pack_uri": pack_uri})


def verify_evidence_pack(
    pack: EvidencePack,
    *,
    gate_results: tuple[GateResult, ...] = (),
    artifacts: tuple[Artifact, ...] = (),
    sign_offs: tuple[SignOffRecord, ...] = (),
    require_sign_off: bool = True,
) -> bool:
    """Verify an evidence pack is intact: the recomputed chain head matches, the
    pack is content-addressed correctly, and the supplied members reproduce the
    recorded references EXACTLY (in order).

    Tamper detection (governance.md §6):

    * a changed gate result re-keys its content-identity reference → the recomputed
      reference list (and chain head) no longer matches the recorded one → False;
    * a swapped / reordered / removed member changes the folded chain head → False;
    * a MISSING sign-off (``require_sign_off`` and ``sign_off_uris`` empty, or the
      supplied sign-offs don't reproduce the recorded sign-off references) → False;
    * a mutated ``chain_head`` or ``pack_uri`` (out-of-band edit of the record
      itself) → False.

    Returns a plain bool (mirrors :func:`verify_audit_chain`); call
    :func:`assert_evidence_pack` for the fail-closed raising variant.
    """
    if require_sign_off and not pack.sign_off_uris:
        return False

    expected_gate_uris = tuple(_gate_result_uri(g) for g in gate_results)
    expected_artifact_uris = tuple(a.uri for a in artifacts)
    expected_sign_off_uris = tuple(_sign_off_uri(s) for s in sign_offs)

    # The supplied members must reproduce the recorded references exactly — a
    # swapped/removed gate result or a missing sign-off shows up here AND in the
    # recomputed chain head below (two independent detections).
    if expected_gate_uris != pack.gate_result_uris:
        return False
    if expected_artifact_uris != pack.artifact_uris:
        return False
    if expected_sign_off_uris != pack.sign_off_uris:
        return False

    references = _ordered_references(
        pack.gate_result_uris, pack.artifact_uris, pack.sign_off_uris
    )
    if chain_over(references) != pack.chain_head:
        return False

    expected_pack_uri = f"{_CAS_SHA256_PREFIX}{pack_evidence_hash(pack)}"
    return pack.pack_uri == expected_pack_uri


def assert_evidence_pack(
    pack: EvidencePack,
    *,
    gate_results: tuple[GateResult, ...] = (),
    artifacts: tuple[Artifact, ...] = (),
    sign_offs: tuple[SignOffRecord, ...] = (),
    require_sign_off: bool = True,
) -> None:
    """Fail-closed verify: raise :class:`EvidencePackError` if the pack is not
    intact (for the release/admission path that must refuse, not branch)."""
    if not verify_evidence_pack(
        pack,
        gate_results=gate_results,
        artifacts=artifacts,
        sign_offs=sign_offs,
        require_sign_off=require_sign_off,
    ):
        raise EvidencePackError(
            f"evidence pack {pack.pack_uri!r} failed integrity verification "
            "(tampered gate result / swapped artifact / missing sign-off / "
            "broken chain) — fail-closed (governance.md §6)"
        )
