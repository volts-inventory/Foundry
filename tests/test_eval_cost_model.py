"""Conformance tests for the per-feature + A/B-scale cost model (T-M6-01).

Contract (docs/evaluation.md §8; ``CostModel`` turns the "viability" / "<=5%
overhead" claims into a concrete, falsifiable accounting model):

* ``feature_cost`` sums the §8 terms correctly — model tokens + trusted-runner
  compute + wall-clock + human-review (HITL) + infra — into an integer total.
* The DOMINANT non-token terms can EXCEED the token term: a "cheaper on tokens"
  feature is more expensive end-to-end (``non_token_dominates``), the load-bearing
  §8 / COST-F5 point the scorecard / optimization consume.
* ``ab_scale_cost`` scales with the ``(n, k)`` rung and sums across arms (the
  ``3 arms x n x k x per-task cost`` roll-up — the decisive run shown affordable).
* Pure + deterministic (same signals -> identical breakdown).
* Integer-money discipline: no float drift; money stays integer micro-USD.

METHODOLOGY DRY-RUN on synthetic inputs — the FORMULA is real, the numbers are
synthetic; asserts NOTHING about real cost.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation.cost_model import CostBreakdown, CostModel, RunSignals
from foundry.evaluation.hypothesis import Arm, Provenance

# A token-CHEAP but non-token-EXPENSIVE feature: tokens are tiny, but the trusted
# runner, wall-clock, HITL, and infra dominate (the §8 / COST-F5 case).
NON_TOKEN_HEAVY = RunSignals(
    model_token_microusd=50_000,  # cheap on tokens
    compute_units=120,  # mutant/perturbation families on the trusted runner
    compute_unit_microusd=2_000,  # -> 240_000 micro-USD
    wall_clock_ms=600_000,  # 10 min lead time
    wall_clock_unit_microusd=1,  # -> 600_000 micro-USD
    human_review_ms=300_000,  # 5 min of HITL review
    human_review_unit_microusd=2,  # -> 600_000 micro-USD
    infra_microusd=100_000,  # replicas / queue / burst share
)

# A token-HEAVY feature: tokens dominate, non-token terms are small.
TOKEN_HEAVY = RunSignals(
    model_token_microusd=5_000_000,
    compute_units=1,
    compute_unit_microusd=1_000,  # -> 1_000
    wall_clock_ms=1_000,
    wall_clock_unit_microusd=1,  # -> 1_000
    human_review_ms=0,
    human_review_unit_microusd=10,  # -> 0
    infra_microusd=2_000,
)


def test_feature_cost_sums_all_terms() -> None:
    """feature_cost prices each §8 term and sums them into the integer total."""
    model = CostModel()
    bd = model.feature_cost(NON_TOKEN_HEAVY)

    assert bd.model_token_microusd == 50_000
    assert bd.compute_microusd == 120 * 2_000  # 240_000
    assert bd.wall_clock_microusd == 600_000 * 1  # 600_000
    assert bd.human_review_microusd == 300_000 * 2  # 600_000
    assert bd.infra_microusd == 100_000

    expected_total = 50_000 + 240_000 + 600_000 + 600_000 + 100_000
    assert bd.total_microusd == expected_total
    # The total is exactly the sum of the five enumerated USD terms.
    assert bd.total_microusd == (
        bd.model_token_microusd
        + bd.compute_microusd
        + bd.wall_clock_microusd
        + bd.human_review_microusd
        + bd.infra_microusd
    )
    # Raw non-USD dimensions are carried alongside for the §7 scorecard.
    assert bd.compute_units == 120
    assert bd.wall_clock_ms == 600_000
    assert bd.human_review_ms == 300_000


def test_non_token_terms_dominate() -> None:
    """The load-bearing §8 case: non-token terms EXCEED tokens end-to-end."""
    model = CostModel()
    bd = model.feature_cost(NON_TOKEN_HEAVY)

    non_token = bd.compute_microusd + bd.wall_clock_microusd + bd.human_review_microusd
    non_token += bd.infra_microusd
    assert bd.non_token_microusd() == non_token
    assert bd.non_token_microusd() == 240_000 + 600_000 + 600_000 + 100_000

    # Cheaper on tokens, MORE expensive end-to-end (COST-F5).
    assert bd.non_token_microusd() > bd.model_token_microusd
    assert bd.non_token_dominates() is True
    # The token line is a SMALL fraction of the real total.
    assert bd.token_fraction() < 0.05

    # And the mirror: a token-heavy feature does NOT trip the dominance flag.
    token_bd = model.feature_cost(TOKEN_HEAVY)
    assert token_bd.non_token_dominates() is False
    assert token_bd.token_fraction() > 0.99


def test_cheaper_on_tokens_more_expensive_end_to_end() -> None:
    """An arm cheaper on TOKENS can be the more expensive arm END-TO-END.

    This is the exact §8 / COST-F5 trap the model exists to catch: choosing the
    arm by ``model_token_microusd`` alone inverts the real ``total_microusd``
    ordering.
    """
    model = CostModel()
    # Arm A: pricier tokens, trivial everything else.
    arm_a = RunSignals(model_token_microusd=2_000_000)
    # Arm B: HALF the tokens, but heavy trusted-runner + HITL + wall-clock.
    arm_b = RunSignals(
        model_token_microusd=1_000_000,
        compute_units=2_000,
        compute_unit_microusd=1_000,  # 2_000_000
        human_review_ms=200_000,
        human_review_unit_microusd=5,  # 1_000_000
    )
    a = model.feature_cost(arm_a)
    b = model.feature_cost(arm_b)

    # B looks cheaper on tokens...
    assert b.model_token_microusd < a.model_token_microusd
    # ...but is MORE expensive end-to-end.
    assert b.total_microusd > a.total_microusd
    assert b.non_token_dominates() is True


def test_ab_scale_cost_scales_with_n_and_k() -> None:
    """ab_scale_cost multiplies per-task cost through the (n, k) rung x arms."""
    model = CostModel()
    arms = {
        Arm.FOUNDRY: NON_TOKEN_HEAVY,
        Arm.BASELINE_SINGLE_AGENT: TOKEN_HEAVY,
        Arm.SINGLE_AGENT_PLUS_GATES: NON_TOKEN_HEAVY,
    }
    n, k = 8, 3
    rung = n * k

    roll = model.ab_scale_cost(arms, n, k)

    # Each arm's per-task total scaled by n*k, summed across the three arms.
    per_arm_total = sum(model.feature_cost(s).total_microusd for s in arms.values())
    assert roll.total_microusd == per_arm_total * rung

    # Doubling k doubles the roll-up (linear in the rung).
    roll_2k = model.ab_scale_cost(arms, n, k * 2)
    assert roll_2k.total_microusd == roll.total_microusd * 2

    # Doubling n likewise doubles it.
    roll_2n = model.ab_scale_cost(arms, n * 2, k)
    assert roll_2n.total_microusd == roll.total_microusd * 2

    # Every term scaled consistently (not just the total).
    feat_compute = sum(model.feature_cost(s).compute_microusd for s in arms.values())
    assert roll.compute_microusd == feat_compute * rung
    feat_units = sum(model.feature_cost(s).compute_units for s in arms.values())
    assert roll.compute_units == feat_units * rung


def test_ab_scale_cost_rejects_bad_rung() -> None:
    """Non-positive n/k or an empty arm map fail closed (ValueError)."""
    model = CostModel()
    arms = {Arm.FOUNDRY: TOKEN_HEAVY}
    with pytest.raises(ValueError):
        model.ab_scale_cost(arms, 0, 3)
    with pytest.raises(ValueError):
        model.ab_scale_cost(arms, 3, 0)
    with pytest.raises(ValueError):
        model.ab_scale_cost({}, 3, 3)


def test_determinism() -> None:
    """Same signals -> identical CostBreakdown (pure; content identity equal)."""
    model = CostModel()
    a = model.feature_cost(NON_TOKEN_HEAVY)
    b = model.feature_cost(NON_TOKEN_HEAVY)
    assert a == b
    assert a.content_identity() == b.content_identity()

    arms = {Arm.FOUNDRY: NON_TOKEN_HEAVY, Arm.BASELINE_SINGLE_AGENT: TOKEN_HEAVY}
    r1 = model.ab_scale_cost(arms, 5, 2)
    r2 = model.ab_scale_cost(arms, 5, 2)
    assert r1 == r2
    assert r1.content_identity() == r2.content_identity()


def test_integer_money_discipline_no_float_drift() -> None:
    """Money is integer micro-USD end-to-end — no float drift across the roll-up.

    Float accumulation of these terms would drift; integer micro-USD does not.
    Every USD field is an ``int``, and an exact integer reconstruction of the big
    roll-up matches to the unit.
    """
    model = CostModel()
    bd = model.feature_cost(NON_TOKEN_HEAVY)
    for field in (
        "model_token_microusd",
        "compute_microusd",
        "wall_clock_microusd",
        "human_review_microusd",
        "infra_microusd",
        "total_microusd",
    ):
        assert isinstance(getattr(bd, field), int)
        assert not isinstance(getattr(bd, field), bool)

    # A large (n, k) roll-up reconstructed by exact integer arithmetic — equal to
    # the unit, no rounding. (A float model would not survive this.)
    arms = {Arm.FOUNDRY: NON_TOKEN_HEAVY, Arm.BASELINE_SINGLE_AGENT: TOKEN_HEAVY}
    n, k = 137, 11  # awkward primes to expose any rounding
    roll = model.ab_scale_cost(arms, n, k)
    expected = sum(model.feature_cost(s).total_microusd for s in arms.values()) * n * k
    assert isinstance(roll.total_microusd, int)
    assert roll.total_microusd == expected


def test_dry_run_honesty_default() -> None:
    """A breakdown defaults to DRY_RUN provenance — the accounting is real, the
    inputs synthetic; it asserts NOTHING about real cost."""
    model = CostModel()
    bd = model.feature_cost(TOKEN_HEAVY)
    assert bd.provenance is Provenance.DRY_RUN
    assert bd.dry_run is True

    scored = CostModel(provenance=Provenance.SCORED)
    sbd = scored.feature_cost(TOKEN_HEAVY)
    assert sbd.provenance is Provenance.SCORED
    assert sbd.dry_run is False
    # Provenance is part of identity: dry-run and scored breakdowns of the same
    # shape do NOT coalesce (don't pass a rehearsal off as funded evidence).
    assert bd.content_identity() != sbd.content_identity()


def test_frozen_and_extra_forbidden() -> None:
    """CostBreakdown / RunSignals are frozen + extra=forbid (FrozenModel base)."""
    bd = CostModel().feature_cost(TOKEN_HEAVY)
    with pytest.raises(ValidationError):
        bd.total_microusd = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CostBreakdown(  # negative money rejected (ge=0)
            model_token_microusd=-1,
            compute_microusd=0,
            wall_clock_microusd=0,
            human_review_microusd=0,
            infra_microusd=0,
            total_microusd=0,
            compute_units=0,
            wall_clock_ms=0,
            human_review_ms=0,
        )
    with pytest.raises(ValidationError):
        RunSignals(bogus_field=1)  # type: ignore[call-arg]


def test_empty_feature_is_zero() -> None:
    """A signal-free feature costs zero; token_fraction is 0.0 (no div-by-zero)."""
    bd = CostModel().feature_cost(RunSignals())
    assert bd.total_microusd == 0
    assert bd.non_token_microusd() == 0
    assert bd.non_token_dominates() is False
    assert bd.token_fraction() == 0.0
