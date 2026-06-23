"""Eval-safeties tests — reward-hacking / eval-lie / nondeterminism, fail-closed.

These exercise :mod:`foundry.evaluation.safeties`: the checks that protect the
scorecard from being gamed (docs/evaluation.md §10). Each safety is tested in
both directions (a violation is flagged + invalidates; a genuine result passes),
plus the fail-closed combination and the determinism of the checks themselves.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation.harness import ArmResult, OutcomeStatus, TaskOutcome
from foundry.evaluation.hypothesis import Arm
from foundry.evaluation.safeties import (
    EvalSafetyReport,
    SafetyKind,
    check_arm_safeties,
    detect_eval_lies,
    detect_nondeterminism,
    detect_reward_hacking,
)
from foundry.gates.discriminator import DiscriminatorReport

# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def _outcome(
    task_id: str,
    *,
    repeat: int = 0,
    arm: Arm = Arm.FOUNDRY,
    resolved: bool = True,
    status: OutcomeStatus | None = None,
    expected: str = "ok",
    produced: str | None = "ok",
    cost: int = 1_000,
) -> TaskOutcome:
    if status is None:
        status = OutcomeStatus.RESOLVED if resolved else OutcomeStatus.UNRESOLVED
    return TaskOutcome(
        task_id=task_id,
        repeat=repeat,
        arm=arm,
        status=status,
        resolved=resolved,
        cost_microusd=cost,
        expected_output=expected,
        produced_output=produced,
    )


def _arm_result(
    outcomes: tuple[TaskOutcome, ...],
    *,
    arm: Arm = Arm.FOUNDRY,
    slice_identity: str = "slice-x",
    seed: int = 0,
    budget: int = 5_000_000,
) -> ArmResult:
    return ArmResult(
        arm=arm,
        slice_identity=slice_identity,
        slice_name="probe",
        k_repeats=1,
        seed=seed,
        budget_usd_microusd=budget,
        attempted=len(outcomes),
        outcomes=outcomes,
        simulated=arm is not Arm.FOUNDRY,
    )


def _disc(
    *,
    passed: bool,
    kill_score: float = 1.0,
    required: float = 1.0,
    total: int = 3,
    killed: int = 3,
    survived: tuple[str, ...] = (),
    invariants_violated: tuple[str, ...] = (),
) -> DiscriminatorReport:
    return DiscriminatorReport(
        passed=passed,
        kill_score=kill_score,
        required_kill_ratio=required,
        mutants_total=total,
        mutants_killed=killed,
        survived=survived,
        invariants_total=len(invariants_violated),
        invariants_violated=invariants_violated,
    )


# --------------------------------------------------------------------------- #
# reward-hacking
# --------------------------------------------------------------------------- #


def test_reward_hacking_genuine_result_passes() -> None:
    report = _disc(passed=True, kill_score=1.0, killed=3)
    assert detect_reward_hacking(report, subject="suite-x") == ()


def test_reward_hacking_surviving_mutant_flagged() -> None:
    report = _disc(passed=False, kill_score=2 / 3, killed=2, survived=("m3",))
    findings = detect_reward_hacking(report, subject="suite-x")
    assert len(findings) == 1
    assert findings[0].kind is SafetyKind.REWARD_HACKING
    assert findings[0].subject == "suite-x"
    assert "SURVIVED" in findings[0].detail


def test_reward_hacking_violated_invariant_flagged() -> None:
    # All mutants killed but a behavioral invariant the spec implies is violated:
    # the produced code is wrong though the suite is green (metric gamed).
    report = _disc(
        passed=False, kill_score=1.0, killed=3, invariants_violated=("inv-pos",)
    )
    findings = detect_reward_hacking(report, subject="suite-x")
    kinds = {f.kind for f in findings}
    assert kinds == {SafetyKind.REWARD_HACKING}
    assert any("invariants VIOLATED" in f.detail for f in findings)


def test_reward_hacking_sub_bar_kill_score_flagged() -> None:
    # Sub-bar kill score with no named survivor still fails closed.
    report = _disc(passed=False, kill_score=0.5, required=1.0, total=0, killed=0)
    findings = detect_reward_hacking(report, subject="suite-x")
    assert len(findings) == 1
    assert "below required" in findings[0].detail


def test_reward_hacking_non_passing_with_no_itemized_reason_fails_closed() -> None:
    # A discriminator that didn't pass but exposes no itemized evidence still
    # invalidates — fail-closed, never slip through as clean.
    report = _disc(passed=False, kill_score=1.0, required=1.0, killed=3)
    findings = detect_reward_hacking(report, subject="suite-x")
    assert len(findings) == 1
    assert findings[0].kind is SafetyKind.REWARD_HACKING


def test_reward_hacking_survivor_and_invariant_both_traced() -> None:
    report = _disc(
        passed=False,
        kill_score=0.5,
        killed=1,
        survived=("m2", "m3"),
        invariants_violated=("inv-a",),
    )
    findings = detect_reward_hacking(report, subject="suite-x")
    # One survivor finding + one invariant finding (both evidences traced).
    assert len(findings) == 2


# --------------------------------------------------------------------------- #
# eval-lie (the oracle wins; the self-report can never set the score)
# --------------------------------------------------------------------------- #


def test_eval_lie_self_report_contradicted_by_oracle_flagged() -> None:
    # The oracle scored unresolved (produced != expected); the agent CLAIMED
    # resolved. The oracle wins — flag the lie.
    outcome = _outcome("t1", resolved=False, produced="wrong", expected="ok")
    findings = detect_eval_lies([outcome], self_reports={("t1", 0): True})
    assert len(findings) == 1
    assert findings[0].kind is SafetyKind.EVAL_LIE
    assert findings[0].subject == "t1"


def test_eval_lie_honest_success_not_flagged() -> None:
    # Self-report matches the oracle (both resolved) — no lie.
    outcome = _outcome("t1", resolved=True, produced="ok", expected="ok")
    assert detect_eval_lies([outcome], self_reports={("t1", 0): True}) == ()


def test_eval_lie_underclaim_is_not_a_lie() -> None:
    # The agent claimed FAILURE but the oracle resolved it — the oracle still
    # wins and sets the score; an under-claim is not a score-inflating lie.
    outcome = _outcome("t1", resolved=True, produced="ok", expected="ok")
    assert detect_eval_lies([outcome], self_reports={("t1", 0): False}) == ()


def test_eval_lie_crashed_attempt_claimed_success_flagged() -> None:
    # A crashed attempt (produced None) the agent claims resolved is a lie — None
    # can never equal the oracle target.
    outcome = _outcome(
        "t1", resolved=False, status=OutcomeStatus.CRASHED, produced=None, expected="ok"
    )
    findings = detect_eval_lies([outcome], self_reports={("t1", 0): True})
    assert len(findings) == 1
    assert findings[0].kind is SafetyKind.EVAL_LIE


def test_eval_lie_absent_self_report_is_no_claim() -> None:
    outcome = _outcome("t1", resolved=False, produced="wrong", expected="ok")
    assert detect_eval_lies([outcome], self_reports={}) == ()


def test_eval_lie_self_report_channel_can_never_set_the_score() -> None:
    # ASSERTION (load-bearing §10): there is no path by which a self-report sets
    # the score. Even with EVERY attempt self-claiming success, the oracle's
    # resolved bit is authoritative and every false claim is flagged.
    outcomes = (
        _outcome("a", resolved=False, produced="x", expected="ok"),
        _outcome("b", resolved=False, produced=None, expected="ok"),
        _outcome("c", resolved=True, produced="ok", expected="ok"),
    )
    self_reports = {("a", 0): True, ("b", 0): True, ("c", 0): True}
    findings = detect_eval_lies(outcomes, self_reports=self_reports)
    # Only the two genuinely-unresolved-but-claimed-success ones are lies; the
    # honest one (c) is not — the oracle, not the claim, decided all three.
    assert {f.subject for f in findings} == {"a", "b"}


# --------------------------------------------------------------------------- #
# nondeterminism (differs across same-seed re-runs)
# --------------------------------------------------------------------------- #


def test_nondeterminism_reproducible_runs_pass() -> None:
    outcomes = (_outcome("t1", resolved=True), _outcome("t2", resolved=False, produced="no"))
    run_a = _arm_result(outcomes)
    run_b = _arm_result(outcomes)  # identical
    assert detect_nondeterminism([run_a, run_b]) == ()


def test_nondeterminism_single_run_is_vacuously_clean() -> None:
    run = _arm_result((_outcome("t1"),))
    assert detect_nondeterminism([run]) == ()
    assert detect_nondeterminism([]) == ()


def test_nondeterminism_flipped_resolved_flagged() -> None:
    run_a = _arm_result((_outcome("t1", resolved=True, produced="ok"),))
    run_b = _arm_result((_outcome("t1", resolved=False, produced="ok"),))
    findings = detect_nondeterminism([run_a, run_b])
    assert len(findings) == 1
    assert findings[0].kind is SafetyKind.NONDETERMINISM
    assert findings[0].subject == "t1"
    assert "flipped resolved" in findings[0].detail


def test_nondeterminism_changed_status_flagged() -> None:
    # Same resolved bit (both unresolved) but a different terminal status is still
    # flaky.
    run_a = _arm_result(
        (_outcome("t1", resolved=False, status=OutcomeStatus.UNRESOLVED, produced=None),)
    )
    run_b = _arm_result(
        (_outcome("t1", resolved=False, status=OutcomeStatus.CRASHED, produced=None),)
    )
    findings = detect_nondeterminism([run_a, run_b])
    assert len(findings) == 1
    assert "changed status" in findings[0].detail


def test_nondeterminism_changed_produced_output_flagged() -> None:
    run_a = _arm_result((_outcome("t1", resolved=False, produced="a"),))
    run_b = _arm_result((_outcome("t1", resolved=False, produced="b"),))
    findings = detect_nondeterminism([run_a, run_b])
    assert len(findings) == 1
    assert "changed produced output" in findings[0].detail


def test_nondeterminism_moved_attempt_set_flagged() -> None:
    # A same-seed re-run that produced a different attempt SET (the denominator
    # moved) is itself nondeterminism.
    run_a = _arm_result((_outcome("t1"), _outcome("t2")))
    # The two runs disagree on attempted; comparability guard is on
    # (arm,slice,seed,budget), all shared here, so this is a genuine flake.
    run_b = ArmResult(
        arm=run_a.arm,
        slice_identity=run_a.slice_identity,
        slice_name=run_a.slice_name,
        k_repeats=1,
        seed=run_a.seed,
        budget_usd_microusd=run_a.budget_usd_microusd,
        attempted=1,
        outcomes=(_outcome("t1"),),
        simulated=run_a.simulated,
    )
    findings = detect_nondeterminism([run_a, run_b])
    assert any("denominator moved" in f.detail for f in findings)
    assert {f.subject for f in findings} == {"t2"}


def test_nondeterminism_three_runs_one_flaky() -> None:
    base = (_outcome("t1", resolved=True, produced="ok"),)
    run_a = _arm_result(base)
    run_b = _arm_result(base)
    run_c = _arm_result((_outcome("t1", resolved=False, produced="ok"),))
    findings = detect_nondeterminism([run_a, run_b, run_c])
    # run_b agrees with the reference; run_c diverges -> exactly one finding.
    assert len(findings) == 1


def test_nondeterminism_incomparable_runs_raise() -> None:
    # Different seeds are not same-seed re-runs — comparing them would manufacture
    # a false flake. Fail closed with a clear error.
    run_a = _arm_result((_outcome("t1"),), seed=0)
    run_b = _arm_result((_outcome("t1"),), seed=1)
    with pytest.raises(ValueError, match="same-seed"):
        detect_nondeterminism([run_a, run_b])


# --------------------------------------------------------------------------- #
# fail-closed combination + report semantics
# --------------------------------------------------------------------------- #


def test_clean_result_passes_all_safeties() -> None:
    outcomes = (_outcome("t1", resolved=True), _outcome("t2", resolved=True))
    run_a = _arm_result(outcomes)
    run_b = _arm_result(outcomes)
    report = check_arm_safeties(
        [run_a, run_b],
        self_reports={("t1", 0): True, ("t2", 0): True},
        discriminator=_disc(passed=True),
    )
    assert report.clean is True
    assert report.invalidated is False
    assert report.findings == ()


def test_any_violation_invalidates_the_result() -> None:
    # One eval-lie is enough to invalidate (fail-closed) — clean is the AND.
    outcome = _outcome("t1", resolved=False, produced="wrong", expected="ok")
    run = _arm_result((outcome,))
    report = check_arm_safeties([run], self_reports={("t1", 0): True})
    assert report.clean is False
    assert report.invalidated is True
    assert report.findings_of(SafetyKind.EVAL_LIE)


def test_combined_report_collects_all_three_violation_kinds() -> None:
    # An arm result that is flaky AND lies AND reward-hacks: all three kinds.
    lie_outcome = _outcome("t1", resolved=False, produced="wrong", expected="ok")
    run_a = _arm_result((lie_outcome,))
    run_b = _arm_result((_outcome("t1", resolved=True, produced="ok", expected="ok"),))
    report = check_arm_safeties(
        [run_a, run_b],
        self_reports={("t1", 0): True},
        discriminator=_disc(passed=False, kill_score=0.0, killed=0, survived=("m1",)),
    )
    kinds = {f.kind for f in report.findings}
    assert kinds == {
        SafetyKind.NONDETERMINISM,
        SafetyKind.EVAL_LIE,
        SafetyKind.REWARD_HACKING,
    }
    assert report.clean is False


def test_omitted_signals_do_not_fabricate_a_pass() -> None:
    # No self_reports + no discriminator: only the (single-run) nondeterminism
    # check runs, finds nothing, and the report is clean — but it did NOT
    # manufacture a pass for the absent channels (they simply contributed none).
    run = _arm_result((_outcome("t1", resolved=True),))
    report = check_arm_safeties([run])
    assert report.clean is True
    assert report.findings == ()


def test_check_requires_at_least_one_run() -> None:
    with pytest.raises(ValueError, match="at least one"):
        check_arm_safeties([])


def test_report_subject_traces_arm_and_slice() -> None:
    run = _arm_result((_outcome("t1"),), arm=Arm.FOUNDRY, slice_identity="sid-9")
    report = check_arm_safeties([run])
    assert report.subject == "foundry:sid-9"


# --------------------------------------------------------------------------- #
# determinism of the safety checks themselves
# --------------------------------------------------------------------------- #


def test_safety_checks_are_deterministic() -> None:
    lie_outcome = _outcome("t1", resolved=False, produced="wrong", expected="ok")
    run_a = _arm_result((lie_outcome,))
    run_b = _arm_result((_outcome("t1", resolved=True, produced="ok", expected="ok"),))
    kwargs = {
        "self_reports": {("t1", 0): True},
        "discriminator": _disc(passed=False, survived=("m1",), kill_score=0.0, killed=0),
    }
    first = check_arm_safeties([run_a, run_b], **kwargs)  # type: ignore[arg-type]
    second = check_arm_safeties([run_a, run_b], **kwargs)  # type: ignore[arg-type]
    # A flaky safety check could not be trusted to catch a flaky eval — identical
    # frozen reports (content identity) on identical inputs.
    assert first == second
    assert first.content_identity() == second.content_identity()


def test_report_is_frozen() -> None:
    report = EvalSafetyReport(subject="x")
    with pytest.raises(ValidationError):  # pydantic frozen-instance error
        report.findings = ()  # type: ignore[misc]
