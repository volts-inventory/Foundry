"""Conformance tests for the Phase-1.5 decision table (T-M15-08).

Contract (docs/evaluation.md §2.5; ``decide`` consumes an ``ABResult`` + the
frozen pre-registration it cites):

* CITE-FROZEN-PREREG GATE: ``decide`` FIRST asserts the cited manifest is
  unmodified; a tampered/edited (!= identity) pre-registration raises BEFORE any
  verdict is computed.
* PROCEED iff the primary is significant_at_alpha AND meets_mde AND FOUNDRY cost
  <= BASELINE (the equal-USD frame). It is IMPOSSIBLE to reach PROCEED on a null.
* HARD_KILL on a USD-null with no supplied positive token reading (terminal).
* BOUNDED_OVERHEAD_REDESIGN on a USD-null WITH a supplied positive effective-token
  secondary (§2.5 row 3) — never fabricated from the USD-denominated secondaries.
* A cost-worse win is NOT a PROCEED.
* Pure + deterministic.

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.ab_runner import (
    ABResult,
    ArmReport,
    BootstrapComparison,
    SecondaryComparison,
)
from foundry.evaluation.decision_table import (
    CostComparison,
    DecisionRecord,
    TokenSecondaryReading,
    decide,
)
from foundry.evaluation.hypothesis import Arm, CentralHypothesis, DecisionClass, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import (
    PreRegistration,
    TamperedPreRegistrationError,
    freeze_preregistration,
)
from foundry.state.global_records import GlobalRecords

_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000


# ---------------------------------------------------------------------------
# Builders: a frozen prereg + hand-constructed ABResults of known shape
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
    scored_identity: str = "scored-set", *, decision_id: str = "dec"
) -> PreRegistration:
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE,
        funded_power_rung=rung,
        provenance=Provenance.SCORED,
        dry_run=False,
    )
    return freeze_preregistration(
        hyp, rung, scored_identity, decision_id=decision_id, ledger=GlobalRecords()
    )


def _arm_report(arm: Arm, *, rate: float, cost: int, simulated: bool) -> ArmReport:
    attempted = 10
    resolved = round(rate * attempted)
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
    provenance: Provenance = Provenance.SCORED,
) -> ABResult:
    """A hand-built ABResult with the exact primary bits + equal-USD costs we need."""
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
        k_repeats=1,
        n_tasks=8,
        arm_reports=(
            _arm_report(Arm.FOUNDRY, rate=0.5, cost=foundry_cost, simulated=False),
            _arm_report(Arm.BASELINE_SINGLE_AGENT, rate=0.3, cost=baseline_cost, simulated=True),
            _arm_report(Arm.SINGLE_AGENT_PLUS_GATES, rate=0.4, cost=900, simulated=True),
        ),
        primary=primary,
        secondaries=(secondary,),
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )


def _token_reading(*, positive: bool) -> TokenSecondaryReading:
    return TokenSecondaryReading(
        point_estimate=0.12 if positive else 0.01,
        mde=0.1,
        alpha=0.05,
        significant_at_alpha=positive,
        meets_mde=positive,
        positive=positive,
    )


# ---------------------------------------------------------------------------
# PROCEED — a clear win (significant + meets_mde + <= cost)
# ---------------------------------------------------------------------------


def test_clear_win_proceeds() -> None:
    prereg = _frozen_prereg()
    ab = _ab_result(
        significant=True, meets_mde=True, point=0.18, foundry_cost=900, baseline_cost=1_000
    )
    rec = decide(ab, prereg)
    assert rec.decision is DecisionClass.PROCEED
    assert rec.primary_significant_at_alpha and rec.primary_meets_mde
    assert rec.cost.at_most_equal is True
    assert rec.observed_effect == pytest.approx(0.18)
    assert rec.committed_mde == _MDE and rec.committed_alpha == _ALPHA
    assert "PROCEED" in rec.rule and "§2.5 row 1" in rec.rule


def test_equal_cost_win_proceeds() -> None:
    # Equal-USD (exactly equal) still satisfies cost <= baseline.
    prereg = _frozen_prereg()
    ab = _ab_result(
        significant=True, meets_mde=True, point=0.2, foundry_cost=1_000, baseline_cost=1_000
    )
    assert decide(ab, prereg).decision is DecisionClass.PROCEED


# ---------------------------------------------------------------------------
# HARD_KILL — a null at the funded MDE
# ---------------------------------------------------------------------------


def test_not_significant_is_hard_kill() -> None:
    prereg = _frozen_prereg()
    ab = _ab_result(significant=False, meets_mde=False, point=0.01)
    rec = decide(ab, prereg)
    assert rec.decision is DecisionClass.HARD_KILL
    assert "TERMINAL" in rec.rule and "not significant" in rec.rule
    assert rec.token_reading is None and rec.token_axis_available is False


def test_effect_below_mde_is_hard_kill() -> None:
    # Significant but the effect does not meet the committed MDE -> still a null.
    prereg = _frozen_prereg()
    ab = _ab_result(significant=True, meets_mde=False, point=0.04)
    rec = decide(ab, prereg)
    assert rec.decision is DecisionClass.HARD_KILL
    assert "effect < committed MDE" in rec.rule


def test_hard_kill_record_exposes_no_repower_path() -> None:
    # The DecisionRecord carries no re-test/re-power field; a HARD_KILL is terminal.
    prereg = _frozen_prereg()
    rec = decide(_ab_result(significant=False, meets_mde=False, point=0.0), prereg)
    assert rec.decision is DecisionClass.HARD_KILL
    fields = set(DecisionRecord.model_fields)
    assert not any("repower" in f or "re_test" in f or "retest" in f for f in fields)


# ---------------------------------------------------------------------------
# BOUNDED_OVERHEAD_REDESIGN — USD-null but token-positive
# ---------------------------------------------------------------------------


def test_usd_null_token_positive_is_bounded_redesign() -> None:
    prereg = _frozen_prereg()
    ab = _ab_result(significant=False, meets_mde=False, point=0.02)
    rec = decide(ab, prereg, token_reading=_token_reading(positive=True))
    assert rec.decision is DecisionClass.BOUNDED_OVERHEAD_REDESIGN
    assert rec.token_axis_available is True
    assert rec.token_reading is not None and rec.token_reading.positive
    assert "scope-frozen" in rec.rule and "NOT an open-ended re-power" in rec.rule


def test_usd_null_token_null_is_hard_kill() -> None:
    # A token reading that is NOT positive does not open the redesign branch.
    prereg = _frozen_prereg()
    ab = _ab_result(significant=False, meets_mde=False, point=0.02)
    rec = decide(ab, prereg, token_reading=_token_reading(positive=False))
    assert rec.decision is DecisionClass.HARD_KILL
    assert rec.token_axis_available is True  # supplied, but null


def test_proceed_ignores_token_secondary() -> None:
    # §2.5 row 1: a positive primary PROCEEDs regardless of the secondary.
    prereg = _frozen_prereg()
    ab = _ab_result(significant=True, meets_mde=True, point=0.2)
    rec = decide(ab, prereg, token_reading=_token_reading(positive=True))
    assert rec.decision is DecisionClass.PROCEED


# ---------------------------------------------------------------------------
# Cost-worse win is NOT a PROCEED
# ---------------------------------------------------------------------------


def test_cost_worse_win_is_not_proceed() -> None:
    # Significant + meets_mde on resolution, but FOUNDRY costs MORE than BASELINE:
    # the equal-USD frame is violated, so this is NOT a PROCEED.
    prereg = _frozen_prereg()
    ab = _ab_result(
        significant=True, meets_mde=True, point=0.2, foundry_cost=2_000, baseline_cost=1_000
    )
    rec = decide(ab, prereg)
    assert rec.decision is DecisionClass.HARD_KILL
    assert rec.cost.at_most_equal is False
    assert "equal-USD frame violated" in rec.rule


def test_cost_worse_win_with_token_positive_is_redesign() -> None:
    # A cost-worse win is a USD-null; with a supplied positive token reading it
    # routes to the bounded redesign, never to PROCEED.
    prereg = _frozen_prereg()
    ab = _ab_result(
        significant=True, meets_mde=True, point=0.2, foundry_cost=2_000, baseline_cost=1_000
    )
    rec = decide(ab, prereg, token_reading=_token_reading(positive=True))
    assert rec.decision is DecisionClass.BOUNDED_OVERHEAD_REDESIGN


# ---------------------------------------------------------------------------
# CITE-FROZEN-PREREG GATE — a tampered manifest raises
# ---------------------------------------------------------------------------


def test_decide_rejects_a_tampered_prereg() -> None:
    prereg = _frozen_prereg()
    # Edit only the top-level mde mirror (leave the rung) -> mirror divergence ->
    # assert_unmodified fails closed BEFORE any verdict is computed.
    tampered = prereg.model_copy(update={"mde": 0.2})
    ab = _ab_result(significant=True, meets_mde=True, point=0.2)
    with pytest.raises(TamperedPreRegistrationError):
        decide(ab, tampered)


# ---------------------------------------------------------------------------
# No-null-to-PROCEED structural invariant (the record itself forbids it)
# ---------------------------------------------------------------------------


def test_cannot_construct_proceed_on_a_null() -> None:
    # Even hand-constructing a DecisionRecord, PROCEED on a null is rejected.
    cost = CostComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        treatment_cost_microusd=1_000,
        control_cost_microusd=1_000,
        at_most_equal=True,
    )
    with pytest.raises(ValueError, match="PROCEED-predicate mismatch"):
        DecisionRecord(
            decision=DecisionClass.PROCEED,
            rule="forged",
            primary_significant_at_alpha=False,  # NULL
            primary_meets_mde=False,
            observed_effect=0.0,
            committed_mde=_MDE,
            committed_alpha=_ALPHA,
            cost=cost,
            token_axis_available=False,
            registration_identity="x",
        )


def test_cannot_construct_redesign_without_positive_token() -> None:
    cost = CostComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        treatment_cost_microusd=1_000,
        control_cost_microusd=1_000,
        at_most_equal=True,
    )
    with pytest.raises(ValueError, match="requires a SUPPLIED positive"):
        DecisionRecord(
            decision=DecisionClass.BOUNDED_OVERHEAD_REDESIGN,
            rule="forged",
            primary_significant_at_alpha=False,
            primary_meets_mde=False,
            observed_effect=0.0,
            committed_mde=_MDE,
            committed_alpha=_ALPHA,
            cost=cost,
            token_axis_available=False,
            token_reading=None,  # no positive token reading
            registration_identity="x",
        )


# ---------------------------------------------------------------------------
# Determinism + provenance carry-through
# ---------------------------------------------------------------------------


def test_decide_is_deterministic() -> None:
    prereg = _frozen_prereg()
    ab = _ab_result(significant=True, meets_mde=True, point=0.2)
    a = decide(ab, prereg)
    b = decide(ab, prereg)
    assert a == b
    assert a.content_identity() == b.content_identity()


def test_decide_carries_provenance_and_dry_run() -> None:
    prereg = _frozen_prereg()
    ab = _ab_result(significant=True, meets_mde=True, point=0.2, provenance=Provenance.SCORED)
    rec = decide(ab, prereg)
    # provenance comes from the cited manifest's hypothesis, not the ABResult.
    assert rec.provenance is Provenance.SCORED and rec.dry_run is False
    assert rec.registration_identity == prereg.registration_identity()
