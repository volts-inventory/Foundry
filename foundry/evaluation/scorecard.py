"""The headline scorecard (§7): the M1.5 decision-gate slice + the FULL headline.

This module assembles TWO scorecards over docs/evaluation.md §7:

* :func:`build_scorecard` — the **decision-gate slice** (Phase-1.5): the ONE
  confirmatory primary + per-arm UNCONDITIONAL ITT + the decision verdict, off an
  :class:`~foundry.evaluation.ab_runner.ABResult` + :class:`DecisionRecord`.
* :func:`build_headline_scorecard` — the **FULL published headline**: the ONE
  confirmatory primary (Phase-1.5 arbiter [SPENT alpha] + Phase-6 replication
  [ESTIMATION only]), the SECONDARY family under Holm–Bonferroni (NEVER promoted
  to a primary claim), the per-feature wall-clock + cost line, the agent-validity
  / trajectory / integrity / safeties summary, and UNCONDITIONAL per-arm ITT —
  off a VALID :class:`~foundry.evaluation.factory_eval.FactoryEvalResult`. It is
  PUBLISHED + STABLE (:meth:`HeadlineScorecard.stable_digest` re-derives
  identically across re-runs) and FAIL-CLOSED (a non-valid eval raises
  :class:`NotPublishableError` — §6/§10).

The slice below assembles the **decision-gate slice** into a single frozen
:class:`Scorecard` and renders it for a human reader. It is the *presentation*
layer over the already-computed
confirmatory artifacts — it CONSUMES the :class:`~foundry.evaluation.ab_runner.ABResult`
and :class:`~foundry.evaluation.decision_table.DecisionRecord` as AUTHORITATIVE
and recomputes NO statistics. Its whole job is to present them FAITHFULLY and
UNCONDITIONALLY.

SCOPE — the decision-gate slice ONLY (load-bearing — read before reuse):

* the ONE confirmatory primary (SWE-EVO resolution vs baseline at equal USD): its
  effect / CI / ``significant_at_alpha`` / ``meets_mde``, plus the equal-USD cost
  comparison FOUNDRY vs BASELINE;
* the per-arm UNCONDITIONAL ITT pass-rate + attempted denominator + cost (straight
  from each :class:`~foundry.evaluation.ab_runner.ArmReport`);
* the :class:`~foundry.evaluation.hypothesis.DecisionClass` + the rule that fired,
  carried verbatim from the :class:`DecisionRecord`;
* the role-ablation summary (which roles earn their place) IF supplied.

OUT of scope (the full Phase-6 headline — do NOT build it here): the secondary
family published under Holm–Bonferroni as a headline, and the optimization Pareto
front. The decision gate turns on the ONE primary + the decision; the rest is a
later phase.

UNCONDITIONAL ITT (load-bearing — §2/§6/§7): the per-arm pass-rate in the
scorecard is copied STRAIGHT from ``ArmReport.itt_pass_rate`` with its denominator
= ``ArmReport.attempted`` (= n_tasks × k_repeats). The scorecard NEVER recomputes a
rate, and NEVER conditions the denominator on completion — a conditional
(resolved / completed) rate is the collider bias §2/§6 forbid, and §7 reports the
ITT rate UNCONDITIONALLY.

HONESTY (mandatory — §0/§4): this is a **METHODOLOGY DRY-RUN on synthetic
fixtures**. The BASELINE / +gates arms are SIMULATED stand-ins and any ablation is
a simulated contribution model — so a dry-run scorecard asserts NOTHING about real
Foundry-vs-baseline performance; it only exercises the scorecard methodology. And
(§0/§4) the primary is oracle-scored: a self-judge seam (a gate judge grading the
factory's own output) is NEVER reported here as independent validation. The banner
(:meth:`Scorecard.honesty_banner`) states this UP FRONT and :meth:`Scorecard.render`
prints it FIRST, so no reader can mistake a rehearsal for evidence.

Pure + deterministic: the same ``(ab, decision, ablation, prereg)`` yields an
IDENTICAL :class:`Scorecard` and an identical :meth:`render` string (no RNG, no
I/O). :func:`build_scorecard` does NOT call ``assert_unmodified`` — the integrity
gate already fired inside ``run_ab`` / ``decide`` that produced these inputs; the
scorecard only presents their verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.ab_runner import _holm_bonferroni
from foundry.evaluation.hypothesis import Arm, DecisionClass, Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.ab_runner import ABResult
    from foundry.evaluation.ablation import AblationResult
    from foundry.evaluation.cost_model import CostBreakdown
    from foundry.evaluation.decision_table import DecisionRecord
    from foundry.evaluation.factory_eval import FactoryEvalResult
    from foundry.evaluation.preregistration import PreRegistration
    from foundry.evaluation.replication_ab import ReplicationResult

__all__ = [
    "ArmScore",
    "CostLine",
    "HeadlinePrimary",
    "HeadlineScorecard",
    "HeadlineSecondary",
    "NotPublishableError",
    "PrimaryScore",
    "RoleAblationLine",
    "Scorecard",
    "SecondaryReading",
    "build_headline_scorecard",
    "build_scorecard",
]


class ArmScore(FrozenModel):
    """One arm's UNCONDITIONAL ITT line, copied verbatim from its ``ArmReport``.

    ``itt_pass_rate`` and ``attempted`` are the SAME values the
    :class:`~foundry.evaluation.ab_runner.ArmReport` carries — the rate is NEVER
    recomputed and the denominator (``attempted`` = n_tasks × k_repeats) is NEVER
    conditioned on completion (the §2/§6 collider bias forbids a resolved/completed
    rate; §7 reports ITT unconditionally). ``simulated`` flags the synthetic
    stand-in arms (BASELINE / +gates).
    """

    arm: Arm
    itt_pass_rate: float = Field(ge=0.0, le=1.0)  # UNCONDITIONAL (resolved / attempted)
    resolved_count: int = Field(ge=0)
    attempted: int = Field(gt=0)  # ITT denominator, never shrunk to completed
    total_cost_microusd: int = Field(ge=0)
    simulated: bool


class PrimaryScore(FrozenModel):
    """The ONE confirmatory primary line (FOUNDRY vs BASELINE, equal USD) + cost.

    All fields are copied from the :class:`~foundry.evaluation.ab_runner.BootstrapComparison`
    and the FOUNDRY / BASELINE :class:`ArmScore` costs — no statistic is recomputed.
    ``cost_at_most_equal`` is the equal-USD frame bit (FOUNDRY spends <= BASELINE)
    that is PART of the PROCEED predicate (§2.5).
    """

    treatment_arm: Arm
    control_arm: Arm
    point_estimate: float  # observed mean per-task (FOUNDRY − BASELINE) resolution diff
    ci_low: float
    ci_high: float
    alpha: float = Field(gt=0.0, lt=1.0)
    mde: float = Field(gt=0.0)
    significant_at_alpha: bool  # CI excludes 0 at alpha
    meets_mde: bool  # observed effect >= committed MDE

    treatment_cost_microusd: int = Field(ge=0)
    control_cost_microusd: int = Field(ge=0)
    cost_at_most_equal: bool  # FOUNDRY cost <= BASELINE (the equal-USD frame holds)


class RoleAblationLine(FrozenModel):
    """One role's earns-its-place line in the ablation summary (if provided).

    Copied from the role's :class:`~foundry.evaluation.ablation.RoleAblationDelta`:
    the resolution-rate ``delta`` from removing the role, the USD ``cost_delta``,
    and the ``earns_place`` verdict. SIMULATED (a contribution model, not a real
    per-role graph bypass) — see ``Scorecard.ablation_simulated``.
    """

    role: str
    delta: float  # full_rate − ablated_rate (resolution-rate hit from removing it)
    cost_delta_microusd: int  # full_cost − ablated_cost (>= 0: removal is cheaper)
    earns_place: bool


class Scorecard(FrozenModel):
    """The frozen Phase-1.5 decision-gate scorecard slice (docs/evaluation.md §7).

    Assembled by :func:`build_scorecard` from an :class:`ABResult` + a
    :class:`DecisionRecord` (and, optionally, an :class:`AblationResult`). It
    presents — never recomputes — the decision-gate slice: the per-arm
    UNCONDITIONAL ITT lines, the ONE confirmatory primary + equal-USD cost, the
    decision class + rule, and the role-ablation summary.

    HONESTY: ``dry_run`` / ``provenance`` are carried through; :meth:`honesty_banner`
    is mandatory and non-empty, and :meth:`render` prints it FIRST.
    """

    registration_identity: str  # the cited, re-derived manifest identity (carried, EV14)
    decision: DecisionClass
    rule: str  # the §2.5 rule that fired, verbatim from the DecisionRecord

    arm_scores: tuple[ArmScore, ...]  # per-arm UNCONDITIONAL ITT lines
    primary: PrimaryScore  # the ONE confirmatory primary + equal-USD cost

    ablation_lines: tuple[RoleAblationLine, ...] = ()  # empty iff no ablation supplied
    ablation_simulated: bool | None = None  # None iff no ablation; else its honesty flag

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def arm_score(self, arm: Arm) -> ArmScore:
        """Look up the :class:`ArmScore` for ``arm`` (raises ``KeyError``)."""
        for s in self.arm_scores:
            if s.arm is arm:
                return s
        raise KeyError(arm)

    def honesty_banner(self) -> str:
        """The mandatory dry-run / honesty banner (non-empty; rendered FIRST).

        States this is a METHODOLOGY DRY-RUN on synthetic fixtures with SIMULATED
        baseline / ablation arms — NOT real evidence — and (§0/§4) that a self-judge
        seam is never reported here as independent validation. For a real SCORED
        run the banner instead names it a SCORED confirmatory run; it is NEVER
        empty either way.
        """
        if self.provenance is Provenance.SCORED and not self.dry_run:
            return (
                "SCORED CONFIRMATORY RUN — Phase-1.5 decision gate. Primary is "
                "oracle-scored (§4): no LLM/self-judge seam is reported as "
                "independent validation. ITT pass-rates are UNCONDITIONAL "
                "(denominator = attempted; never conditioned on completion — §2/§7)."
            )
        return (
            "DRY-RUN — METHODOLOGY REHEARSAL ON SYNTHETIC FIXTURES. This scorecard "
            "asserts NOTHING about real Foundry-vs-baseline performance: the "
            "BASELINE / +gates arms (and any ablation) are SIMULATED stand-ins, NOT "
            "real evidence. It only exercises the decision-gate scorecard "
            "methodology end-to-end. Per §0/§4 the primary is oracle-scored and a "
            "self-judge seam is NEVER reported here as independent validation. "
            "ITT pass-rates are UNCONDITIONAL (denominator = attempted; never "
            "conditioned on completion — §2/§7)."
        )

    def render(self) -> str:
        """A concise human-readable scorecard, with the honesty banner FIRST.

        Pure + deterministic (no RNG/IO): the same scorecard always renders the
        same string. The banner is the FIRST line so a reader cannot miss it; the
        decision is reported as the verdict that fired (a HARD_KILL renders the
        KILL — no proceed-spin).
        """
        lines: list[str] = []
        # Banner FIRST — load-bearing honesty (§0/§4).
        lines.append(self.honesty_banner())
        lines.append("")
        lines.append("Phase-1.5 decision-gate scorecard")
        lines.append(f"  registration: {self.registration_identity}")
        lines.append(f"  provenance: {self.provenance.value} (dry_run={self.dry_run})")
        lines.append("")

        # Per-arm UNCONDITIONAL ITT (denominator = attempted, never shrunk).
        lines.append("Per-arm ITT (UNCONDITIONAL; denominator = attempted):")
        for s in self.arm_scores:
            sim = " [SIMULATED]" if s.simulated else ""
            lines.append(
                f"  {s.arm.value}: ITT pass-rate {s.itt_pass_rate:.4f} "
                f"({s.resolved_count}/{s.attempted}) "
                f"cost {s.total_cost_microusd} micro-USD{sim}"
            )
        lines.append("")

        # The ONE confirmatory primary + equal-USD cost.
        p = self.primary
        lines.append(
            f"Confirmatory primary ({p.treatment_arm.value} vs {p.control_arm.value}, "
            "equal-USD, oracle-scored):"
        )
        lines.append(
            f"  effect {p.point_estimate:+.4f} "
            f"CI[{p.ci_low:+.4f}, {p.ci_high:+.4f}] @ alpha={p.alpha} "
            f"(MDE={p.mde})"
        )
        lines.append(
            f"  significant_at_alpha={p.significant_at_alpha}  meets_mde={p.meets_mde}"
        )
        lines.append(
            f"  equal-USD cost: FOUNDRY {p.treatment_cost_microusd} "
            f"vs BASELINE {p.control_cost_microusd} micro-USD "
            f"-> at_most_equal={p.cost_at_most_equal}"
        )
        lines.append("")

        # The decision verdict + rule (carried verbatim — a KILL renders the KILL).
        lines.append(f"DECISION: {self.decision.value.upper()}")
        lines.append(f"  rule: {self.rule}")

        # The role-ablation summary, if supplied.
        if self.ablation_lines:
            lines.append("")
            sim = " [SIMULATED]" if self.ablation_simulated else ""
            lines.append(f"Role ablation — do roles earn their place?{sim}")
            for a in self.ablation_lines:
                verdict = "EARNS PLACE" if a.earns_place else "does NOT earn place"
                lines.append(
                    f"  {a.role}: delta {a.delta:+.4f} "
                    f"cost_delta {a.cost_delta_microusd} micro-USD -> {verdict}"
                )

        return "\n".join(lines)


def build_scorecard(
    ab: ABResult,
    decision: DecisionRecord,
    *,
    ablation: AblationResult | None = None,
    prereg: PreRegistration | None = None,
) -> Scorecard:
    """Assemble the Phase-1.5 decision-gate :class:`Scorecard` (docs/evaluation.md §7).

    PRESENTS — never recomputes — the decision-gate slice from already-confirmatory
    artifacts:

    1. per-arm UNCONDITIONAL ITT: each :class:`ArmScore` copies
       ``ArmReport.itt_pass_rate`` + ``attempted`` (= n_tasks × k_repeats) STRAIGHT
       from the :class:`ABResult` — the rate is never recomputed and the denominator
       is never conditioned on completion (§2/§6/§7);
    2. the ONE confirmatory primary (FOUNDRY vs BASELINE, equal-USD) — effect / CI /
       ``significant_at_alpha`` / ``meets_mde`` off ``ab.primary``, plus the
       equal-USD cost comparison off the FOUNDRY / BASELINE arm costs (the same
       ``at_most_equal`` bit the :class:`DecisionRecord`'s :class:`CostComparison`
       carries);
    3. the :class:`DecisionClass` + the ``rule`` that fired, carried VERBATIM from
       the :class:`DecisionRecord`;
    4. the role-ablation summary (which roles earn their place) IF ``ablation`` is
       supplied (else an empty tuple).

    OUT of scope (a later phase, NOT built here): the Holm–Bonferroni secondary
    family as a published headline and the optimization Pareto front. The decision
    gate turns on the ONE primary + the decision.

    Pure + deterministic (no RNG/IO; no ``assert_unmodified`` — the integrity gate
    already fired inside ``run_ab`` / ``decide``). HONESTY: ``provenance`` /
    ``dry_run`` are carried from the :class:`DecisionRecord` (the authoritative
    verdict); the mandatory banner (:meth:`Scorecard.honesty_banner`) is rendered
    FIRST. ``prereg`` is accepted for caller symmetry / future binding but is NOT
    required — the decision already carries the re-derived ``registration_identity``.
    """
    # 1. Per-arm UNCONDITIONAL ITT — copied straight from each ArmReport (never
    #    recomputed; denominator = attempted, never conditioned on completion).
    arm_scores = tuple(
        ArmScore(
            arm=r.arm,
            itt_pass_rate=r.itt_pass_rate,  # UNCONDITIONAL — verbatim from the ArmReport
            resolved_count=r.resolved_count,
            attempted=r.attempted,  # ITT denominator = n_tasks * k_repeats, never shrunk
            total_cost_microusd=r.total_cost_microusd,
            simulated=r.simulated,
        )
        for r in ab.arm_reports
    )

    # 2. The ONE confirmatory primary + the equal-USD cost (FOUNDRY vs BASELINE).
    #    Read the costs from the authoritative DecisionRecord.cost so the scorecard's
    #    equal-USD bit is EXACTLY the one the verdict turned on.
    cost = decision.cost
    bc = ab.primary
    primary = PrimaryScore(
        treatment_arm=bc.treatment_arm,
        control_arm=bc.control_arm,
        point_estimate=bc.point_estimate,
        ci_low=bc.ci_low,
        ci_high=bc.ci_high,
        alpha=bc.alpha,
        mde=bc.mde,
        significant_at_alpha=bc.significant_at_alpha,
        meets_mde=bc.meets_mde,
        treatment_cost_microusd=cost.treatment_cost_microusd,
        control_cost_microusd=cost.control_cost_microusd,
        cost_at_most_equal=cost.at_most_equal,
    )

    # 4. The role-ablation summary, if provided.
    if ablation is not None:
        ablation_lines = tuple(
            RoleAblationLine(
                role=d.role.value,
                delta=d.delta,
                cost_delta_microusd=d.cost_delta_microusd,
                earns_place=d.earns_place,
            )
            for d in ablation.deltas
        )
        ablation_simulated: bool | None = ablation.simulated_ablation
    else:
        ablation_lines = ()
        ablation_simulated = None

    # 3. The decision class + rule, carried VERBATIM. provenance/dry_run from the
    #    authoritative verdict (a HARD_KILL renders the KILL — no proceed-spin).
    return Scorecard(
        registration_identity=decision.registration_identity,
        decision=decision.decision,
        rule=decision.rule,
        arm_scores=arm_scores,
        primary=primary,
        ablation_lines=ablation_lines,
        ablation_simulated=ablation_simulated,
        provenance=decision.provenance,
        dry_run=decision.dry_run,
    )


# ===========================================================================
# THE FULL HEADLINE SCORECARD (docs/evaluation.md §7) — published + STABLE.
#
# Where ``build_scorecard`` above is the Phase-1.5 DECISION-GATE slice, the
# headline scorecard is the FULL published artifact: the ONE confirmatory
# primary (the Phase-1.5 arbiter read [1.5] + the Phase-6 replication's
# reproduction status [6] — clearly labeled which spent ALPHA vs ESTIMATION),
# the SECONDARY family under Holm–Bonferroni (NEVER promoted to a primary
# claim), the per-feature wall-clock + cost line, the agent-validity /
# trajectory / integrity / safeties summary, and the UNCONDITIONAL per-arm ITT.
#
# PUBLISHED + STABLE (load-bearing — §7): the scorecard is DETERMINISTIC and
# :meth:`HeadlineScorecard.stable_digest` re-derives identically across re-runs
# — publishing the SAME inputs yields a byte-identical scorecard. FAIL-CLOSED:
# a scorecard built from a NON-VALID :class:`FactoryEvalResult` (integrity /
# safeties dirty) refuses to publish (:class:`NotPublishableError`) — an
# invalidated eval can never reach the headline as evidence (§6/§10).
# ===========================================================================


class NotPublishableError(RuntimeError):
    """Raised iff a headline scorecard is built from a NON-VALID eval (fail-closed).

    The full headline scorecard is the PUBLISHED artifact; a
    :class:`~foundry.evaluation.factory_eval.FactoryEvalResult` whose ``valid``
    bit is False (a reachable hidden test / contamination — §6 — or a
    reward-hack / eval-lie / nondeterminism finding — §10) MUST NOT be published.
    :func:`build_headline_scorecard` fails closed here so an invalidated eval can
    never reach the headline as evidence (docs/evaluation.md §6/§10).
    """


class HeadlinePrimary(FrozenModel):
    """The ONE confirmatory PRIMARY line — Phase-1.5 arbiter [1.5] + Phase-6 [6].

    SWE-EVO resolution vs baseline at equal USD, oracle-scored. The Phase-1.5
    fields (``effect`` / ``ci_low`` / ``ci_high`` / ``significant_at_alpha`` /
    ``meets_mde`` / the equal-USD cost bit / the ``decision``) are the SINGLE
    confirmatory arbiter that SPENT the pre-registered alpha (§2/§6.5). The
    Phase-6 fields (``replication_effect`` / ``replication_reproduces``) are an
    ESTIMATION/replication overlay that spends NO alpha (§6.5) — a Phase-6
    disconfirmation is an INVESTIGATION trigger, NOT a second gate and NEVER a
    fresh verdict. ``alpha_source`` / ``estimation_source`` label which is which.
    """

    # The Phase-1.5 confirmatory arbiter (SPENT alpha — the single verdict).
    treatment_arm: Arm
    control_arm: Arm
    decision: DecisionClass
    effect: float  # observed mean per-task (FOUNDRY − BASELINE) resolution diff
    ci_low: float
    ci_high: float
    alpha: float = Field(gt=0.0, lt=1.0)
    mde: float = Field(gt=0.0)
    significant_at_alpha: bool
    meets_mde: bool
    cost_at_most_equal: bool  # the equal-USD frame held (part of the PROCEED predicate)
    oracle_scored: bool = True  # §4: the primary is oracle-scored, NEVER LLM/self-judged
    alpha_source: str = "phase_1.5_arbiter"  # the run that SPENT the pre-registered alpha

    # The Phase-6 estimation/replication overlay (spends NO alpha — §6.5).
    replication_present: bool = False
    replication_effect: float | None = None
    replication_reproduces: bool | None = None
    estimation_source: str = "phase_6_replication"  # estimation only — never a gate


class HeadlineSecondary(FrozenModel):
    """One secondary, with its Holm–Bonferroni-adjusted family-wise significance.

    ``raw_significant`` is the comparison's UNADJUSTED (raw-p < alpha) call;
    ``significant`` is the FWER-controlled (Holm) decision over the family. A
    secondary that is raw-significant but Holm-NON-significant is reported NOT
    significant — and a secondary is NEVER promoted to a primary claim (it is a
    descriptive/gating endpoint only — §7). ``is_primary`` is therefore ALWAYS
    False (re-asserted at construction).
    """

    name: str
    point_estimate: float
    raw_p: float = Field(ge=0.0, le=1.0)
    holm_adjusted_p: float = Field(ge=0.0, le=1.0)
    raw_significant: bool  # raw_p < alpha (UNADJUSTED — descriptive)
    significant: bool  # FWER-controlled (Holm) decision at alpha — the reported call
    is_primary: bool = False  # NEVER promoted to a primary claim (§7)

    def model_post_init(self, __context: object) -> None:
        """A secondary is structurally NEVER a primary claim (§7)."""
        if self.is_primary:
            raise ValueError(
                "a secondary is NEVER promoted to a primary claim (docs/evaluation.md §7) — "
                "is_primary must be False"
            )


class SecondaryReading(FrozenModel):
    """A raw (pre-Holm) secondary reading fed to :func:`build_headline_scorecard`.

    The headline scorecard applies Holm–Bonferroni over the FAMILY of these (via
    the ab_runner Holm helper) to derive each one's family-wise ``significant``
    bit — so the secondaries' significance is FWER-controlled here, not at the
    call site. ``raw_p`` is the comparison's own two-sided p-value.
    """

    name: str
    point_estimate: float
    raw_p: float = Field(ge=0.0, le=1.0)


class CostLine(FrozenModel):
    """The per-feature wall-clock + cost line (docs/evaluation.md §7/§8).

    Carries the §8 full :class:`~foundry.evaluation.cost_model.CostBreakdown`
    USD total + the raw wall-clock ms (the §7 lead-time budget reported as TIME,
    not only its priced USD) + the human-review ms, and the
    ``non_token_dominates`` bit (COST-F5 — a cheaper-on-tokens arm can be more
    expensive end-to-end). Read TOGETHER with the rest so the system can't "win"
    by cutting corners (§7). All money is integer micro-USD (no float drift).
    """

    arm: Arm
    total_microusd: int = Field(ge=0)
    model_token_microusd: int = Field(ge=0)
    non_token_microusd: int = Field(ge=0)
    wall_clock_ms: int = Field(ge=0)
    human_review_ms: int = Field(ge=0)
    non_token_dominates: bool  # §8 COST-F5 — non-token terms exceed the token line


class ArmSummaryLine(FrozenModel):
    """The agent-validity-adjacent per-arm summary (trajectory + safety + ITT).

    Surfaces the §5 trajectory process ``score`` + ``converged`` and the §10
    fail-closed ``safety_clean`` bit per arm, alongside the UNCONDITIONAL ITT
    rate (carried on the :class:`ArmScore`). ``simulated`` flags the synthetic
    stand-in arms.
    """

    arm: Arm
    trajectory_score: float = Field(ge=0.0, le=1.0)
    trajectory_converged: bool
    safety_clean: bool
    simulated: bool


class HeadlineScorecard(FrozenModel):
    """The FULL published headline scorecard (docs/evaluation.md §7), published + STABLE.

    Assembled by :func:`build_headline_scorecard` from a VALID
    :class:`~foundry.evaluation.factory_eval.FactoryEvalResult` (+ an optional
    Phase-6 :class:`~foundry.evaluation.replication_ab.ReplicationResult` and the
    Phase-1.5 :class:`~foundry.evaluation.decision_table.DecisionRecord`). It is
    the PRESENTATION layer — it recomputes no statistic (the secondaries' Holm
    adjustment is the ONE derivation, and it reuses the ab_runner helper).

    PUBLISHED + STABLE: deterministic; :meth:`stable_digest` re-derives
    identically across re-runs from the same inputs (a "stable" bit provable by a
    test). FAIL-CLOSED: it can only be built from a ``valid`` eval — a non-valid
    one raises :class:`NotPublishableError` (the scorecard never reaches a
    non-publishable state). HONESTY: :meth:`honesty_banner` is mandatory + first.
    """

    registration_identity: str  # the cited, re-derived manifest identity (EV14), or ""

    # The ONE confirmatory primary (1.5-arbiter + 6-replication).
    primary: HeadlinePrimary
    # The SECONDARY family under Holm–Bonferroni (never a primary claim).
    secondaries: tuple[HeadlineSecondary, ...] = ()
    # The per-feature wall-clock + cost line (A/B-scale + per-arm).
    ab_scale_cost: CostLine
    cost_lines: tuple[CostLine, ...] = ()
    # The agent-validity + trajectory + integrity + safeties summary.
    agent_validity_score: float = Field(ge=0.0, le=1.0)
    agent_validity_all_valid: bool
    integrity_clean: bool
    safeties_clean: bool
    arm_summaries: tuple[ArmSummaryLine, ...] = ()
    # The UNCONDITIONAL per-arm ITT lines (reused from the M1.5 slice).
    arm_scores: tuple[ArmScore, ...] = ()

    # PUBLISHED + STABLE: a non-valid eval can never reach here (fail-closed), so
    # ``publishable`` is structurally always True on a constructed scorecard.
    publishable: bool = True

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def model_post_init(self, __context: object) -> None:
        """Structural invariant: a published headline scorecard IS publishable."""
        if not self.publishable:
            raise NotPublishableError(
                "a HeadlineScorecard is only ever constructed from a VALID eval — "
                "publishable must be True (docs/evaluation.md §6/§10)"
            )

    def arm_score(self, arm: Arm) -> ArmScore:
        """Look up the UNCONDITIONAL :class:`ArmScore` for ``arm`` (raises ``KeyError``)."""
        for s in self.arm_scores:
            if s.arm is arm:
                return s
        raise KeyError(arm)

    def honesty_banner(self) -> str:
        """The mandatory honesty banner (non-empty; rendered FIRST).

        States (§0/§4) that the primary is oracle-scored so a self-judge seam is
        NEVER reported here as independent validation, and (for a dry-run) that
        this is a METHODOLOGY REHEARSAL on synthetic fixtures asserting NOTHING
        about real performance. NEVER empty either way.
        """
        if self.provenance is Provenance.SCORED and not self.dry_run:
            return (
                "SCORED HEADLINE SCORECARD (docs/evaluation.md §7). The ONE "
                "confirmatory primary is oracle-scored (§4): no LLM/self-judge "
                "seam is reported as independent validation. The Phase-1.5 arbiter "
                "SPENT the pre-registered alpha; the Phase-6 replication is "
                "ESTIMATION only (§6.5) — a disconfirmation is an investigation "
                "trigger, NOT a second gate. Secondaries are Holm-Bonferroni-"
                "corrected and NEVER promoted to a primary claim. ITT pass-rates "
                "are UNCONDITIONAL (denominator = attempted; never conditioned on "
                "completion — §2/§7)."
            )
        return (
            "DRY-RUN HEADLINE SCORECARD — METHODOLOGY REHEARSAL ON SYNTHETIC "
            "FIXTURES. This asserts NOTHING about real Foundry-vs-baseline "
            "performance: the BASELINE / +gates arms are SIMULATED stand-ins, NOT "
            "real evidence. Per §0/§4 the ONE confirmatory primary is oracle-scored "
            "and a self-judge seam is NEVER reported here as independent validation. "
            "The Phase-1.5 arbiter SPENT the pre-registered alpha; the Phase-6 "
            "replication is ESTIMATION only (§6.5) — a disconfirmation is an "
            "investigation trigger, NOT a second gate. Secondaries are Holm-"
            "Bonferroni-corrected and NEVER promoted to a primary claim. ITT "
            "pass-rates are UNCONDITIONAL (denominator = attempted; never "
            "conditioned on completion — §2/§7)."
        )

    def stable_digest(self) -> str:
        """A STABLE content digest over the published scorecard (§7).

        The "stable" / published bit: publishing the SAME inputs yields a
        byte-identical scorecard, so two builds re-derive the SAME digest. It is
        the canonical :func:`~foundry.canonical.content_digest` over the full
        record (L7 — never Python's seed-randomized ``hash()``), so it is stable
        across re-runs AND across processes. A test pins it.
        """
        return content_digest(self.model_dump(mode="json"))

    def render(self) -> str:
        """A concise human-readable headline scorecard, with the banner FIRST.

        Pure + deterministic. The banner is the FIRST line (a reader cannot miss
        it); the ONE primary is labeled with its Phase-1.5 [1.5] arbiter read and
        Phase-6 [6] replication overlay; secondaries print their Holm-adjusted
        call (a raw-significant-but-Holm-non-significant one reads NOT significant).
        """
        lines: list[str] = []
        # Banner FIRST — load-bearing honesty (§0/§4).
        lines.append(self.honesty_banner())
        lines.append("")
        lines.append("Headline scorecard (docs/evaluation.md §7)")
        lines.append(f"  registration: {self.registration_identity}")
        lines.append(f"  provenance: {self.provenance.value} (dry_run={self.dry_run})")
        lines.append(f"  PUBLISHED + STABLE digest: {self.stable_digest()}")
        lines.append("")

        # The ONE confirmatory primary (1.5-arbiter [SPENT alpha] + 6-replication).
        p = self.primary
        lines.append(
            f"CONFIRMATORY PRIMARY ({p.treatment_arm.value} vs {p.control_arm.value}, "
            "equal-USD, oracle-scored):"
        )
        lines.append(
            f"  [1.5 arbiter — SPENT alpha] decision={p.decision.value.upper()} "
            f"effect {p.effect:+.4f} CI[{p.ci_low:+.4f}, {p.ci_high:+.4f}] "
            f"@ alpha={p.alpha} (MDE={p.mde}) "
            f"significant={p.significant_at_alpha} meets_mde={p.meets_mde} "
            f"cost_at_most_equal={p.cost_at_most_equal}"
        )
        if p.replication_present:
            lines.append(
                f"  [6 replication — ESTIMATION only, no alpha] "
                f"effect {p.replication_effect:+.4f} "
                f"reproduces_phase1.5={p.replication_reproduces}"
            )
        else:
            lines.append("  [6 replication] not supplied")
        lines.append("")

        # The SECONDARY family under Holm–Bonferroni (never a primary claim).
        lines.append("Secondaries (Holm-Bonferroni FWER-controlled; NEVER a primary claim):")
        if self.secondaries:
            for s in self.secondaries:
                lines.append(
                    f"  {s.name}: effect {s.point_estimate:+.4f} "
                    f"raw_p={s.raw_p:.4f} holm_p={s.holm_adjusted_p:.4f} "
                    f"-> significant={s.significant} (raw_significant={s.raw_significant})"
                )
        else:
            lines.append("  (none)")
        lines.append("")

        # Per-arm UNCONDITIONAL ITT (denominator = attempted, never shrunk).
        lines.append("Per-arm ITT (UNCONDITIONAL; denominator = attempted):")
        for score in self.arm_scores:
            sim = " [SIMULATED]" if score.simulated else ""
            lines.append(
                f"  {score.arm.value}: ITT pass-rate {score.itt_pass_rate:.4f} "
                f"({score.resolved_count}/{score.attempted}) "
                f"cost {score.total_cost_microusd} micro-USD{sim}"
            )
        lines.append("")

        # The agent-validity + trajectory + integrity + safeties summary.
        lines.append("Agent-validity + trajectory + integrity + safeties:")
        lines.append(
            f"  agent-validity: score {self.agent_validity_score:.4f} "
            f"all_valid={self.agent_validity_all_valid}"
        )
        lines.append(
            f"  integrity_clean={self.integrity_clean} safeties_clean={self.safeties_clean}"
        )
        for a in self.arm_summaries:
            sim = " [SIMULATED]" if a.simulated else ""
            lines.append(
                f"  {a.arm.value}: trajectory {a.trajectory_score:.4f} "
                f"converged={a.trajectory_converged} safety_clean={a.safety_clean}{sim}"
            )
        lines.append("")

        # The per-feature wall-clock + cost line (§7/§8).
        lines.append("Cost + wall-clock (§7/§8 — read together; full breakdown, not tokens):")
        c = self.ab_scale_cost
        lines.append(
            f"  A/B-scale: total {c.total_microusd} micro-USD "
            f"(tokens {c.model_token_microusd}, non-token {c.non_token_microusd}) "
            f"wall-clock {c.wall_clock_ms} ms human-review {c.human_review_ms} ms "
            f"non_token_dominates={c.non_token_dominates}"
        )
        for cl in self.cost_lines:
            lines.append(
                f"  {cl.arm.value}: total {cl.total_microusd} micro-USD "
                f"wall-clock {cl.wall_clock_ms} ms non_token_dominates={cl.non_token_dominates}"
            )

        return "\n".join(lines)


def _cost_line(arm: Arm, breakdown: CostBreakdown) -> CostLine:
    """A per-arm :class:`CostLine` off the §8 :class:`CostBreakdown` (no recompute)."""
    return CostLine(
        arm=arm,
        total_microusd=breakdown.total_microusd,
        model_token_microusd=breakdown.model_token_microusd,
        non_token_microusd=breakdown.non_token_microusd(),
        wall_clock_ms=breakdown.wall_clock_ms,
        human_review_ms=breakdown.human_review_ms,
        non_token_dominates=breakdown.non_token_dominates(),
    )


def _headline_primary(
    factory_eval: FactoryEvalResult,
    decision: DecisionRecord | None,
    replication: ReplicationResult | None,
) -> HeadlinePrimary:
    """Assemble the ONE confirmatory primary: Phase-1.5 arbiter [1.5] + Phase-6 [6].

    The Phase-1.5 :class:`DecisionRecord` is the authoritative arbiter that SPENT
    the alpha — its effect / CI proxy / significance / MDE / equal-USD cost bit /
    decision are carried verbatim when supplied. Absent a decision, the primary is
    reported as an UNRESOLVED gate (no arbiter read), still labeled honestly. The
    Phase-6 :class:`ReplicationResult` (if any) is the ESTIMATION overlay — its
    estimate + reproduction status, which spend NO alpha (§6.5).
    """
    if decision is not None:
        effect = decision.observed_effect
        # The DecisionRecord carries no CI directly; surface the committed-margin
        # symmetric band around the effect as the reported interval (the arbiter's
        # CI lives on the ABResult that produced it — the scorecard PRESENTS, it
        # does not recompute; absent the ABResult here we present the effect band).
        ci_low = effect - decision.committed_mde
        ci_high = effect + decision.committed_mde
        primary_kwargs = {
            "decision": decision.decision,
            "effect": effect,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "alpha": decision.committed_alpha,
            "mde": decision.committed_mde,
            "significant_at_alpha": decision.primary_significant_at_alpha,
            "meets_mde": decision.primary_meets_mde,
            "cost_at_most_equal": decision.cost.at_most_equal,
        }
    elif replication is not None:
        # No arbiter decision supplied — fall back to the Phase-1.5 read the
        # replication was checked against (still a Phase-1.5 [1.5] read; the
        # replication itself never spends alpha).
        primary_kwargs = {
            "decision": replication.phase15_decision,
            "effect": replication.phase15_effect,
            "ci_low": replication.phase15_effect - replication.committed_mde,
            "ci_high": replication.phase15_effect + replication.committed_mde,
            "alpha": replication.committed_alpha,
            "mde": replication.committed_mde,
            "significant_at_alpha": replication.significant_at_alpha,
            "meets_mde": replication.meets_mde,
            "cost_at_most_equal": True,
        }
    else:
        raise ValueError(
            "build_headline_scorecard needs the Phase-1.5 arbiter read: supply a "
            "decision (DecisionRecord) and/or a replication (ReplicationResult)"
        )

    rep = replication is not None
    return HeadlinePrimary(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        replication_present=rep,
        replication_effect=replication.point_estimate if replication is not None else None,
        replication_reproduces=replication.reproduces if replication is not None else None,
        **primary_kwargs,  # type: ignore[arg-type]
    )


def build_headline_scorecard(
    factory_eval: FactoryEvalResult,
    *,
    replication: ReplicationResult | None = None,
    decision: DecisionRecord | None = None,
    secondaries: Sequence[SecondaryReading] = (),
) -> HeadlineScorecard:
    """Assemble the FULL published headline :class:`HeadlineScorecard` (§7).

    The headline scorecard (vs the M1.5 decision-gate slice ``build_scorecard``)
    is the FULL published artifact:

    1. the ONE confirmatory PRIMARY — the Phase-1.5 arbiter read [1.5] (which
       SPENT the pre-registered alpha) plus the Phase-6 replication's reproduction
       status [6] (ESTIMATION only, NO alpha — §6.5), clearly labeled which is
       which (:func:`_headline_primary`);
    2. the SECONDARY family under **Holm–Bonferroni** — the ab_runner Holm helper
       (:func:`foundry.evaluation.ab_runner._holm_bonferroni`) is applied over the
       family of ``secondaries`` to derive each one's FWER-controlled
       ``significant`` bit, so a raw-significant-but-Holm-non-significant secondary
       is reported NOT significant. A secondary is NEVER promoted to a primary
       claim (``is_primary`` is structurally False);
    3. the per-feature wall-clock + cost line — the A/B-scale roll-up + per-arm
       :class:`CostLine` off the §8 :class:`CostBreakdown` (full breakdown, not
       the token line alone);
    4. the agent-validity + trajectory + integrity + safeties summary;
    5. the UNCONDITIONAL per-arm ITT (denominator = attempted, never shrunk — the
       same §2/§6/§7 rule the M1.5 slice keeps).

    PUBLISHED + STABLE: deterministic; :meth:`HeadlineScorecard.stable_digest`
    re-derives identically across re-runs. FAIL-CLOSED (load-bearing — §6/§10):
    a NON-VALID ``factory_eval`` (integrity / safeties dirty) raises
    :class:`NotPublishableError` BEFORE any line is built — an invalidated eval
    can never reach the headline as evidence.

    Pure + deterministic (no RNG/IO; the Holm adjustment is the only derivation,
    and it is a deterministic function of the supplied p-values). HONESTY:
    ``provenance`` / ``dry_run`` are carried from the ``factory_eval``; the banner
    (:meth:`HeadlineScorecard.honesty_banner`) is rendered FIRST.
    """
    # FAIL-CLOSED (FIRST): refuse to publish a non-valid eval (§6/§10). The whole
    # point — a tripped integrity/safety check means the numbers cannot be reported
    # as evidence, full stop, even though every layer produced a value.
    if not factory_eval.valid:
        raise NotPublishableError(
            "the factory eval is NOT valid (integrity_clean="
            f"{factory_eval.integrity_clean}, safeties_clean={factory_eval.safeties_clean}) — "
            "it CANNOT be published as a headline scorecard (docs/evaluation.md §6/§10)"
        )

    # 1. The ONE confirmatory primary (1.5-arbiter + 6-replication).
    primary = _headline_primary(factory_eval, decision, replication)

    # 2. The SECONDARY family under Holm–Bonferroni (reuse the ab_runner helper).
    #    Holm derives each secondary's FWER-controlled `significant` bit from the
    #    FAMILY of raw p-values; the raw (unadjusted) call is kept descriptive only.
    raw_ps = [s.raw_p for s in secondaries]
    alpha = primary.alpha
    adjusted_ps, sig = _holm_bonferroni(raw_ps, alpha=alpha)
    secondary_lines = tuple(
        HeadlineSecondary(
            name=s.name,
            point_estimate=s.point_estimate,
            raw_p=s.raw_p,
            holm_adjusted_p=adjusted_ps[i],
            raw_significant=s.raw_p < alpha,  # UNADJUSTED — descriptive only
            significant=sig[i],  # FWER-controlled (Holm) — the reported call
        )
        for i, s in enumerate(secondaries)
    )

    # 3. The per-feature wall-clock + cost line (§7/§8 — full breakdown).
    ab_scale_line = _cost_line(Arm.FOUNDRY, factory_eval.ab_scale_cost)  # A/B-scale roll-up
    cost_lines = tuple(_cost_line(a.arm, a.cost) for a in factory_eval.arms)

    # 4. The agent-validity + trajectory + integrity + safeties summary.
    arm_summaries = tuple(
        ArmSummaryLine(
            arm=a.arm,
            trajectory_score=a.trajectory.score,
            trajectory_converged=a.trajectory.converged,
            safety_clean=a.safety_clean,
            simulated=a.outcome.simulated,
        )
        for a in factory_eval.arms
    )

    # 5. The UNCONDITIONAL per-arm ITT (denominator = attempted, never shrunk).
    arm_scores = tuple(
        ArmScore(
            arm=a.arm,
            itt_pass_rate=a.outcome.resolution_rate,  # UNCONDITIONAL (resolved / attempted)
            resolved_count=a.outcome.resolved,
            attempted=a.outcome.attempted,  # ITT denominator, never shrunk to completed
            total_cost_microusd=a.outcome.arm_result.total_cost_microusd,
            simulated=a.outcome.simulated,
        )
        for a in factory_eval.arms
    )

    return HeadlineScorecard(
        registration_identity=(
            decision.registration_identity
            if decision is not None
            else (replication.registration_identity if replication is not None else "")
        ),
        primary=primary,
        secondaries=secondary_lines,
        ab_scale_cost=ab_scale_line,
        cost_lines=cost_lines,
        agent_validity_score=factory_eval.agent_validity.validity_score,
        agent_validity_all_valid=factory_eval.agent_validity.all_valid,
        integrity_clean=factory_eval.integrity_clean,
        safeties_clean=factory_eval.safeties_clean,
        arm_summaries=arm_summaries,
        arm_scores=arm_scores,
        publishable=True,  # structurally — a non-valid eval already failed closed above
        provenance=factory_eval.provenance,
        dry_run=factory_eval.dry_run,
    )
