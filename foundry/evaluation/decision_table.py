"""The Phase-1.5 decision table — the {primary × secondary} branch point (§2.5).

This module is the **pure, deterministic decision logic** that turns a scored
:class:`~foundry.evaluation.ab_runner.ABResult` (+ the frozen pre-registration it
cites) into a single :class:`DecisionRecord` carrying one of the three
pre-registered verdicts (docs/evaluation.md §2.5):

================================  =================================  =============================
Primary (equal-USD, oracle)       Secondary (equal-effective-token)  Verdict
================================  =================================  =============================
**Positive** at funded MDE/alpha  (any)                              PROCEED
**Null** at funded MDE            **Null** (or absent)               HARD_KILL
**Null** at funded MDE            **Positive**                       BOUNDED_OVERHEAD_REDESIGN
================================  =================================  =============================

"Positive" on the primary means **all three** of: significant at the
pre-registered alpha (the CI excludes 0), the observed effect meets the committed
MDE, AND the cost test holds in the **equal-USD frame** (FOUNDRY spends no more
than BASELINE). A primary that wins on resolution but costs MORE is **not** a
PROCEED — the equalizer is part of the hypothesis (FOUNDRY beats baseline *at
equal or lower cost*); a cost-worse win routes to the null branch.

INTEGRITY (load-bearing — docs/evaluation.md §2/§8):

* **A decision MUST CITE AN UNMODIFIED FROZEN PRE-REGISTRATION.** The FIRST thing
  :func:`decide` does is :func:`assert_unmodified` on the cited manifest — a
  tampered/edited manifest (mde/alpha/rung drift) fails closed
  (:class:`~foundry.evaluation.preregistration.TamperedPreRegistrationError`)
  BEFORE any verdict is computed. The decision is bound to that re-derived
  ``registration_identity`` (recorded in the record's ``provenance``).
* **It is IMPOSSIBLE to reach PROCEED on a null.** PROCEED requires the primary
  to be BOTH significant AND meet the MDE AND pass the cost test; the null
  branches (HARD_KILL / BOUNDED_OVERHEAD_REDESIGN) are the *complement* — they
  fire precisely when PROCEED's predicate is False. There is no third path and no
  re-power escape: a HARD_KILL is terminal (no re-test field is exposed), and the
  redesign branch only opens on a SUPPLIED, pre-registered positive token reading
  — never silently from the USD-null itself.

THE TOKEN AXIS — HONEST GAP (read before reuse):

The {USD-null, token-positive} redesign branch is defined in §2.5 on the
**equal-effective-task-token** secondary, which carries its OWN pre-registered
MDE/alpha/power (§2). **That secondary does NOT exist in the current**
:class:`~foundry.evaluation.ab_runner.ABResult`: its ``secondaries`` are the
``foundry_vs_plus_gates`` *resolution* gating-secondary and a
``foundry_vs_baseline`` *resolutions-per-USD efficiency* comparison — both
USD-denominated; **neither is the effective-token axis** the redesign fork turns
on. Inferring "token-positive" from the USD efficiency secondary would be
FABRICATING the very signal §2.5 says must be its own powered endpoint.

So this module does NOT manufacture a token reading. The redesign branch is gated
behind an EXPLICIT, optional :class:`TokenSecondaryReading` argument that a caller
must supply from the (not-yet-built) effective-token secondary. When it is absent,
a USD-null routes to **HARD_KILL** — the conservative, pre-registered default
(§1: a null at the funded MDE is a real KILL trigger). The record's
``token_axis_available`` bit makes the gap auditable; the BOUNDED_OVERHEAD_REDESIGN
branch is reachable ONLY with a real, supplied positive token reading.

HONESTY: like the rest of the M1.5 harness this is a methodology DRY-RUN on
synthetic fixtures. ``dry_run`` / ``provenance`` are carried from the cited
pre-registration through to the record; a dry-run decision asserts NOTHING about
real Foundry-vs-baseline performance — it only exercises the decision logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from foundry.evaluation.hypothesis import Arm, DecisionClass, Provenance
from foundry.evaluation.preregistration import assert_unmodified
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.ab_runner import ABResult
    from foundry.evaluation.preregistration import PreRegistration

__all__ = [
    "CostComparison",
    "DecisionRecord",
    "TokenSecondaryReading",
    "decide",
]


class CostComparison(FrozenModel):
    """The equal-USD cost test (FOUNDRY vs BASELINE) for the PROCEED predicate.

    The confirmatory comparison is FOUNDRY beats BASELINE **at <= cost** in the
    equal-USD frame (§2): ``treatment_cost_microusd <= control_cost_microusd``.
    Micro-USD integers (never floats — pinned global constraint). ``at_most_equal``
    is the load-bearing bit PROCEED requires.
    """

    treatment_arm: Arm
    control_arm: Arm
    treatment_cost_microusd: int = Field(ge=0)
    control_cost_microusd: int = Field(ge=0)
    at_most_equal: bool  # treatment_cost <= control_cost (the equal-USD frame holds)


class TokenSecondaryReading(FrozenModel):
    """A POSITIVE/NULL reading from the pre-registered effective-token secondary (§2).

    HONESTY: this is the equal-effective-task-token secondary that the redesign
    fork (§2.5) turns on. It is a SEPARATE, decision-grade endpoint with its OWN
    pre-registered MDE/alpha/power — NOT covered by the USD-primary's alpha nor the
    gating family's FWER correction (§2). It does NOT currently live in
    :class:`~foundry.evaluation.ab_runner.ABResult`, so a caller must construct it
    from that secondary's scored read and pass it to :func:`decide` explicitly.
    Absent it, the redesign branch cannot fire (a USD-null is a HARD_KILL).

    ``positive`` is True iff the secondary is significant at ITS own alpha AND
    meets ITS own committed MDE on the effective-token (action-token) scale — the
    {USD-null, token-positive} signal that routes a redesign instead of a kill.
    """

    name: str = "foundry_vs_baseline_effective_token"
    point_estimate: float  # observed effective-token effect (FOUNDRY − BASELINE)
    mde: float = Field(gt=0.0)  # the token secondary's OWN committed MDE (§2)
    alpha: float = Field(gt=0.0, lt=1.0)  # the token secondary's OWN committed alpha
    significant_at_alpha: bool  # significant at the token secondary's OWN alpha
    meets_mde: bool  # effect >= the token secondary's OWN MDE
    positive: bool  # significant AND meets_mde — the redesign-vs-kill signal


class DecisionRecord(FrozenModel):
    """The frozen verdict of the Phase-1.5 decision gate (docs/evaluation.md §2.5).

    Carries the :class:`DecisionClass`, the rationale (which rule fired + why), the
    observed primary effect vs the committed MDE, the significance bit, the
    equal-USD :class:`CostComparison`, the (optional) token reading + whether the
    token axis was even available, and the integrity binding
    (``registration_identity`` re-derived by :func:`assert_unmodified`) +
    ``provenance``/``dry_run`` for honesty.

    INVARIANTS (asserted at construction):

    * ``decision == PROCEED`` IFF the primary is significant AND meets the MDE AND
      the cost test holds — it is IMPOSSIBLE to construct a PROCEED record on a
      null (a model-validator rejects it).
    * ``HARD_KILL`` is terminal: there is NO re-test/re-power field on this record.
      The redesign branch carries the SUPPLIED token reading (never a fabricated
      one), so a null can never silently re-power.
    """

    decision: DecisionClass
    rule: str  # the rule that fired (human-readable; cites the §2.5 row)

    # The primary read, recorded against the committed MDE/alpha (from the manifest):
    primary_significant_at_alpha: bool
    primary_meets_mde: bool
    observed_effect: float  # point estimate, mean per-task (FOUNDRY − BASELINE) diff
    committed_mde: float = Field(gt=0.0)
    committed_alpha: float = Field(gt=0.0, lt=1.0)

    cost: CostComparison  # the equal-USD frame test (load-bearing for PROCEED)

    # The token axis is honestly OPTIONAL (see module docstring): the redesign
    # branch is reachable ONLY when a real positive reading is supplied.
    token_axis_available: bool  # was a TokenSecondaryReading supplied?
    token_reading: TokenSecondaryReading | None = None

    registration_identity: str  # the cited, re-derived manifest identity (EV14)
    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def model_post_init(self, __context: object) -> None:
        """Fail-closed structural invariants (no null-to-PROCEED, no silent re-power)."""
        proceed_predicate = (
            self.primary_significant_at_alpha
            and self.primary_meets_mde
            and self.cost.at_most_equal
        )
        # IMPOSSIBLE to reach PROCEED on a null: PROCEED iff the full predicate holds.
        if (self.decision is DecisionClass.PROCEED) != proceed_predicate:
            raise ValueError(
                "decision/PROCEED-predicate mismatch: PROCEED requires "
                "significant AND meets_mde AND cost<=baseline "
                f"(significant={self.primary_significant_at_alpha}, "
                f"meets_mde={self.primary_meets_mde}, cost_ok={self.cost.at_most_equal}); "
                f"got decision={self.decision}"
            )
        # The redesign branch fires ONLY with a SUPPLIED positive token reading —
        # never silently re-powering off a null.
        if self.decision is DecisionClass.BOUNDED_OVERHEAD_REDESIGN and not (
            self.token_reading is not None and self.token_reading.positive
        ):
            raise ValueError(
                "BOUNDED_OVERHEAD_REDESIGN requires a SUPPLIED positive "
                "effective-token secondary reading (§2.5); none was provided — a "
                "USD-null without a token-positive signal is a HARD_KILL, not a re-power"
            )


def decide(
    ab: ABResult,
    prereg: PreRegistration,
    *,
    token_reading: TokenSecondaryReading | None = None,
) -> DecisionRecord:
    """Apply the §2.5 decision table to a scored ``ab`` against the cited ``prereg``.

    Steps (in order — the integrity gate is FIRST):

    1. :func:`assert_unmodified` the cited ``prereg`` — a tampered/edited manifest
       fails closed BEFORE any verdict is computed (a decision MUST cite an
       unmodified frozen pre-registration; §2/§8). The decision is bound to the
       re-derived ``registration_identity``.
    2. Read the PRIMARY: ``significant_at_alpha`` (CI excludes 0 at the committed
       alpha) and ``meets_mde`` (observed effect >= committed MDE) off
       ``ab.primary``, and the equal-USD :class:`CostComparison` off the FOUNDRY /
       BASELINE :class:`~foundry.evaluation.ab_runner.ArmReport` costs.
    3. Branch:

       * **PROCEED** iff ``significant_at_alpha AND meets_mde AND cost <=
         baseline`` (the equal-USD frame). The secondary is irrelevant once the
         primary is positive (§2.5 row 1: "(any)").
       * Otherwise (a USD-null — not significant, or effect < MDE, or a cost-worse
         win): if a SUPPLIED ``token_reading`` is **positive**, route
         **BOUNDED_OVERHEAD_REDESIGN** (§2.5 row 3). Else **HARD_KILL** (§2.5 row
         2; the conservative default — a null at the funded MDE is a real KILL
         trigger, §1).

    HONEST NOTE on the token axis: ``ABResult`` does NOT carry the
    equal-effective-token secondary (its secondaries are USD-denominated; see the
    module docstring), so ``decide`` will NOT manufacture a token reading from
    them. The redesign branch is reachable ONLY via an explicit ``token_reading``
    from that (separately powered) secondary; absent it, a USD-null is a HARD_KILL.

    Pure + deterministic: the same ``(ab, prereg, token_reading)`` always yields an
    identical :class:`DecisionRecord` (no RNG, no I/O). HONESTY: ``provenance`` /
    ``dry_run`` are carried from the cited manifest's hypothesis.
    """
    # GUARD (FIRST): the decision MUST cite an unmodified frozen pre-registration.
    registration_identity = assert_unmodified(prereg)

    primary = ab.primary
    significant = primary.significant_at_alpha
    meets_mde = primary.meets_mde

    # The equal-USD cost test (FOUNDRY vs BASELINE) — load-bearing for PROCEED.
    treatment = ab.report(Arm.FOUNDRY)
    control = ab.report(Arm.BASELINE_SINGLE_AGENT)
    cost = CostComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        treatment_cost_microusd=treatment.total_cost_microusd,
        control_cost_microusd=control.total_cost_microusd,
        at_most_equal=treatment.total_cost_microusd <= control.total_cost_microusd,
    )

    provenance = prereg.hypothesis.provenance

    def _record(decision: DecisionClass, rule: str) -> DecisionRecord:
        """Build the record with the shared, already-computed fields (typed)."""
        return DecisionRecord(
            decision=decision,
            rule=rule,
            primary_significant_at_alpha=significant,
            primary_meets_mde=meets_mde,
            observed_effect=primary.point_estimate,
            committed_mde=prereg.mde,
            committed_alpha=prereg.alpha,
            cost=cost,
            token_axis_available=token_reading is not None,
            token_reading=token_reading,
            registration_identity=registration_identity,
            provenance=provenance,
            dry_run=provenance is not Provenance.SCORED,
        )

    primary_positive = significant and meets_mde and cost.at_most_equal
    if primary_positive:
        # §2.5 row 1: Positive at funded MDE/alpha & at <= cost -> PROCEED (any secondary).
        return _record(
            DecisionClass.PROCEED,
            "PROCEED (§2.5 row 1): primary significant at alpha AND effect >= "
            "committed MDE AND FOUNDRY cost <= BASELINE (equal-USD frame); the "
            "secondary is irrelevant once the primary is positive.",
        )

    # A USD-null (not significant, or effect < MDE, or a cost-worse win). The
    # redesign branch fires ONLY on a SUPPLIED positive effective-token reading
    # (§2.5 row 3); otherwise the null is a HARD_KILL (§2.5 row 2 / §1).
    if token_reading is not None and token_reading.positive:
        return _record(
            DecisionClass.BOUNDED_OVERHEAD_REDESIGN,
            "BOUNDED_OVERHEAD_REDESIGN (§2.5 row 3): USD-primary null at the "
            "funded MDE, but the pre-registered effective-token secondary is "
            "positive at its OWN MDE/alpha -> a SINGLE, scope-frozen "
            "overhead-reduction re-test (NOT an open-ended re-power).",
        )

    why_null = _null_reason(
        significant=significant, meets_mde=meets_mde, cost_ok=cost.at_most_equal
    )
    return _record(
        DecisionClass.HARD_KILL,
        f"HARD_KILL (§2.5 row 2): USD-primary null at the funded MDE ({why_null}) "
        "with no supplied positive effective-token secondary. TERMINAL — a null "
        "at the funded MDE is a real KILL trigger (§1), NOT a cue to re-power.",
    )


def _null_reason(*, significant: bool, meets_mde: bool, cost_ok: bool) -> str:
    """Human-readable summary of WHY the primary failed the PROCEED predicate."""
    reasons: list[str] = []
    if not significant:
        reasons.append("not significant at alpha")
    if not meets_mde:
        reasons.append("effect < committed MDE")
    if not cost_ok:
        reasons.append("FOUNDRY cost > BASELINE (equal-USD frame violated)")
    return "; ".join(reasons) if reasons else "PROCEED predicate not met"
