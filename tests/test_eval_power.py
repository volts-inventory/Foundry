"""Conformance tests for the power calc + (n,k) allocation (T-M15-05).

Contract (docs/evaluation.md §2; consumes foundry/evaluation/pilot.py read-only):

* CLOSED-FORM: with a chosen base rate / variance / MDE / alpha / power the
  computed per-arm n matches the analytic two-sample z-test expectation;
* MONOTONICITY: smaller MDE -> larger n; higher target_power -> larger n; higher
  variance -> larger n;
* BUDGET CONSTRAINT: an insufficient budget yields `funded == False` /
  UNDERPOWERED and does NOT mutate the committed MDE/alpha;
* DETERMINISM: same inputs -> identical rung (`content_identity()` stable);
* k_repeats: clustered repeats reduce the within-task variance per the formula
  (more k cuts required n while within-task nondeterminism dominates).

PURE STATISTICS dry-run — no model calls, asserts NOTHING about real perf.
"""

from __future__ import annotations

import math

import pytest

from foundry.evaluation.hypothesis import PowerRung, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import (
    _per_arm_task_variance,
    _required_n_per_arm,
    compute_power_rung,
    is_funded,
    normal_cdf,
    normal_ppf,
)

# A micro-USD per-task-repeat cost cheap enough that the budget never binds in the
# "funded" tests (we make the budget large there and tiny in the underpowered test).
_REPEAT_COST = 1_000  # 0.001 USD per task-repeat per arm
_BIG_BUDGET = 10_000_000_000  # 10k USD — never binds for the funded cases


def _pilot(
    *,
    control_base_rate: float = 0.2,
    effect_size_variance: float = 0.0,
    control_resolution_variance: float = 0.0,
    pilot_n: int = 8,
    k_repeats: int = 2,
) -> PilotResult:
    """A hand-built read-only PilotResult fixture (no harness run needed)."""
    return PilotResult(
        control_base_rate=control_base_rate,
        effect_size_variance=effect_size_variance,
        control_resolution_variance=control_resolution_variance,
        pilot_n=pilot_n,
        k_repeats=k_repeats,
        consumed_budget_microusd=0,
        eval_set_identity="test-frame-identity",
    )


# ---- normal helpers ---------------------------------------------------------


def test_normal_ppf_known_quantiles() -> None:
    # Textbook critical values used by the power formula.
    assert normal_ppf(0.975) == pytest.approx(1.959963985, abs=1e-6)
    assert normal_ppf(0.8) == pytest.approx(0.841621233, abs=1e-6)
    assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-9)


def test_normal_ppf_cdf_roundtrip() -> None:
    for p in (0.01, 0.1, 0.5, 0.9, 0.99):
        assert normal_cdf(normal_ppf(p)) == pytest.approx(p, abs=1e-6)


def test_normal_ppf_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        normal_ppf(0.0)
    with pytest.raises(ValueError):
        normal_ppf(1.0)


# ---- closed-form analytic case ----------------------------------------------


def test_closed_form_required_n_matches_analytic() -> None:
    # base_rate=0.2, treatment=0.3 (mde=0.1), zero between-task var, k=1: per-arm
    # per-task variance is the pure Bernoulli p(1-p) on each arm.
    var_c = 0.2 * 0.8
    var_t = 0.3 * 0.7
    z = normal_ppf(0.975) + normal_ppf(0.8)
    analytic = math.ceil(z * z * (var_c + var_t) / 0.1**2)
    assert analytic == 291  # pinned closed-form expectation

    got = _required_n_per_arm(
        var_treatment=var_t, var_control=var_c, mde=0.1, alpha=0.05, target_power=0.8
    )
    assert got == analytic


def test_compute_rung_closed_form_end_to_end() -> None:
    # Same scenario through compute_power_rung at k=1 (k_max=1 forces it). With zero
    # between-task variance, k>1 would only shrink n, so pin k=1 to match analytic.
    pilot = _pilot(control_base_rate=0.2, control_resolution_variance=0.0, effect_size_variance=0.0)
    rung = compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
        k_max=1,
    )
    assert rung.funded is True
    assert rung.underpowered is False
    assert rung.k_repeats == 1
    assert rung.n_tasks == 291
    assert rung.required_n_tasks == 291
    assert rung.achieved_power >= 0.8
    # Cost = 2 arms * n * k * per-repeat cost.
    assert rung.funded_budget_microusd == 2 * 291 * 1 * _REPEAT_COST


# ---- monotonicity -----------------------------------------------------------


def _required_n(
    pilot: PilotResult, *, mde: float, alpha: float, power: float, k_max: int = 1
) -> int:
    rung = compute_power_rung(
        pilot,
        mde=mde,
        alpha=alpha,
        target_power=power,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
        k_max=k_max,
    )
    assert rung.n_tasks is not None
    return rung.n_tasks


def test_monotonic_smaller_mde_needs_more_n() -> None:
    pilot = _pilot()
    n_big_mde = _required_n(pilot, mde=0.2, alpha=0.05, power=0.8)
    n_small_mde = _required_n(pilot, mde=0.1, alpha=0.05, power=0.8)
    assert n_small_mde > n_big_mde


def test_monotonic_higher_power_needs_more_n() -> None:
    pilot = _pilot()
    n_low = _required_n(pilot, mde=0.1, alpha=0.05, power=0.8)
    n_high = _required_n(pilot, mde=0.1, alpha=0.05, power=0.95)
    assert n_high > n_low


def test_monotonic_higher_variance_needs_more_n() -> None:
    low_var = _pilot(control_resolution_variance=0.0, effect_size_variance=0.0)
    high_var = _pilot(control_resolution_variance=0.05, effect_size_variance=0.05)
    n_low = _required_n(low_var, mde=0.1, alpha=0.05, power=0.8)
    n_high = _required_n(high_var, mde=0.1, alpha=0.05, power=0.8)
    assert n_high > n_low


# ---- k_repeats reduces within-task variance ---------------------------------


def test_more_k_reduces_per_task_variance() -> None:
    # The within-task Bernoulli term p(1-p)/k shrinks with k; between-task does not.
    v1 = _per_arm_task_variance(between_task_var=0.01, base_rate=0.2, k_repeats=1)
    v2 = _per_arm_task_variance(between_task_var=0.01, base_rate=0.2, k_repeats=2)
    v4 = _per_arm_task_variance(between_task_var=0.01, base_rate=0.2, k_repeats=4)
    assert v1 > v2 > v4
    # Floor is the between-task variance (within term -> 0 as k -> inf).
    assert v4 > 0.01


def test_more_k_reduces_required_n_when_within_dominates() -> None:
    # Zero between-task var => per-task variance is ALL within-task, so doubling k
    # halves the per-task variance and (roughly) halves required n.
    pilot = _pilot(control_resolution_variance=0.0, effect_size_variance=0.0)
    n_k1 = _required_n(pilot, mde=0.1, alpha=0.05, power=0.8, k_max=1)
    # Force k=2 only by widening k_max but checking the k=2 required-n directly.
    var_c = _per_arm_task_variance(between_task_var=0.0, base_rate=0.2, k_repeats=2)
    var_t = _per_arm_task_variance(between_task_var=0.0, base_rate=0.3, k_repeats=2)
    n_k2 = _required_n_per_arm(
        var_treatment=var_t, var_control=var_c, mde=0.1, alpha=0.05, target_power=0.8
    )
    assert n_k2 < n_k1
    assert n_k2 == pytest.approx(n_k1 / 2, rel=0.02)


# ---- budget constraint / UNDERPOWERED ---------------------------------------


def test_insufficient_budget_reports_underpowered_without_shrinking_mde() -> None:
    pilot = _pilot(
        control_base_rate=0.2, control_resolution_variance=0.01, effect_size_variance=0.01
    )
    # A tiny budget that cannot reach power 0.8 at mde=0.05.
    tiny_budget = 50 * _REPEAT_COST  # only ~25 task-repeats total across both arms
    rung = compute_power_rung(
        pilot,
        mde=0.05,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=tiny_budget,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )
    assert rung.funded is False
    assert rung.underpowered is True
    # The COMMITTED mde/alpha are unchanged — NOT shrunk to fabricate a pass.
    assert rung.mde == 0.05
    assert rung.alpha == 0.05
    assert rung.target_power == 0.8
    # It reports the power the budget actually buys, which is below target.
    assert rung.achieved_power < 0.8
    # The unconstrained required n is reported so reviewers see the shortfall.
    assert rung.required_n_tasks > 0


def test_is_funded_explicit_check() -> None:
    pilot = _pilot(control_base_rate=0.2, control_resolution_variance=0.0)
    rung = compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
        k_max=1,
    )
    assert rung.funded_budget_microusd is not None
    # Funded against its own committed budget; not funded against a smaller one.
    assert is_funded(rung, rung.funded_budget_microusd) is True
    assert is_funded(rung, rung.funded_budget_microusd - 1) is False


def test_is_funded_raises_without_budget_field() -> None:
    rung = PowerRung(mde=0.1, funded_budget_microusd=None)
    with pytest.raises(ValueError):
        is_funded(rung, 1_000)


def test_is_funded_false_on_degenerate_underpowered_rung() -> None:
    # A budget that cannot fund even ONE task-repeat per arm yields a DEGENERATE
    # underpowered rung with funded_budget_microusd == 0. A naive `cost <= budget`
    # would read `0 <= budget` as funded; is_funded must short-circuit False on the
    # rung's own underpowered/not-funded verdict (M15-R1-02).
    pilot = _pilot(
        control_base_rate=0.2, control_resolution_variance=0.01, effect_size_variance=0.01
    )
    rung = compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=1,  # < cost of one task-repeat per arm
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )
    assert rung.underpowered is True and rung.funded is False
    assert rung.funded_budget_microusd == 0  # degenerate
    # Despite funded_budget_microusd == 0 <= any budget, the rung is NOT funded.
    assert is_funded(rung, _BIG_BUDGET) is False
    assert is_funded(rung, 0) is False


def test_budget_just_below_required_is_underpowered() -> None:
    # Compute the funded cost, then offer one micro-USD less -> must be underpowered.
    pilot = _pilot(control_base_rate=0.2, control_resolution_variance=0.0, effect_size_variance=0.0)
    funded = compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
        k_max=1,
    )
    assert funded.funded is True
    assert funded.funded_budget_microusd is not None
    starved = compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=funded.funded_budget_microusd - 1,
        per_task_repeat_cost_microusd=_REPEAT_COST,
        k_max=1,
    )
    assert starved.funded is False
    assert starved.underpowered is True
    assert starved.mde == 0.1  # MDE untouched


# ---- determinism ------------------------------------------------------------


def test_determinism_identical_inputs_identical_rung() -> None:
    pilot = _pilot(
        control_base_rate=0.23, control_resolution_variance=0.012, effect_size_variance=0.02
    )
    kw = {
        "mde": 0.08,
        "alpha": 0.05,
        "target_power": 0.8,
        "budget_usd_microusd": _BIG_BUDGET,
        "per_task_repeat_cost_microusd": _REPEAT_COST,
    }
    a = compute_power_rung(pilot, **kw)
    b = compute_power_rung(pilot, **kw)
    assert a == b
    assert a.content_identity() == b.content_identity()


# ---- validation -------------------------------------------------------------


def test_rejects_bad_params() -> None:
    pilot = _pilot()
    with pytest.raises(ValueError):
        compute_power_rung(
            pilot,
            mde=0.0,
            alpha=0.05,
            target_power=0.8,
            budget_usd_microusd=_BIG_BUDGET,
            per_task_repeat_cost_microusd=_REPEAT_COST,
        )
    with pytest.raises(ValueError):
        compute_power_rung(
            pilot,
            mde=0.1,
            alpha=1.0,
            target_power=0.8,
            budget_usd_microusd=_BIG_BUDGET,
            per_task_repeat_cost_microusd=_REPEAT_COST,
        )
    with pytest.raises(ValueError):
        compute_power_rung(
            pilot,
            mde=0.1,
            alpha=0.05,
            target_power=1.0,
            budget_usd_microusd=_BIG_BUDGET,
            per_task_repeat_cost_microusd=_REPEAT_COST,
        )
    with pytest.raises(ValueError):
        compute_power_rung(
            pilot,
            mde=0.1,
            alpha=0.05,
            target_power=0.8,
            budget_usd_microusd=_BIG_BUDGET,
            per_task_repeat_cost_microusd=0,
        )


def test_rung_provenance_pilot_unchanged_by_calc() -> None:
    # The calc consumes the pilot read-only — does not flip its provenance.
    pilot = _pilot()
    assert pilot.provenance is Provenance.PILOT
    assert pilot.scored is False
    compute_power_rung(
        pilot,
        mde=0.1,
        alpha=0.05,
        target_power=0.8,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )
    assert pilot.provenance is Provenance.PILOT
    assert pilot.scored is False
