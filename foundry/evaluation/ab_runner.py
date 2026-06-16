"""Powered 3-arm A/B runner over the scored slice (Phase-1.5 decision gate).

This module is the **confirmatory A/B runner** the Phase-1.5 decision table
consumes (docs/evaluation.md §2/§7). It runs the three pre-registered arms
(FOUNDRY / BASELINE_SINGLE_AGENT / SINGLE_AGENT_PLUS_GATES) over the SCORED slice
of the shared frame (:mod:`foundry.evaluation.task_fixtures`), via the
deterministic oracle-scored per-arm harness (:func:`foundry.evaluation.harness.run_arm`,
ITT pass-rate scored unconditionally), and produces the decision-ready
:class:`ABResult`:

* the **primary comparison** FOUNDRY vs BASELINE_SINGLE_AGENT — a **paired
  cluster bootstrap** that resamples *tasks* (the inferential unit / cluster —
  §2; treating ``n×k`` as independent draws overstates effective N), carrying
  each task's repeats together and pairing the two arms WITHIN a task, producing
  the point estimate of the per-task resolution-rate difference, a bootstrap CI
  at the pre-registered alpha, a ``significant_at_alpha`` bit, and the observed
  effect vs the committed MDE;
* the **secondary family** (FOUNDRY vs SINGLE_AGENT_PLUS_GATES, and an
  efficiency/cost comparison) with **Holm–Bonferroni** FWER control over the
  family (adjusted significance per comparison).

INTEGRITY (load-bearing — docs/evaluation.md §2/§8):

* **A scored run MUST CITE AN UNMODIFIED FROZEN PRE-REGISTRATION.** The FIRST
  thing :func:`run_ab` does is :func:`foundry.evaluation.preregistration.assert_unmodified`
  on the cited manifest — a tampered/edited manifest (mde/alpha/rung drift) fails
  closed (:class:`~foundry.evaluation.preregistration.TamperedPreRegistrationError`).
  The funded rung's ``(n_tasks, k_repeats)`` and the committed ``alpha``/``mde``
  come from the manifest, never from the call site.
* **Scored slice ONLY, disjoint from the pilot.** The run is over
  ``frame.scored_slice`` and asserts it is disjoint from ``frame.pilot_slice``
  (raises :class:`ScoredSliceContaminationError` otherwise) — the pilot's
  estimation never leaks into the confirmatory run. The scored slice's identity
  MUST match the manifest's ``scored_eval_set_identity`` (the manifest binds the
  run to a specific frame/partition — EV7).
* **ITT, unconditional.** The per-arm pass rate is :func:`itt_pass_rate`
  (resolved / all-initiated; non-completion is unresolved, never dropped — §2);
  the denominator is NEVER conditioned on completion (the collider bias §2/§6
  forbid).

DETERMINISM: the bootstrap RNG is seeded, so the same ``(frame, prereg, seed)``
yields an IDENTICAL :class:`ABResult` (same CI, same significance bits).

HONESTY NOTE (read before reuse): this is a **methodology DRY-RUN on synthetic
fixtures**. The BASELINE / +gates arms are SIMULATED stand-ins (no real strong
single agent exists in Phase-1.5 — see :mod:`foundry.evaluation.harness`), so an
:class:`ABResult` asserts NOTHING about real Foundry-vs-baseline performance — it
only exercises the full powered-A/B methodology end-to-end. ``provenance`` is
carried through so a consumer can tell a dry-run rehearsal from a funded run.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.harness import ArmResult, itt_pass_rate, run_arm
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.preregistration import assert_unmodified
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.preregistration import PreRegistration
    from foundry.evaluation.task_fixtures import TaskFixture, TaskFrame

__all__ = [
    "AB_ARMS",
    "ABResult",
    "ArmReport",
    "BootstrapComparison",
    "ScoredSliceContaminationError",
    "SecondaryComparison",
    "run_ab",
]

# The three pre-registered arms (docs/evaluation.md §2), in a fixed order so the
# result is deterministic.
AB_ARMS: tuple[Arm, ...] = (
    Arm.FOUNDRY,
    Arm.BASELINE_SINGLE_AGENT,
    Arm.SINGLE_AGENT_PLUS_GATES,
)

# Number of bootstrap resamples for the paired cluster bootstrap. Fixed (not a
# free knob) so the CI is reproducible from the seed alone; large enough that the
# empirical quantiles are stable for the dry-run.
_BOOTSTRAP_RESAMPLES = 2000


class ScoredSliceContaminationError(RuntimeError):
    """Raised iff the scored slice is not disjoint from the pilot slice, or does
    not match the manifest's bound ``scored_eval_set_identity`` (integrity §2).

    The confirmatory A/B must run on the SCORED slice ONLY — disjoint from the
    pilot (so pilot estimation cannot leak in) and bound to the manifest's frame.
    Fail closed.
    """


class ArmReport(FrozenModel):
    """The per-arm summary on the scored slice (ITT pass-rate + equal-USD cost).

    ``itt_pass_rate`` is the UNCONDITIONAL ITT pass rate (resolved / all-initiated
    — §2; non-completion is unresolved, never dropped). ``total_cost_microusd`` is
    the equal-USD accounting figure for the equal-budget check.
    """

    arm: Arm
    itt_pass_rate: float = Field(ge=0.0, le=1.0)
    resolved_count: int = Field(ge=0)
    attempted: int = Field(gt=0)  # ITT denominator (UNCONDITIONAL) = n_tasks * k_repeats
    total_cost_microusd: int = Field(ge=0)
    simulated: bool  # True for the synthetic stand-in arms (BASELINE / +gates)


class BootstrapComparison(FrozenModel):
    """The PRIMARY paired-cluster-bootstrap comparison (FOUNDRY vs BASELINE).

    The point estimate is the mean per-task resolution-rate DIFFERENCE
    (FOUNDRY − BASELINE) over the scored tasks (the TASK is the inferential unit —
    §2). The CI is a paired cluster bootstrap that resamples TASKS with
    replacement (carrying each task's repeats + pairing the two arms within a
    task), at the pre-registered two-sided ``alpha``. ``significant_at_alpha`` is
    True iff the CI excludes 0; ``meets_mde`` iff the observed effect >= the
    committed ``mde`` (the pre-registered exit margin).
    """

    treatment_arm: Arm
    control_arm: Arm
    point_estimate: float  # mean per-task (treatment − control) resolution-rate diff
    ci_low: float
    ci_high: float
    alpha: float = Field(gt=0.0, lt=1.0)
    mde: float = Field(gt=0.0)
    n_tasks: int = Field(gt=0)  # the cluster count resampled (the inferential unit)
    resamples: int = Field(gt=0)
    significant_at_alpha: bool  # CI excludes 0 at alpha
    meets_mde: bool  # observed effect >= committed MDE


class SecondaryComparison(FrozenModel):
    """One secondary comparison, with its Holm–Bonferroni-adjusted significance.

    ``raw_p`` is the comparison's own (paired-cluster-bootstrap) two-sided p-value;
    ``holm_adjusted_p`` is the step-down-adjusted p; ``significant`` is the
    family-wise (FWER-controlled) decision at the pre-registered ``alpha``.
    """

    name: str
    treatment_arm: Arm
    control_arm: Arm
    point_estimate: float
    raw_p: float = Field(ge=0.0, le=1.0)
    holm_adjusted_p: float = Field(ge=0.0, le=1.0)
    significant: bool  # FWER-controlled (Holm) decision at alpha


class ABResult(FrozenModel):
    """The decision-ready result of the powered 3-arm A/B (docs/evaluation.md §2/§7).

    Carries the per-arm ITT summary (UNCONDITIONAL denominator + equal-USD cost),
    the PRIMARY paired-cluster-bootstrap comparison (FOUNDRY vs BASELINE), and the
    Holm–Bonferroni-corrected SECONDARY family. The decision table (a later M1.5
    task) consumes THIS object.

    INTEGRITY: bound to the cited pre-registration via ``registration_identity``
    (re-derived + asserted unmodified by :func:`run_ab`) and to the scored slice
    via ``scored_eval_set_identity``.

    HONESTY: ``provenance`` is carried through; the simulated baseline arms mean a
    dry-run :class:`ABResult` asserts NOTHING about real performance (module
    docstring). ``dry_run`` mirrors ``provenance != SCORED``.
    """

    registration_identity: str  # the cited, re-derived manifest identity (EV14)
    scored_eval_set_identity: str  # the scored slice this ran over (EV7)
    seed: int = Field(ge=0)
    k_repeats: int = Field(gt=0)
    n_tasks: int = Field(gt=0)

    arm_reports: tuple[ArmReport, ...]
    primary: BootstrapComparison
    secondaries: tuple[SecondaryComparison, ...]

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def report(self, arm: Arm) -> ArmReport:
        """Look up the :class:`ArmReport` for ``arm`` (raises ``KeyError``)."""
        for r in self.arm_reports:
            if r.arm is arm:
                return r
        raise KeyError(arm)


# ---------------------------------------------------------------------------
# Per-task statistics (the TASK is the inferential unit — §2)
# ---------------------------------------------------------------------------


def _per_task_rates(result: ArmResult) -> dict[str, float]:
    """Collapse each task's ``k_repeats`` to ONE per-task resolution rate.

    The TASK is the cluster / inferential unit (§2) — we collapse repeats BEFORE
    any inference so ``n×k`` is not mistaken for independent draws.
    """
    resolved: dict[str, int] = {}
    total: dict[str, int] = {}
    for o in result.outcomes:
        resolved[o.task_id] = resolved.get(o.task_id, 0) + (1 if o.resolved else 0)
        total[o.task_id] = total.get(o.task_id, 0) + 1
    return {tid: resolved[tid] / total[tid] for tid in total}


def _paired_diffs(treatment: ArmResult, control: ArmResult) -> list[float]:
    """Per-task (treatment − control) resolution-rate differences (paired by task).

    Ordered by task_id (sorted) so the vector — and every downstream bootstrap
    draw seeded off its indices — is independent of outcome iteration order.
    """
    t_rates = _per_task_rates(treatment)
    c_rates = _per_task_rates(control)
    common = sorted(set(t_rates) & set(c_rates))
    return [t_rates[tid] - c_rates[tid] for tid in common]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _bootstrap_diff_means(diffs: list[float], *, resamples: int, rng: random.Random) -> list[float]:
    """Paired cluster bootstrap: resample TASKS (clusters) with replacement.

    Each resample draws ``len(diffs)`` task-level paired differences with
    replacement (the cluster IS the task; pairing the two arms within a task is
    already baked into ``diffs``), then takes the mean. Returns the sorted
    bootstrap distribution of the mean per-task difference. Resampling the task —
    not the run — is what makes the TASK the inferential unit (§2).
    """
    n = len(diffs)
    means: list[float] = []
    for _ in range(resamples):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(_mean(sample))
    means.sort()
    return means


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (0<=q<=1) of an ALREADY-SORTED list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _bootstrap_two_sided_p(boot_means: list[float]) -> float:
    """Two-sided bootstrap p-value for H0: mean per-task difference == 0.

    The p-value is twice the smaller tail mass on the WRONG side of 0 (the
    fraction of bootstrap means at/below 0 vs at/above 0), clamped to [0, 1]. A
    distribution entirely on one side of 0 yields p ~= 0 (strong evidence of a
    non-zero effect); one centered on 0 yields p ~= 1.
    """
    n = len(boot_means)
    if n == 0:
        return 1.0
    below = sum(1 for m in boot_means if m <= 0.0) / n
    above = sum(1 for m in boot_means if m >= 0.0) / n
    return min(1.0, 2.0 * min(below, above))


def _holm_bonferroni(raw_ps: list[float], *, alpha: float) -> tuple[list[float], list[bool]]:
    """Holm–Bonferroni step-down over a family of raw p-values.

    Sorts the p-values ascending; the i-th smallest (0-based) is compared to
    ``alpha / (m - i)`` and rejected iff it AND all smaller ones clear their
    thresholds (step-down: the first acceptance stops all further rejections).
    Returns (adjusted_p, significant) in the ORIGINAL order. The adjusted p is the
    monotone (cumulative-max) ``(m - i) * p`` capped at 1 — so adjusted p is
    non-decreasing in rank, the standard Holm reporting form. FWER-controlling
    (conservative on positively-correlated endpoints — §2).
    """
    m = len(raw_ps)
    if m == 0:
        return [], []
    order = sorted(range(m), key=lambda i: raw_ps[i])
    adjusted_sorted: list[float] = []
    running_max = 0.0
    for rank, idx in enumerate(order):
        adj = min(1.0, (m - rank) * raw_ps[idx])
        running_max = max(running_max, adj)  # enforce monotone non-decreasing adj-p
        adjusted_sorted.append(running_max)

    adjusted = [0.0] * m
    significant = [False] * m
    # Step-down rejection: reject the run of smallest p's until the first failure.
    still_rejecting = True
    for rank, idx in enumerate(order):
        adjusted[idx] = adjusted_sorted[rank]
        if still_rejecting and raw_ps[idx] <= alpha / (m - rank):
            significant[idx] = True
        else:
            still_rejecting = False
            significant[idx] = False
    return adjusted, significant


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _seeded_rng(seed: int, *labels: str) -> random.Random:
    """A process-stable seeded RNG keyed on ``(seed, *labels)``.

    Seeds off a ``content_digest`` (L7 — never Python's ``hash()``, which is
    seed-randomized per process for strings and would break cross-process
    bootstrap determinism) so distinct comparisons get independent draws yet the
    whole result is reproducible from ``seed`` alone.
    """
    digest = content_digest({"seed": seed, "labels": list(labels)})
    return random.Random(int(digest[:16], 16))  # bootstrap resampling, not crypto


def run_ab(frame: TaskFrame, prereg: PreRegistration, *, seed: int = 0) -> ABResult:
    """Run the powered 3-arm A/B over ``frame.scored_slice`` against a cited manifest.

    Steps (in order — the integrity gate is FIRST):

    1. :func:`assert_unmodified` the cited ``prereg`` — a tampered/edited manifest
       fails closed BEFORE any arm runs (a scored verdict MUST cite an unmodified
       frozen pre-registration; §2/§8). The funded rung's ``(n_tasks, k_repeats)``
       and the committed ``alpha``/``mde`` are read from the manifest.
    2. Assert the SCORED slice is disjoint from the pilot slice AND that its
       identity matches the manifest's ``scored_eval_set_identity`` (raises
       :class:`ScoredSliceContaminationError` otherwise) — scored-slice-only,
       bound to the manifest's frame.
    3. Run all three arms (:data:`AB_ARMS`) over the scored slice via
       :func:`run_arm`, honoring the funded rung's ``k_repeats``; record each
       arm's UNCONDITIONAL ITT pass-rate + equal-USD cost + per-task outcomes.
    4. PRIMARY: a paired cluster bootstrap (resampling TASKS) of the per-task
       FOUNDRY − BASELINE resolution-rate difference -> point estimate + CI at
       ``alpha`` + ``significant_at_alpha`` + ``meets_mde``.
    5. SECONDARIES: FOUNDRY vs SINGLE_AGENT_PLUS_GATES (resolution) and an
       efficiency/cost comparison, with Holm–Bonferroni FWER control over the
       family -> adjusted significance.

    Deterministic in ``seed`` (the bootstrap RNG is seeded). HONESTY: a dry-run
    ``ABResult`` (simulated baseline arms) asserts NOTHING about real performance.
    """
    # GUARD 1 (FIRST): a scored run MUST cite an unmodified frozen pre-registration.
    registration_identity = assert_unmodified(prereg)

    funded = prereg.funded_rung
    if funded.n_tasks is None or funded.k_repeats is None:
        raise ScoredSliceContaminationError(
            "the cited pre-registration has no concrete (n_tasks, k_repeats) allocation; "
            "it is not a fundable scored run (evaluation.md §2)"
        )
    alpha = prereg.alpha
    mde = prereg.mde
    k_repeats = funded.k_repeats

    scored = frame.scored_slice
    pilot = frame.pilot_slice

    # GUARD 2: scored-slice-only — disjoint from the pilot, bound to the manifest.
    overlap = set(scored.task_ids) & set(pilot.task_ids)
    if overlap:
        raise ScoredSliceContaminationError(
            f"scored slice overlaps the pilot slice on {sorted(overlap)} — the confirmatory "
            "A/B must run on tasks DISJOINT from the pilot (evaluation.md §2)"
        )
    if scored.identity != prereg.scored_eval_set_identity:
        raise ScoredSliceContaminationError(
            "scored slice identity does not match the cited pre-registration's bound "
            f"scored_eval_set_identity ({scored.identity!r} != "
            f"{prereg.scored_eval_set_identity!r}) — the run is not bound to the registered "
            "frame/partition (evaluation.md §2/§8; EV7)"
        )
    if not scored.task_ids:
        raise ScoredSliceContaminationError("the scored slice is empty — nothing to run")

    # The fixtures the harness indexes into — RESTRICTED to the scored slice so a
    # stray lookup can never touch a pilot task.
    scored_ids = set(scored.task_ids)
    scored_fixtures: dict[str, TaskFixture] = {
        f.task_id: f for f in frame.fixtures if f.task_id in scored_ids
    }

    # Step 3: run all three arms over the SCORED slice (ITT, unconditional).
    results: dict[Arm, ArmResult] = {}
    arm_reports: list[ArmReport] = []
    for arm in AB_ARMS:
        result = run_arm(
            arm,
            scored,
            scored_fixtures,
            k_repeats=k_repeats,
            seed=seed,
            budget_usd_microusd=prereg.funded_rung.funded_budget_microusd or 5_000_000,
        )
        results[arm] = result
        arm_reports.append(
            ArmReport(
                arm=arm,
                itt_pass_rate=itt_pass_rate(result),  # UNCONDITIONAL denominator (§2)
                resolved_count=result.resolved_count,
                attempted=result.attempted,
                total_cost_microusd=result.total_cost_microusd,
                simulated=result.simulated,
            )
        )

    foundry = results[Arm.FOUNDRY]
    baseline = results[Arm.BASELINE_SINGLE_AGENT]
    plus_gates = results[Arm.SINGLE_AGENT_PLUS_GATES]

    # Step 4: PRIMARY — paired cluster bootstrap (resample TASKS) FOUNDRY vs BASELINE.
    primary_diffs = _paired_diffs(foundry, baseline)
    n_clusters = len(primary_diffs)
    point = _mean(primary_diffs)
    # Seed the RNG off (seed, "primary") so distinct comparisons don't share draws
    # yet the whole result is reproducible from `seed` alone.
    rng_primary = _seeded_rng(seed, "primary")
    boot = _bootstrap_diff_means(primary_diffs, resamples=_BOOTSTRAP_RESAMPLES, rng=rng_primary)
    ci_low = _percentile(boot, alpha / 2.0)
    ci_high = _percentile(boot, 1.0 - alpha / 2.0)
    primary = BootstrapComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=point,
        ci_low=ci_low,
        ci_high=ci_high,
        alpha=alpha,
        mde=mde,
        n_tasks=n_clusters,
        resamples=_BOOTSTRAP_RESAMPLES,
        significant_at_alpha=(ci_low > 0.0 or ci_high < 0.0),  # CI excludes 0
        meets_mde=point >= mde,
    )

    # Step 5: SECONDARY family + Holm–Bonferroni FWER control.
    # (a) FOUNDRY vs SINGLE_AGENT_PLUS_GATES resolution-rate difference.
    sec_specs: list[tuple[str, Arm, Arm, list[float]]] = [
        (
            "foundry_vs_plus_gates_resolution",
            Arm.FOUNDRY,
            Arm.SINGLE_AGENT_PLUS_GATES,
            _paired_diffs(foundry, plus_gates),
        ),
        # (b) Cost/efficiency: per-task resolutions-per-USD difference (FOUNDRY vs
        # BASELINE) — a decision-grade efficiency secondary. Resolved-per-mega-USD
        # so the magnitudes are O(1) for a stable bootstrap.
        (
            "foundry_vs_baseline_efficiency",
            Arm.FOUNDRY,
            Arm.BASELINE_SINGLE_AGENT,
            _efficiency_diffs(foundry, baseline),
        ),
    ]

    raw_ps: list[float] = []
    points: list[float] = []
    for i, (name, _t, _c, diffs) in enumerate(sec_specs):
        rng = _seeded_rng(seed, "secondary", name, str(i))
        boot_s = _bootstrap_diff_means(diffs, resamples=_BOOTSTRAP_RESAMPLES, rng=rng)
        raw_ps.append(_bootstrap_two_sided_p(boot_s))
        points.append(_mean(diffs))

    adjusted_ps, sig = _holm_bonferroni(raw_ps, alpha=alpha)
    secondaries = tuple(
        SecondaryComparison(
            name=name,
            treatment_arm=t,
            control_arm=c,
            point_estimate=points[i],
            raw_p=raw_ps[i],
            holm_adjusted_p=adjusted_ps[i],
            significant=sig[i],
        )
        for i, (name, t, c, _diffs) in enumerate(sec_specs)
    )

    provenance = prereg.hypothesis.provenance
    return ABResult(
        registration_identity=registration_identity,
        scored_eval_set_identity=scored.identity,
        seed=seed,
        k_repeats=k_repeats,
        n_tasks=len(scored.task_ids),
        arm_reports=tuple(arm_reports),
        primary=primary,
        secondaries=secondaries,
        provenance=provenance,
        dry_run=provenance is not Provenance.SCORED,
    )


def _efficiency_diffs(treatment: ArmResult, control: ArmResult) -> list[float]:
    """Per-task resolutions-per-mega-USD difference (treatment − control), paired.

    A decision-grade efficiency/cost secondary: each arm's per-task resolution
    count is normalized by the USD it spent on that task (resolved per 1e6 micro-
    USD = per USD), then paired by task. The TASK stays the inferential unit (§2).
    """
    t_eff = _per_task_efficiency(treatment)
    c_eff = _per_task_efficiency(control)
    common = sorted(set(t_eff) & set(c_eff))
    return [t_eff[tid] - c_eff[tid] for tid in common]


def _per_task_efficiency(result: ArmResult) -> dict[str, float]:
    """Per-task resolutions per USD (resolved count / task USD spend), keyed by task."""
    resolved: dict[str, int] = {}
    cost: dict[str, int] = {}
    for o in result.outcomes:
        resolved[o.task_id] = resolved.get(o.task_id, 0) + (1 if o.resolved else 0)
        cost[o.task_id] = cost.get(o.task_id, 0) + o.cost_microusd
    out: dict[str, float] = {}
    for tid, spend in cost.items():
        # resolutions per USD (1e6 micro-USD); zero-cost task -> zero efficiency.
        out[tid] = (resolved[tid] / (spend / 1_000_000)) if spend > 0 else 0.0
    return out
