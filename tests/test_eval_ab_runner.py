"""Conformance tests for the powered 3-arm A/B runner (T-M15-07).

Contract (docs/evaluation.md §2/§7; the decision table consumes ``ABResult``):

* CITE-FROZEN-PREREG GATE: ``run_ab`` FIRST asserts the cited manifest is
  unmodified; a tampered/edited (!= identity) pre-registration raises before any
  arm runs.
* SCORED-SLICE-ONLY: runs over ``frame.scored_slice`` (disjoint from the pilot,
  bound to the manifest's ``scored_eval_set_identity``); a mismatched identity or
  pilot overlap raises.
* THREE ARMS: all of FOUNDRY / BASELINE_SINGLE_AGENT / SINGLE_AGENT_PLUS_GATES
  are present; ITT pass-rate uses the UNCONDITIONAL denominator (n*k); equal-USD
  cost recorded per arm.
* PAIRED CLUSTER BOOTSTRAP: deterministic (seed -> identical CI); a large true
  FOUNDRY−BASELINE gap is significant, a zero gap is not.
* HOLM–BONFERRONI: monotone step-down ordering on a constructed p-vector; FWER
  controlled.

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.ab_runner import (
    AB_ARMS,
    ABResult,
    ScoredSliceContaminationError,
    _bootstrap_diff_means,
    _holm_bonferroni,
    _paired_diffs,
    _seeded_rng,
    run_ab,
)
from foundry.evaluation.harness import ArmResult, TaskOutcome, run_arm
from foundry.evaluation.hypothesis import Arm, CentralHypothesis, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import (
    PreRegistration,
    TamperedPreRegistrationError,
    freeze_preregistration,
)
from foundry.evaluation.task_fixtures import generate_synthetic_frame
from foundry.state.global_records import GlobalRecords

_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000
_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8


# ---------------------------------------------------------------------------
# Fixtures: a small synthetic frame + a funded manifest BOUND to its scored slice
# ---------------------------------------------------------------------------


def _pilot() -> PilotResult:
    return PilotResult(
        control_base_rate=0.2,
        effect_size_variance=0.01,
        control_resolution_variance=0.01,
        pilot_n=8,
        k_repeats=2,
        consumed_budget_microusd=10_000,
        eval_set_identity="pilot-eval-set",
    )


def _funded_rung(*, k_repeats: int = 1) -> FundedPowerRung:
    rung = compute_power_rung(
        _pilot(),
        mde=_MDE,
        alpha=_ALPHA,
        target_power=_TARGET_POWER,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )
    # The dry-run A/B uses a small k so the FOUNDRY arm (real pipeline keyless via
    # the doubles) stays cheap; the power calc's n is irrelevant to the runner.
    return rung.model_copy(update={"k_repeats": k_repeats, "n_tasks": rung.n_tasks})


def _frozen_prereg(
    frame, *, k_repeats: int = 1, provenance: Provenance = Provenance.SCORED
) -> PreRegistration:
    rung = _funded_rung(k_repeats=k_repeats)
    hyp = CentralHypothesis(
        mde=_MDE,
        funded_power_rung=rung,
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )
    return freeze_preregistration(
        hyp,
        rung,
        frame.scored_slice.identity,  # BIND to the scored slice
        decision_id="dec-ab",
        ledger=GlobalRecords(),
    )


# ---------------------------------------------------------------------------
# CITE-FROZEN-PREREG GATE
# ---------------------------------------------------------------------------


def test_run_ab_rejects_a_tampered_prereg() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=1)
    prereg = _frozen_prereg(frame)
    # Edit only the top-level mde mirror (leave the rung) -> mirror divergence ->
    # assert_unmodified fails closed BEFORE any arm runs.
    tampered = prereg.model_copy(update={"mde": 0.2})
    with pytest.raises(TamperedPreRegistrationError):
        run_ab(frame, tampered, seed=0)


def test_run_ab_prereg_gate_fires_before_slice_checks() -> None:
    # The cite-frozen-prereg gate is FIRST: a tampered prereg whose scored binding
    # would ALSO mismatch the frame still raises the TAMPER error (not the slice
    # error) — proving assert_unmodified runs before the slice checks.
    frame = generate_synthetic_frame(n_tasks=6, seed=1)
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE, funded_power_rung=rung, provenance=Provenance.SCORED, dry_run=False
    )
    prereg = freeze_preregistration(
        hyp, rung, "some-other-frame", decision_id="dec-ab", ledger=GlobalRecords()
    )
    tampered = prereg.model_copy(update={"mde": 0.2})  # mirror divergence
    with pytest.raises(TamperedPreRegistrationError):
        run_ab(frame, tampered, seed=0)


# ---------------------------------------------------------------------------
# SCORED-SLICE-ONLY
# ---------------------------------------------------------------------------


def test_run_ab_runs_scored_slice_disjoint_from_pilot() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=2)
    prereg = _frozen_prereg(frame)
    result = run_ab(frame, prereg, seed=0)

    scored_ids = set(frame.scored_slice.task_ids)
    pilot_ids = set(frame.pilot_slice.task_ids)
    assert scored_ids and not (scored_ids & pilot_ids)  # disjoint, non-empty
    assert result.scored_eval_set_identity == frame.scored_slice.identity
    assert result.n_tasks == len(scored_ids)


def test_run_ab_rejects_scored_identity_mismatch() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=3)
    # Freeze the manifest bound to a DIFFERENT scored-slice identity.
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE, funded_power_rung=rung, provenance=Provenance.SCORED, dry_run=False
    )
    prereg = freeze_preregistration(
        hyp, rung, "some-other-frame", decision_id="dec-ab", ledger=GlobalRecords()
    )
    with pytest.raises(ScoredSliceContaminationError):
        run_ab(frame, prereg, seed=0)


# ---------------------------------------------------------------------------
# THREE ARMS + ITT UNCONDITIONAL + EQUAL-USD
# ---------------------------------------------------------------------------


def test_run_ab_reports_three_arms_itt_unconditional_and_cost() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=4)
    prereg = _frozen_prereg(frame, k_repeats=2)
    result = run_ab(frame, prereg, seed=0)

    arms_present = {r.arm for r in result.arm_reports}
    assert arms_present == set(AB_ARMS)
    assert set(AB_ARMS) == {Arm.FOUNDRY, Arm.BASELINE_SINGLE_AGENT, Arm.SINGLE_AGENT_PLUS_GATES}

    n_scored = len(frame.scored_slice.task_ids)
    for r in result.arm_reports:
        # ITT denominator is UNCONDITIONAL: n_tasks * k_repeats, never conditioned
        # on completion.
        assert r.attempted == n_scored * 2
        assert 0.0 <= r.itt_pass_rate <= 1.0
        assert r.itt_pass_rate == pytest.approx(r.resolved_count / r.attempted)
        assert r.total_cost_microusd > 0  # equal-USD cost recorded

    # The two simulated arms are flagged; FOUNDRY runs the real pipeline.
    assert result.report(Arm.FOUNDRY).simulated is False
    assert result.report(Arm.BASELINE_SINGLE_AGENT).simulated is True
    assert result.report(Arm.SINGLE_AGENT_PLUS_GATES).simulated is True


def test_run_ab_carries_provenance_and_dry_run_flag() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=5)
    scored = run_ab(frame, _frozen_prereg(frame, provenance=Provenance.SCORED), seed=0)
    assert scored.provenance is Provenance.SCORED and scored.dry_run is False

    dry = run_ab(frame, _frozen_prereg(frame, provenance=Provenance.DRY_RUN), seed=0)
    assert dry.provenance is Provenance.DRY_RUN and dry.dry_run is True


# ---------------------------------------------------------------------------
# PAIRED CLUSTER BOOTSTRAP — determinism + correctness on constructed data
# ---------------------------------------------------------------------------


def test_run_ab_is_deterministic_same_seed_same_ci() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=6)
    prereg = _frozen_prereg(frame)
    a = run_ab(frame, prereg, seed=0)
    b = run_ab(frame, prereg, seed=0)
    assert a.primary.ci_low == b.primary.ci_low
    assert a.primary.ci_high == b.primary.ci_high
    assert a.primary.point_estimate == b.primary.point_estimate
    assert a == b  # the whole frozen result coincides


def _arm_result_from_rates(arm: Arm, rates: dict[str, float]) -> ArmResult:
    """A hand-built single-repeat ArmResult whose per-task resolution == ``rates``.

    Each rate must be 0.0 or 1.0 (one repeat/task). Used to drive the bootstrap
    helpers on CONSTRUCTED data with a known FOUNDRY−BASELINE gap.
    """
    outcomes = tuple(
        TaskOutcome(
            task_id=tid,
            repeat=0,
            arm=arm,
            status="resolved" if r >= 0.5 else "unresolved",  # type: ignore[arg-type]
            resolved=r >= 0.5,
            cost_microusd=1_000_000,
            expected_output="x",
            produced_output="x" if r >= 0.5 else None,
        )
        for tid, r in sorted(rates.items())
    )
    return ArmResult(
        arm=arm,
        slice_identity="constructed",
        slice_name="scored",
        k_repeats=1,
        seed=0,
        budget_usd_microusd=5_000_000,
        attempted=len(rates),
        outcomes=outcomes,
        simulated=arm is not Arm.FOUNDRY,
    )


def test_bootstrap_large_gap_is_significant() -> None:
    # FOUNDRY resolves every task, BASELINE resolves none -> per-task diff == 1.0
    # for all tasks -> the bootstrap CI is entirely above 0 -> significant.
    tids = [f"t{i:02d}" for i in range(20)]
    foundry = _arm_result_from_rates(Arm.FOUNDRY, {t: 1.0 for t in tids})
    baseline = _arm_result_from_rates(Arm.BASELINE_SINGLE_AGENT, {t: 0.0 for t in tids})
    diffs = _paired_diffs(foundry, baseline)
    assert diffs == [1.0] * 20

    boot = _bootstrap_diff_means(diffs, resamples=2000, rng=_seeded_rng(0, "primary"))
    ci_low, ci_high = boot[int(0.025 * (len(boot) - 1))], boot[int(0.975 * (len(boot) - 1))]
    assert ci_low > 0.0 and ci_high > 0.0  # CI excludes 0 -> significant


def test_bootstrap_zero_gap_is_not_significant() -> None:
    # Identical per-task resolution in both arms -> every per-task diff == 0 ->
    # the bootstrap distribution is a point mass at 0 -> CI = [0, 0] -> NOT
    # significant (does not exclude 0).
    tids = [f"t{i:02d}" for i in range(20)]
    rates = {t: (1.0 if i % 2 == 0 else 0.0) for i, t in enumerate(tids)}
    foundry = _arm_result_from_rates(Arm.FOUNDRY, rates)
    baseline = _arm_result_from_rates(Arm.BASELINE_SINGLE_AGENT, rates)
    diffs = _paired_diffs(foundry, baseline)
    assert diffs == [0.0] * 20

    boot = _bootstrap_diff_means(diffs, resamples=2000, rng=_seeded_rng(0, "primary"))
    assert all(m == 0.0 for m in boot)
    ci_low, ci_high = boot[0], boot[-1]
    assert ci_low == 0.0 and ci_high == 0.0  # CI includes 0 -> NOT significant


def test_bootstrap_resamples_tasks_not_runs() -> None:
    # The bootstrap is over the per-task cluster vector: its length == n_tasks, and
    # each resample is a mean of n_tasks task-level diffs. With k>1 the per-task
    # rate collapses repeats BEFORE the bootstrap (n*k is not independent draws).
    frame = generate_synthetic_frame(n_tasks=8, seed=7)
    prereg = _frozen_prereg(frame, k_repeats=3)
    result = run_ab(frame, prereg, seed=0)
    # The primary's n_tasks is the CLUSTER count (distinct scored tasks), not n*k.
    assert result.primary.n_tasks == len(frame.scored_slice.task_ids)
    assert result.k_repeats == 3


# ---------------------------------------------------------------------------
# HOLM–BONFERRONI
# ---------------------------------------------------------------------------


def test_holm_bonferroni_step_down_ordering_and_monotonicity() -> None:
    # Classic worked example: m=4, alpha=0.05.
    raw = [0.01, 0.04, 0.03, 0.005]
    adjusted, sig = _holm_bonferroni(raw, alpha=0.05)

    # Adjusted p (in rank order) is monotone non-decreasing.
    order = sorted(range(4), key=lambda i: raw[i])
    adj_sorted = [adjusted[i] for i in order]
    assert adj_sorted == sorted(adj_sorted)

    # Step-down thresholds: sorted raw = [0.005, 0.01, 0.03, 0.04];
    # compare to 0.05/4=0.0125, 0.05/3=0.01667, 0.05/2=0.025, 0.05/1=0.05.
    #  0.005 <= 0.0125  -> reject
    #  0.01  <= 0.01667 -> reject
    #  0.03  >  0.025   -> ACCEPT -> stop (step-down halts here)
    #  0.04             -> not rejected (even though 0.04 <= 0.05)
    assert sig[raw.index(0.005)] is True
    assert sig[raw.index(0.01)] is True
    assert sig[raw.index(0.03)] is False
    assert sig[raw.index(0.04)] is False  # step-down: blocked by the earlier accept


def test_holm_bonferroni_controls_fwer_all_large_p() -> None:
    # All large p-values -> nothing significant (FWER controlled).
    raw = [0.5, 0.6, 0.9]
    _adj, sig = _holm_bonferroni(raw, alpha=0.05)
    assert sig == [False, False, False]


def test_holm_bonferroni_smallest_p_uses_full_correction() -> None:
    # A single very small p must clear alpha/m to be rejected.
    raw = [0.001, 0.5, 0.5, 0.5]  # m=4 -> threshold 0.0125 for the smallest
    adjusted, sig = _holm_bonferroni(raw, alpha=0.05)
    assert sig[0] is True
    assert adjusted[0] == pytest.approx(min(1.0, 4 * 0.001))


def test_holm_bonferroni_empty_family() -> None:
    assert _holm_bonferroni([], alpha=0.05) == ([], [])


# ---------------------------------------------------------------------------
# Secondary family is present + Holm-corrected in the ABResult
# ---------------------------------------------------------------------------


def test_run_ab_secondary_family_holm_corrected() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=8)
    result = run_ab(frame, _frozen_prereg(frame), seed=0)
    assert len(result.secondaries) == 2
    for s in result.secondaries:
        assert 0.0 <= s.raw_p <= 1.0
        assert 0.0 <= s.holm_adjusted_p <= 1.0
        # Holm adjusted p is never smaller than the raw p (it only inflates).
        assert s.holm_adjusted_p >= s.raw_p - 1e-12


def test_abresult_is_a_frozen_model() -> None:
    assert ABResult.model_config["frozen"] is True
    assert ABResult.model_config["extra"] == "forbid"


def test_run_arm_sanity_on_scored_slice() -> None:
    # Sanity: run_arm over the scored slice yields one outcome per (task, repeat).
    frame = generate_synthetic_frame(n_tasks=6, seed=9)
    scored = frame.scored_slice
    fixtures = {f.task_id: f for f in frame.fixtures if f.task_id in set(scored.task_ids)}
    res = run_arm(Arm.BASELINE_SINGLE_AGENT, scored, fixtures, k_repeats=2, seed=0)
    assert len(res.outcomes) == len(scored.task_ids) * 2
