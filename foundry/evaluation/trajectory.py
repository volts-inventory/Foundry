"""Trajectory evals — score the agent's PROCESS / path, not its outcome (evaluation.md §5).

The outcome suites (:mod:`foundry.evaluation.suites`) ask *did the oracle resolve
the task*. A **trajectory eval** asks the orthogonal §5 question: *how good was
the PATH* — first-pass yield, rework loops, gate-fail-then-fix churn,
convergence. Two runs can resolve the same task (identical outcome) yet differ
sharply on process: one converges clean on the first pass; the other thrashes
through repeated gate failures and rework loops. §5 scores that difference.

This module is **deterministic over STRUCTURED run-trajectory signals** — NOT
parsed from live OpenTelemetry here, but the same signal vocabulary §5 names
(step/loop count, rework loops/feature, gate-fail-then-fix, policy violations,
convergence). evaluation.md §5 reserves LLM-as-judge for "where no oracle
exists"; a process score is a deterministic FUNCTION of the recorded signals, so
NO judge is consulted — the eval is reproducible by construction.

Scoring axis convention (LOAD-BEARING): the process score is in ``[0.0, 1.0]``,
**higher is better** (a clean, first-pass, converged path → near 1.0; a
high-rework, gate-thrashing, non-converged path → near 0.0). Each penalty
component is bounded and monotone in its signal, so a strictly higher-rework run
can NEVER out-score an otherwise-identical lower-rework run (a property the tests
pin).

HONESTY NOTE: scored over SYNTHETIC structured signals for a methodology dry-run;
``provenance`` defaults to :data:`Provenance.DRY_RUN`. The weights are a
manufactured dry-run rubric, not a calibrated claim.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from foundry.canonical import content_digest
from foundry.evaluation.hypothesis import Provenance
from foundry.schemas.base import FrozenModel

__all__ = [
    "ProcessAxis",
    "ProcessScore",
    "TrajectorySignals",
    "score_trajectory",
]


# Penalty weights per process signal (manufactured dry-run rubric — NOT a
# calibrated claim). Each is the score lost when its signal is at the saturation
# point below; signals saturate so an unbounded count can't drive the score
# negative. Weights sum to <= 1.0 so the clean-path score is exactly 1.0.
_W_REWORK = 0.30  # rework loops/feature (§5) — the headline process-churn signal
_W_GATE_FAIL_FIX = 0.25  # gate-fail-then-fix cycles (§5) — fail then re-submit churn
_W_EXCESS_STEPS = 0.20  # steps beyond the ideal step floor (§5 step/loop count)
_W_POLICY = 0.15  # policy-compliance violations (§5) — should be ZERO
_W_NONCONVERGENCE = 0.10  # did NOT converge to all-green (§5 convergence)

# Saturation points: the signal count at which a penalty component is fully spent
# (further churn does not reduce the score below this floor — bounded, monotone).
_SAT_REWORK = 4  # 4+ rework loops fully spends the rework penalty
_SAT_GATE_FAIL_FIX = 4  # 4+ gate-fail-then-fix cycles fully spends its penalty
_SAT_EXCESS_STEPS = 10  # 10+ steps over the ideal floor fully spends its penalty
_SAT_POLICY = 1  # ANY policy violation fully spends the policy penalty (zero-tolerance)


class ProcessAxis(FrozenModel):
    """One named process-axis component of the overall trajectory score.

    Each axis carries its raw ``signal`` count, the ``penalty`` it incurred (the
    score it cost, in ``[0, weight]``), and the ``weight`` (its max possible
    penalty). ``contribution = weight - penalty`` is what this axis ADDED to the
    final score. Exposed so a process score is auditable, not a black box.
    """

    name: str
    signal: int = Field(ge=0)  # the raw process-signal count (e.g. rework loops)
    weight: float = Field(ge=0.0, le=1.0)  # max penalty this axis can incur
    penalty: float = Field(ge=0.0, le=1.0)  # penalty actually incurred in [0, weight]

    @property
    def contribution(self) -> float:
        """Score this axis ADDED to the total (``weight - penalty``, in [0, weight])."""
        return self.weight - self.penalty


class TrajectorySignals(FrozenModel):
    """Structured run-trajectory signals for ONE feature/run (evaluation.md §5).

    These are the deterministic process signals §5 scores — captured here as a
    frozen record (in production they are reduced from OpenTelemetry traces; here
    they are supplied directly so the eval is reproducible). ``ideal_steps`` is
    the floor a clean path would take; ``steps`` over it is excess churn.

    SYNTHETIC: a dry-run signal record, not a real trace reduction.
    """

    feature_id: str
    steps: int = Field(ge=0)  # total steps/tool-calls taken on the path (§5)
    ideal_steps: int = Field(ge=0)  # the clean-path step floor (excess = steps - ideal)
    rework_loops: int = Field(ge=0)  # rework loops/feature (§5 headline churn signal)
    gate_fail_then_fix: int = Field(ge=0)  # gate-fail → fix → re-submit cycles (§5)
    policy_violations: int = Field(ge=0)  # unauthorized tool-calls / un-reviewed edits (§5)
    converged: bool  # reached all-green convergence (§5)
    synthetic: bool = True
    version: int = 1

    @model_validator(mode="after")
    def _check_steps(self) -> TrajectorySignals:
        if self.steps < self.ideal_steps:
            raise ValueError("steps must be >= ideal_steps (the ideal is a floor)")
        return self

    @property
    def excess_steps(self) -> int:
        """Steps taken BEYOND the clean-path floor (the §5 step/loop churn signal)."""
        return self.steps - self.ideal_steps


class ProcessScore(FrozenModel):
    """The deterministic PROCESS score for one trajectory (evaluation.md §5).

    ``score`` is in ``[0, 1]``, **higher is better** — a clean, first-pass,
    converged path scores near 1.0; a high-rework, gate-thrashing, non-converged
    path near 0.0. ``axes`` decomposes the score so each penalty is auditable.
    ``score_id`` is content-addressed (L7) over the signals so identical signals
    yield an identical score record (determinism witness).

    HONESTY: a dry-run process rubric; ``provenance`` defaults to DRY_RUN.
    """

    feature_id: str
    score: float = Field(ge=0.0, le=1.0)
    axes: tuple[ProcessAxis, ...]
    converged: bool
    score_id: str  # content_digest over the scored signals — L7 (determinism witness)
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def total_penalty(self) -> float:
        """Sum of all axis penalties (``1.0 - score`` modulo float rounding)."""
        return sum(a.penalty for a in self.axes)


def _saturating_penalty(signal: int, weight: float, saturation: int) -> float:
    """Bounded, monotone penalty: ``weight * min(signal, saturation) / saturation``.

    Monotone non-decreasing in ``signal`` and capped at ``weight`` — so a strictly
    higher signal never DECREASES the penalty (hence never increases the score),
    and an unbounded count can't drive the total score negative. ``saturation`` is
    always >= 1 (the module constants guarantee it).
    """
    capped = min(signal, saturation)
    return weight * capped / saturation


def score_trajectory(signals: TrajectorySignals) -> ProcessScore:
    """Score a trajectory's PROCESS deterministically (evaluation.md §5).

    Builds one bounded, monotone penalty per §5 signal (rework loops,
    gate-fail-then-fix, excess steps, policy violations, non-convergence), sums
    them, and returns ``score = 1.0 - total_penalty`` clamped to ``[0, 1]``.

    Properties (the tests pin these):

    * **Deterministic** — same signals ⇒ identical :class:`ProcessScore` (same
      ``score`` and same ``score_id``); NO LLM judge enters (§5: judge only where
      no oracle exists — a process score is a pure function of the signals).
    * **Monotone in churn** — a run that is strictly worse on a process signal
      (more rework / more gate-fail-then-fix / more excess steps / a policy
      violation / non-convergence) and no better elsewhere scores ``<=`` an
      otherwise-identical run. So a high-rework run can never out-score a
      low-rework one on the process axis.
    * **Clean path → 1.0** — zero churn, converged ⇒ score exactly 1.0.

    Higher is better.
    """
    axes = (
        ProcessAxis(
            name="rework_loops",
            signal=signals.rework_loops,
            weight=_W_REWORK,
            penalty=_saturating_penalty(signals.rework_loops, _W_REWORK, _SAT_REWORK),
        ),
        ProcessAxis(
            name="gate_fail_then_fix",
            signal=signals.gate_fail_then_fix,
            weight=_W_GATE_FAIL_FIX,
            penalty=_saturating_penalty(
                signals.gate_fail_then_fix, _W_GATE_FAIL_FIX, _SAT_GATE_FAIL_FIX
            ),
        ),
        ProcessAxis(
            name="excess_steps",
            signal=signals.excess_steps,
            weight=_W_EXCESS_STEPS,
            penalty=_saturating_penalty(signals.excess_steps, _W_EXCESS_STEPS, _SAT_EXCESS_STEPS),
        ),
        ProcessAxis(
            name="policy_violations",
            signal=signals.policy_violations,
            weight=_W_POLICY,
            penalty=_saturating_penalty(signals.policy_violations, _W_POLICY, _SAT_POLICY),
        ),
        ProcessAxis(
            name="non_convergence",
            signal=0 if signals.converged else 1,
            weight=_W_NONCONVERGENCE,
            # Non-convergence is the full penalty; convergence is none.
            penalty=0.0 if signals.converged else _W_NONCONVERGENCE,
        ),
    )

    total_penalty = sum(a.penalty for a in axes)
    score = max(0.0, min(1.0, 1.0 - total_penalty))

    score_id = content_digest(
        {
            "feature_id": signals.feature_id,
            "steps": signals.steps,
            "ideal_steps": signals.ideal_steps,
            "rework_loops": signals.rework_loops,
            "gate_fail_then_fix": signals.gate_fail_then_fix,
            "policy_violations": signals.policy_violations,
            "converged": signals.converged,
        }
    )

    return ProcessScore(
        feature_id=signals.feature_id,
        score=score,
        axes=axes,
        converged=signals.converged,
        score_id=score_id,
    )
