"""Phase-6 confirmatory A/B — ESTIMATION / REPLICATION ONLY (docs/evaluation.md §6.5).

The central hypothesis is tested twice — once at **Phase 1.5** (the decision gate)
and again at **Phase 6** (the confirmatory exit). Testing the SAME hypothesis at
two gates, each spending its own alpha with inverted confirmatory roles, is
uncontrolled multiplicity. The design resolves it **by designation, not
correction** (§6.5): the **Phase-1.5 run is the SINGLE confirmatory arbiter** and
owns the pre-registered alpha (:mod:`foundry.evaluation.ab_runner` /
:mod:`foundry.evaluation.decision_table`). The **Phase-6 A/B — THIS module — is
estimation/replication only**: it re-runs the 3-arm A/B on the scored slice,
reports an **effect estimate + CI**, and reports whether it **REPRODUCES** the
Phase-1.5 primary direction/decision — it does **NOT** draw a new alpha look on
the same decision (it is NOT a second gate).

THE LOAD-BEARING INVARIANT — ALPHA IS NOT RE-SPENT (§6.5):

* This module binds the alpha look-ledger READ-ONLY via the :class:`LedgerView`
  Protocol, which exposes ONLY ``alpha_looks_spent`` (a read). It STRUCTURALLY
  CANNOT reach ``record_alpha_look`` / ``try_first_alpha_look`` (they are not on
  the Protocol) — the Phase-1.5 decision's alpha budget can never be re-drawn.
* :func:`run_replication_ab` records ``alpha_looks_spent((eval_set_identity,
  decision_id))`` BEFORE and AFTER the replication and ASSERTS it is UNCHANGED
  (:class:`AlphaReSpentError` fail-closed otherwise). The result carries
  ``is_estimation_only=True`` and ``alpha_spent_delta=0``.
* A Phase-6 **disconfirmation** (``reproduces=False``) is RECORDED but does NOT
  un-spend or alter the Phase-1.5 decision — it cannot un-spend the sunk cost of
  the machinery Phase 1.5 already licensed (§6.5). A materially-inconsistent
  Phase-6 read is an INVESTIGATION trigger on the scorecard, NOT an automatic gate
  on the Autonomous-mode claim, which remains licensed by the Phase-1.5 arbiter.

REPRODUCTION (NOT a fresh verdict): :meth:`ReplicationResult.reproduces_phase15`
compares the replication's estimate to the Phase-1.5
:class:`~foundry.evaluation.decision_table.DecisionRecord`. A PROCEED is reproduced
iff the replication is significant in the SAME (positive) direction AND meets the
committed MDE; a HARD_KILL / non-PROCEED is reproduced iff the replication is
likewise a non-significant / sub-MDE null. Same direction + significance ⇒
reproduced.

DETERMINISM: the estimation core is :func:`foundry.evaluation.ab_runner.run_ab`
(its bootstrap RNG is seeded), so the same ``(frame, prereg, seed)`` yields an
IDENTICAL :class:`ReplicationResult`.

HONESTY NOTE (read before reuse): like the rest of the M1.5/M6 harness this is a
methodology DRY-RUN on synthetic fixtures (simulated baseline arms). A
:class:`ReplicationResult` asserts NOTHING about real Foundry-vs-baseline
performance; ``provenance`` is carried through so a consumer can tell a dry-run
rehearsal from a funded run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import Field

from foundry.evaluation.ab_runner import ABResult, run_ab
from foundry.evaluation.hypothesis import DecisionClass, Provenance
from foundry.evaluation.preregistration import assert_unmodified
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from foundry.evaluation.decision_table import DecisionRecord
    from foundry.evaluation.preregistration import PreRegistration
    from foundry.evaluation.task_fixtures import TaskFrame

    # The estimation core: re-runs the 3-arm A/B on the scored slice and returns
    # its ABResult. Defaults to ab_runner.run_ab; injectable so a caller (or test)
    # can supply a scored read of a known shape. It is ESTIMATION — it draws NO
    # alpha look (it is bound to no writable ledger).
    EstimationCore = Callable[["TaskFrame", "PreRegistration", int], ABResult]

__all__ = [
    "AlphaReSpentError",
    "LedgerView",
    "ReplicationResult",
    "reproduces_phase15",
    "run_replication_ab",
]


class AlphaReSpentError(RuntimeError):
    """Raised iff the alpha look-count CHANGED across the replication (fail closed).

    The Phase-6 replication is estimation only and MUST NOT re-spend the
    Phase-1.5 decision's alpha (§6.5). If ``alpha_looks_spent`` differs before vs
    after :func:`run_replication_ab`, something drew a second look on the
    confirmatory decision — the load-bearing invariant is violated. Fail closed.
    """


class LedgerView(Protocol):
    """The READ-ONLY slice of the alpha look-ledger this replication may touch.

    Phase 1.5 is the sole confirmatory arbiter (§6.5); this Phase-6 replication is
    estimation/replication only and NEVER re-spends alpha. So it depends on ONLY
    the read method — ``alpha_looks_spent`` — and structurally CANNOT call
    ``record_alpha_look`` / ``try_first_alpha_look`` (they are not on this
    Protocol). ``foundry.state.global_records.GlobalRecords`` satisfies it.
    """

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int: ...


class ReplicationResult(FrozenModel):
    """The Phase-6 estimation/replication result (docs/evaluation.md §6.5).

    Carries the replication's effect ESTIMATE + CI (re-derived from the scored
    slice via the Phase-1.5 estimation core, NOT a fresh gate), the Phase-1.5
    primary effect it is being checked against, and whether it REPRODUCES the
    Phase-1.5 primary direction/decision.

    THE ALPHA-NOT-RE-SPENT INVARIANT (load-bearing — §6.5):

    * ``is_estimation_only`` is ALWAYS True — this run is NOT a second gate.
    * ``alpha_looks_before == alpha_looks_after`` and ``alpha_spent_delta == 0``
      (asserted at construction): the replication drew NO new alpha look on the
      Phase-1.5 decision. (``None`` look counts when no ledger was bound — never a
      fabricated 0.)

    INTEGRITY: bound to the cited Phase-1.5 pre-registration via
    ``registration_identity`` (re-derived + asserted unmodified by
    :func:`run_replication_ab`) and to the scored slice via
    ``scored_eval_set_identity``.

    HONESTY: ``provenance`` is carried through; the simulated baseline arms mean a
    dry-run result asserts NOTHING about real performance. ``dry_run`` mirrors
    ``provenance != SCORED``.
    """

    registration_identity: str  # the cited, re-derived Phase-1.5 manifest identity (EV14)
    scored_eval_set_identity: str  # the scored slice this re-ran over (EV7)
    decision_id: str  # the Phase-1.5 decision being reproduced (read-only ledger key)
    seed: int = Field(ge=0)

    # The replication's PRIMARY estimate (FOUNDRY − BASELINE), re-derived:
    point_estimate: float
    ci_low: float
    ci_high: float
    significant_at_alpha: bool  # CI excludes 0 at the committed alpha
    meets_mde: bool  # estimate >= the committed MDE
    committed_mde: float = Field(gt=0.0)
    committed_alpha: float = Field(gt=0.0, lt=1.0)

    # The Phase-1.5 read this replication is checked against (for audit):
    phase15_decision: DecisionClass
    phase15_effect: float

    # Whether the replication REPRODUCES the Phase-1.5 primary direction/decision:
    reproduces: bool

    # THE LOAD-BEARING INVARIANT (§6.5): estimation only, alpha NOT re-spent.
    is_estimation_only: bool = True
    alpha_looks_before: int | None = Field(default=None, ge=0)
    alpha_looks_after: int | None = Field(default=None, ge=0)
    alpha_spent_delta: int = 0

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def model_post_init(self, __context: object) -> None:
        """Fail-closed structural invariants (estimation only; alpha NOT re-spent)."""
        if not self.is_estimation_only:
            raise ValueError(
                "the Phase-6 replication is estimation/replication ONLY (§6.5) — "
                "is_estimation_only must be True; it is never a second confirmatory gate"
            )
        if self.alpha_spent_delta != 0:
            raise ValueError(
                "alpha_spent_delta must be 0 — the Phase-6 replication NEVER re-spends "
                f"the Phase-1.5 decision's alpha (§6.5); got {self.alpha_spent_delta}"
            )
        # If a ledger was bound, the before/after look-counts MUST be identical.
        if (
            self.alpha_looks_before is not None
            and self.alpha_looks_after is not None
            and self.alpha_looks_before != self.alpha_looks_after
        ):
            raise ValueError(
                "alpha look-count changed across the replication "
                f"({self.alpha_looks_before} -> {self.alpha_looks_after}) — the replication "
                "must NOT re-spend the Phase-1.5 decision's alpha (§6.5)"
            )

    def reproduces_phase15(self, phase15_decision: DecisionRecord) -> bool:
        """True iff this replication REPRODUCES ``phase15_decision`` (§6.5).

        Reproduction compares the replication estimate to the Phase-1.5
        :class:`~foundry.evaluation.decision_table.DecisionRecord` — same
        direction/significance ⇒ reproduced. It is a SELF-CONSISTENCY check, NOT a
        fresh verdict (no alpha is spent here either):

        * A Phase-1.5 **PROCEED** is reproduced iff the replication is significant
          at the committed alpha in the SAME positive direction AND meets the
          committed MDE (it confirms structure beats baseline).
        * A Phase-1.5 **non-PROCEED** (HARD_KILL / BOUNDED_OVERHEAD_REDESIGN — a
          USD-primary null) is reproduced iff the replication is likewise a null
          on the primary (NOT significant, OR sub-MDE) — i.e. it does not flip the
          null into a positive.
        """
        if phase15_decision.decision is DecisionClass.PROCEED:
            return self.significant_at_alpha and self.meets_mde and self.point_estimate > 0.0
        # A Phase-1.5 primary null is reproduced iff the replication is also a null.
        return not (self.significant_at_alpha and self.meets_mde and self.point_estimate > 0.0)


def reproduces_phase15(
    result: ReplicationResult, phase15_decision: DecisionRecord
) -> bool:
    """Module-level alias for :meth:`ReplicationResult.reproduces_phase15`.

    Provided so a caller can compare a :class:`ReplicationResult` to a Phase-1.5
    :class:`~foundry.evaluation.decision_table.DecisionRecord` without reaching for
    the method (same direction/significance ⇒ reproduced). No alpha is spent.
    """
    return result.reproduces_phase15(phase15_decision)


def _default_estimation_core(
    frame: TaskFrame, prereg: PreRegistration, seed: int
) -> ABResult:
    """The default estimation core: the Phase-1.5 :func:`run_ab` machinery.

    Re-runs the 3-arm A/B on ``frame.scored_slice`` (run_ab does its OWN
    cite-frozen-prereg + scored-slice integrity guards). It is ESTIMATION: it
    binds NO writable ledger and so draws no alpha look.
    """
    return run_ab(frame, prereg, seed=seed)


def run_replication_ab(
    frame: TaskFrame,
    prereg: PreRegistration,
    phase15_decision: DecisionRecord,
    *,
    seed: int = 0,
    ledger: LedgerView | None = None,
    estimation_core: EstimationCore | None = None,
) -> ReplicationResult:
    """Run the Phase-6 ESTIMATION/REPLICATION A/B over ``frame.scored_slice`` (§6.5).

    Steps (in order — the integrity gate is FIRST, the alpha-not-re-spent invariant
    is enforced LAST):

    1. :func:`assert_unmodified` the cited Phase-1.5 ``prereg`` — a tampered/edited
       manifest fails closed BEFORE anything runs (the replication MUST cite the
       SAME unmodified frozen pre-registration the Phase-1.5 arbiter did; §2/§8).
    2. Record ``alpha_looks_spent((eval_set_identity, decision_id))`` BEFORE the
       replication (read-only; ``None`` when no ledger is bound — never a
       fabricated 0).
    3. Re-run the 3-arm A/B on the scored slice via the Phase-1.5 estimation core
       (:func:`foundry.evaluation.ab_runner.run_ab`) — this is ESTIMATION: it
       reuses the paired-cluster-bootstrap machinery to produce the effect
       estimate + CI, NOT to draw a new alpha look. (``run_ab`` does its OWN
       scored-slice / cite-frozen-prereg integrity guards.)
    4. Record ``alpha_looks_spent`` AFTER, and ASSERT it is UNCHANGED — the
       replication drew NO new alpha look on the Phase-1.5 decision
       (:class:`AlphaReSpentError` fail-closed otherwise). The
       :class:`ReplicationResult` carries ``is_estimation_only=True`` and
       ``alpha_spent_delta=0``.
    5. Compute whether the replication REPRODUCES the Phase-1.5 primary
       direction/decision (:meth:`ReplicationResult.reproduces_phase15`).

    A Phase-6 disconfirmation (``reproduces=False``) is RECORDED but does NOT
    un-spend or alter the Phase-1.5 decision/ledger (§6.5) — it is not a gate. The
    ``decision_id`` the read is keyed on comes from the cited ``prereg`` (the SAME
    decision the Phase-1.5 arbiter spent its alpha against), so the before/after
    read is over the right ledger key.

    Deterministic in ``seed`` (the estimation core's bootstrap RNG is seeded).
    HONESTY: a dry-run result (simulated baseline arms) asserts NOTHING about real
    performance.
    """
    # GUARD (FIRST): the replication MUST cite the SAME unmodified frozen
    # pre-registration the Phase-1.5 arbiter did.
    registration_identity = assert_unmodified(prereg)

    decision_id = prereg.decision_id
    eval_set_identity = prereg.scored_eval_set_identity

    # Step 2: OBSERVE the alpha look-count BEFORE (read-only; None without a ledger).
    looks_before = (
        ledger.alpha_looks_spent(eval_set_identity, decision_id)
        if ledger is not None
        else None
    )

    # Step 3: ESTIMATION — re-run the 3-arm A/B via the Phase-1.5 estimation core.
    # This reuses run_ab's paired-cluster-bootstrap machinery to produce the effect
    # estimate + CI; it draws NO alpha look (the LedgerView Protocol binds the
    # ledger read-only — record_alpha_look/try_first_alpha_look are unreachable).
    # The core defaults to ab_runner.run_ab; an injected core lets a caller supply
    # a scored read of a known shape (still estimation — no alpha write path).
    core: EstimationCore = estimation_core or _default_estimation_core
    ab: ABResult = core(frame, prereg, seed)
    primary = ab.primary

    # Step 4: OBSERVE the alpha look-count AFTER and ASSERT it is UNCHANGED (the
    # load-bearing invariant — the replication NEVER re-spends the Phase-1.5 alpha).
    looks_after = (
        ledger.alpha_looks_spent(eval_set_identity, decision_id)
        if ledger is not None
        else None
    )
    if (
        looks_before is not None
        and looks_after is not None
        and looks_before != looks_after
    ):
        raise AlphaReSpentError(
            "alpha look-count changed across the Phase-6 replication "
            f"({looks_before} -> {looks_after}) on (eval_set_identity={eval_set_identity!r}, "
            f"decision_id={decision_id!r}) — the replication is estimation ONLY and MUST NOT "
            "re-spend the Phase-1.5 decision's alpha (docs/evaluation.md §6.5)"
        )

    provenance = prereg.hypothesis.provenance

    result = ReplicationResult(
        registration_identity=registration_identity,
        scored_eval_set_identity=ab.scored_eval_set_identity,
        decision_id=decision_id,
        seed=seed,
        point_estimate=primary.point_estimate,
        ci_low=primary.ci_low,
        ci_high=primary.ci_high,
        significant_at_alpha=primary.significant_at_alpha,
        meets_mde=primary.meets_mde,
        committed_mde=prereg.mde,
        committed_alpha=prereg.alpha,
        phase15_decision=phase15_decision.decision,
        phase15_effect=phase15_decision.observed_effect,
        reproduces=False,  # set below once computed (kept explicit for the invariant)
        is_estimation_only=True,
        alpha_looks_before=looks_before,
        alpha_looks_after=looks_after,
        alpha_spent_delta=0,
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )
    # Step 5: REPRODUCTION — same direction/significance vs the Phase-1.5 decision.
    # (FrozenModel is immutable, so re-build with the computed `reproduces` bit.)
    return result.model_copy(
        update={"reproduces": result.reproduces_phase15(phase15_decision)}
    )
