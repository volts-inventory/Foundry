"""Verdict-class gating machinery — how each class 1-5 is enforced. (domains.md §1)

A pack's :meth:`~foundry.domains.contract.DomainPackContract.assign_verdict_class`
hook maps a gate/criterion key -> the :class:`~foundry.schemas.gates.VerdictClass`
that key must be grounded by (the composed, strictest-wins
:class:`~foundry.domains.contract.VerdictClassPolicy`). THIS module is the core
machinery that turns such an assignment into the concrete *requirements* the
supervisor/gates enforce, gate-class-aware and FAIL-CLOSED:

* **class 1 (config-evaluable) / class 2 (execution-deterministic)** — machine
  grounded by the EXISTING gates (zone-B re-derive / held-out-mutant +
  behavioral-invariance discriminator). Pass-through: no extra binding.
* **class 3 (statistical)** — a statistical gate must be bound to a FROZEN alpha
  look (the cross-fork ``(eval_set_identity, decision_id)`` ledger,
  ``foundry/state/global_records.py``) AND keyed on a ``state_sha`` (so a
  data/model change re-draws). A class-3 verdict that does NOT cite a frozen
  alpha look + a ``state_sha`` is INVALID (fail-closed). We REUSE the eval
  alpha-ledger machinery — never rebuild it.
* **class 4 (human-witnessed)** — requires an approver-of-record (a recorded
  :class:`~foundry.schemas.governance.SignOffRecord`; reuse
  :mod:`foundry.governance.sign_off`). Autonomous / auto-approve is UNREACHABLE.
* **class 5 (perceptual)** — mandatory HITL over a rendered-evidence bundle;
  never auto-approved.

The strictness order is the SAME the contract's join uses
(:data:`foundry.domains.contract._VERDICT_CLASS_STRICTNESS`) so a HIGHER class can
NEVER be satisfied by a LOWER-class gate: a key assigned class 4 is not satisfied
by a class-2 machine gate, even a green one. Everything here is PURE and
DETERMINISTIC — no I/O, no signing; the helpers READ the ledger view and a
sign-off record, and decide.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from foundry.domains.contract import _VERDICT_CLASS_STRICTNESS
from foundry.governance.sign_off import release_allowed
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import VerdictClass

if TYPE_CHECKING:
    from foundry.schemas.governance import SignOffRecord


class AlphaLedgerView(Protocol):
    """The read-only slice of the class-3 alpha look-ledger this module needs.

    :class:`foundry.state.global_records.GlobalRecords` satisfies it structurally
    (``alpha_looks_spent`` reports the looks drawn for a
    ``(eval_set_identity, decision_id)`` pair). Keeping it to the single read we
    require means this module reuses — never rebuilds — the eval alpha-ledger
    machinery, and decides over a committed view (pure, no writes).
    """

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int: ...

__all__ = [
    "AlphaLedgerView",
    "Class3Binding",
    "GateRequirement",
    "GroundingKind",
    "InvalidClass3BindingError",
    "VerdictClassError",
    "assert_class3_binds_ledger",
    "autonomous_reachable",
    "class4_attested",
    "class5_hitl_satisfied",
    "gate_requirements_for",
    "satisfies",
]


class VerdictClassError(RuntimeError):
    """Base — a verdict-class requirement was violated (fail-closed)."""


class InvalidClass3BindingError(VerdictClassError):
    """A class-3 verdict does not cite a FROZEN alpha look + ``state_sha``.

    domains.md §1: a class-3 (statistical) gate is bound to the alpha look-ledger
    ``(eval_set_identity, decision_id)`` AND keyed on a ``state_sha`` (recorded
    per-look for audit; a data/model change re-draws). A class-3 gate that does
    NOT bind the ledger + ``state_sha`` is INVALID — reject fail-closed rather
    than admit an ungrounded statistical verdict.
    """


class GroundingKind(StrEnum):
    """HOW a verdict class is gated — the requirement the supervisor enforces."""

    MACHINE = "machine"  # classes 1/2: existing zone-B / discriminator gates
    ALPHA_STATE_SHA = "alpha+state_sha"  # class 3: frozen alpha look + state_sha
    APPROVER_OF_RECORD = "approver-of-record"  # class 4: recorded human sign-off
    PERCEPTUAL_HITL = "perceptual-hitl"  # class 5: mandatory HITL + rendered evidence


class GateRequirement(FrozenModel):
    """The concrete requirement a verdict class imposes on its gate (domains.md §1).

    A pure, frozen description the supervisor/gates read to know HOW to ground a
    keyed verdict. ``rank`` mirrors the contract's strictness order so
    :func:`satisfies` can refuse a higher-class requirement met by a lower-class
    gate. The flags are derived from ``verdict_class`` (a single source of truth):

    * ``grounding`` — which seam grounds it (machine / alpha+state_sha /
      approver / perceptual-HITL).
    * ``requires_alpha_state_sha`` — class 3 MUST bind a frozen alpha look + a
      ``state_sha``.
    * ``requires_approver`` — class 4 MUST carry a recorded approver-of-record.
    * ``requires_hitl`` — classes 4 and 5 require a human in the loop.
    * ``auto_approvable`` — FALSE for classes 4 and 5 (Autonomous/auto-approve
      is UNREACHABLE); machine + statistical classes are runner-attestable.
    * ``in_signed_predicate`` — class 5 is excluded from the signed SLSA
      predicate (advisory model + human); 1-4 enter it.
    """

    verdict_class: VerdictClass
    grounding: GroundingKind
    rank: int
    requires_alpha_state_sha: bool
    requires_approver: bool
    requires_hitl: bool
    auto_approvable: bool
    in_signed_predicate: bool


# Per-class requirement template (domains.md §1 taxonomy table). Built once from
# the verdict-class enum so the mapping has a single, exhaustive source of truth.
def _requirement(vc: VerdictClass) -> GateRequirement:
    rank = _VERDICT_CLASS_STRICTNESS[vc]
    if vc in (VerdictClass.CONFIG_EVALUABLE, VerdictClass.EXECUTION_DETERMINISTIC):
        return GateRequirement(
            verdict_class=vc,
            grounding=GroundingKind.MACHINE,
            rank=rank,
            requires_alpha_state_sha=False,
            requires_approver=False,
            requires_hitl=False,
            auto_approvable=True,
            in_signed_predicate=True,
        )
    if vc is VerdictClass.STATISTICAL:
        return GateRequirement(
            verdict_class=vc,
            grounding=GroundingKind.ALPHA_STATE_SHA,
            rank=rank,
            requires_alpha_state_sha=True,
            requires_approver=False,
            requires_hitl=False,
            auto_approvable=True,  # runner re-derives the metric; no human needed
            in_signed_predicate=True,
        )
    if vc is VerdictClass.HUMAN_WITNESSED:
        return GateRequirement(
            verdict_class=vc,
            grounding=GroundingKind.APPROVER_OF_RECORD,
            rank=rank,
            requires_alpha_state_sha=False,
            requires_approver=True,
            requires_hitl=True,
            auto_approvable=False,  # an agent-identity class-4 write is rejected
            in_signed_predicate=True,
        )
    # VerdictClass.PERCEPTUAL — class 5
    return GateRequirement(
        verdict_class=vc,
        grounding=GroundingKind.PERCEPTUAL_HITL,
        rank=rank,
        requires_alpha_state_sha=False,
        requires_approver=False,
        requires_hitl=True,
        auto_approvable=False,  # never auto-approved — mandatory HITL
        in_signed_predicate=False,  # excluded from the signed predicate (§1 table)
    )


_REQUIREMENTS: dict[VerdictClass, GateRequirement] = {
    vc: _requirement(vc) for vc in VerdictClass
}


def gate_requirements_for(verdict_class: VerdictClass) -> GateRequirement:
    """The :class:`GateRequirement` an assigned ``verdict_class`` imposes (domains.md §1).

    Given the class a pack assigned a key (via the composed
    :class:`~foundry.domains.contract.VerdictClassPolicy`), return the gate-class-
    aware requirement the supervisor/gates enforce: machine (1/2) /
    alpha+state_sha (3) / approver-of-record (4) / perceptual-HITL (5). Pure +
    deterministic — the requirement is a fixed function of the class.
    """
    return _REQUIREMENTS[verdict_class]


def autonomous_reachable(verdict_class: VerdictClass) -> bool:
    """Is Autonomous / auto-approve REACHABLE for ``verdict_class``? (domains.md §1)

    FALSE for class 4 (human-witnessed — a named human must attest) and class 5
    (perceptual — mandatory HITL, never auto-approved). The mode/tier veto
    (:class:`~foundry.domains.contract.ModeTierPolicy`) is enforced separately;
    this is the verdict-class-level fact the gate plumbing reads.
    """
    return _REQUIREMENTS[verdict_class].auto_approvable


class Class3Binding(FrozenModel):
    """A class-3 verdict's binding to a frozen alpha look + a ``state_sha``.

    domains.md §1: a class-3 (statistical) gate is bound to the alpha look-ledger
    keyed on ``(eval_set_identity, decision_id)`` (NOT ``state_sha`` — forking /
    retraining cannot re-draw the budget) AND keyed on a ``state_sha`` (recorded
    per-look; a data/model change re-draws). This is the citation a class-3
    verdict carries; :func:`assert_class3_binds_ledger` re-checks it against the
    global ledger view, fail-closed.
    """

    eval_set_identity: str
    decision_id: str
    state_sha: str


def assert_class3_binds_ledger(
    binding: Class3Binding | None, ledger: AlphaLedgerView
) -> Class3Binding:
    """Assert a class-3 verdict cites a FROZEN alpha look + a ``state_sha`` (fail-closed).

    domains.md §1 / global_records.py: the class-3 alpha look-ledger is keyed on
    ``(eval_set_identity, decision_id)``; a verdict is grounded ONLY if that pair
    has actually been DRAWN in the ledger (the look is frozen, not self-asserted)
    AND the verdict carries a non-empty ``state_sha`` (recorded per-look for
    audit; a data/model change re-draws). REUSES the eval alpha-ledger machinery
    (``GlobalRecordsView.alpha_looks_spent``) — never rebuilds it.

    Raises :class:`InvalidClass3BindingError` if the binding is absent, missing
    any of its three fields, or the ``(eval_set_identity, decision_id)`` pair has
    NOT been drawn (zero looks spent) — an ungrounded statistical verdict is
    rejected rather than admitted.
    """
    if binding is None:
        raise InvalidClass3BindingError(
            "class-3 (statistical) verdict cites no alpha-look binding — a "
            "statistical gate MUST bind (eval_set_identity, decision_id) + a "
            "state_sha (domains.md §1) — fail-closed"
        )
    if not binding.eval_set_identity or not binding.decision_id:
        raise InvalidClass3BindingError(
            "class-3 binding is missing eval_set_identity / decision_id — the "
            "alpha look-ledger key is incomplete (domains.md §1) — fail-closed"
        )
    if not binding.state_sha:
        raise InvalidClass3BindingError(
            "class-3 binding is missing state_sha — a data/model change must "
            "re-draw the statistical gate (domains.md §1) — fail-closed"
        )
    spent = ledger.alpha_looks_spent(binding.eval_set_identity, binding.decision_id)
    if spent < 1:
        raise InvalidClass3BindingError(
            "class-3 binding cites an UNDRAWN alpha look "
            f"(eval_set_identity={binding.eval_set_identity!r}, "
            f"decision_id={binding.decision_id!r}, looks_spent={spent}); the look "
            "must be frozen in the ledger before the verdict is valid "
            "(domains.md §1; global_records.py) — fail-closed"
        )
    return binding


def class4_attested(
    record: SignOffRecord | None,
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
) -> bool:
    """Is a class-4 artifact attested by a recorded approver-of-record? (domains.md §1)

    Class 4 (human-witnessed) requires a recorded
    :class:`~foundry.schemas.governance.SignOffRecord` that is ``approved`` and
    binds the EXACT decision anchors (subject / ``state_sha`` / evidence hash /
    nonce). REUSES :func:`foundry.governance.sign_off.release_allowed` — the same
    fail-closed admission the Gated release uses (no record ⇒ not attested; a
    sign-off for a different artifact/state/evidence/nonce can never attest THIS
    artifact). Autonomous is unreachable for class 4 — a missing record is NOT an
    auto-approve, it is a block.
    """
    return release_allowed(
        record,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
    )


def class5_hitl_satisfied(
    record: SignOffRecord | None,
    *,
    subject_artifact_uri: str,
    state_sha: str,
    presented_evidence_hash: str,
    server_nonce: str,
) -> bool:
    """Is a class-5 perceptual gate satisfied by a HITL sign-off over the bundle?

    domains.md §1 (class 5): perceptual verdicts are honestly NOT deterministic —
    blocked until human sign-off, NEVER auto-approved. The human signs over a
    rendered/evidence bundle, so satisfaction REQUIRES a recorded sign-off that
    binds the bundle's ``presented_evidence_hash`` (same anchors as class 4);
    there is no machine path that satisfies it. Reuses ``release_allowed`` so a
    sign-off for a different bundle/state/nonce can never satisfy THIS gate.
    """
    return release_allowed(
        record,
        subject_artifact_uri=subject_artifact_uri,
        state_sha=state_sha,
        presented_evidence_hash=presented_evidence_hash,
        server_nonce=server_nonce,
    )


def satisfies(required: VerdictClass, gate_class: VerdictClass) -> bool:
    """Can a verdict of ``gate_class`` satisfy a key REQUIRING ``required``? (domains.md §1)

    Gate-class-aware + FAIL-CLOSED: a HIGHER class can NEVER be satisfied by a
    LOWER-class gate. The strictness order is the SAME the contract's
    :class:`~foundry.domains.contract.VerdictClassPolicy` join uses
    (config < execution < statistical < perceptual < human-witnessed), so a key
    assigned class 4 (human-witnessed) is NOT satisfied by a class-2 machine
    verdict even when green: the gate's class must be AT LEAST as strict as the
    required class.

    A gate of a STRICTER class than required does satisfy it (the pack may reroute
    a criterion to a stricter grounding; that only tightens). Equality satisfies.
    """
    return _VERDICT_CLASS_STRICTNESS[gate_class] >= _VERDICT_CLASS_STRICTNESS[required]
