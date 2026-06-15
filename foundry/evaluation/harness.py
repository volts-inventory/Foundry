"""Oracle-scored per-arm harness + the ITT runner (Phase-1.5 decision gate).

This module is the **deterministic per-arm runner** the Phase-1.5 A/B (and the
variance/base-rate pilot) drive on the synthetic task frame
(:mod:`foundry.evaluation.task_fixtures`). For each :class:`TaskFixture` in a
:class:`TaskSlice` it produces a per-(task, repeat) resolution OUTCOME, scores it
against the fixture's :class:`OracleDescriptor` by a deterministic **oracle**
equality check (evaluation.md §4 — "the primary is never LLM-judge-scored"), and
records the USD cost so the arms can be compared at **equal USD** (§2).

ITT semantics (evaluation.md §2, LOAD-BEARING)
----------------------------------------------
The primary endpoint is scored **intent-to-treat**: the denominator is **all
*initiated* tasks per arm** (the full slice x ``k_repeats``), and **any
non-completion — crash, budget exhaustion, escalation-without-resolution —
scores as an unresolved task, never a dropped sample**. Differential attrition
otherwise inflates whichever arm aborts/escalates more (Foundry has more
abort/escalate paths than the ungated baseline). So:

* every initiated attempt yields exactly one :class:`TaskOutcome` on the result
  (CRASHED / OVER_BUDGET attempts included, scored ``resolved=False``);
* :func:`itt_pass_rate` divides ``resolved`` by ``attempted`` where
  ``attempted == len(slice) * k_repeats`` — it can NEVER silently shrink the
  denominator (a guard ``assert``s the count of recorded outcomes equals the
  initiated count).

HONESTY NOTE (read before reuse): this is a **methodology dry-run on synthetic
fixtures**, NOT funded real evidence. Outcomes default
``provenance == Provenance.DRY_RUN``.

* The **FOUNDRY** arm wraps the SHIPPED pipeline (:func:`foundry.cli.run`, which
  compiles :func:`build_factory_graph`) and runs **keyless** via the existing
  test doubles (LEARNINGS D1-D3): a fixture resolves iff that run reaches a clean
  PASS *and* emits an ``open_pr`` action. Its USD cost is SIMULATED
  deterministically (the shipped ``RunResult`` carries no metered cost yet); the
  resolution bit is REAL pipeline behavior, the dollar figure is a stand-in.
* The **BASELINE_SINGLE_AGENT** and **SINGLE_AGENT_PLUS_GATES** arms are
  **SIMULATED stand-ins** — there is no real strong single agent in Phase-1.5.
  Their resolved/unresolved outcome is a deterministic Bernoulli draw folded from
  ``(fixture.base_resolution_seed, arm, repeat)`` (process-stable
  ``content_digest``, never ``hash()`` — L7), so the harness is reproducible.
  These arms assert NOTHING about a real single agent; they exist only to
  exercise the methodology end-to-end.

There is statistics-ADJACENT logic here (a deterministic pass-rate ratio) but NO
inferential statistics — power calcs, bootstrap, alpha-spending, and the decision
table live in later M1.5 modules.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.task_fixtures import TaskFixture, TaskSlice

__all__ = [
    "ArmResult",
    "FoundryRunner",
    "OutcomeStatus",
    "Run",
    "TaskOutcome",
    "default_foundry_runner",
    "itt_pass_rate",
    "make_run",
    "run_arm",
]

# Default per-attempt SIMULATED USD cost (micro-USD, never float — base.py
# convention). Foundry pays coordination + monitor overhead the baseline pours
# into the task (evaluation.md §8 roll-up), so its per-attempt stand-in cost is
# higher. These are DRY-RUN fixtures, not a metered figure.
_SIMULATED_COST_MICROUSD: dict[Arm, int] = {
    Arm.FOUNDRY: 1_200_000,
    Arm.BASELINE_SINGLE_AGENT: 800_000,
    Arm.SINGLE_AGENT_PLUS_GATES: 1_000_000,
}

# Per-arm multiplier on the control base rate for the SIMULATED arms — a
# manufactured dry-run knob (NOT a claim). The +gates arm gets a small uplift
# over the bare baseline so the three arms are distinguishable in a dry run.
_SIMULATED_RATE_FACTOR: dict[Arm, float] = {
    Arm.BASELINE_SINGLE_AGENT: 1.0,
    Arm.SINGLE_AGENT_PLUS_GATES: 1.1,
}


class OutcomeStatus(StrEnum):
    """Terminal disposition of one initiated attempt.

    Every status other than RESOLVED scores ``resolved=False`` and STILL counts
    toward the ITT denominator (evaluation.md §2 — non-completion is unresolved,
    never dropped).
    """

    RESOLVED = "resolved"  # oracle check passed
    UNRESOLVED = "unresolved"  # ran to completion, oracle check failed
    CRASHED = "crashed"  # the attempt errored/aborted — ITT: counts as unresolved
    OVER_BUDGET = "over_budget"  # exhausted the per-attempt USD budget — ITT: unresolved


class TaskOutcome(FrozenModel):
    """The deterministic outcome of ONE initiated (task, repeat) attempt.

    ``resolved`` is the oracle verdict (deterministic equality vs the fixture's
    :class:`OracleDescriptor`, NOT an LLM judge — §4). A non-RESOLVED status is
    scored ``resolved=False`` and is RETAINED on the result (ITT — §2).
    """

    task_id: str
    repeat: int = Field(ge=0)
    arm: Arm
    status: OutcomeStatus
    resolved: bool
    cost_microusd: int = Field(ge=0)  # SIMULATED per-attempt USD (micro-USD, never float)
    # The oracle target this attempt was scored against (provenance for audit):
    expected_output: str
    produced_output: str | None  # None when the attempt crashed before producing output
    version: int = 1


class ArmResult(FrozenModel):
    """All per-(task, repeat) outcomes for ONE arm over ONE slice.

    Frozen + content-addressable. The ITT denominator is ``attempted`` (the full
    slice x ``k_repeats``); :func:`itt_pass_rate` divides ``resolved_count`` by it
    and cannot silently shrink it (see :func:`run_arm`'s guard).

    HONESTY: ``provenance`` defaults to DRY_RUN; SIMULATED arms (everything but
    FOUNDRY) make no claim about a real single agent (see module docstring).
    """

    arm: Arm
    slice_identity: str  # TaskSlice.identity it ran over (binds to the parent frame — EV7)
    slice_name: str
    k_repeats: int = Field(gt=0)
    seed: int = Field(ge=0)
    budget_usd_microusd: int = Field(ge=0)  # per-attempt equal-USD budget cap
    attempted: int = Field(gt=0)  # ITT denominator: len(slice) * k_repeats (initiated tasks)
    outcomes: tuple[TaskOutcome, ...]
    simulated: bool  # True for the synthetic stand-in arms (BASELINE / +gates)
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def resolved_count(self) -> int:
        """Number of attempts the oracle scored resolved."""
        return sum(1 for o in self.outcomes if o.resolved)

    @property
    def total_cost_microusd(self) -> int:
        """Total SIMULATED USD spent across all attempts (for equal-USD compare)."""
        return sum(o.cost_microusd for o in self.outcomes)


class Run(FrozenModel):
    """A content-addressed record of one harness invocation (one arm x one slice).

    Captures the arm, the slice/eval-set identity, the provenance (DRY_RUN by
    default), and the full :class:`ArmResult` (per-task outcomes + costs). It is
    the unit the pilot and the A/B runner aggregate; ``run_id`` is content-
    addressed (L7 ``content_digest``, never ``hash()``) so an identical
    invocation produces an identical id.

    HONESTY: a dry-run record asserts NOTHING about real performance.
    """

    arm: Arm
    slice_identity: str
    seed: int = Field(ge=0)
    provenance: Provenance = Provenance.DRY_RUN
    result: ArmResult
    run_id: str  # content_digest over (arm, slice_identity, seed, k, budget) — L7
    version: int = 1


# A runner for the FOUNDRY arm: maps a fixture to a (resolved, produced_output)
# pair by exercising the shipped pipeline. Injectable so tests pin it; the
# default wraps foundry.cli.run keyless via the doubles (D1-D3).
FoundryRunner = Callable[["TaskFixture", int], "tuple[bool, str | None]"]


def default_foundry_runner(fixture: TaskFixture, repeat: int) -> tuple[bool, str | None]:
    """Drive the SHIPPED pipeline (:func:`foundry.cli.run`) for one fixture.

    Runs **keyless** via the existing test doubles (LEARNINGS D1-D3 — MockTransport
    + StubAdapter, no live API key / no ``claude -p`` / no network). The fixture is
    RESOLVED iff the run reaches a clean PASS *and* emits an ``open_pr`` action
    (the shipped definition of a completed delivery, mirroring the CLI smoke
    contract). The produced output, on a PASS, is the fixture's oracle target — a
    dry-run convenience so the synthetic oracle scores resolved when the REAL
    pipeline completes; the resolution BIT is genuine pipeline behavior.

    The ``foundry.cli`` import is deferred (function-local) to keep
    ``foundry.evaluation.harness`` import-cheap — the heavy graph/proxy wiring is
    only pulled in when the FOUNDRY arm actually runs.
    """
    from foundry.cli import run as cli_run  # deferred: keep harness import-cheap

    thread_id = f"eval-foundry-{fixture.task_id}-r{repeat}"
    result = cli_run(fixture.spec, thread_id=thread_id)
    completed = bool(result.passed and result.open_pr_idempotency_key is not None)
    # On a genuine completion the arm "produces" the oracle target; otherwise it
    # produced something that fails the oracle equality check (non-resolution).
    produced = fixture.oracle.expected_output if completed else None
    return completed, produced


def _simulated_resolution(fixture: TaskFixture, arm: Arm, repeat: int, seed: int) -> bool:
    """Deterministic Bernoulli draw for a SIMULATED arm.

    Folds the fixture's process-stable ``base_resolution_seed`` with the arm,
    repeat, and run seed via ``content_digest`` (L7 — never ``hash()``, which is
    seed-randomized per process) into a uniform in [0, 1), and resolves iff it is
    below the arm-adjusted control base rate (capped at <1 so it stays a draw).

    SYNTHETIC: a manufactured stand-in for a real single agent (module docstring).
    """
    digest = content_digest(
        {
            "base_resolution_seed": fixture.base_resolution_seed,
            "arm": arm.value,
            "repeat": repeat,
            "seed": seed,
        }
    )
    draw = int(digest[:8], 16) / 0x1_0000_0000  # uniform in [0, 1)
    rate = min(0.99, fixture.control_base_rate * _SIMULATED_RATE_FACTOR[arm])
    return draw < rate


def _score(expected: str, produced: str | None) -> bool:
    """The deterministic ORACLE check: exact equality (NOT an LLM judge — §4).

    A ``None`` produced output (a crashed/aborted attempt that produced nothing)
    can never equal the oracle target, so it scores not-resolved — the ITT rule.
    """
    return produced is not None and produced == expected


def run_arm(
    arm: Arm,
    slice: TaskSlice,
    fixtures: dict[str, TaskFixture],
    *,
    k_repeats: int = 1,
    seed: int = 0,
    budget_usd_microusd: int = 5_000_000,
    foundry_runner: FoundryRunner = default_foundry_runner,
) -> ArmResult:
    """Run ``arm`` over every fixture in ``slice`` (``k_repeats`` each), oracle-scored.

    Deterministic: the same ``(arm, slice, fixtures, k_repeats, seed,
    budget_usd_microusd)`` yields an IDENTICAL :class:`ArmResult` (same outcomes,
    same costs, same order). Outcomes are ordered ``(task_id sorted, repeat)`` so
    the result is independent of dict iteration order.

    ITT (evaluation.md §2): EVERY initiated attempt (``len(slice) * k_repeats``)
    yields exactly one :class:`TaskOutcome` — a crash/abort or an over-budget
    attempt is scored ``resolved=False`` and RETAINED, never dropped. A guard
    asserts the recorded-outcome count equals the initiated count, so the ITT
    denominator can never silently shrink.

    Parameters
    ----------
    arm:
        FOUNDRY wraps the shipped pipeline (real resolution bit, simulated cost);
        the other two arms are SIMULATED stand-ins (module docstring).
    slice:
        The :class:`TaskSlice` to run (its ``task_ids`` index into ``fixtures``).
    fixtures:
        ``task_id -> TaskFixture`` lookup (e.g. ``{f.task_id: f for f in frame.fixtures}``).
    k_repeats:
        Repeats per task (averages LLM nondeterminism — §7). The ITT denominator
        is ``len(slice) * k_repeats``.
    seed:
        Folded into the SIMULATED arms' resolution draw (FOUNDRY ignores it — its
        determinism comes from the doubles).
    budget_usd_microusd:
        The equal-USD per-attempt budget cap. An attempt whose simulated cost
        exceeds it is charged the CAP and scored OVER_BUDGET / unresolved (ITT).
    foundry_runner:
        Injectable FOUNDRY-arm runner (defaults to the keyless shipped pipeline);
        tests pin it to force crash/over-budget/known-resolution scenarios.
    """
    task_ids = tuple(sorted(slice.task_ids))  # sorted -> order-independent result
    attempted = len(task_ids) * k_repeats
    simulated = arm is not Arm.FOUNDRY

    outcomes: list[TaskOutcome] = []
    for task_id in task_ids:
        fixture = fixtures[task_id]
        for repeat in range(k_repeats):
            outcomes.append(
                _attempt(
                    arm,
                    fixture,
                    repeat,
                    seed=seed,
                    budget_usd_microusd=budget_usd_microusd,
                    foundry_runner=foundry_runner,
                )
            )

    # ITT GUARD: one retained outcome per initiated attempt — the denominator can
    # never be silently shrunk by dropping a crashed/over-budget attempt (§2).
    assert len(outcomes) == attempted, (
        f"ITT violation: {len(outcomes)} outcomes for {attempted} initiated attempts"
    )

    return ArmResult(
        arm=arm,
        slice_identity=slice.identity,
        slice_name=slice.name,
        k_repeats=k_repeats,
        seed=seed,
        budget_usd_microusd=budget_usd_microusd,
        attempted=attempted,
        outcomes=tuple(outcomes),
        simulated=simulated,
    )


def _attempt(
    arm: Arm,
    fixture: TaskFixture,
    repeat: int,
    *,
    seed: int,
    budget_usd_microusd: int,
    foundry_runner: FoundryRunner,
) -> TaskOutcome:
    """Produce + oracle-score ONE (task, repeat) attempt, charging USD cost.

    Order of ITT dispositions: a crash (the runner raised) is CRASHED/unresolved;
    an over-budget attempt is OVER_BUDGET/unresolved (charged the CAP, not the
    overrun); otherwise the oracle equality verdict decides RESOLVED vs UNRESOLVED.
    """
    expected = fixture.oracle.expected_output
    nominal_cost = _SIMULATED_COST_MICROUSD[arm]

    # Equal-USD budget: an attempt that would exceed the cap is a non-completion
    # (ITT unresolved), charged the CAP so equal-USD accounting stays honest.
    if nominal_cost > budget_usd_microusd:
        return TaskOutcome(
            task_id=fixture.task_id,
            repeat=repeat,
            arm=arm,
            status=OutcomeStatus.OVER_BUDGET,
            resolved=False,
            cost_microusd=budget_usd_microusd,
            expected_output=expected,
            produced_output=None,
        )

    if arm is Arm.FOUNDRY:
        try:
            completed, produced = foundry_runner(fixture, repeat)
        except Exception:  # ITT: any crash is a non-resolution, not a dropped sample
            return TaskOutcome(
                task_id=fixture.task_id,
                repeat=repeat,
                arm=arm,
                status=OutcomeStatus.CRASHED,
                resolved=False,
                cost_microusd=nominal_cost,
                expected_output=expected,
                produced_output=None,
            )
        produced_output = produced if completed else None
    else:
        # SIMULATED arm: a deterministic Bernoulli draw stands in for the run.
        completed = _simulated_resolution(fixture, arm, repeat, seed)
        produced_output = expected if completed else None

    resolved = _score(expected, produced_output)
    return TaskOutcome(
        task_id=fixture.task_id,
        repeat=repeat,
        arm=arm,
        status=OutcomeStatus.RESOLVED if resolved else OutcomeStatus.UNRESOLVED,
        resolved=resolved,
        cost_microusd=nominal_cost,
        expected_output=expected,
        produced_output=produced_output,
    )


def itt_pass_rate(arm_result: ArmResult) -> float:
    """Intent-to-treat pass rate: ``resolved / attempted`` (evaluation.md §2).

    The denominator is ``arm_result.attempted`` — the FULL slice x ``k_repeats``
    (all *initiated* tasks). Crashes, over-budget, and escalation-without-
    resolution are already RETAINED in ``outcomes`` as ``resolved=False``, so they
    sit in the numerator's complement, NOT outside the denominator. A guard
    asserts the denominator equals the count of recorded outcomes — it can never
    silently shrink (the very attrition bias §2 forbids).
    """
    assert arm_result.attempted == len(arm_result.outcomes), (
        "ITT violation: denominator != recorded outcomes (a sample was dropped)"
    )
    if arm_result.attempted == 0:  # defensive: ArmResult requires attempted>0 anyway
        return 0.0
    return arm_result.resolved_count / arm_result.attempted


def make_run(arm_result: ArmResult) -> Run:
    """Wrap an :class:`ArmResult` in a content-addressed :class:`Run` record.

    ``run_id`` is a ``content_digest`` (L7) over the invocation identity so an
    identical invocation produces an identical id (and a DRY_RUN vs SCORED run of
    the same shape differ via ``provenance`` in the digest).
    """
    run_id = content_digest(
        {
            "arm": arm_result.arm.value,
            "slice_identity": arm_result.slice_identity,
            "seed": arm_result.seed,
            "k_repeats": arm_result.k_repeats,
            "budget": arm_result.budget_usd_microusd,
            "provenance": arm_result.provenance.value,
        }
    )
    return Run(
        arm=arm_result.arm,
        slice_identity=arm_result.slice_identity,
        seed=arm_result.seed,
        provenance=arm_result.provenance,
        result=arm_result,
        run_id=run_id,
    )
