"""Outcome-eval **suites** — named groups of oracle-scored tasks (evaluation.md §3).

This module sits ON TOP of the per-arm oracle harness
(:mod:`foundry.evaluation.harness`) and the synthetic task frame
(:mod:`foundry.evaluation.task_fixtures`). Where the harness scores ONE arm over
ONE slice, a **suite** is the §3 outcome-eval unit: a *named* group of
oracle-scored tasks (golden / in-house-novel / SWE-bench-style / SWE-EVO-style)
that reports a single suite-level **resolution rate** plus the per-task outcomes
that rolled up into it.

Two load-bearing properties, both inherited from the harness and re-asserted here
so a suite can never quietly violate them:

* **Oracle-scored only** (§3/§4): suite resolution reuses the harness oracle
  equality check verbatim — there is NO LLM judge anywhere in this module.
* **Intent-to-treat denominator** (§2/§6): a suite's resolution is
  ``resolved ÷ attempted`` where ``attempted`` is EVERY initiated (task, repeat)
  attempt — crashes / over-budget / non-completion are RETAINED as unresolved,
  never dropped. A guard asserts the rolled-up denominator equals the sum of the
  member arm denominators, so the suite denominator can never silently shrink
  (the very §2 attrition bias).

HONESTY NOTE (read before reuse): a suite built here is a **methodology dry-run
on synthetic fixtures** — its resolution rate asserts NOTHING about real Foundry
performance. ``provenance`` defaults to :data:`Provenance.DRY_RUN`.

There is NO inferential statistics here (no power/bootstrap/alpha) — only a
deterministic resolution ROLL-UP. The powered A/B and decision table live in the
sibling M1.5 modules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.harness import ArmResult, itt_pass_rate, run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.harness import FoundryRunner
    from foundry.evaluation.task_fixtures import TaskFixture, TaskSlice

__all__ = [
    "OutcomeSuite",
    "SuiteKind",
    "SuiteResult",
    "TaskResolution",
    "run_suite",
]


class SuiteKind(StrEnum):
    """The §3 outcome-eval suite families.

    SYNTHETIC dry-run labels — they tag which §3 family a suite stands in for, NOT
    a claim that the fixtures are real tasks of that family.
    """

    GOLDEN = "golden"  # curated greenfield/feature/refactor/bugfix + hidden tests
    IN_HOUSE_NOVEL = "in_house_novel"  # post-cutoff headline suite (contamination-resistant)
    SWE_BENCH_STYLE = "swe_bench_style"  # public; relative baselining only
    SWE_EVO_STYLE = "swe_evo_style"  # long-horizon multi-commit; the §2 A/B arena


class OutcomeSuite(FrozenModel):
    """A NAMED group of oracle-scored tasks (evaluation.md §3).

    A suite is a *selection* over a frame: a stable ``name``, a :class:`SuiteKind`,
    and the ordered ``task_ids`` it scores. ``identity`` is content-addressed (L7
    ``content_digest``, never ``hash()``) over the kind + ordered ids so two equal
    suites hash identically and a membership change flips it — exactly like a
    :class:`~foundry.evaluation.task_fixtures.TaskSlice`, but carrying the §3
    suite SEMANTICS (kind + name) the bare slice does not.

    SYNTHETIC: ``synthetic`` defaults True (dry-run frame).
    """

    name: str
    kind: SuiteKind
    task_ids: tuple[str, ...]  # ordered, deterministic
    identity: str  # content_digest over (kind, name, task_ids) — L7
    synthetic: bool = True
    version: int = 1

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.task_ids

    def as_slice(self) -> TaskSlice:
        """View this suite as a harness :class:`TaskSlice` (so the harness can run it).

        The harness scores a ``TaskSlice``; a suite IS a slice plus §3 semantics.
        The slice ``identity`` mirrors the suite identity so the arm result binds
        back to this suite (EV7 frame-binding).
        """
        from foundry.evaluation.task_fixtures import TaskSlice  # deferred: import-cheap

        return TaskSlice(name=self.name, task_ids=self.task_ids, identity=self.identity)


def make_suite(
    name: str,
    kind: SuiteKind,
    task_ids: tuple[str, ...],
) -> OutcomeSuite:
    """Build an :class:`OutcomeSuite` with a content-addressed ``identity`` (L7).

    ``task_ids`` are taken in the given order (the caller fixes a deterministic
    order — e.g. ``tuple(sorted(...))``); the identity hashes the ordered ids with
    the kind + name so equal suites coalesce and a membership change flips it.
    """
    identity = content_digest({"kind": kind.value, "name": name, "task_ids": list(task_ids)})
    return OutcomeSuite(name=name, kind=kind, task_ids=task_ids, identity=identity)


class TaskResolution(FrozenModel):
    """Per-task resolution roll-up across an arm's repeats within a suite.

    A task may be attempted ``k_repeats`` times; this records how many of those
    attempts the ORACLE scored resolved (``resolved_repeats``) out of
    ``attempted_repeats`` (ITT — every initiated repeat counts). ``resolved`` is
    the per-task headline bit: did the oracle resolve this task on at least one
    initiated attempt (``resolved_repeats > 0``).
    """

    task_id: str
    attempted_repeats: int = Field(gt=0)  # ITT: all initiated repeats for this task
    resolved_repeats: int = Field(ge=0)  # oracle-scored resolved among them
    resolved: bool  # resolved on >= 1 initiated attempt

    @property
    def per_repeat_rate(self) -> float:
        """Oracle resolution rate ACROSS this task's repeats (ITT over repeats)."""
        return self.resolved_repeats / self.attempted_repeats


class SuiteResult(FrozenModel):
    """The outcome of running ONE arm over ONE :class:`OutcomeSuite` (evaluation.md §3).

    Reports the suite-level **resolution rate** (ITT — ``resolved ÷ attempted``,
    reusing the harness denominator verbatim) AND the per-task resolutions that
    rolled into it. Frozen + content-addressable.

    The ``arm_result`` it wraps is the full per-(task, repeat) harness result, so
    nothing is lost in the roll-up: the suite headline is a VIEW over the retained
    ITT outcomes, never a re-derivation that could drop a sample.

    HONESTY: ``provenance`` defaults to DRY_RUN; ``simulated`` mirrors the wrapped
    arm result (everything but FOUNDRY is a synthetic stand-in).
    """

    suite_name: str
    suite_kind: SuiteKind
    suite_identity: str
    arm: Arm
    attempted: int = Field(gt=0)  # ITT denominator: len(suite) * k_repeats
    resolved: int = Field(ge=0)  # oracle-resolved attempts (ITT numerator)
    task_resolutions: tuple[TaskResolution, ...]
    arm_result: ArmResult  # the full retained per-(task, repeat) ITT outcomes
    simulated: bool
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def resolution_rate(self) -> float:
        """Suite-level ITT resolution rate: ``resolved ÷ attempted`` (§2/§3).

        A guard asserts the denominator equals the wrapped arm's retained-outcome
        count, so the suite rate can NEVER be computed off a shrunk denominator
        (the §2 attrition bias). Tasks-attempted (per-task headline) resolution is
        :meth:`task_resolution_rate`.
        """
        assert self.attempted == len(self.arm_result.outcomes), (
            "ITT violation: suite denominator != retained outcomes (a sample was dropped)"
        )
        return self.resolved / self.attempted

    @property
    def task_resolution_rate(self) -> float:
        """Fraction of distinct TASKS resolved on >= 1 attempt (the §3 headline).

        Distinct from :meth:`resolution_rate` (which is over (task, repeat)
        attempts): this is over distinct tasks, the natural "how many tasks did we
        solve" reading of a suite.
        """
        if not self.task_resolutions:
            return 0.0
        return sum(1 for t in self.task_resolutions if t.resolved) / len(self.task_resolutions)


def run_suite(
    arm: Arm,
    suite: OutcomeSuite,
    fixtures: dict[str, TaskFixture],
    *,
    k_repeats: int = 1,
    seed: int = 0,
    budget_usd_microusd: int = 5_000_000,
    foundry_runner: FoundryRunner | None = None,
) -> SuiteResult:
    """Run ``arm`` over every task in ``suite`` and roll up its ORACLE resolution.

    Deterministic: identical inputs ⇒ identical :class:`SuiteResult` (the harness
    is deterministic and the per-task roll-up is order-stable — outcomes are
    grouped by ``task_id`` in the suite's declared order).

    ITT (evaluation.md §2/§6): the suite denominator is the harness denominator
    (``len(suite) * k_repeats`` — all initiated attempts). Crashed / over-budget /
    non-completing attempts are RETAINED as unresolved by the harness and counted
    here unconditionally; a guard re-asserts the denominator against the retained
    outcomes so a sample can never be dropped.

    Oracle-scored only (§3/§4): resolution is the harness oracle equality verdict,
    reused verbatim. No LLM judge is consulted.

    Parameters mirror :func:`foundry.evaluation.harness.run_arm`; ``foundry_runner``
    is forwarded only when provided (so the harness default applies otherwise).
    """
    run_kwargs: dict[str, object] = {
        "k_repeats": k_repeats,
        "seed": seed,
        "budget_usd_microusd": budget_usd_microusd,
    }
    if foundry_runner is not None:
        run_kwargs["foundry_runner"] = foundry_runner

    arm_result = run_arm(arm, suite.as_slice(), fixtures, **run_kwargs)  # type: ignore[arg-type]

    # Per-task roll-up over the RETAINED ITT outcomes (grouped by task_id in the
    # suite's declared order — order-stable, never dict-iteration-dependent).
    by_task: dict[str, list[bool]] = {tid: [] for tid in suite.task_ids}
    for outcome in arm_result.outcomes:
        by_task[outcome.task_id].append(outcome.resolved)

    task_resolutions: list[TaskResolution] = []
    for task_id in suite.task_ids:
        bits = by_task[task_id]
        resolved_repeats = sum(1 for b in bits if b)
        task_resolutions.append(
            TaskResolution(
                task_id=task_id,
                attempted_repeats=len(bits),
                resolved_repeats=resolved_repeats,
                resolved=resolved_repeats > 0,
            )
        )

    # Suite denominator IS the harness ITT denominator — re-asserted, never
    # recomputed off a filtered view (§2: no silent shrink).
    assert arm_result.attempted == len(arm_result.outcomes), (
        "ITT violation: harness denominator != retained outcomes"
    )

    return SuiteResult(
        suite_name=suite.name,
        suite_kind=suite.kind,
        suite_identity=suite.identity,
        arm=arm,
        attempted=arm_result.attempted,
        resolved=arm_result.resolved_count,
        task_resolutions=tuple(task_resolutions),
        arm_result=arm_result,
        simulated=arm_result.simulated,
    )


def suite_itt_pass_rate(result: SuiteResult) -> float:
    """Suite ITT pass rate via the harness primitive (re-exported for symmetry).

    Delegates to :func:`foundry.evaluation.harness.itt_pass_rate` on the wrapped
    arm result so the suite headline and the harness headline can NEVER disagree
    (one source of truth for the ITT denominator).
    """
    return itt_pass_rate(result.arm_result)
