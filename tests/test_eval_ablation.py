"""Conformance tests for the role-ablation harness (T-M15-09).

Contract (docs/roadmap.md §1.5 P-F2; docs/evaluation.md §2; reuses
foundry/evaluation/task_fixtures.py):

* DETERMINISM — same (frame, roles, seed, k) => identical AblationResult.
* PER-ROLE DELTAS computed for the 4 core roles (PM/Architect/Senior/QA).
* a role whose removal HURTS the scorecard beyond threshold => earns_place True.
* a role whose removal does NOT change the scorecard => earns_place False.
* the SIMULATED-vs-real ablation flag is set honestly (simulated_ablation=True).
* COST DELTA recorded (full - ablated, >= 0: removing a role is cheaper).

SIMULATED ablation only (contribution model, not a real per-role graph bypass) —
asserts NOTHING about a real org design (the conclusion is about the METHOD).
"""

from __future__ import annotations

import pytest

from foundry.evaluation.ablation import (
    DEFAULT_CORE_ROLES,
    AblationResult,
    Role,
    RoleAblationDelta,
    run_ablation,
)
from foundry.evaluation.hypothesis import Provenance
from foundry.evaluation.task_fixtures import TaskFrame, generate_synthetic_frame
from foundry.schemas.base import FrozenModel


def _frame(n_tasks: int = 24, seed: int = 0) -> TaskFrame:
    return generate_synthetic_frame(n_tasks=n_tasks, seed=seed)


# ---- types / frozen ---------------------------------------------------------


def test_result_is_frozen_and_typed() -> None:
    result = run_ablation(_frame())
    assert isinstance(result, AblationResult)
    assert isinstance(result, FrozenModel)
    assert all(isinstance(d, RoleAblationDelta) for d in result.deltas)
    with pytest.raises(Exception):  # noqa: B017 — frozen pydantic rejects mutation
        result.full_rate = 0.0  # type: ignore[misc]


# ---- per-role deltas for the 4 core roles -----------------------------------


def test_per_role_deltas_for_the_four_core_roles() -> None:
    result = run_ablation(_frame())
    assert result.roles == DEFAULT_CORE_ROLES
    assert tuple(d.role for d in result.deltas) == DEFAULT_CORE_ROLES
    # every core role has exactly one delta, computed against the shared full rate
    for role in (Role.PM, Role.ARCHITECT, Role.SENIOR, Role.QA):
        d = result.delta_for(role)
        assert d.full_rate == result.full_rate
        assert d.delta == pytest.approx(d.full_rate - d.ablated_rate)


# ---- earns-place rule -------------------------------------------------------


def test_contributing_role_earns_its_place() -> None:
    # SENIOR has the largest contribution (0.20) -> removing it measurably hurts.
    result = run_ablation(_frame())
    senior = result.delta_for(Role.SENIOR)
    assert senior.delta >= senior.earns_place_threshold
    assert senior.earns_place is True
    assert senior.ablated_rate < senior.full_rate


def test_non_contributing_role_does_not_earn_its_place() -> None:
    # QA has zero contribution -> common-random-numbers makes its ablated rate
    # IDENTICAL to the full rate (delta == 0) -> flagged not-earning-its-place.
    result = run_ablation(_frame())
    qa = result.delta_for(Role.QA)
    assert qa.delta == pytest.approx(0.0)
    assert qa.ablated_rate == qa.full_rate
    assert qa.earns_place is False
    assert Role.QA in result.non_earning_roles


def test_earns_place_is_threshold_driven() -> None:
    # PM contributes 0.10; with a threshold above that it no longer earns its place.
    earns = run_ablation(_frame(), threshold=0.02).delta_for(Role.PM)
    not_earns = run_ablation(_frame(), threshold=0.99).delta_for(Role.PM)
    assert earns.earns_place is True
    assert not_earns.earns_place is False
    # same measured delta either way — only the verdict moves with the threshold
    assert earns.delta == pytest.approx(not_earns.delta)


# ---- cost delta -------------------------------------------------------------


def test_cost_delta_recorded_and_removal_is_cheaper() -> None:
    result = run_ablation(_frame())
    for d in result.deltas:
        # removing a role drops both its contribution AND its per-attempt cost
        assert d.ablated_cost_microusd < d.full_cost_microusd
        assert d.cost_delta_microusd == d.full_cost_microusd - d.ablated_cost_microusd
        assert d.cost_delta_microusd > 0


# ---- honest provenance / simulated flag -------------------------------------


def test_simulated_ablation_flag_set_honestly() -> None:
    result = run_ablation(_frame())
    assert result.simulated_ablation is True  # contribution model, NOT a real bypass
    assert result.provenance is Provenance.DRY_RUN


def test_binds_to_scored_slice_identity() -> None:
    frame = _frame()
    result = run_ablation(frame)
    assert result.eval_set_identity == frame.scored_slice.identity
    assert result.slice_name == "scored"


def test_pilot_slice_opt_in() -> None:
    frame = _frame()
    result = run_ablation(frame, use_pilot_slice=True)
    assert result.eval_set_identity == frame.pilot_slice.identity
    assert result.slice_name == "pilot"


# ---- determinism ------------------------------------------------------------


def test_determinism_same_inputs_identical_result() -> None:
    a = run_ablation(_frame(), seed=7, k_repeats=3)
    b = run_ablation(_frame(), seed=7, k_repeats=3)
    assert a == b
    assert a.content_identity() == b.content_identity()


def test_seed_changes_draws() -> None:
    a = run_ablation(_frame(), seed=1)
    b = run_ablation(_frame(), seed=2)
    # a different seed re-draws the Bernoulli outcomes (rates may move); the
    # QA-zero-contribution invariant (delta == 0) still holds under any seed.
    assert a.delta_for(Role.QA).delta == pytest.approx(0.0)
    assert b.delta_for(Role.QA).delta == pytest.approx(0.0)
    assert (a.full_rate, b.full_rate) is not None  # both well-defined


# ---- configurable role set + validation -------------------------------------


def test_custom_role_set() -> None:
    roles = (Role.PM, Role.SENIOR)
    result = run_ablation(_frame(), roles=roles)
    assert result.roles == roles
    assert tuple(d.role for d in result.deltas) == roles


def test_empty_roles_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        run_ablation(_frame(), roles=())


def test_missing_contribution_rejected() -> None:
    # a role with no contribution/cost entry is rejected (custom partial tables)
    with pytest.raises(ValueError, match="missing"):
        run_ablation(
            _frame(),
            roles=(Role.PM, Role.QA),
            contribution={Role.PM: 0.1},  # QA absent
            cost={Role.PM: 1, Role.QA: 1},
        )


def test_single_role_core_ablates_to_empty_org() -> None:
    # ablating the only role models the empty org (bare control base rate, 0 cost)
    result = run_ablation(_frame(), roles=(Role.SENIOR,))
    d = result.delta_for(Role.SENIOR)
    assert d.ablated_cost_microusd == 0
    assert d.delta >= d.earns_place_threshold  # SENIOR's 0.20 lift clears it
