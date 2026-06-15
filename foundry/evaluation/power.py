"""Power calc + (n,k) constrained allocation for the Phase-1.5 decision gate.

This module is the **pure-statistics power calc** the §2 pre-registration freezes
*before* any scored run (docs/evaluation.md §2, "Pre-registered funded power rung
— BLOCKER" + "`n`/`k` allocation is a constrained optimization"). It CONSUMES the
read-only :class:`foundry.evaluation.pilot.PilotResult` (the strong-baseline
control base rate + the task-level variance components) together with the
program's **pre-committed** ``mde`` / ``alpha`` / ``target_power`` and the funded
equal-USD budget, and returns the required ``(n_tasks, k_repeats)`` plus whether
the budget can fund a run that reaches ``target_power`` at the committed ``mde``.

INTEGRITY (load-bearing — docs/evaluation.md §2 / §8):

* The rung is computed from **(pilot variance/base-rate + the PRE-COMMITTED
  mde/alpha/target_power) ONLY.** ``mde``/``alpha`` are inputs the *caller* froze
  in the preregistration; this module NEVER chooses or relaxes them.
* If the funded budget cannot reach ``target_power`` at the committed ``mde``,
  :func:`compute_power_rung` reports **UNDERPOWERED at the funded budget**
  (``is_funded(rung, budget) is False`` / ``rung.achieved_power < target_power``)
  — it does **not** silently shrink the MDE or inflate alpha to manufacture a
  "funded" verdict. A null at the funded MDE is a real KILL trigger (§1/§2.5), so
  there is **no "re-pick a smaller MDE after data" path** here: the returned rung
  is what the caller hashes into the manifest, frozen, before scored runs.

The statistics are **deterministic and contain NO model calls** — they read the
pilot summary and the committed parameters and apply a closed-form clustered
two-proportion (difference-in-means on per-task rates) power formula (see
:func:`compute_power_rung` for the formula + assumptions).

HONESTY: the pilot it consumes is a methodology dry-run on synthetic, simulated
arms (:mod:`foundry.evaluation.pilot`), so a rung computed here asserts NOTHING
about real Foundry-vs-baseline performance — it only exercises the sizing path
end-to-end on a stable, validated contract.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from foundry.evaluation.hypothesis import PowerRung

if TYPE_CHECKING:
    from foundry.evaluation.pilot import PilotResult

__all__ = [
    "DEFAULT_K_MAX",
    "FundedPowerRung",
    "compute_power_rung",
    "is_funded",
    "normal_cdf",
    "normal_ppf",
]

# The default cap on repeats/task. ``k`` only pays off while within-task (run-level)
# nondeterminism dominates the between-task variance (docs/evaluation.md §2); beyond
# that the budget is better spent on ``n``, so an unbounded ``k`` search is pointless.
DEFAULT_K_MAX = 16


def normal_ppf(p: float) -> float:
    """Inverse standard-normal CDF (the quantile/probit function).

    Acklam's rational approximation (abs error < ~1.15e-9 on the open interval),
    used for the ``z_{1-alpha/2}`` and ``z_{1-beta}`` critical values. Deterministic
    and dependency-free (no scipy) so the power calc stays import-cheap and pure.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"normal_ppf requires 0 < p < 1, got {p}")

    # Coefficients (Peter Acklam's algorithm).
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def normal_cdf(x: float) -> float:
    """Standard-normal CDF via :func:`math.erf` (deterministic, dependency-free)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class FundedPowerRung(PowerRung):
    """A :class:`PowerRung` extended with the calc's achieved power + funded verdict.

    Subclasses the frozen :class:`foundry.evaluation.hypothesis.PowerRung` (so it
    is the SAME contract the manifest hashes — ``mde``/``target_power``/``n_tasks``/
    ``k_repeats``/``funded_budget_microusd``) and ADDS the two read-only outputs the
    decision gate needs: the power actually achieved at ``(n_tasks, k_repeats)``
    under the funded budget, and the ``funded`` boolean.

    INTEGRITY: ``mde`` is the value the CALLER committed pre-run; this record never
    contains a *relaxed* MDE. When ``funded is False`` the rung reports UNDERPOWERED
    at the funded budget AT THE COMMITTED MDE — it is not a cue to descend to a
    cheaper rung (docs/evaluation.md §1/§2/§8).
    """

    alpha: float  # the pre-committed significance level the calc sized against
    required_n_tasks: int  # n the formula needs (UNCONSTRAINED by budget) at the chosen k
    achieved_power: float  # power at (n_tasks, k_repeats) under the funded budget
    funded: bool  # budget reaches target_power at the committed MDE/alpha?
    underpowered: bool  # mirror of (not funded): UNDERPOWERED at the funded budget


def _per_arm_task_variance(
    *, between_task_var: float, base_rate: float, k_repeats: int
) -> float:
    """Variance of a single arm's per-TASK resolution rate at ``k`` repeats/task.

    The per-task rate is the cluster-level statistic (the inferential unit is the
    TASK — docs/evaluation.md §2). Its variance has two components:

    * **between-task** ``sigma_b^2`` — the task-distribution dispersion the pilot
      measures (``control_resolution_variance``); does NOT shrink with ``k``; and
    * **within-task** ``sigma_w^2 / k`` — run-level Bernoulli nondeterminism that
      AVERAGES DOWN as repeats/task rise (``k`` averaging — §7). With a per-repeat
      Bernoulli resolution at rate ``p``, ``sigma_w^2 = p*(1-p)``.

    Returns ``sigma_b^2 + p*(1-p)/k``. This is why ``k`` only pays off while
    within-task variance dominates: once ``sigma_w^2/k`` is small vs ``sigma_b^2``,
    extra repeats barely cut the per-task variance and the budget is better spent
    on ``n`` (more tasks).
    """
    within = base_rate * (1.0 - base_rate)
    return between_task_var + within / k_repeats


def _required_n_per_arm(
    *, var_treatment: float, var_control: float, mde: float, alpha: float, target_power: float
) -> int:
    """Closed-form per-arm task count for a two-sample difference-in-means z-test.

    Detecting a margin ``mde`` between two arms' mean per-task rates, with the two
    arms' per-task variances ``var_treatment`` / ``var_control``, at two-sided
    ``alpha`` and power ``target_power``:

        n_per_arm = ceil( (z_{1-alpha/2} + z_{1-beta})^2 * (var_T + var_C) / mde^2 )

    (the standard unequal-variance two-sample formula; equal allocation per arm).
    ``z_{1-alpha/2}`` is the two-sided critical value, ``z_{1-beta}`` the power
    quantile. Monotone as expected: smaller ``mde`` -> larger ``n``; higher power
    (larger ``z_{1-beta}``) -> larger ``n``; larger variance -> larger ``n``.
    """
    z_alpha = normal_ppf(1.0 - alpha / 2.0)
    z_beta = normal_ppf(target_power)
    numerator = (z_alpha + z_beta) ** 2 * (var_treatment + var_control)
    return math.ceil(numerator / (mde * mde))


def _achieved_power(
    *, var_treatment: float, var_control: float, mde: float, alpha: float, n_per_arm: int
) -> float:
    """Power actually achieved at ``n_per_arm`` tasks/arm (inverse of the n formula).

    With per-arm per-task variances and ``n_per_arm`` clusters per arm, the
    standard error of the difference is ``sqrt((var_T + var_C) / n_per_arm)``, so

        power = Phi( mde / SE - z_{1-alpha/2} ).

    Used to report the power the FUNDED budget actually buys (which may be < the
    target when the budget caps ``n`` below ``required_n_per_arm``).
    """
    if n_per_arm <= 0:
        return 0.0
    z_alpha = normal_ppf(1.0 - alpha / 2.0)
    se = math.sqrt((var_treatment + var_control) / n_per_arm)
    if se == 0.0:
        return 1.0
    return normal_cdf(mde / se - z_alpha)


def is_funded(rung: PowerRung, budget_usd_microusd: int) -> bool:
    """Can ``budget_usd_microusd`` fund a run that reaches the rung's target power?

    A rung is FUNDED iff its frozen ``funded_budget_microusd`` (the equal-USD cost
    of running BOTH arms at the rung's ``(n_tasks, k_repeats)``) is <= the available
    budget. This is the EXPLICIT, side-effect-free funded check the §2 BLOCKER
    requires: it reads the rung and the budget and returns a bool — it NEVER mutates
    ``mde``/``alpha`` or re-picks a cheaper rung (docs/evaluation.md §1/§2/§8).

    A :class:`FundedPowerRung` already produced UNDERPOWERED (``funded is False``)
    has ``funded_budget_microusd`` set to the cost of the BUDGET-CAPPED allocation,
    which by construction is <= budget; the authoritative underpowered signal is
    then ``rung.achieved_power < rung.target_power`` (mirrored by ``.underpowered``).
    """
    cost = rung.funded_budget_microusd
    if cost is None:
        raise ValueError("rung has no funded_budget_microusd — cannot check fundedness")
    return cost <= budget_usd_microusd


def compute_power_rung(
    pilot: PilotResult,
    *,
    mde: float,
    alpha: float,
    target_power: float,
    budget_usd_microusd: int,
    per_task_repeat_cost_microusd: int,
    k_max: int = DEFAULT_K_MAX,
) -> FundedPowerRung:
    """Size the funded ``(n_tasks, k_repeats)`` rung off the pilot (deterministic).

    Formula + assumptions (docs/evaluation.md §2, "Power" + "`n`/`k` allocation"):

    * **Inferential unit = the TASK (cluster).** Each task contributes one per-task
      resolution rate (its ``k`` repeats collapsed); we power a **two-sample
      difference-in-means z-test** on the FOUNDRY vs BASELINE per-task rates with a
      pre-registered margin ``mde``. Treating ``n*k`` as independent draws would
      overstate effective N — we do NOT.
    * **Per-task variance has two components** (:func:`_per_arm_task_variance`):
      a between-task term (the pilot's ``control_resolution_variance``, which does
      NOT shrink with ``k``) plus a within-task Bernoulli term ``p(1-p)/k`` (run-
      level nondeterminism, which AVERAGES DOWN with ``k``). The control arm's base
      rate is the pilot's ``control_base_rate``; the treatment arm's base rate is
      taken as ``control_base_rate + mde`` (the H1 effect), clamped to ``[0,1]``.
      When the pilot exposes ``effect_size_variance`` (the between-task variance of
      the per-task FOUNDRY-CONTROL difference) we use the LARGER of it and the
      control's between-task variance as the treatment arm's between-task floor — a
      conservative choice (never under-powers vs the borrowed control dispersion).
    * **(n,k) is a constrained optimization, NOT two free numbers.** For each
      candidate ``k`` in ``[1, k_max]`` we compute the budget-unconstrained
      ``required_n_per_arm`` (:func:`_required_n_per_arm`) and the equal-USD cost
      of running BOTH arms (``2 * n * k * per_task_repeat_cost_microusd``). Among
      the candidates whose cost fits ``budget_usd_microusd`` AND reach
      ``target_power``, we pick the **cheapest** (min funded cost); this naturally
      prefers small ``k`` once within-task variance stops dominating. If NONE fit,
      we report UNDERPOWERED (below).

    UNDERPOWERED without shrinking MDE (load-bearing — §1/§2/§8): if no feasible
    ``(n,k)`` reaches ``target_power`` within budget, the returned rung keeps the
    **committed** ``mde``/``alpha`` unchanged, sets ``funded = False`` /
    ``underpowered = True``, and reports the ``achieved_power`` of the BEST
    budget-affordable allocation (the ``(n,k)`` that maximizes power within budget).
    We NEVER relax ``mde`` or ``alpha`` to fabricate a funded verdict, and there is
    no path that re-picks a smaller MDE after seeing data. The caller freezes the
    returned rung in the preregistration BEFORE scored runs.

    Parameters
    ----------
    pilot:
        Read-only :class:`PilotResult` (control base rate + variance components).
    mde, alpha, target_power:
        The program's PRE-COMMITTED minimum detectable effect, two-sided
        significance level, and target power. Inputs only — never chosen here.
    budget_usd_microusd:
        The funded equal-USD budget (integer micro-USD) the run must fit within.
    per_task_repeat_cost_microusd:
        Micro-USD cost of ONE task-repeat in ONE arm (the equal-USD per-attempt
        cost). Total run cost = ``2 * n_tasks * k_repeats * this`` (two arms).
    k_max:
        Cap on repeats/task searched (default :data:`DEFAULT_K_MAX`).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0,1), got {alpha}")
    if not 0.0 < target_power < 1.0:
        raise ValueError(f"target_power must be in (0,1), got {target_power}")
    if mde <= 0.0:
        raise ValueError(f"mde must be > 0, got {mde}")
    if k_max < 1:
        raise ValueError(f"k_max must be >= 1, got {k_max}")
    if per_task_repeat_cost_microusd <= 0:
        raise ValueError("per_task_repeat_cost_microusd must be > 0")

    control_rate = pilot.control_base_rate
    treatment_rate = min(1.0, max(0.0, control_rate + mde))

    control_between = pilot.control_resolution_variance
    # Conservative treatment-arm between-task floor: never assume the treatment is
    # LESS dispersed than the control or than the measured per-task-difference var.
    treatment_between = max(control_between, pilot.effect_size_variance)

    best_feasible: tuple[int, int, int, float, int] | None = None  # (cost, n, k, power, req_n)
    best_affordable: tuple[int, int, int, float, int] | None = None  # max power within budget

    for k in range(1, k_max + 1):
        var_c = _per_arm_task_variance(
            between_task_var=control_between, base_rate=control_rate, k_repeats=k
        )
        var_t = _per_arm_task_variance(
            between_task_var=treatment_between, base_rate=treatment_rate, k_repeats=k
        )
        req_n = _required_n_per_arm(
            var_treatment=var_t,
            var_control=var_c,
            mde=mde,
            alpha=alpha,
            target_power=target_power,
        )
        full_cost = 2 * req_n * k * per_task_repeat_cost_microusd

        # Candidate that reaches target_power within budget (the n the formula needs).
        if full_cost <= budget_usd_microusd:
            power_at_req = _achieved_power(
                var_treatment=var_t, var_control=var_c, mde=mde, alpha=alpha, n_per_arm=req_n
            )
            cand = (full_cost, req_n, k, power_at_req, req_n)
            if best_feasible is None or cand[0] < best_feasible[0]:
                best_feasible = cand

        # Track the BEST budget-affordable allocation (max n that fits) for the
        # UNDERPOWERED report — never relax MDE; just report what the budget buys.
        affordable_n = budget_usd_microusd // (2 * k * per_task_repeat_cost_microusd)
        if affordable_n >= 1:
            power_aff = _achieved_power(
                var_treatment=var_t,
                var_control=var_c,
                mde=mde,
                alpha=alpha,
                n_per_arm=affordable_n,
            )
            aff_cost = 2 * affordable_n * k * per_task_repeat_cost_microusd
            cand_aff = (aff_cost, affordable_n, k, power_aff, req_n)
            if best_affordable is None or cand_aff[3] > best_affordable[3]:
                best_affordable = cand_aff

    if best_feasible is not None:
        cost, n, k, power, req_n = best_feasible
        return FundedPowerRung(
            mde=mde,
            alpha=alpha,
            target_power=target_power,
            n_tasks=n,
            k_repeats=k,
            funded_budget_microusd=cost,
            required_n_tasks=req_n,
            achieved_power=power,
            funded=True,
            underpowered=False,
        )

    # UNDERPOWERED: no (n,k) reaches target_power within budget. Report the best the
    # budget can buy at the COMMITTED mde/alpha — do NOT shrink mde or inflate alpha.
    if best_affordable is None:
        # The budget cannot afford even a single task-repeat per arm at any k.
        # Report a degenerate underpowered rung (k=1, n=0-power) at committed params.
        var_c = _per_arm_task_variance(
            between_task_var=control_between, base_rate=control_rate, k_repeats=1
        )
        var_t = _per_arm_task_variance(
            between_task_var=treatment_between, base_rate=treatment_rate, k_repeats=1
        )
        req_n = _required_n_per_arm(
            var_treatment=var_t,
            var_control=var_c,
            mde=mde,
            alpha=alpha,
            target_power=target_power,
        )
        return FundedPowerRung(
            mde=mde,
            alpha=alpha,
            target_power=target_power,
            n_tasks=None,
            k_repeats=None,
            funded_budget_microusd=0,
            required_n_tasks=req_n,
            achieved_power=0.0,
            funded=False,
            underpowered=True,
        )

    cost, n, k, power, req_n = best_affordable
    return FundedPowerRung(
        mde=mde,
        alpha=alpha,
        target_power=target_power,
        n_tasks=n,
        k_repeats=k,
        funded_budget_microusd=cost,
        required_n_tasks=req_n,
        achieved_power=power,
        funded=False,
        underpowered=True,
    )
