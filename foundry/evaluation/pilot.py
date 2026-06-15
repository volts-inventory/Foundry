"""Variance/base-rate PILOT for the Phase-1.5 decision gate (evaluation.md §1/§2).

This module is the **budgeted variance/base-rate pilot** that runs FIRST, on
tasks DISJOINT from the scored A/B, drawn from the SAME frame
(:mod:`foundry.evaluation.task_fixtures`). Its *only* purpose is to estimate the
two quantities a correct power calc needs but that are **not known a priori**
(evaluation.md §2, "Variance/base-rate pilot — BLOCKER"):

* (a) the **control-arm base resolution rate** — this study's strong-baseline
  control rate on the USD-primary resolution scale, NOT the borrowed ~21%
  frontier figure; and
* (b) the **task-level effect-size variance** — the between-task variance of the
  per-task control resolution AND of the per-task FOUNDRY−BASELINE difference,
  so ``n``/``k`` can be sized off the inferential unit (the TASK), not run-level
  draws (treating ``n×k`` as independent overstates effective N — §2).

These outputs are **read-only into the power calc** — the pilot is *not* a second
scored arm, *not* a rung to descend to, and CANNOT be promoted to evidence. It
**consumes part of the funded budget** (§8 roll-up reserves it; recorded here).

INTEGRITY GUARDS (load-bearing — evaluation.md §1/§2):

1. **Disjoint-slice only.** :func:`run_pilot` runs over ``frame.pilot_slice``
   ONLY and asserts it is disjoint from ``frame.scored_slice``; asking it to run
   over (or touch) any scored task RAISES (:class:`ScoredSliceTouchedError`). The
   scored A/B tasks are never observed by the pilot, so the pilot cannot leak
   information that would invalidate the confirmatory run.
2. **NOT a scored arm.** :class:`PilotResult` carries ``provenance=PILOT`` and a
   ``scored=False`` flag, and there is NO path that turns a pilot run into a
   scored verdict — it exposes only the read-only summary the power calc consumes.
   Re-running the pilot does **not** re-draw or otherwise alter the scored A/B
   slice (it is a deterministic prefix-cut of one frame permutation — EV6); the
   pilot only re-estimates variance/base-rate, it never selects the scored tasks.
3. **Budget consumed + recorded.** ``consumed_budget_microusd`` records the USD
   the pilot spent (the sum of every arm's simulated per-attempt cost); the §8
   roll-up reserves this as a budgeted line item.

HONESTY NOTE (read before reuse): this is a **methodology dry-run on synthetic
fixtures**, NOT funded real evidence. The control "base rate" and "variance"
estimated here are computed off the SIMULATED stand-in arms (the baseline is not
a real strong single agent — see :mod:`foundry.evaluation.harness`), so they
assert NOTHING about real Foundry-vs-baseline performance. They only exercise the
estimation path end-to-end so the later power-calc module (which CONSUMES this
result) builds on a stable, validated contract. There is no inferential
statistics (power calc, bootstrap, alpha-spending) here — that lives downstream
and reads :class:`PilotResult` read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from foundry.evaluation.harness import ArmResult, run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.task_fixtures import TaskFixture, TaskFrame

__all__ = [
    "DEFAULT_PILOT_ARMS",
    "PilotResult",
    "ScoredSliceTouchedError",
    "run_pilot",
]

# The pilot estimates the control base rate + variance on the CONTROL arm and the
# FOUNDRY−CONTROL per-task difference, so it runs the control arm and the
# treatment arm. (The +gates arm is NOT needed for the §2 primary power calc; the
# pilot deliberately stays minimal — it consumes the funded budget.)
_CONTROL_ARM = Arm.BASELINE_SINGLE_AGENT  # the §2 control / strong-baseline arm
_TREATMENT_ARM = Arm.FOUNDRY  # the §2 treatment arm (for the difference variance)
DEFAULT_PILOT_ARMS: tuple[Arm, ...] = (_TREATMENT_ARM, _CONTROL_ARM)


class ScoredSliceTouchedError(RuntimeError):
    """Raised iff the pilot is asked to observe a scored A/B task (integrity §1).

    The pilot runs on the pilot slice ONLY; touching the scored slice would let
    pilot estimation leak into the confirmatory run. Fail closed.
    """


def _per_task_resolution_rates(result: ArmResult) -> dict[str, float]:
    """Per-task resolution rate (resolved / repeats) for ONE arm, keyed by task.

    The TASK is the inferential unit (evaluation.md §2 — power is based on
    task-level effect-size variance, not run-level), so we collapse each task's
    ``k_repeats`` attempts to a single per-task rate before taking variance.
    """
    resolved: dict[str, int] = {}
    total: dict[str, int] = {}
    for o in result.outcomes:
        resolved[o.task_id] = resolved.get(o.task_id, 0) + (1 if o.resolved else 0)
        total[o.task_id] = total.get(o.task_id, 0) + 1
    return {tid: resolved[tid] / total[tid] for tid in total}


def _population_variance(values: list[float]) -> float:
    """Population variance of ``values`` (>= 0; 0.0 for < 2 samples).

    Population (divide-by-N), not sample, variance — the pilot estimates the
    task-distribution's variance component, and the power-calc module applies its
    own finite-sample correction. Always non-negative.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return sum((v - mean) ** 2 for v in values) / n


class PilotResult(FrozenModel):
    """The READ-ONLY summary the power calc consumes (evaluation.md §2).

    Exposes EXACTLY the pilot outputs the power calc needs — the control base
    rate, the task-level variance components, the pilot size, the consumed
    budget, and the bound pilot-slice identity — and nothing that could be
    mistaken for a scored verdict.

    INTEGRITY: ``provenance == Provenance.PILOT`` and ``scored is False`` — this
    is NOT a scored arm and CANNOT be promoted to evidence (evaluation.md §1/§2).
    The power calc reads these fields read-only to freeze ``n``/``k``/MDE; it does
    NOT re-run or descend to the pilot, and re-running the pilot does not re-draw
    the scored A/B slice (the partition is a deterministic prefix-cut, EV6).

    HONESTY: a dry-run estimate on synthetic, simulated arms — asserts NOTHING
    about real Foundry-vs-baseline performance (module docstring).
    """

    # (a) The control-arm base resolution rate (the §2 strong-baseline control
    # rate on the USD-primary resolution scale; ITT pass-rate of the control arm):
    control_base_rate: float = Field(ge=0.0, le=1.0)

    # (b) Task-level effect-size variance components (the inferential unit is the
    # TASK — §2). `effect_size_variance` is the between-task variance of the
    # per-task FOUNDRY−CONTROL difference (the effect-size the A/B is powered on);
    # `control_resolution_variance` is the between-task variance of the control
    # arm's per-task resolution (the base-rate dispersion the power calc also uses).
    effect_size_variance: float = Field(ge=0.0)
    control_resolution_variance: float = Field(ge=0.0)

    pilot_n: int = Field(gt=0)  # number of DISTINCT pilot tasks observed
    k_repeats: int = Field(gt=0)  # repeats/task used to average run-level nondeterminism
    consumed_budget_microusd: int = Field(ge=0)  # budget the pilot spent (recorded — §8)

    # Binds the estimate to the pilot slice it was measured on (EV7 — the slice
    # identity is content-addressed to its parent frame; the power calc / manifest
    # check the scored slice is drawn from the SAME frame):
    eval_set_identity: str

    # INTEGRITY flags — the pilot is NOT a scored arm and cannot be promoted:
    provenance: Provenance = Provenance.PILOT
    scored: bool = False
    version: int = 1


def run_pilot(
    frame: TaskFrame,
    *,
    arms: tuple[Arm, ...] = DEFAULT_PILOT_ARMS,
    k_repeats: int = 2,
    seed: int = 0,
    budget_usd_microusd: int = 5_000_000,
) -> PilotResult:
    """Run the variance/base-rate pilot over ``frame.pilot_slice`` ONLY.

    Runs each arm in ``arms`` over the pilot slice via the harness
    :func:`foundry.evaluation.harness.run_arm`, then estimates the read-only
    summary the power calc needs (evaluation.md §2):

    * ``control_base_rate`` — the control arm's ITT pass-rate;
    * ``effect_size_variance`` — between-task variance of the per-task
      FOUNDRY−CONTROL difference (the effect-size the A/B is powered on);
    * ``control_resolution_variance`` — between-task variance of the control
      arm's per-task resolution;
    * ``consumed_budget_microusd`` — the budget the pilot consumed (recorded).

    INTEGRITY (evaluation.md §1/§2):

    * **Disjoint-slice only.** Runs over ``frame.pilot_slice`` and ASSERTS it is
      disjoint from ``frame.scored_slice``; if any pilot task id appears in the
      scored slice (or vice versa) it raises :class:`ScoredSliceTouchedError` —
      the pilot never observes a scored A/B task.
    * **Not a scored arm.** The returned :class:`PilotResult` carries
      ``provenance=PILOT`` + ``scored=False`` and exposes only the read-only
      power-calc summary. There is no path from this function to a scored
      verdict. Re-running it does NOT re-draw the scored slice (the partition is
      a deterministic, seed-fixed prefix-cut of one frame permutation — EV6); it
      only re-estimates variance/base-rate.
    * **Budget recorded.** The pilot consumes part of the funded budget (§8);
      ``consumed_budget_microusd`` records what it spent.

    Parameters
    ----------
    frame:
        The shared sampling frame. The pilot runs on ``frame.pilot_slice`` only.
    arms:
        The arms to run (default: FOUNDRY treatment + the strong-baseline
        control). The control arm (:data:`_CONTROL_ARM`) MUST be present — the
        base rate is undefined without it.
    k_repeats:
        Repeats per task (averages run-level nondeterminism — §7). The per-task
        resolution rate collapses these to one value per task before the
        between-task variance is taken (the TASK is the inferential unit — §2).
    seed:
        Folded into the SIMULATED arms' draws (harness determinism). Same
        ``(frame, arms, k_repeats, seed, budget)`` ⇒ identical
        :class:`PilotResult`.
    budget_usd_microusd:
        The equal-USD per-attempt budget cap forwarded to each arm.
    """
    if _CONTROL_ARM not in arms:
        raise ValueError(
            f"the pilot must run the control arm {_CONTROL_ARM.value!r} to estimate the base rate"
        )

    pilot = frame.pilot_slice
    scored = frame.scored_slice

    # INTEGRITY GUARD §1: the pilot slice must be DISJOINT from the scored slice —
    # the pilot never observes a scored A/B task. Fail closed on any overlap.
    overlap = set(pilot.task_ids) & set(scored.task_ids)
    if overlap:
        raise ScoredSliceTouchedError(
            f"pilot slice overlaps the scored A/B slice on {sorted(overlap)} — "
            "the variance/base-rate pilot must run on tasks DISJOINT from the scored run "
            "(evaluation.md §2)"
        )
    if not pilot.task_ids:
        raise ValueError("the pilot slice is empty — nothing to estimate")

    # The fixtures the harness indexes into — RESTRICTED to the pilot slice so the
    # pilot can never touch a scored task even via a stray fixture lookup.
    pilot_ids = set(pilot.task_ids)
    pilot_fixtures: dict[str, TaskFixture] = {
        f.task_id: f for f in frame.fixtures if f.task_id in pilot_ids
    }

    results: dict[Arm, ArmResult] = {}
    consumed = 0
    for arm in arms:
        result = run_arm(
            arm,
            pilot,
            pilot_fixtures,
            k_repeats=k_repeats,
            seed=seed,
            budget_usd_microusd=budget_usd_microusd,
        )
        results[arm] = result
        consumed += result.total_cost_microusd

    control = results[_CONTROL_ARM]

    # (a) Control base rate = the control arm's ITT pass-rate (resolved / attempted).
    control_base_rate = control.resolved_count / control.attempted

    # (b) Task-level variance components (the TASK is the inferential unit — §2).
    control_rates = _per_task_resolution_rates(control)
    control_resolution_variance = _population_variance(list(control_rates.values()))

    if _TREATMENT_ARM in results:
        treatment_rates = _per_task_resolution_rates(results[_TREATMENT_ARM])
        # Per-task FOUNDRY−CONTROL difference, over the COMMON task set (sorted ->
        # order-independent). This paired per-task difference is the effect-size
        # the §2 cluster-bootstrap A/B is powered on.
        diffs = [
            treatment_rates[tid] - control_rates[tid]
            for tid in sorted(control_rates)
            if tid in treatment_rates
        ]
        effect_size_variance = _population_variance(diffs)
    else:
        # No treatment arm supplied: fall back to the control's resolution
        # variance as the effect-size dispersion (still a valid >= 0 estimate).
        effect_size_variance = control_resolution_variance

    return PilotResult(
        control_base_rate=control_base_rate,
        effect_size_variance=effect_size_variance,
        control_resolution_variance=control_resolution_variance,
        pilot_n=len(pilot.task_ids),
        k_repeats=k_repeats,
        consumed_budget_microusd=consumed,
        eval_set_identity=pilot.identity,
    )
