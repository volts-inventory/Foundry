"""Conformance tests for the trajectory (PROCESS) eval (T-M6-02; docs/evaluation.md §5).

Contract:

* scores the agent's PROCESS from structured §5 signals (rework loops,
  gate-fail-then-fix, excess steps, policy violations, convergence);
* a clean first-pass converged path scores 1.0;
* a HIGH-rework run scores STRICTLY WORSE than an otherwise-identical low-rework
  run (monotone in churn) — process-axis penalises thrash;
* determinism — same signals => identical ProcessScore (score + score_id);
* score is bounded in [0,1] even under saturating churn.

SYNTHETIC dry-run only.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.hypothesis import Provenance
from foundry.evaluation.trajectory import (
    ProcessAxis,
    ProcessScore,
    TrajectorySignals,
    score_trajectory,
)
from foundry.schemas.base import FrozenModel


def _clean(feature_id: str = "feat-clean") -> TrajectorySignals:
    return TrajectorySignals(
        feature_id=feature_id,
        steps=5,
        ideal_steps=5,
        rework_loops=0,
        gate_fail_then_fix=0,
        policy_violations=0,
        converged=True,
    )


# ---- types / frozen ---------------------------------------------------------


def test_score_is_frozen_and_typed() -> None:
    score = score_trajectory(_clean())
    assert isinstance(score, ProcessScore)
    assert isinstance(score, FrozenModel)
    assert all(isinstance(a, ProcessAxis) for a in score.axes)
    with pytest.raises(Exception):  # noqa: B017 — frozen
        score.score = 0.0  # type: ignore[misc]


def test_signals_reject_steps_below_ideal() -> None:
    with pytest.raises(ValueError, match="ideal"):
        TrajectorySignals(
            feature_id="bad",
            steps=2,
            ideal_steps=5,
            rework_loops=0,
            gate_fail_then_fix=0,
            policy_violations=0,
            converged=True,
        )


# ---- clean path -------------------------------------------------------------


def test_clean_path_scores_one() -> None:
    score = score_trajectory(_clean())
    assert score.score == 1.0
    assert score.total_penalty == 0.0
    assert score.converged is True


# ---- monotone in churn (the headline property) ------------------------------


def test_high_rework_scores_strictly_worse() -> None:
    low = score_trajectory(_clean("feat").model_copy(update={"rework_loops": 0}))
    high = score_trajectory(_clean("feat").model_copy(update={"rework_loops": 3}))
    assert high.score < low.score


def test_gate_fail_then_fix_penalised() -> None:
    base = score_trajectory(_clean("f"))
    thrash = score_trajectory(_clean("f").model_copy(update={"gate_fail_then_fix": 2}))
    assert thrash.score < base.score


def test_excess_steps_penalised() -> None:
    base = score_trajectory(_clean("f"))
    wandering = score_trajectory(
        _clean("f").model_copy(update={"steps": 12, "ideal_steps": 5})
    )
    assert wandering.score < base.score


def test_policy_violation_zero_tolerance() -> None:
    base = score_trajectory(_clean("f"))
    violating = score_trajectory(_clean("f").model_copy(update={"policy_violations": 1}))
    assert violating.score < base.score
    # ANY violation fully spends the policy penalty (saturates at 1).
    one = score_trajectory(_clean("f").model_copy(update={"policy_violations": 1}))
    five = score_trajectory(_clean("f").model_copy(update={"policy_violations": 5}))
    assert one.score == five.score


def test_non_convergence_penalised() -> None:
    base = score_trajectory(_clean("f"))
    stuck = score_trajectory(_clean("f").model_copy(update={"converged": False}))
    assert stuck.score < base.score
    assert stuck.converged is False


def test_monotone_across_each_signal() -> None:
    """Strictly more churn on any one signal never increases the score."""
    prev = score_trajectory(_clean("f")).score
    for n in range(1, 6):
        cur = score_trajectory(_clean("f").model_copy(update={"rework_loops": n})).score
        assert cur <= prev
        prev = cur


# ---- bounded ----------------------------------------------------------------


def test_score_bounded_under_saturating_churn() -> None:
    worst = score_trajectory(
        TrajectorySignals(
            feature_id="worst",
            steps=999,
            ideal_steps=1,
            rework_loops=999,
            gate_fail_then_fix=999,
            policy_violations=999,
            converged=False,
        )
    )
    assert 0.0 <= worst.score <= 1.0


# ---- determinism ------------------------------------------------------------


def test_score_is_deterministic() -> None:
    sig = _clean("f").model_copy(update={"rework_loops": 2, "gate_fail_then_fix": 1})
    a = score_trajectory(sig)
    b = score_trajectory(sig)
    assert a.score == b.score
    assert a.score_id == b.score_id
    assert a.content_identity() == b.content_identity()


def test_score_id_distinguishes_signals() -> None:
    a = score_trajectory(_clean("f").model_copy(update={"rework_loops": 1}))
    b = score_trajectory(_clean("f").model_copy(update={"rework_loops": 2}))
    assert a.score_id != b.score_id


def test_provenance_dry_run() -> None:
    assert score_trajectory(_clean()).provenance is Provenance.DRY_RUN


def test_axes_contributions_sum_to_score() -> None:
    score = score_trajectory(_clean("f").model_copy(update={"rework_loops": 2}))
    total_contribution = sum(a.contribution for a in score.axes)
    assert total_contribution == pytest.approx(score.score)
