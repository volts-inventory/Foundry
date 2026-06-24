"""Conformance tests for the Phase-1.5 decision-gate scorecard (T-M15-10).

Contract (docs/evaluation.md §7; ``build_scorecard`` PRESENTS — never recomputes —
the decision-gate slice from an ``ABResult`` + the ``DecisionRecord`` it produced):

* UNCONDITIONAL ITT: per-arm ITT pass-rate in the scorecard EQUALS the ``ArmReport``
  rate, with denominator = ``attempted`` (n_tasks × k_repeats) — never recomputed,
  never shrunk to completed.
* The ONE confirmatory primary (FOUNDRY vs BASELINE, equal-USD) + the equal-USD cost
  bit are carried faithfully off ``ab.primary`` / the ``DecisionRecord.cost``.
* The decision class + the rule are carried VERBATIM from the ``DecisionRecord``.
* The honesty / dry-run banner is present, non-empty, and appears FIRST in
  ``render()``.
* The role-ablation summary is included when an ``AblationResult`` is supplied.
* Pure + deterministic (same inputs -> identical scorecard + identical render()).
* A HARD_KILL scorecard renders the KILL (no proceed-spin).

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation.ab_runner import (
    ABResult,
    ArmReport,
    BootstrapComparison,
    SecondaryComparison,
)
from foundry.evaluation.ablation import run_ablation
from foundry.evaluation.decision_table import TokenSecondaryReading, decide
from foundry.evaluation.hypothesis import Arm, CentralHypothesis, DecisionClass, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import PreRegistration, freeze_preregistration
from foundry.evaluation.scorecard import (
    ArmScore,
    PrimaryScore,
    Scorecard,
    build_scorecard,
)
from foundry.evaluation.task_fixtures import generate_synthetic_frame
from foundry.state.global_records import GlobalRecords

_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000


# ---------------------------------------------------------------------------
# Builders: a frozen prereg + hand-built ABResults of known shape (mirrors the
# decision-table test fixtures so the scorecard is tested against the SAME types).
# ---------------------------------------------------------------------------


def _funded_rung() -> FundedPowerRung:
    return compute_power_rung(
        PilotResult(
            control_base_rate=0.2,
            effect_size_variance=0.01,
            control_resolution_variance=0.01,
            pilot_n=8,
            k_repeats=2,
            consumed_budget_microusd=10_000,
            eval_set_identity="pilot-eval-set",
        ),
        mde=_MDE,
        alpha=_ALPHA,
        target_power=_TARGET_POWER,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )


def _frozen_prereg(
    scored_identity: str = "scored-set",
    *,
    decision_id: str = "dec",
    provenance: Provenance = Provenance.SCORED,
) -> PreRegistration:
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE,
        funded_power_rung=rung,
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )
    return freeze_preregistration(
        hyp, rung, scored_identity, decision_id=decision_id, ledger=GlobalRecords()
    )


def _arm_report(
    arm: Arm, *, resolved: int, attempted: int, cost: int, simulated: bool
) -> ArmReport:
    return ArmReport(
        arm=arm,
        itt_pass_rate=resolved / attempted,
        resolved_count=resolved,
        attempted=attempted,
        total_cost_microusd=cost,
        simulated=simulated,
    )


def _ab_result(
    *,
    significant: bool,
    meets_mde: bool,
    point: float,
    foundry_cost: int = 1_000,
    baseline_cost: int = 1_000,
    identity: str = "reg-id",
    provenance: Provenance = Provenance.DRY_RUN,
) -> ABResult:
    primary = BootstrapComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=point,
        ci_low=point - 0.05,
        ci_high=point + 0.05,
        alpha=_ALPHA,
        mde=_MDE,
        n_tasks=8,
        resamples=2000,
        significant_at_alpha=significant,
        meets_mde=meets_mde,
    )
    secondary = SecondaryComparison(
        name="foundry_vs_baseline_efficiency",
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=0.0,
        raw_p=0.9,
        holm_adjusted_p=0.9,
        significant=False,
    )
    return ABResult(
        registration_identity=identity,
        scored_eval_set_identity="scored-set",
        seed=0,
        k_repeats=2,
        n_tasks=8,
        # Distinct, KNOWN per-arm ITT denominators / rates so we can assert the
        # scorecard copies them verbatim (unconditional, never recomputed).
        arm_reports=(
            _arm_report(
                Arm.FOUNDRY, resolved=9, attempted=16, cost=foundry_cost, simulated=False
            ),
            _arm_report(
                Arm.BASELINE_SINGLE_AGENT,
                resolved=5,
                attempted=16,
                cost=baseline_cost,
                simulated=True,
            ),
            _arm_report(
                Arm.SINGLE_AGENT_PLUS_GATES,
                resolved=7,
                attempted=16,
                cost=900,
                simulated=True,
            ),
        ),
        primary=primary,
        secondaries=(secondary,),
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )


def _proceed_ab(*, provenance: Provenance = Provenance.DRY_RUN) -> ABResult:
    return _ab_result(
        significant=True,
        meets_mde=True,
        point=0.2,
        foundry_cost=1_000,
        baseline_cost=2_000,
        provenance=provenance,
    )


def _kill_ab(*, provenance: Provenance = Provenance.DRY_RUN) -> ABResult:
    return _ab_result(
        significant=False, meets_mde=False, point=0.01, provenance=provenance
    )


# ---------------------------------------------------------------------------
# UNCONDITIONAL ITT — the scorecard copies the ArmReport rate + attempted verbatim
# ---------------------------------------------------------------------------


def test_itt_rates_equal_arm_report_unconditional() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    decision = decide(ab, prereg)
    card = build_scorecard(ab, decision)

    for report in ab.arm_reports:
        score = card.arm_score(report.arm)
        # The rate is the SAME object value — never recomputed.
        assert score.itt_pass_rate == report.itt_pass_rate
        # Denominator is the UNCONDITIONAL attempted count, never shrunk to completed.
        assert score.attempted == report.attempted
        assert score.resolved_count == report.resolved_count
        # And it IS the unconditional rate (resolved / attempted), not a conditional one.
        assert score.itt_pass_rate == report.resolved_count / report.attempted
        assert score.total_cost_microusd == report.total_cost_microusd
        assert score.simulated == report.simulated


def test_denominator_is_attempted_not_completed() -> None:
    # FOUNDRY: 9 resolved of 16 attempted. A CONDITIONAL rate would divide by a
    # smaller completed count; the scorecard must keep the full attempted denominator.
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    card = build_scorecard(ab, decide(ab, prereg))
    foundry = card.arm_score(Arm.FOUNDRY)
    assert foundry.attempted == 16
    assert foundry.itt_pass_rate == 9 / 16


# ---------------------------------------------------------------------------
# Primary + cost carried faithfully
# ---------------------------------------------------------------------------


def test_primary_and_cost_carried_faithfully() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    decision = decide(ab, prereg)
    card = build_scorecard(ab, decision)

    p = card.primary
    assert p.treatment_arm is Arm.FOUNDRY
    assert p.control_arm is Arm.BASELINE_SINGLE_AGENT
    assert p.point_estimate == ab.primary.point_estimate
    assert p.ci_low == ab.primary.ci_low
    assert p.ci_high == ab.primary.ci_high
    assert p.alpha == ab.primary.alpha
    assert p.mde == ab.primary.mde
    assert p.significant_at_alpha == ab.primary.significant_at_alpha
    assert p.meets_mde == ab.primary.meets_mde
    # Equal-USD cost mirrors the DecisionRecord's CostComparison exactly.
    assert p.treatment_cost_microusd == decision.cost.treatment_cost_microusd
    assert p.control_cost_microusd == decision.cost.control_cost_microusd
    assert p.cost_at_most_equal == decision.cost.at_most_equal
    assert p.cost_at_most_equal is True  # FOUNDRY 1000 <= BASELINE 2000


# ---------------------------------------------------------------------------
# Decision class + rule carried verbatim
# ---------------------------------------------------------------------------


def test_decision_and_rule_carried_faithfully() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    decision = decide(ab, prereg)
    card = build_scorecard(ab, decision)
    assert card.decision is DecisionClass.PROCEED
    assert card.decision is decision.decision
    assert card.rule == decision.rule
    assert card.registration_identity == decision.registration_identity


# ---------------------------------------------------------------------------
# Honesty / dry-run banner present, non-empty, FIRST in render()
# ---------------------------------------------------------------------------


def test_honesty_banner_present_and_first() -> None:
    prereg = _frozen_prereg(provenance=Provenance.DRY_RUN)
    ab = _proceed_ab(provenance=Provenance.DRY_RUN)
    card = build_scorecard(ab, decide(ab, prereg))

    banner = card.honesty_banner()
    assert banner  # non-empty
    assert banner.strip()

    rendered = card.render()
    # The banner is the FIRST thing in render() — a reader cannot miss it.
    assert rendered.startswith(banner)
    # It names the dry-run / synthetic / not-real-evidence honesty.
    low = banner.lower()
    assert "dry-run" in low
    assert "synthetic" in low or "simulated" in low
    # §0/§4: a self-judge seam is never reported as independent validation.
    assert "self-judge" in low or "oracle-scored" in low


def test_dry_run_flag_carried() -> None:
    prereg = _frozen_prereg(provenance=Provenance.DRY_RUN)
    ab = _proceed_ab(provenance=Provenance.DRY_RUN)
    card = build_scorecard(ab, decide(ab, prereg))
    assert card.dry_run is True
    assert card.provenance is Provenance.DRY_RUN


# ---------------------------------------------------------------------------
# Ablation summary included when provided
# ---------------------------------------------------------------------------


def test_ablation_summary_included_when_provided() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    decision = decide(ab, prereg)
    frame = generate_synthetic_frame(seed=1)
    ablation = run_ablation(frame, seed=1)

    card = build_scorecard(ab, decision, ablation=ablation)
    assert len(card.ablation_lines) == len(ablation.deltas)
    assert card.ablation_simulated is ablation.simulated_ablation
    by_role = {line.role: line for line in card.ablation_lines}
    for d in ablation.deltas:
        line = by_role[d.role.value]
        assert line.delta == d.delta
        assert line.cost_delta_microusd == d.cost_delta_microusd
        assert line.earns_place == d.earns_place
    # A non-earning role (QA, contribution 0.0 in the dry-run table) shows up.
    assert any(not line.earns_place for line in card.ablation_lines)
    # And the ablation summary surfaces in render().
    assert "earn their place" in card.render().lower()


def test_no_ablation_means_empty_summary() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    card = build_scorecard(ab, decide(ab, prereg))
    assert card.ablation_lines == ()
    assert card.ablation_simulated is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_scorecard_and_render() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    decision = decide(ab, prereg)
    frame = generate_synthetic_frame(seed=3)
    ablation = run_ablation(frame, seed=3)

    a = build_scorecard(ab, decision, ablation=ablation)
    b = build_scorecard(ab, decision, ablation=ablation)
    assert a == b
    assert a.content_identity() == b.content_identity()
    assert a.render() == b.render()


# ---------------------------------------------------------------------------
# A HARD_KILL renders the KILL — no proceed-spin
# ---------------------------------------------------------------------------


def test_hard_kill_renders_the_kill() -> None:
    prereg = _frozen_prereg()
    ab = _kill_ab()  # not significant, effect < MDE
    decision = decide(ab, prereg)  # no token reading -> HARD_KILL
    assert decision.decision is DecisionClass.HARD_KILL

    card = build_scorecard(ab, decision)
    assert card.decision is DecisionClass.HARD_KILL
    rendered = card.render()
    assert "HARD_KILL" in rendered
    assert card.rule in rendered
    # No proceed-spin: the verdict is not dressed up as a PROCEED.
    assert "PROCEED" not in rendered.upper().replace("HARD_KILL", "")


def test_redesign_branch_renders_when_token_positive() -> None:
    prereg = _frozen_prereg()
    ab = _kill_ab()  # USD-null
    token = TokenSecondaryReading(
        point_estimate=0.12,
        mde=0.1,
        alpha=0.05,
        significant_at_alpha=True,
        meets_mde=True,
        positive=True,
    )
    decision = decide(ab, prereg, token_reading=token)
    assert decision.decision is DecisionClass.BOUNDED_OVERHEAD_REDESIGN
    card = build_scorecard(ab, decision)
    assert card.decision is DecisionClass.BOUNDED_OVERHEAD_REDESIGN
    assert "BOUNDED_OVERHEAD_REDESIGN" in card.render()


# ---------------------------------------------------------------------------
# Scored-provenance banner is also non-empty (and not the dry-run text)
# ---------------------------------------------------------------------------


def test_scored_banner_non_empty() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab(provenance=Provenance.SCORED)
    card = build_scorecard(ab, decide(ab, prereg))
    banner = card.honesty_banner()
    assert banner
    assert "scored" in banner.lower()
    assert card.render().startswith(banner)


# ---------------------------------------------------------------------------
# Type sanity: the public score types are frozen
# ---------------------------------------------------------------------------


def test_score_types_are_frozen() -> None:
    prereg = _frozen_prereg()
    ab = _proceed_ab()
    card = build_scorecard(ab, decide(ab, prereg))
    assert isinstance(card, Scorecard)
    assert isinstance(card.arm_scores[0], ArmScore)
    assert isinstance(card.primary, PrimaryScore)
    with pytest.raises(ValidationError):
        card.arm_scores[0].itt_pass_rate = 0.5  # type: ignore[misc]
