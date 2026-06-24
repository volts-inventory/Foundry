"""Conformance tests for eval-integrity, fail-closed (T-M6-02; docs/evaluation.md §6).

Contract:

* a RUNTIME-REACHABLE hidden test => integrity violation (eval INVALID);
* a CONTAMINATED task (oracle/canary leaked into agent inputs) => INVALID;
* a genuinely held-out + uncontaminated + unconditional-ITT eval => CLEAN;
* ITT is reported UNCONDITIONALLY — a dropped sample or a conditioned (self-
  passing) denominator is a violation;
* fail-closed: ``require_clean`` raises on a dirty report.

SYNTHETIC dry-run only.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.harness import run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.integrity import (
    AgentRuntimeSurface,
    EvalIntegrityError,
    IntegrityReport,
    TaskInputManifest,
    ViolationKind,
    assert_held_out_unreachable,
    assert_itt_unconditional,
    check_eval_integrity,
    contamination_probe,
    require_clean,
)
from foundry.evaluation.suites import SuiteKind, make_suite, run_suite
from foundry.evaluation.task_fixtures import (
    OracleDescriptor,
    TaskFixture,
    generate_synthetic_frame,
)
from foundry.schemas.base import FrozenModel


def _frame(n: int = 6, seed: int = 0):  # type: ignore[no-untyped-def]
    return generate_synthetic_frame(n_tasks=n, seed=seed)


def _oracles(fixtures: dict[str, TaskFixture]) -> dict[str, OracleDescriptor]:
    return {tid: f.oracle for tid, f in fixtures.items()}


def _clean_surfaces(fixtures: dict[str, TaskFixture]) -> dict[str, AgentRuntimeSurface]:
    # Reachable refs that DON'T include any held-out token => unreachable.
    return {
        tid: AgentRuntimeSurface(task_id=tid, reachable_refs=frozenset({f"src::{tid}"}))
        for tid in fixtures
    }


def _clean_manifests(fixtures: dict[str, TaskFixture]) -> dict[str, TaskInputManifest]:
    return {
        tid: TaskInputManifest(task_id=tid, input_texts=(f.spec,))
        for tid, f in fixtures.items()
    }


# ---- types ------------------------------------------------------------------


def test_report_is_frozen() -> None:
    report = check_eval_integrity(oracles={}, surfaces={}, manifests={})
    assert isinstance(report, IntegrityReport)
    assert isinstance(report, FrozenModel)
    assert report.clean is True
    with pytest.raises(Exception):  # noqa: B017 — frozen
        report.violations = ()  # type: ignore[misc]


# ---- clean eval -------------------------------------------------------------


def test_genuinely_held_out_uncontaminated_eval_is_clean() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    oracles = _oracles(fixtures)
    suite = make_suite("s", SuiteKind.SWE_EVO_STYLE, tuple(sorted(fixtures)))
    arm_result = run_arm(Arm.BASELINE_SINGLE_AGENT, suite.as_slice(), fixtures)
    report = check_eval_integrity(
        oracles=oracles,
        surfaces=_clean_surfaces(fixtures),
        manifests=_clean_manifests(fixtures),
        arm_results=(arm_result,),
    )
    assert report.clean is True
    assert report.violations == ()
    assert report.provenance is Provenance.DRY_RUN
    # require_clean returns it unchanged.
    assert require_clean(report) is report


# ---- mechanism 1: reachable hidden test -------------------------------------


def test_reachable_hidden_test_invalidates() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    oracles = _oracles(fixtures)
    surfaces = _clean_surfaces(fixtures)
    # Make ONE task's hidden test reachable to the producing agent.
    victim = next(iter(fixtures))
    surfaces[victim] = AgentRuntimeSurface(
        task_id=victim,
        reachable_refs=frozenset({oracles[victim].hidden_test_id, "src::x"}),
    )
    report = check_eval_integrity(oracles=oracles, surfaces=surfaces, manifests={})
    assert report.clean is False
    hits = report.violations_of(ViolationKind.REACHABLE_HIDDEN_TEST)
    assert any(v.task_id == victim for v in hits)
    with pytest.raises(EvalIntegrityError):
        require_clean(report)


def test_missing_surface_fails_closed() -> None:
    """No recorded surface => unreachability UNPROVEN => violation (fail-closed)."""
    fixtures = {f.task_id: f for f in _frame().fixtures}
    oracles = _oracles(fixtures)
    report = check_eval_integrity(oracles=oracles, surfaces={}, manifests={})
    assert report.clean is False
    assert len(report.violations_of(ViolationKind.REACHABLE_HIDDEN_TEST)) == len(oracles)


def test_assert_held_out_unreachable_clean() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    assert assert_held_out_unreachable(_oracles(fixtures), _clean_surfaces(fixtures)) == []


# ---- mechanism 2: ITT unconditional -----------------------------------------


def test_itt_unconditional_on_well_formed_result() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    suite = make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures)))
    arm_result = run_arm(Arm.SINGLE_AGENT_PLUS_GATES, suite.as_slice(), fixtures, k_repeats=2)
    assert assert_itt_unconditional(arm_result) == []


def test_conditioned_itt_denominator_violates() -> None:
    """A denominator filtered to the self-passing subset is collider bias => violation."""
    fixtures = {f.task_id: f for f in _frame().fixtures}
    suite = make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures)))

    def _never(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
        return False, None

    # An all-unresolved FOUNDRY run, then forge a CONDITIONED result whose
    # denominator == resolved_count (0) while unresolved outcomes exist.
    arm_result = run_arm(Arm.FOUNDRY, suite.as_slice(), fixtures, foundry_runner=_never)
    # Keep only resolved outcomes (none) but claim the full attempted denominator
    # collapsed to the resolved subset — i.e. attempted forged to resolved_count.
    conditioned = arm_result.model_copy(update={"attempted": arm_result.resolved_count})
    violations = assert_itt_unconditional(conditioned)
    kinds = {v.kind for v in violations}
    # Dropping the denominator below the retained outcomes trips MISSING_OUTCOME;
    # equal-to-resolved-with-unresolved-present trips CONDITIONED_ITT.
    assert ViolationKind.CONDITIONED_ITT in kinds or ViolationKind.MISSING_OUTCOME in kinds
    assert conditioned.resolved_count == 0


def test_dropped_sample_violates() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    suite = make_suite("s", SuiteKind.GOLDEN, tuple(sorted(fixtures)))
    arm_result = run_arm(Arm.BASELINE_SINGLE_AGENT, suite.as_slice(), fixtures)
    # Forge a result that claims MORE attempts than it retained (a dropped sample).
    dropped = arm_result.model_copy(update={"attempted": arm_result.attempted + 1})
    violations = assert_itt_unconditional(dropped)
    assert any(v.kind is ViolationKind.MISSING_OUTCOME for v in violations)


# ---- mechanism 3: contamination probe ---------------------------------------


def test_contaminated_task_invalidates() -> None:
    """Oracle expected_output leaked into the agent's inputs => contaminated."""
    fixtures = {f.task_id: f for f in _frame().fixtures}
    oracles = _oracles(fixtures)
    victim = next(iter(fixtures))
    manifests = _clean_manifests(fixtures)
    # Leak the oracle target verbatim into the victim's input text.
    manifests[victim] = TaskInputManifest(
        task_id=victim,
        input_texts=(f"context blob {oracles[victim].expected_output} more",),
    )
    report = check_eval_integrity(
        oracles=oracles, surfaces=_clean_surfaces(fixtures), manifests=manifests
    )
    assert report.clean is False
    hits = report.violations_of(ViolationKind.CONTAMINATED_TASK)
    assert any(v.task_id == victim for v in hits)
    with pytest.raises(EvalIntegrityError):
        require_clean(report)


def test_canary_token_leak_detected() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    oracles = _oracles(fixtures)
    victim = next(iter(fixtures))
    canary = "CANARY-7f3a"
    manifests = {
        tid: TaskInputManifest(
            task_id=tid, input_texts=(f.spec,), canary_tokens=frozenset({canary})
        )
        for tid, f in fixtures.items()
    }
    # Plant the canary into the victim's inputs.
    manifests[victim] = TaskInputManifest(
        task_id=victim,
        input_texts=(f"retrieved snippet with {canary} embedded",),
        canary_tokens=frozenset({canary}),
    )
    violations = contamination_probe(oracles, manifests)
    assert any(
        v.task_id == victim and v.kind is ViolationKind.CONTAMINATED_TASK for v in violations
    )


def test_uncontaminated_probe_clean() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    assert contamination_probe(_oracles(fixtures), _clean_manifests(fixtures)) == []


# ---- end-to-end via a suite -------------------------------------------------


def test_suite_integrity_end_to_end_clean() -> None:
    from foundry.evaluation.integrity import check_suite_integrity

    fixtures = {f.task_id: f for f in _frame().fixtures}
    suite = make_suite("s", SuiteKind.SWE_EVO_STYLE, tuple(sorted(fixtures)))
    suite_result = run_suite(Arm.BASELINE_SINGLE_AGENT, suite, fixtures)
    report = check_suite_integrity(
        suite_result,
        oracles=_oracles(fixtures),
        surfaces=_clean_surfaces(fixtures),
        manifests=_clean_manifests(fixtures),
    )
    assert report.clean is True


def test_report_id_deterministic() -> None:
    fixtures = {f.task_id: f for f in _frame().fixtures}
    a = check_eval_integrity(
        oracles=_oracles(fixtures),
        surfaces=_clean_surfaces(fixtures),
        manifests=_clean_manifests(fixtures),
    )
    b = check_eval_integrity(
        oracles=_oracles(fixtures),
        surfaces=_clean_surfaces(fixtures),
        manifests=_clean_manifests(fixtures),
    )
    assert a.report_id == b.report_id
