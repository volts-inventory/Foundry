"""Conformance tests for the variance/base-rate PILOT (T-M15-04).

Contract (docs/evaluation.md §1/§2; reuses foundry/evaluation/{task_fixtures,harness}.py):

* the pilot runs ONLY on `frame.pilot_slice` — touching the scored slice RAISES;
* DETERMINISM — same (frame, arms, k, seed, budget) => identical PilotResult;
* base-rate + variance are computed and in valid ranges (rate in [0,1], var >= 0);
* the consumed budget is recorded;
* `provenance == PILOT` and the "not a scored arm" guard (`scored is False`);
* the pilot/scored slices are enforced DISJOINT.

SYNTHETIC dry-run only — asserts NOTHING about real Foundry-vs-baseline perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.harness import run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.pilot import (
    DEFAULT_PILOT_ARMS,
    PilotResult,
    ScoredSliceTouchedError,
    run_pilot,
)
from foundry.evaluation.task_fixtures import TaskFrame, generate_synthetic_frame
from foundry.schemas.base import FrozenModel

# The control arm runs the SIMULATED stand-in (no real pipeline), so a pilot over
# control-only arms is cheap + deterministic. FOUNDRY-inclusive runs wrap the
# shipped pipeline keyless via the doubles and stay on a SMALL frame.
_CONTROL_ONLY: tuple[Arm, ...] = (Arm.BASELINE_SINGLE_AGENT,)


def _frame(n_tasks: int = 12, seed: int = 0, pilot_fraction: float = 0.4) -> TaskFrame:
    return generate_synthetic_frame(n_tasks=n_tasks, seed=seed, pilot_fraction=pilot_fraction)


# ---- runs ONLY on the pilot slice -------------------------------------------


def test_pilot_runs_only_on_pilot_slice() -> None:
    frame = _frame()
    result = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=2)
    # The pilot size equals the pilot slice, NOT the scored slice or the whole frame.
    assert result.pilot_n == len(frame.pilot_slice.task_ids)
    assert result.pilot_n != len(frame.fixtures)
    assert result.eval_set_identity == frame.pilot_slice.identity


def test_pilot_does_not_observe_scored_tasks() -> None:
    # The estimate is bound to the PILOT slice identity, never the scored slice.
    frame = _frame()
    result = run_pilot(frame, arms=_CONTROL_ONLY)
    assert result.eval_set_identity != frame.scored_slice.identity
    assert result.pilot_n + len(frame.scored_slice.task_ids) == len(frame.fixtures)


# ---- touching the scored slice raises ---------------------------------------


def test_overlapping_pilot_and_scored_slice_raises() -> None:
    # A hand-built frame whose pilot/scored slices OVERLAP trips the integrity
    # guard — the pilot must never observe a scored A/B task (§2).
    frame = _frame()
    overlap_id = frame.scored_slice.task_ids[0]

    class _OverlappingFrame(TaskFrame):
        @property
        def pilot_slice(self):  # type: ignore[override]
            base = super().pilot_slice
            # Inject a scored task id into the pilot slice (the forbidden leak).
            return base.model_copy(update={"task_ids": (*base.task_ids, overlap_id)})

    bad = _OverlappingFrame(**frame.model_dump())
    with pytest.raises(ScoredSliceTouchedError, match=r"disjoint|scored A/B"):
        run_pilot(bad, arms=_CONTROL_ONLY)


def test_pilot_never_runs_a_scored_fixture() -> None:
    # Even though the frame carries the scored fixtures, the harness is handed
    # ONLY the pilot fixtures — every recorded outcome is a pilot task id.
    frame = _frame()
    pilot_ids = set(frame.pilot_slice.task_ids)
    scored_ids = set(frame.scored_slice.task_ids)
    # Re-run the control arm exactly as the pilot does and confirm task scope.
    pilot_fixtures = {f.task_id: f for f in frame.fixtures if f.task_id in pilot_ids}
    arm_result = run_arm(Arm.BASELINE_SINGLE_AGENT, frame.pilot_slice, pilot_fixtures)
    observed = {o.task_id for o in arm_result.outcomes}
    assert observed <= pilot_ids
    assert observed.isdisjoint(scored_ids)


# ---- determinism ------------------------------------------------------------


def test_determinism_same_inputs_identical_result() -> None:
    frame = _frame(n_tasks=16, seed=3)
    a = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=3, seed=7)
    b = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=3, seed=7)
    assert a == b
    assert a.content_identity() == b.content_identity()


def test_determinism_with_foundry_arm() -> None:
    # FOUNDRY wraps the shipped pipeline keyless via the doubles — keep it small.
    frame = _frame(n_tasks=4, seed=1, pilot_fraction=0.5)
    a = run_pilot(frame, k_repeats=1)  # DEFAULT_PILOT_ARMS includes FOUNDRY
    b = run_pilot(frame, k_repeats=1)
    assert a == b
    assert a.content_identity() == b.content_identity()


# ---- base-rate + variance in valid ranges -----------------------------------


def test_base_rate_and_variance_in_valid_ranges() -> None:
    frame = _frame(n_tasks=24, seed=0)
    result = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=3, seed=2)
    assert 0.0 <= result.control_base_rate <= 1.0
    assert result.effect_size_variance >= 0.0
    assert result.control_resolution_variance >= 0.0


def test_base_rate_matches_control_itt_pass_rate() -> None:
    # The base rate IS the control arm's ITT pass-rate over the pilot slice.
    frame = _frame(n_tasks=20, seed=5)
    result = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=2, seed=4)
    pilot_fixtures = {
        f.task_id: f for f in frame.fixtures if f.task_id in set(frame.pilot_slice.task_ids)
    }
    control = run_arm(
        Arm.BASELINE_SINGLE_AGENT, frame.pilot_slice, pilot_fixtures, k_repeats=2, seed=4
    )
    assert result.control_base_rate == control.resolved_count / control.attempted


def test_variance_nondegenerate_on_spread_frame() -> None:
    # The frame spans EASY/MEDIUM/HARD strata (round-robin), so the per-task
    # control resolution rates differ -> a strictly positive between-task variance.
    frame = _frame(n_tasks=24, seed=0)
    result = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=4, seed=1)
    assert result.control_resolution_variance > 0.0


def test_effect_size_variance_uses_foundry_minus_control_diff() -> None:
    # With the FOUNDRY arm present the effect-size variance is the per-task
    # FOUNDRY-CONTROL difference variance (>= 0), computed on the pilot tasks.
    frame = _frame(n_tasks=4, seed=2, pilot_fraction=0.5)
    result = run_pilot(frame, k_repeats=1)
    assert result.effect_size_variance >= 0.0


# ---- consumed budget recorded -----------------------------------------------


def test_consumed_budget_recorded_and_positive() -> None:
    frame = _frame(n_tasks=12, seed=1)
    result = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=2)
    # The control arm has a positive per-attempt simulated cost over every task.
    expected = run_arm(
        Arm.BASELINE_SINGLE_AGENT,
        frame.pilot_slice,
        {f.task_id: f for f in frame.fixtures if f.task_id in set(frame.pilot_slice.task_ids)},
        k_repeats=2,
    ).total_cost_microusd
    assert result.consumed_budget_microusd == expected
    assert result.consumed_budget_microusd > 0


def test_consumed_budget_sums_all_arms() -> None:
    frame = _frame(n_tasks=4, seed=1, pilot_fraction=0.5)
    control_only = run_pilot(frame, arms=_CONTROL_ONLY, k_repeats=1)
    both = run_pilot(frame, arms=DEFAULT_PILOT_ARMS, k_repeats=1)
    # Running FOUNDRY + control consumes strictly more than control alone.
    assert both.consumed_budget_microusd > control_only.consumed_budget_microusd


# ---- provenance == PILOT and NOT a scored arm -------------------------------


def test_provenance_is_pilot_and_not_scored() -> None:
    frame = _frame()
    result = run_pilot(frame, arms=_CONTROL_ONLY)
    assert result.provenance is Provenance.PILOT
    assert result.scored is False  # the pilot is NOT a scored arm (cannot be promoted)


def test_pilot_result_is_frozen_read_only() -> None:
    frame = _frame()
    result = run_pilot(frame, arms=_CONTROL_ONLY)
    assert isinstance(result, PilotResult)
    assert isinstance(result, FrozenModel)
    with pytest.raises(Exception):  # noqa: B017 — frozen pydantic rejects mutation
        result.scored = True  # type: ignore[misc]


def test_pilot_result_exposes_only_readonly_power_calc_summary() -> None:
    # The result exposes EXACTLY the read-only fields the power calc consumes —
    # base rate, variance components, n, k, consumed budget, slice identity, and
    # the integrity flags. No per-task outcomes / scored verdict leak through.
    frame = _frame()
    result = run_pilot(frame, arms=_CONTROL_ONLY)
    assert set(result.model_dump()) == {
        "control_base_rate",
        "effect_size_variance",
        "control_resolution_variance",
        "pilot_n",
        "k_repeats",
        "consumed_budget_microusd",
        "eval_set_identity",
        "provenance",
        "scored",
        "version",
    }


# ---- re-running the pilot does NOT re-draw the scored A/B -------------------


def test_rerun_does_not_redraw_scored_slice() -> None:
    # The scored slice is a deterministic prefix-cut of one frame permutation
    # (EV6); running the pilot is read-only over the partition, so the scored
    # slice identity is unchanged before/after — the pilot can't re-draw it.
    frame = _frame(n_tasks=16, seed=3)
    before = frame.scored_slice.identity
    run_pilot(frame, arms=_CONTROL_ONLY, seed=1)
    run_pilot(frame, arms=_CONTROL_ONLY, seed=99)  # different seed, same partition
    after = frame.scored_slice.identity
    assert before == after


# ---- guards on misuse -------------------------------------------------------


def test_missing_control_arm_raises() -> None:
    # The base rate is undefined without the control arm.
    frame = _frame()
    with pytest.raises(ValueError, match="control arm"):
        run_pilot(frame, arms=(Arm.FOUNDRY,))


def test_disjoint_slices_enforced_for_normal_frame() -> None:
    # A normally-generated frame has DISJOINT + exhaustive pilot/scored slices
    # (EV6), so the pilot runs without tripping the integrity guard.
    frame = _frame(n_tasks=10, seed=7)
    assert set(frame.pilot_slice.task_ids).isdisjoint(frame.scored_slice.task_ids)
    result = run_pilot(frame, arms=_CONTROL_ONLY)  # does not raise
    assert result.pilot_n == len(frame.pilot_slice.task_ids)
