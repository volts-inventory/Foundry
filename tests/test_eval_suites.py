"""Conformance tests for the outcome-eval SUITES (T-M6-02; docs/evaluation.md §3).

Contract:

* a suite aggregates the harness ORACLE resolution correctly (reuses harness
  scoring; no LLM judge — §3/§4);
* the suite ITT denominator is UNCONDITIONAL — ``len(suite) * k`` — and a
  crashed/over-budget attempt is RETAINED as unresolved, never dropped (§2/§6);
* determinism — same inputs => identical SuiteResult roll-up;
* per-task vs per-attempt resolution rates are both exposed and consistent.

SYNTHETIC dry-run only — asserts NOTHING about real Foundry-vs-baseline perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.harness import OutcomeStatus, run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.suites import (
    OutcomeSuite,
    SuiteKind,
    SuiteResult,
    TaskResolution,
    run_suite,
)
from foundry.evaluation.suites import (
    make_suite as _make_suite,
)
from foundry.evaluation.task_fixtures import TaskFixture, generate_synthetic_frame
from foundry.schemas.base import FrozenModel


def _fixtures(n: int = 8, seed: int = 0) -> dict[str, TaskFixture]:
    frame = generate_synthetic_frame(n_tasks=n, seed=seed)
    return {f.task_id: f for f in frame.fixtures}


def _suite(
    fixtures: dict[str, TaskFixture],
    kind: SuiteKind = SuiteKind.SWE_EVO_STYLE,
) -> OutcomeSuite:
    return _make_suite("dry-run-suite", kind, tuple(sorted(fixtures)))


# ---- types / frozen ---------------------------------------------------------


def test_suite_and_result_are_frozen() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)
    result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures)
    assert isinstance(suite, FrozenModel)
    assert isinstance(result, SuiteResult)
    assert all(isinstance(t, TaskResolution) for t in result.task_resolutions)
    with pytest.raises(Exception):  # noqa: B017 — frozen pydantic rejects mutation
        result.resolved = 0  # type: ignore[misc]


def test_suite_identity_content_addressed() -> None:
    fixtures = _fixtures()
    a = _make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures)))
    b = _make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures)))
    assert a.identity == b.identity
    # A membership change flips the identity.
    c = _make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures))[:-1])
    assert c.identity != a.identity
    # A kind change flips it too.
    d = _make_suite("s", SuiteKind.IN_HOUSE_NOVEL, tuple(sorted(fixtures)))
    assert d.identity != a.identity


# ---- oracle aggregation -----------------------------------------------------


def test_suite_resolution_matches_harness() -> None:
    """Suite resolution roll-up == the harness arm result (reuses oracle scoring)."""
    fixtures = _fixtures()
    suite = _suite(fixtures)
    arm_result = run_arm(Arm.BASELINE_SINGLE_AGENT, suite.as_slice(), fixtures)
    suite_result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures)
    assert suite_result.resolved == arm_result.resolved_count
    assert suite_result.attempted == arm_result.attempted
    assert suite_result.resolution_rate == arm_result.resolved_count / arm_result.attempted


def test_per_task_rollup_sums_to_attempts() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)
    result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures, k_repeats=3)
    # Per-task attempted repeats sum to the suite denominator.
    assert sum(t.attempted_repeats for t in result.task_resolutions) == result.attempted
    assert sum(t.resolved_repeats for t in result.task_resolutions) == result.resolved
    # Every task in the suite has exactly one resolution record, in suite order.
    assert tuple(t.task_id for t in result.task_resolutions) == suite.task_ids


def test_task_resolution_rate_distinct_from_attempt_rate() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)
    result = run_suite(Arm.SINGLE_AGENT_PLUS_GATES, suite, fixtures, k_repeats=2)
    # Both rates are well-defined and in [0,1].
    assert 0.0 <= result.resolution_rate <= 1.0
    assert 0.0 <= result.task_resolution_rate <= 1.0
    # task headline counts a task resolved on >=1 attempt.
    expected_tasks_resolved = sum(1 for t in result.task_resolutions if t.resolved_repeats > 0)
    assert result.task_resolution_rate == expected_tasks_resolved / len(result.task_resolutions)


# ---- ITT denominator unconditional ------------------------------------------


def test_itt_denominator_is_unconditional() -> None:
    """Denominator == len(suite) * k, regardless of how many resolved."""
    fixtures = _fixtures()
    suite = _suite(fixtures)
    for k in (1, 2, 4):
        result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures, k_repeats=k)
        assert result.attempted == len(suite.task_ids) * k
        assert len(result.arm_result.outcomes) == result.attempted


def test_crashed_attempt_retained_not_dropped() -> None:
    """A FOUNDRY crash is retained as unresolved; the denominator is unchanged."""
    fixtures = _fixtures()
    suite = _suite(fixtures)

    def _crashing_runner(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        raise RuntimeError("simulated crash")

    result = run_suite(
        Arm.FOUNDRY, suite, fixtures, k_repeats=2, foundry_runner=_crashing_runner
    )
    # Denominator unchanged, zero resolved, every outcome a retained CRASH.
    assert result.attempted == len(suite.task_ids) * 2
    assert result.resolved == 0
    assert result.resolution_rate == 0.0
    assert all(o.status is OutcomeStatus.CRASHED for o in result.arm_result.outcomes)


def test_over_budget_attempt_retained_unresolved() -> None:
    """An over-budget attempt counts toward the denominator (unresolved)."""
    fixtures = _fixtures()
    suite = _suite(fixtures)
    # Budget below FOUNDRY's simulated per-attempt cost forces OVER_BUDGET.
    result = run_suite(Arm.FOUNDRY, suite, fixtures, budget_usd_microusd=1)
    assert result.attempted == len(suite.task_ids)
    assert result.resolved == 0
    assert all(o.status is OutcomeStatus.OVER_BUDGET for o in result.arm_result.outcomes)


def test_known_resolution_via_injected_runner() -> None:
    """A runner that always completes => every task oracle-resolved."""
    fixtures = _fixtures()
    suite = _suite(fixtures)

    def _always_resolved(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        return True, fixture.oracle.expected_output

    result = run_suite(Arm.FOUNDRY, suite, fixtures, foundry_runner=_always_resolved)
    assert result.resolved == result.attempted
    assert result.resolution_rate == 1.0
    assert all(t.resolved for t in result.task_resolutions)


def test_never_resolved_runner() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)

    def _never(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        return False, None

    result = run_suite(Arm.FOUNDRY, suite, fixtures, foundry_runner=_never)
    assert result.resolved == 0
    assert result.resolution_rate == 0.0
    assert not any(t.resolved for t in result.task_resolutions)


# ---- determinism ------------------------------------------------------------


def test_suite_run_is_deterministic() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)
    a = run_suite(Arm.SINGLE_AGENT_PLUS_GATES, suite, fixtures, k_repeats=3, seed=7)
    b = run_suite(Arm.SINGLE_AGENT_PLUS_GATES, suite, fixtures, k_repeats=3, seed=7)
    assert a.content_identity() == b.content_identity()


def test_suite_result_provenance_dry_run() -> None:
    fixtures = _fixtures()
    suite = _suite(fixtures)
    result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures)
    assert result.provenance is Provenance.DRY_RUN
    assert result.simulated is True  # baseline is a synthetic stand-in
