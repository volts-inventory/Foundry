"""The Phase-1.5 DECISION-GATE scorecard slice + unconditional ITT reporting (§7).

This module assembles the **decision-gate slice** of the headline scorecard
(docs/evaluation.md §7) into a single frozen :class:`Scorecard` and renders it
for a human reader. It is the *presentation* layer over the already-computed
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

from typing import TYPE_CHECKING

from pydantic import Field

from foundry.evaluation.hypothesis import Arm, DecisionClass, Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.ab_runner import ABResult
    from foundry.evaluation.ablation import AblationResult
    from foundry.evaluation.decision_table import DecisionRecord
    from foundry.evaluation.preregistration import PreRegistration

__all__ = [
    "ArmScore",
    "PrimaryScore",
    "RoleAblationLine",
    "Scorecard",
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
