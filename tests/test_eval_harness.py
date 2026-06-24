"""Conformance tests for the oracle-scored harness + ITT runner (T-M15-03).

Contract (docs/evaluation.md §2/§4; reuses foundry/evaluation/task_fixtures.py):

* DETERMINISM — same seed/slice => identical ArmResult.
* ORACLE scoring is EXACT (deterministic equality, no LLM judge — §4): a
  known-resolved fixture scores resolved; a known-unresolved scores not-resolved.
* ITT denominator == attempted (full slice x k): injecting a simulated crash /
  over-budget attempt counts it as a NON-resolution and leaves the denominator
  unchanged (evaluation.md §2 — no dropped samples).
* the FOUNDRY arm runs KEYLESS via the shipped pipeline doubles.
* the SIMULATED stand-in arms are reproducible and CLEARLY flagged synthetic.

SYNTHETIC dry-run only — asserts NOTHING about real Foundry-vs-baseline perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.harness import (
    ArmResult,
    OutcomeStatus,
    Run,
    TaskOutcome,
    itt_pass_rate,
    make_run,
    run_arm,
)
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.task_fixtures import (
    OracleDescriptor,
    TaskFixture,
    TaskSlice,
    generate_synthetic_frame,
)
from foundry.schemas.base import FrozenModel


def _fixtures(frame_n: int = 8, seed: int = 0) -> dict[str, TaskFixture]:
    frame = generate_synthetic_frame(n_tasks=frame_n, seed=seed)
    return {f.task_id: f for f in frame.fixtures}


def _frame_and_slice(frame_n: int = 8, seed: int = 0) -> tuple[dict[str, TaskFixture], TaskSlice]:
    frame = generate_synthetic_frame(n_tasks=frame_n, seed=seed)
    return {f.task_id: f for f in frame.fixtures}, frame.scored_slice


# ---- frozen / types ---------------------------------------------------------


def test_arm_result_is_frozen() -> None:
    fixtures, slc = _frame_and_slice()
    result = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures)
    assert isinstance(result, ArmResult)
    assert isinstance(result, FrozenModel)
    assert all(isinstance(o, TaskOutcome) for o in result.outcomes)
    with pytest.raises(Exception):  # noqa: B017 — frozen pydantic rejects mutation
        result.attempted = 0  # type: ignore[misc]


# ---- determinism ------------------------------------------------------------


def test_determinism_simulated_arm() -> None:
    fixtures, slc = _frame_and_slice(frame_n=12, seed=3)
    a = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=3, seed=42)
    b = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=3, seed=42)
    assert a == b
    assert a.content_identity() == b.content_identity()
    assert a.outcomes == b.outcomes


def test_determinism_foundry_arm_keyless() -> None:
    # The FOUNDRY arm wraps foundry.cli.run keyless via the doubles (D1-D3).
    fixtures, slc = _frame_and_slice(frame_n=4, seed=1)
    a = run_arm(Arm.FOUNDRY, slc, fixtures)
    b = run_arm(Arm.FOUNDRY, slc, fixtures)
    assert a == b
    assert a.arm is Arm.FOUNDRY
    assert a.simulated is False  # FOUNDRY is the REAL pipeline, not a stand-in


def test_seed_changes_simulated_outcomes() -> None:
    fixtures, slc = _frame_and_slice(frame_n=20, seed=5)
    a = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures, k_repeats=2, seed=1)
    b = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures, k_repeats=2, seed=2)
    # Same slice/denominator, but the draw differs -> outcomes differ.
    assert a.attempted == b.attempted
    assert a.outcomes != b.outcomes


# ---- oracle scoring is exact (no LLM judge) ---------------------------------


def _known_fixture(task_id: str, expected: str, base_seed: int) -> TaskFixture:
    return TaskFixture(
        task_id=task_id,
        spec=f"[SYNTHETIC] {task_id}",
        oracle=OracleDescriptor(hidden_test_id=f"hidden::{task_id}", expected_output=expected),
        difficulty="medium",  # type: ignore[arg-type]
        control_base_rate=0.5,
        base_resolution_seed=base_seed,
    )


def _solo_slice(task_id: str) -> TaskSlice:
    return TaskSlice(name="probe", task_ids=(task_id,), identity=f"probe::{task_id}")


def test_oracle_scores_known_resolved() -> None:
    fx = _known_fixture("t-res", "ORACLE-TARGET", base_seed=1)
    slc = _solo_slice("t-res")
    # FOUNDRY runner pinned to a completion producing the EXACT oracle target.
    result = run_arm(
        Arm.FOUNDRY,
        slc,
        {"t-res": fx},
        foundry_runner=lambda fixture, repeat: (True, fixture.oracle.expected_output),
    )
    (outcome,) = result.outcomes
    assert outcome.resolved is True
    assert outcome.status is OutcomeStatus.RESOLVED
    assert itt_pass_rate(result) == 1.0


def test_oracle_scores_known_unresolved_on_mismatch() -> None:
    fx = _known_fixture("t-unres", "ORACLE-TARGET", base_seed=1)
    slc = _solo_slice("t-unres")
    # Runner "completes" but produces the WRONG output -> oracle equality fails.
    result = run_arm(
        Arm.FOUNDRY,
        slc,
        {"t-unres": fx},
        foundry_runner=lambda fixture, repeat: (True, "WRONG-OUTPUT"),
    )
    (outcome,) = result.outcomes
    assert outcome.resolved is False
    assert outcome.status is OutcomeStatus.UNRESOLVED
    assert itt_pass_rate(result) == 0.0


def test_oracle_is_exact_equality_not_substring() -> None:
    fx = _known_fixture("t-sub", "TARGET", base_seed=1)
    slc = _solo_slice("t-sub")
    result = run_arm(
        Arm.FOUNDRY,
        slc,
        {"t-sub": fx},
        foundry_runner=lambda fixture, repeat: (True, "TARGET-PLUS-EXTRA"),
    )
    assert result.outcomes[0].resolved is False  # not a substring match


# ---- ITT: denominator == attempted, crashes/over-budget count, not dropped --


def test_itt_denominator_equals_attempted() -> None:
    fixtures, slc = _frame_and_slice(frame_n=10, seed=2)
    k = 3
    result = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=k)
    assert result.attempted == len(slc.task_ids) * k
    assert len(result.outcomes) == result.attempted  # one retained outcome per attempt
    assert itt_pass_rate(result) == result.resolved_count / result.attempted


def test_crash_counts_as_non_resolution_denominator_unchanged() -> None:
    # Inject a FOUNDRY runner that CRASHES on every attempt — ITT: each crash is
    # a non-resolution RETAINED in the result, the denominator is unchanged.
    fixtures, slc = _frame_and_slice(frame_n=6, seed=4)

    def _crashing_runner(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        raise RuntimeError("simulated agent crash")

    result = run_arm(Arm.FOUNDRY, slc, fixtures, k_repeats=2, foundry_runner=_crashing_runner)
    assert result.attempted == len(slc.task_ids) * 2
    assert len(result.outcomes) == result.attempted  # NOT dropped
    assert all(o.status is OutcomeStatus.CRASHED for o in result.outcomes)
    assert all(o.resolved is False for o in result.outcomes)
    assert result.resolved_count == 0
    assert itt_pass_rate(result) == 0.0  # crashes counted in the denominator


def test_over_budget_counts_as_non_resolution_denominator_unchanged() -> None:
    # A per-attempt budget below the arm's simulated cost forces OVER_BUDGET on
    # every attempt — ITT: unresolved, retained, denominator unchanged.
    fixtures, slc = _frame_and_slice(frame_n=6, seed=4)
    result = run_arm(
        Arm.BASELINE_SINGLE_AGENT,
        slc,
        fixtures,
        k_repeats=2,
        budget_usd_microusd=1,  # below any per-attempt simulated cost
    )
    assert len(result.outcomes) == result.attempted
    assert all(o.status is OutcomeStatus.OVER_BUDGET for o in result.outcomes)
    assert all(o.resolved is False for o in result.outcomes)
    # Charged the CAP, not the overrun (equal-USD honesty):
    assert all(o.cost_microusd == 1 for o in result.outcomes)
    assert itt_pass_rate(result) == 0.0


def test_partial_crash_keeps_full_denominator() -> None:
    # Mix of resolved + crashed across repeats: denominator is still the full
    # initiated count, the rate is resolved/attempted (no conditioning).
    fx = _known_fixture("t-mix", "OK", base_seed=1)
    slc = _solo_slice("t-mix")
    calls = {"n": 0}

    def _flaky(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        calls["n"] += 1
        if repeat == 0:
            return True, fixture.oracle.expected_output  # resolved
        raise RuntimeError("crash on the second repeat")

    result = run_arm(Arm.FOUNDRY, slc, {"t-mix": fx}, k_repeats=2, foundry_runner=_flaky)
    assert result.attempted == 2
    assert len(result.outcomes) == 2
    statuses = {o.status for o in result.outcomes}
    assert statuses == {OutcomeStatus.RESOLVED, OutcomeStatus.CRASHED}
    assert itt_pass_rate(result) == 0.5  # 1 resolved / 2 attempted, crash NOT dropped


def test_itt_guard_rejects_shrunken_denominator() -> None:
    # A hand-built ArmResult whose attempted disagrees with its outcomes (a
    # dropped sample) trips the guard — the denominator can't silently shrink.
    bad = ArmResult(
        arm=Arm.BASELINE_SINGLE_AGENT,
        slice_identity="x",
        slice_name="probe",
        k_repeats=1,
        seed=0,
        budget_usd_microusd=5_000_000,
        attempted=4,  # claims 4 initiated...
        outcomes=(  # ...but only 2 retained (2 silently dropped)
            TaskOutcome(
                task_id="a",
                repeat=0,
                arm=Arm.BASELINE_SINGLE_AGENT,
                status=OutcomeStatus.RESOLVED,
                resolved=True,
                cost_microusd=10,
                expected_output="e",
                produced_output="e",
            ),
            TaskOutcome(
                task_id="b",
                repeat=0,
                arm=Arm.BASELINE_SINGLE_AGENT,
                status=OutcomeStatus.UNRESOLVED,
                resolved=False,
                cost_microusd=10,
                expected_output="e",
                produced_output=None,
            ),
        ),
        simulated=True,
    )
    with pytest.raises(AssertionError, match="ITT"):
        itt_pass_rate(bad)


# ---- FOUNDRY arm runs keyless via the doubles -------------------------------


def test_foundry_arm_runs_keyless_via_doubles() -> None:
    # No foundry_runner override -> the default wraps foundry.cli.run, which uses
    # MockTransport + StubAdapter (no live API key / claude binary / network).
    fixtures, slc = _frame_and_slice(frame_n=4, seed=1)
    result = run_arm(Arm.FOUNDRY, slc, fixtures)
    assert result.arm is Arm.FOUNDRY
    assert result.simulated is False
    assert len(result.outcomes) == result.attempted
    # The shipped smoke pipeline reaches a clean PASS, so the synthetic oracle
    # (produced == expected on completion) scores these resolved.
    assert result.resolved_count == result.attempted
    assert itt_pass_rate(result) == 1.0


# ---- simulated arms reproducible + clearly flagged synthetic ----------------


def test_simulated_arms_flagged_synthetic() -> None:
    fixtures, slc = _frame_and_slice(frame_n=6, seed=2)
    for arm in (Arm.BASELINE_SINGLE_AGENT, Arm.SINGLE_AGENT_PLUS_GATES):
        result = run_arm(arm, slc, fixtures)
        assert result.simulated is True  # CLEARLY flagged as a stand-in
        assert result.provenance is Provenance.DRY_RUN


def test_simulated_arms_reproducible_and_bounded() -> None:
    fixtures, slc = _frame_and_slice(frame_n=24, seed=0)
    a = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=2, seed=9)
    b = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=2, seed=9)
    assert a.outcomes == b.outcomes  # reproducible across invocations
    rate = itt_pass_rate(a)
    assert 0.0 <= rate <= 1.0


def test_simulated_arms_distinguishable() -> None:
    # The +gates stand-in has a small dry-run uplift over the bare baseline, so a
    # methodology dry-run can exercise an arm difference (NOT a real claim).
    fixtures, slc = _frame_and_slice(frame_n=24, seed=0)
    base = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures, k_repeats=3, seed=1)
    gated = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, slc, fixtures, k_repeats=3, seed=1)
    assert itt_pass_rate(gated) >= itt_pass_rate(base)


# ---- cost recorded for equal-USD comparison ---------------------------------


def test_per_attempt_cost_recorded_for_equal_usd() -> None:
    fixtures, slc = _frame_and_slice(frame_n=8, seed=1)
    result = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures, k_repeats=2)
    assert all(o.cost_microusd > 0 for o in result.outcomes)
    assert result.total_cost_microusd == sum(o.cost_microusd for o in result.outcomes)


# ---- Run record -------------------------------------------------------------


def test_run_record_content_addressed() -> None:
    fixtures, slc = _frame_and_slice(frame_n=6, seed=1)
    result = run_arm(Arm.BASELINE_SINGLE_AGENT, slc, fixtures, seed=3)
    run_a = make_run(result)
    run_b = make_run(result)
    assert isinstance(run_a, Run)
    assert run_a.run_id == run_b.run_id  # content-addressed, stable
    assert run_a.provenance is Provenance.DRY_RUN
    assert run_a.result is result
