"""Frozen pre-registration manifest for the Phase-1.5 decision gate.

This module builds the **immutable, content-addressed pre-registration** that
BINDS the Phase-1.5 confirmatory A/B before any scored data is seen
(docs/evaluation.md §2/§8). It is the integrity keystone of the whole harness:
once the manifest is frozen and the alpha draw is recorded, NOTHING the manifest
commits to — the central hypothesis, the minimum-detectable-effect (MDE), the
significance level (alpha), or the funded power rung — can be changed after a
scored run without producing a DIFFERENT, detectable manifest.

INTEGRITY (load-bearing — docs/evaluation.md §2/§8):

* **Pre-registration is BINDING at the FUNDED power rung.** The MDE/alpha are
  committed BEFORE the first scored run; a null at the funded MDE is a real KILL
  trigger (§1/§2.5), NOT a cue to slide down the de-scoping ladder. So freezing
  is allowed ONLY with a :class:`FundedPowerRung` that is actually funded — you
  cannot pre-register an UNDERPOWERED study as if it were powered
  (:class:`UnderpoweredPreRegistrationError`).
* **The alpha look-ledger binding stops fork/retrain re-draws.** Freezing RECORDS
  the ``(scored_eval_set_identity, decision_id)`` draw in the global alpha
  look-ledger (``foundry/state/global_records.py``), which is keyed on that pair
  — NOT on ``state_sha`` — so forking (a new ``run_id``) or retraining can never
  silently re-draw the alpha budget. A second freeze of the same pair is REJECTED
  fail-closed (:class:`AlphaAlreadyDrawnError`).
* **The manifest is content-addressed = tamper-evident.** Its ``content_identity``
  is a digest over the full canonical record (the :class:`FrozenModel` contract),
  so altering ``mde``/``alpha``/the rung after the fact yields a DIFFERENT
  identity; :func:`assert_unmodified` re-derives the bound identity and fails
  closed on any drift.

HONESTY: like the rest of the M1.5 harness this is a methodology DRY-RUN on
synthetic fixtures — the hypothesis defaults ``provenance == DRY_RUN``. The
manifest asserts NOTHING about real Foundry-vs-baseline performance; it only
fixes (and freezes + binds) the pre-registration SHAPE so the powered A/B runner
and decision table have an immutable, content-addressed object to cite. SCORED
RUNS MUST CITE A FROZEN :class:`PreRegistration` (its ``content_identity`` is the
registration's identity): a scored verdict whose manifest does not re-derive to
the cited identity is tampered evidence and must be rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.evaluation.hypothesis import CentralHypothesis
from foundry.evaluation.power import FundedPowerRung
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.state.global_records import GlobalRecords

__all__ = [
    "AlphaAlreadyDrawnError",
    "PreRegistration",
    "PreRegistrationError",
    "TamperedPreRegistrationError",
    "UnderpoweredPreRegistrationError",
    "assert_unmodified",
    "freeze_preregistration",
]


class PreRegistrationError(RuntimeError):
    """Base class for pre-registration integrity failures (fail closed)."""


class UnderpoweredPreRegistrationError(PreRegistrationError):
    """Raised iff a freeze is attempted with an UNDERPOWERED rung (§2 BLOCKER).

    You cannot pre-register an underpowered study as if it were powered — the
    funded MDE/alpha would license claims the budget can never support.
    """


class AlphaAlreadyDrawnError(PreRegistrationError):
    """Raised iff ``(scored_eval_set_identity, decision_id)`` is already drawn.

    The alpha look-ledger is keyed on that pair (NOT ``state_sha``), so a fork /
    retrain that re-presents the same scored slice + decision cannot re-draw the
    alpha budget. Fail closed.
    """


class TamperedPreRegistrationError(PreRegistrationError):
    """Raised by :func:`assert_unmodified` when a manifest fails re-derivation.

    The content-addressed identity bound at freeze time no longer matches the
    re-derived identity (or the cited identity) — an mde/alpha/rung edit after
    the fact is detectable. Fail closed.
    """


class PreRegistration(FrozenModel):
    """The IMMUTABLE, content-addressed pre-registration manifest (§2/§8).

    Captures — BEFORE any scored run — everything the Phase-1.5 confirmatory A/B
    is bound to: the :class:`CentralHypothesis` (H0/H1 + primary metric), the
    committed ``mde`` + ``alpha``, the FUNDED :class:`FundedPowerRung` (n/k frozen
    off the pilot, T-M15-05), the ``eval_set_identity`` of the SCORED slice, the
    ``decision_id`` the alpha budget is drawn against, a reference to the decision
    table thresholds, and ``provenance`` for honesty.

    Frozen + content-addressed via :class:`FrozenModel`: ``content_identity()`` is
    THE registration's identity (``registration_identity``). Any post-hoc edit to
    ``mde``/``alpha``/the rung (or anything else) yields a DIFFERENT identity, so
    tampering is detectable — :func:`assert_unmodified` re-derives it.

    HONESTY: the hypothesis defaults ``provenance == DRY_RUN`` — a methodology
    dry-run on synthetic fixtures (module docstring). A scored run flips the
    hypothesis to ``Provenance.SCORED`` before freezing; ``content_identity``
    includes ``provenance`` (EV4), so a dry-run rehearsal and the real scored
    pre-registration do NOT coalesce.
    """

    hypothesis: CentralHypothesis  # H0/H1 + primary metric (oracle-scored, §4)
    mde: float  # committed minimum detectable effect (mirror of the funded rung)
    alpha: float  # committed significance level (mirror of the funded rung)
    funded_rung: FundedPowerRung  # the funded (n,k) rung — MUST be funded (§2 BLOCKER)
    scored_eval_set_identity: str  # the SCORED slice identity the run is bound to (EV7)
    decision_id: str  # the alpha budget is drawn against (eval_set_identity, decision_id)

    # Reference to the pre-registered decision-table thresholds (§2.5). The full
    # table is a later M1.5 task; the manifest pins WHICH thresholds apply so a
    # scored verdict cites a fixed mapping, not a post-hoc one.
    decision_table_ref: str = "evaluation.md#2.5-decision-table"

    provenance_note: str = (
        "Pre-registration BINDS at the funded power rung before any scored run "
        "(evaluation.md §2/§8). The MDE/alpha are committed pre-run; a null at the "
        "funded MDE is a real KILL trigger, not a de-scoping cue. Content-addressed: "
        "any post-hoc edit yields a different content_identity (tamper-evident)."
    )
    version: int = 1

    def registration_identity(self) -> str:
        """The manifest's content-addressed identity (== ``content_identity()``).

        This is the value a SCORED RUN MUST CITE. A scored verdict whose manifest
        does not re-derive to this identity is tampered evidence (use
        :func:`assert_unmodified`).
        """
        return self.content_identity()


def freeze_preregistration(
    hypothesis: CentralHypothesis,
    funded_rung: FundedPowerRung,
    scored_eval_set_identity: str,
    *,
    decision_id: str,
    ledger: GlobalRecords,
    state_sha: str = "preregistration",
    run_id: str = "preregistration",
) -> PreRegistration:
    """Freeze the pre-registration manifest + record the alpha draw (§2/§8).

    Validates the rung is FUNDED (raises if underpowered — you cannot
    pre-register an underpowered study as if powered), then RECORDS the
    ``(scored_eval_set_identity, decision_id)`` draw in the global alpha
    look-ledger (``ledger``). The ledger is keyed on that pair (NOT
    ``state_sha``), so a fork/retrain re-presenting the same scored slice +
    decision is REJECTED (``AlphaAlreadyDrawnError``) — the budget cannot be
    silently re-drawn. Returns the frozen, content-addressed manifest.

    The freeze is the ONLY moment the alpha budget for this decision is drawn, so
    it MUST precede any scored run. ``state_sha`` is recorded per-look for audit
    ONLY (it is never part of the ledger key — EV7 / global_records.py).

    Parameters
    ----------
    hypothesis:
        The pre-registered :class:`CentralHypothesis` (committed H0/H1 + primary
        metric). A scored run flips its ``provenance`` to SCORED before freezing.
    funded_rung:
        The :class:`FundedPowerRung` from the power calc (T-M15-05). It MUST be
        funded (``funded is True`` / ``underpowered is False``) AND it MUST carry
        a concrete ``(n_tasks, k_repeats)`` allocation — else
        :class:`UnderpoweredPreRegistrationError`.
    scored_eval_set_identity:
        The ``eval_set_identity`` of the SCORED slice (EV7), the first half of the
        alpha-ledger key.
    decision_id:
        The decision the alpha budget is drawn against (second half of the key).
    ledger:
        The writable global :class:`~foundry.state.global_records.GlobalRecords`
        alpha look-ledger.
    state_sha, run_id:
        Recorded per-look for AUDIT only (never part of the ledger key).

    Raises
    ------
    UnderpoweredPreRegistrationError
        If ``funded_rung`` is not funded (or lacks a concrete (n,k) allocation).
    AlphaAlreadyDrawnError
        If ``(scored_eval_set_identity, decision_id)`` was already drawn (a
        fork/retrain re-draw — rejected fail-closed).
    """
    # GUARD 1: freezing is allowed ONLY with a FUNDED rung (§2 BLOCKER). An
    # underpowered rung licenses claims the budget cannot support — do not
    # pre-register it as if powered.
    if funded_rung.underpowered or not funded_rung.funded:
        raise UnderpoweredPreRegistrationError(
            "cannot freeze a pre-registration on an UNDERPOWERED rung "
            f"(funded={funded_rung.funded}, underpowered={funded_rung.underpowered}, "
            f"achieved_power={funded_rung.achieved_power} < target={funded_rung.target_power}); "
            "the funded MDE/alpha must be supportable at the funded budget (evaluation.md §2)"
        )
    if funded_rung.n_tasks is None or funded_rung.k_repeats is None:
        # A funded rung always carries a concrete allocation; defend in depth.
        raise UnderpoweredPreRegistrationError(
            "a funded rung must carry a concrete (n_tasks, k_repeats) allocation; "
            f"got n_tasks={funded_rung.n_tasks}, k_repeats={funded_rung.k_repeats}"
        )

    # GUARD 2: the alpha budget for this (scored_eval_set_identity, decision_id)
    # may be drawn ONCE. Fail closed BEFORE recording so a re-draw is rejected and
    # never appended to the ledger (a fork/retrain cannot spend it twice). The
    # ledger key is the pair — NOT state_sha — so forking (new run_id) can't slip
    # past (EV7 / global_records.py).
    if ledger.alpha_looks_spent(scored_eval_set_identity, decision_id) > 0:
        raise AlphaAlreadyDrawnError(
            "alpha budget already drawn for "
            f"(eval_set_identity={scored_eval_set_identity!r}, decision_id={decision_id!r}); "
            "a fork/retrain cannot re-draw the budget (evaluation.md §2; ledger keyed on the "
            "pair, NOT state_sha) — reusing it would invalidate the confirmatory run"
        )

    # Record the single look. state_sha/run_id are audit-only (never the key).
    ledger.record_alpha_look(scored_eval_set_identity, decision_id, state_sha, run_id)

    return PreRegistration(
        hypothesis=hypothesis,
        mde=funded_rung.mde,
        alpha=funded_rung.alpha,
        funded_rung=funded_rung,
        scored_eval_set_identity=scored_eval_set_identity,
        decision_id=decision_id,
    )


def assert_unmodified(prereg: PreRegistration, *, expected_identity: str | None = None) -> str:
    """Re-derive the manifest's content identity and fail closed on tampering.

    Returns the re-derived ``registration_identity()``. A frozen
    :class:`FrozenModel` cannot be mutated in place, so the practical tamper
    vector is reconstructing a manifest with an edited ``mde``/``alpha``/rung and
    presenting it as the original. Pass the ``expected_identity`` a scored run
    CITED: if the re-derived identity differs, the manifest was altered after the
    fact (raises :class:`TamperedPreRegistrationError`).

    With no ``expected_identity`` this still asserts the internal invariant — the
    committed ``mde``/``alpha`` mirror the funded rung's — so a hand-built
    manifest whose top-level ``mde``/``alpha`` were edited away from the rung's is
    rejected (the rung is part of the content identity, so a divergent mirror is
    itself a tamper signal a scored verdict's cited identity would catch).
    """
    identity = prereg.registration_identity()

    # Internal consistency: the committed mde/alpha MUST mirror the funded rung
    # (the rung is content-addressed into the manifest; an edited top-level mirror
    # is a tamper signal even without an external expected identity).
    if prereg.mde != prereg.funded_rung.mde or prereg.alpha != prereg.funded_rung.alpha:
        raise TamperedPreRegistrationError(
            "pre-registration mde/alpha do not mirror the funded rung "
            f"(manifest mde={prereg.mde}/alpha={prereg.alpha} vs "
            f"rung mde={prereg.funded_rung.mde}/alpha={prereg.funded_rung.alpha}) — "
            "post-hoc edit detected"
        )

    if expected_identity is not None and identity != expected_identity:
        raise TamperedPreRegistrationError(
            "pre-registration content identity does not match the cited identity "
            f"(re-derived {identity!r} != cited {expected_identity!r}) — the manifest "
            "was altered after registration (tamper-evident; evaluation.md §2/§8)"
        )
    return identity
