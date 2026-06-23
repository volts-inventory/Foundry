"""Constrained workflow optimization — Pareto-improving ONLY (docs/evaluation.md §9).

The §9 optimization tunes the workflow configuration (the change-risk TIERING per
node, graph-topology toggles, retry/convergence caps, the role set itself) to
**maximize** novel-suite + SWE-EVO resolution **subject to NO regression** in
escaped defects, cost, wall-clock, or policy compliance. A candidate config is
PROMOTED only if it is a genuine **Pareto improvement** over the baseline; a
non-Pareto change is REJECTED. (docs/evaluation.md §9: "promote only
Pareto-improving configs".)

THE LOAD-BEARING RULE (read before reuse — §9):

* A candidate may be ADOPTED only if it improves >= 1 headline metric in its
  better-direction AND regresses NONE of the constrained metrics
  {resolution-rate, escaped-defects, cost, wall-clock, policy-compliance}. This
  is a *constrained* optimization: a win on resolution that BUYS the win with a
  cost / wall-clock / escaped-defect / policy-compliance REGRESSION is NOT a
  Pareto improvement and is REJECTED fail-closed (never adopted, never traded
  off). A TIE (no metric strictly improves) is also REJECTED — it is not a
  Pareto improvement (no progress on the objective).
* **Tiering is a VALIDATED OUTPUT of optimization, not an intuition** (§9: "tiering
  is an *output*, validated, not an intuition"). A proposed tiering change is fed
  through the SAME Pareto gate: it is adopted only if it Pareto-improves the
  headline scorecard, and rejected if it regresses ANY constrained metric — a
  retiering can never be adopted on the grounds that "the diff is small" if it
  costs a headline metric.

PER-METRIC DIRECTION (explicit — the optimization respects each metric's
better-direction, never a blanket "bigger is better"):

* resolution-rate          — HIGHER is better (the objective being maximized);
* policy-compliance        — HIGHER is better;
* escaped-defects          — LOWER is better (a constraint);
* cost (micro-USD)         — LOWER is better (a constraint);
* wall-clock (ms)          — LOWER is better (a constraint).

This module READS already-computed metrics; it NEVER recomputes the eval (the
scorecard / cost-model own those numbers — scorecard.py / cost_model.py). It is
PURE + DETERMINISTIC: the same ``(baseline, candidate)`` yields an IDENTICAL
:class:`OptimizationDecision` (no RNG, no I/O).

HONESTY NOTE (read before reuse): like the rest of the harness this is a
**methodology dry-run** — the input :class:`MetricVector`s are SYNTHETIC fixtures
and a DRY_RUN decision asserts NOTHING about a real config win. But the
**optimization LOGIC is real**: the Pareto-dominance test + the fail-closed
no-regression gate are exactly what would run against funded metrics. Money is
integer micro-USD and time is integer ms (the pinned global constraint — no float
drift on the constrained metrics); resolution / compliance are rates in [0, 1].
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from foundry.evaluation.hypothesis import Provenance
from foundry.schemas.base import FrozenModel
from foundry.schemas.governance import TierDecision

__all__ = [
    "MetricVector",
    "OptimizationDecision",
    "evaluate_optimization",
    "evaluate_tiering_change",
    "pareto_improves",
]


class MetricVector(FrozenModel):
    """The headline metric bundle the §9 optimization reads (per-metric direction).

    These are the SAME headline metrics the scorecard / cost-model own — the
    optimization READS them, it never recomputes the eval. Each metric carries an
    EXPLICIT better-direction (encoded by :meth:`_directions`), so the Pareto test
    respects every metric's own sense, never a blanket "bigger is better":

    * ``resolution_rate``    — HIGHER is better (the objective being maximized);
    * ``policy_compliance``  — HIGHER is better;
    * ``escaped_defects``    — LOWER is better (a constraint);
    * ``cost_microusd``      — LOWER is better (a constraint; integer micro-USD);
    * ``wall_clock_ms``      — LOWER is better (a constraint; integer ms).

    Money is integer micro-USD and time is integer ms (no float drift on the
    constrained metrics); the rates are floats in ``[0, 1]`` and the defect count
    is a non-negative integer.
    """

    resolution_rate: float = Field(ge=0.0, le=1.0)  # HIGHER better — the objective
    policy_compliance: float = Field(ge=0.0, le=1.0)  # HIGHER better — a constraint
    escaped_defects: int = Field(ge=0)  # LOWER better — a constraint
    cost_microusd: int = Field(ge=0)  # LOWER better — a constraint (integer micro-USD)
    wall_clock_ms: int = Field(ge=0)  # LOWER better — a constraint (integer ms)

    version: int = 1

    @staticmethod
    def _directions() -> tuple[tuple[str, bool], ...]:
        """The (metric-name, higher_is_better) table — the SINGLE source of direction.

        ``True`` = higher-is-better (resolution, policy-compliance); ``False`` =
        lower-is-better (escaped-defects, cost, wall-clock). Every Pareto / regression
        check iterates THIS table so a metric's direction is never hard-coded twice.
        """
        return (
            ("resolution_rate", True),
            ("policy_compliance", True),
            ("escaped_defects", False),
            ("cost_microusd", False),
            ("wall_clock_ms", False),
        )

    def _improves_on(self, other: MetricVector, metric: str, *, higher_is_better: bool) -> bool:
        """True iff ``self`` is STRICTLY better than ``other`` on ``metric``."""
        mine = getattr(self, metric)
        theirs = getattr(other, metric)
        return mine > theirs if higher_is_better else mine < theirs

    def _regresses_on(self, other: MetricVector, metric: str, *, higher_is_better: bool) -> bool:
        """True iff ``self`` is STRICTLY worse than ``other`` on ``metric`` (a regression)."""
        mine = getattr(self, metric)
        theirs = getattr(other, metric)
        return mine < theirs if higher_is_better else mine > theirs


def _regressions(baseline: MetricVector, candidate: MetricVector) -> tuple[str, ...]:
    """The constrained metrics on which ``candidate`` REGRESSES vs ``baseline``.

    Iterates the SINGLE direction table so each metric is judged in its own sense
    (escaped-defects / cost / wall-clock lower-is-better; resolution / compliance
    higher-is-better). Order is the table order (deterministic).
    """
    return tuple(
        name
        for name, higher in MetricVector._directions()
        if candidate._regresses_on(baseline, name, higher_is_better=higher)
    )


def _improvements(baseline: MetricVector, candidate: MetricVector) -> tuple[str, ...]:
    """The metrics on which ``candidate`` STRICTLY improves over ``baseline``."""
    return tuple(
        name
        for name, higher in MetricVector._directions()
        if candidate._improves_on(baseline, name, higher_is_better=higher)
    )


def pareto_improves(baseline: MetricVector, candidate: MetricVector) -> bool:
    """True iff ``candidate`` is a genuine Pareto improvement over ``baseline`` (§9).

    A Pareto improvement = ``candidate`` is >= ``baseline`` on EVERY metric in that
    metric's better-direction (NO regression on ANY constrained metric) AND strictly
    better on >= 1 metric. Concretely:

    * NO regression on any of {resolution-rate, escaped-defects, cost, wall-clock,
      policy-compliance} — a single constrained-metric regression disqualifies the
      candidate, even if it wins big on resolution (the §9 constraint is hard, not a
      trade-off); AND
    * STRICTLY better on >= 1 metric — a TIE (no strict improvement) is NOT a Pareto
      improvement (no progress on the objective).

    Pure / deterministic. This is the predicate :func:`evaluate_optimization` and
    :func:`evaluate_tiering_change` adopt on.
    """
    if _regressions(baseline, candidate):
        return False  # ANY constrained-metric regression disqualifies (fail-closed)
    return bool(_improvements(baseline, candidate))  # require >= 1 strict win (no tie)


class OptimizationDecision(FrozenModel):
    """The frozen ADOPT / REJECT verdict for one candidate config (§9).

    ``adopted`` is True iff the candidate is a genuine Pareto improvement
    (:func:`pareto_improves`). When REJECTED, ``regressed_metrics`` NAMES the
    constrained metric(s) that regressed (empty when the rejection was a TIE — no
    strict improvement and no regression), and ``improved_metrics`` records any
    strict wins (a rejected-on-regression candidate may still have "won" on
    resolution — recorded so the audit shows the win was correctly NOT bought with
    a regression). ``rationale`` states WHY in one line.

    HONESTY: ``provenance`` / ``dry_run`` are carried through; a DRY_RUN decision
    asserts NOTHING about a real config win — the LOGIC is real, the inputs synthetic.
    """

    adopted: bool
    improved_metrics: tuple[str, ...]  # metrics the candidate STRICTLY wins on
    regressed_metrics: tuple[str, ...]  # constrained metrics it REGRESSES on (empty if none)
    rationale: str
    kind: str = "config"  # "config" | "tiering" — what kind of candidate was judged

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1


def evaluate_optimization(
    baseline: MetricVector,
    candidate: MetricVector,
    *,
    kind: str = "config",
    provenance: Provenance = Provenance.DRY_RUN,
) -> OptimizationDecision:
    """Judge a candidate config against the baseline — ADOPT iff Pareto-improving (§9).

    The constrained-optimization gate, fail-closed:

    * ADOPT iff :func:`pareto_improves` — a strict win on >= 1 metric with NO
      regression on ANY of {resolution-rate, escaped-defects, cost, wall-clock,
      policy-compliance}.
    * Otherwise REJECT. Two rejection shapes, both NAMED:
        - a REGRESSION on >= 1 constrained metric → REJECTED with the regressed
          metric(s) named (even if it improved resolution — the win is NOT bought
          with a regression; the §9 constraint is hard);
        - a TIE (no strict improvement, no regression) → REJECTED as not a Pareto
          improvement (no progress on the objective).

    Pure / deterministic: same inputs → identical decision. READS the metrics; never
    recomputes the eval. ``provenance`` / ``dry_run`` are carried onto the verdict.
    """
    improved = _improvements(baseline, candidate)
    regressed = _regressions(baseline, candidate)
    adopted = not regressed and bool(improved)

    if adopted:
        rationale = (
            "ADOPT — genuine Pareto improvement (§9): strictly improves "
            f"{list(improved)} with NO regression on any constrained metric "
            "{resolution-rate, escaped-defects, cost, wall-clock, policy-compliance}."
        )
    elif regressed:
        rationale = (
            "REJECT — NOT Pareto-improving (§9, fail-closed): regresses constrained "
            f"metric(s) {list(regressed)}"
            + (
                f" despite improving {list(improved)} — a win is NEVER bought with a "
                "constrained-metric regression."
                if improved
                else " — no metric may regress."
            )
        )
    else:
        rationale = (
            "REJECT — NOT Pareto-improving (§9): a TIE (no metric strictly improves) "
            "makes no progress on the objective; only a strict Pareto win is adopted."
        )

    return OptimizationDecision(
        adopted=adopted,
        improved_metrics=improved,
        regressed_metrics=regressed,
        rationale=rationale,
        kind=kind,
        provenance=provenance,
        dry_run=provenance is Provenance.DRY_RUN,
    )


def evaluate_tiering_change(
    baseline: MetricVector,
    candidate: MetricVector,
    *,
    proposed_tiers: Sequence[TierDecision] = (),
    provenance: Provenance = Provenance.DRY_RUN,
) -> OptimizationDecision:
    """Treat a proposed TIERING change as a candidate optimization — Pareto-VALIDATE it (§9).

    **Tiering is a VALIDATED OUTPUT of optimization, not an intuition** (§9: "tiering
    is an *output*, validated, not an intuition"). A proposed retiering (the per-node
    change-risk tiers the optimization searched over) is adopted ONLY if its measured
    headline metrics are a genuine Pareto improvement over the baseline tiering — and
    REJECTED, with the regressed metric(s) named, if it regresses ANY constrained
    metric. A retiering can NEVER be adopted on "the diff is small" grounds if it
    costs a headline metric; conversely a tiering change that Pareto-improves IS
    adopted.

    This reuses the SAME :func:`evaluate_optimization` gate (``kind="tiering"``) so a
    tiering candidate is judged by exactly the same fail-closed Pareto rule as any
    other config candidate — no parallel, laxer path. ``proposed_tiers`` is accepted
    for audit symmetry (the tiers the candidate metrics were measured under); it is
    validated NOT to relax integrity (tiering scales depth/HITL only — governance.md
    §1e), so a Pareto-validated tiering can never smuggle in an integrity relaxation.
    """
    # Tiering NEVER relaxes integrity (governance.md §1e) — defense in depth: a
    # proposed tier that claimed to relax integrity could never be a legitimate
    # optimization output, so reject the whole change before the Pareto gate.
    for t in proposed_tiers:
        if t.relaxes_integrity():
            raise ValueError(
                "a proposed tiering change must NEVER relax integrity (governance.md "
                f"§1e) — tier {t.tier.value!r} reported relaxes_integrity() is True"
            )
    return evaluate_optimization(
        baseline,
        candidate,
        kind="tiering",
        provenance=provenance,
    )
