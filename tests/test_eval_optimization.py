"""Conformance tests for the §9 constrained workflow optimization (T-M6-08).

Contract (docs/evaluation.md §9; the optimization READS already-computed headline
metrics — it never recomputes the eval — and promotes ONLY Pareto-improving configs):

* ``pareto_improves(baseline, candidate)`` is True iff the candidate is >= baseline
  on EVERY metric in that metric's better-direction (NO regression on any constrained
  metric {resolution-rate, escaped-defects, cost, wall-clock, policy-compliance}) AND
  strictly > on >= 1 metric.
* ``evaluate_optimization`` ADOPTS iff Pareto-improving; otherwise REJECTS:
    - a candidate better-on-resolution-but-worse-on-cost → REJECT (cost named);
    - a candidate worse on escaped-defects → REJECT (escaped-defects named);
    - a TIE (no strict improvement) → REJECT (no progress on the objective);
  i.e. a win is NEVER bought with a constrained-metric regression (fail-closed).
* Per-metric DIRECTION is respected: escaped-defects / cost / wall-clock are
  lower-is-better; resolution-rate / policy-compliance are higher-is-better.
* Tiering-as-validated-output: a proposed tiering change is Pareto-VALIDATED through
  the SAME gate — a tiering change that Pareto-improves is adopted; one that regresses
  a constrained metric is rejected.
* Determinism (same inputs → identical decision) + non-vacuity (adopt vs reject flip
  on a single metric).

METHODOLOGY DRY-RUN on synthetic fixtures — the LOGIC is real; the inputs synthetic.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.hypothesis import Provenance
from foundry.evaluation.optimization import (
    MetricVector,
    OptimizationDecision,
    evaluate_optimization,
    evaluate_tiering_change,
    pareto_improves,
)
from foundry.governance.tiering import classify_tier
from foundry.schemas.governance import TierDecision
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=0, node_id="triage", local=0)


def _baseline() -> MetricVector:
    """A baseline headline metric vector (synthetic fixture)."""
    return MetricVector(
        resolution_rate=0.50,
        policy_compliance=0.90,
        escaped_defects=10,
        cost_microusd=1_000_000,
        wall_clock_ms=60_000,
    )


# --------------------------------------------------------------------------- #
# pareto_improves — the core predicate (direction-aware, no-regression).
# --------------------------------------------------------------------------- #
def test_genuine_pareto_improvement_is_adopted() -> None:
    """Better on resolution, identical elsewhere → a genuine Pareto improvement."""
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.60})  # one strict win, no regression
    assert pareto_improves(base, cand) is True

    decision = evaluate_optimization(base, cand)
    assert isinstance(decision, OptimizationDecision)
    assert decision.adopted is True
    assert "resolution_rate" in decision.improved_metrics
    assert decision.regressed_metrics == ()
    assert decision.kind == "config"
    assert "ADOPT" in decision.rationale


def test_multi_metric_pareto_improvement_is_adopted() -> None:
    """Strictly better on several metrics (each in its own direction), no regression."""
    base = _baseline()
    cand = base.model_copy(
        update={
            "resolution_rate": 0.55,  # higher better — win
            "escaped_defects": 8,  # lower better — win
            "cost_microusd": 900_000,  # lower better — win
        }
    )
    assert pareto_improves(base, cand) is True
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is True
    assert set(decision.improved_metrics) == {
        "resolution_rate",
        "escaped_defects",
        "cost_microusd",
    }
    assert decision.regressed_metrics == ()


# --------------------------------------------------------------------------- #
# Fail-closed rejection — a win bought with a regression is NOT Pareto.
# --------------------------------------------------------------------------- #
def test_better_resolution_worse_cost_is_rejected_cost_named() -> None:
    """Improves resolution but REGRESSES cost → REJECT, cost named (fail-closed)."""
    base = _baseline()
    cand = base.model_copy(
        update={"resolution_rate": 0.70, "cost_microusd": 1_500_000}  # cost UP = regression
    )
    assert pareto_improves(base, cand) is False

    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert "cost_microusd" in decision.regressed_metrics
    # The resolution win is recorded but did NOT buy adoption.
    assert "resolution_rate" in decision.improved_metrics
    assert "REJECT" in decision.rationale
    assert "cost_microusd" in decision.rationale


def test_better_resolution_worse_wall_clock_is_rejected() -> None:
    """Improves resolution but REGRESSES wall-clock → REJECT (wall-clock named)."""
    base = _baseline()
    cand = base.model_copy(
        update={"resolution_rate": 0.70, "wall_clock_ms": 90_000}  # slower = regression
    )
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert "wall_clock_ms" in decision.regressed_metrics


def test_worse_escaped_defects_is_rejected() -> None:
    """More escaped defects → REJECT even with a resolution win (defects named)."""
    base = _baseline()
    cand = base.model_copy(
        update={"resolution_rate": 0.65, "escaped_defects": 15}  # MORE defects = regression
    )
    assert pareto_improves(base, cand) is False
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert "escaped_defects" in decision.regressed_metrics


def test_worse_policy_compliance_is_rejected() -> None:
    """Lower policy-compliance (higher-is-better) → REJECT (compliance named)."""
    base = _baseline()
    cand = base.model_copy(
        update={"resolution_rate": 0.65, "policy_compliance": 0.80}  # LOWER = regression
    )
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert "policy_compliance" in decision.regressed_metrics


def test_tie_is_rejected_not_pareto() -> None:
    """A TIE (identical metrics, no strict improvement) → REJECT (no progress)."""
    base = _baseline()
    cand = base.model_copy()  # identical → no regression, no strict win
    assert pareto_improves(base, cand) is False

    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert decision.regressed_metrics == ()  # no regression — it is a TIE
    assert decision.improved_metrics == ()
    assert "TIE" in decision.rationale


# --------------------------------------------------------------------------- #
# Per-metric DIRECTION is respected.
# --------------------------------------------------------------------------- #
def test_lower_is_better_metrics_improve_when_smaller() -> None:
    """escaped-defects / cost / wall-clock: SMALLER is the improvement direction."""
    base = _baseline()
    cand = base.model_copy(
        update={"escaped_defects": 5, "cost_microusd": 500_000, "wall_clock_ms": 30_000}
    )
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is True
    assert set(decision.improved_metrics) == {
        "escaped_defects",
        "cost_microusd",
        "wall_clock_ms",
    }


def test_higher_is_better_metrics_regress_when_smaller() -> None:
    """resolution / policy-compliance: SMALLER is the regression direction."""
    base = _baseline()
    # Only resolution drops; nothing else changes → a pure regression, no win.
    cand = base.model_copy(update={"resolution_rate": 0.40})
    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert decision.regressed_metrics == ("resolution_rate",)
    assert decision.improved_metrics == ()


# --------------------------------------------------------------------------- #
# Tiering as a VALIDATED OUTPUT of optimization (§9).
# --------------------------------------------------------------------------- #
def test_tiering_change_that_pareto_improves_is_adopted() -> None:
    """A proposed tiering change whose metrics Pareto-improve → ADOPT (kind=tiering)."""
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.58, "cost_microusd": 950_000})
    tier: TierDecision = classify_tier("rename a variable", seq=SEQ)  # a real LOW-tier output

    decision = evaluate_tiering_change(base, cand, proposed_tiers=[tier])
    assert decision.adopted is True
    assert decision.kind == "tiering"
    assert decision.regressed_metrics == ()


def test_tiering_change_that_regresses_a_metric_is_rejected() -> None:
    """A tiering change that regresses a constrained metric → REJECT (named)."""
    base = _baseline()
    # Faster wall-clock but MORE escaped defects — a retiering can't be adopted on
    # "the diff is small" grounds if it costs a headline metric (§9).
    cand = base.model_copy(update={"wall_clock_ms": 30_000, "escaped_defects": 20})
    tier = classify_tier("rename a variable", seq=SEQ)

    decision = evaluate_tiering_change(base, cand, proposed_tiers=[tier])
    assert decision.adopted is False
    assert "escaped_defects" in decision.regressed_metrics


def test_tiering_uses_the_same_pareto_gate_as_config() -> None:
    """Tiering is judged by EXACTLY the same fail-closed gate (only kind differs)."""
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.62})
    as_config = evaluate_optimization(base, cand)
    as_tiering = evaluate_tiering_change(base, cand)
    assert as_config.adopted == as_tiering.adopted is True
    assert as_config.improved_metrics == as_tiering.improved_metrics
    assert as_config.regressed_metrics == as_tiering.regressed_metrics
    assert as_config.kind == "config"
    assert as_tiering.kind == "tiering"


def test_tiering_rejects_a_proposed_integrity_relaxation() -> None:
    """A proposed tier claiming to relax integrity is rejected before the Pareto gate."""

    class _BadTier(TierDecision):
        def relaxes_integrity(self) -> bool:  # type: ignore[override]
            return True

    bad = _BadTier.model_construct(**classify_tier("typo", seq=SEQ).model_dump())
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.60})
    with pytest.raises(ValueError, match="NEVER relax integrity"):
        evaluate_tiering_change(base, cand, proposed_tiers=[bad])


# --------------------------------------------------------------------------- #
# Determinism + non-vacuity.
# --------------------------------------------------------------------------- #
def test_decision_is_deterministic() -> None:
    """Same inputs → identical decision (content-identical, twice)."""
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.60, "cost_microusd": 900_000})
    d1 = evaluate_optimization(base, cand)
    d2 = evaluate_optimization(base, cand)
    assert d1 == d2
    assert d1.content_identity() == d2.content_identity()


def test_non_vacuity_adopt_vs_reject_flip_on_a_single_metric() -> None:
    """The adopt vs reject verdict flips on a SINGLE metric (the gate is real)."""
    base = _baseline()
    # Identical save resolution: a higher resolution ADOPTS, a lower one REJECTS.
    adopt = evaluate_optimization(base, base.model_copy(update={"resolution_rate": 0.60}))
    reject = evaluate_optimization(base, base.model_copy(update={"resolution_rate": 0.40}))
    assert adopt.adopted is True
    assert reject.adopted is False
    # And the SAME win flips to a reject the moment ONE constrained metric regresses.
    win = evaluate_optimization(base, base.model_copy(update={"resolution_rate": 0.60}))
    bought = evaluate_optimization(
        base,
        base.model_copy(update={"resolution_rate": 0.60, "cost_microusd": 1_000_001}),
    )
    assert win.adopted is True
    assert bought.adopted is False
    assert bought.regressed_metrics == ("cost_microusd",)


def test_provenance_is_carried_and_dry_run_derived() -> None:
    """provenance flows onto the decision; dry_run is derived from it."""
    base = _baseline()
    cand = base.model_copy(update={"resolution_rate": 0.60})
    dry = evaluate_optimization(base, cand)
    assert dry.provenance is Provenance.DRY_RUN
    assert dry.dry_run is True
    scored = evaluate_optimization(base, cand, provenance=Provenance.SCORED)
    assert scored.provenance is Provenance.SCORED
    assert scored.dry_run is False
