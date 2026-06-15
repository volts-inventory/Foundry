"""Conformance tests for the Phase-1.5 central-hypothesis schemas.

Contract under test (docs/evaluation.md §1/§2/§2.5; conventions from
foundry/schemas/base.py): frozen pydantic v2 models (frozen, extra="forbid"),
the three arms + decision classes, the primary metric (oracle-scored, equal-USD,
ITT, higher-is-better), MDE/alpha validation, the funded power rung, and the
dry-run honesty default. Pure schema/enum module — no statistics logic.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation import (
    Arm,
    CentralHypothesis,
    DecisionClass,
    Equalizer,
    PowerRung,
    PrimaryMetric,
    Provenance,
)
from foundry.evaluation.hypothesis import CentralHypothesis as CH_direct
from foundry.schemas.base import FrozenModel


def _hypothesis(**overrides: object) -> CentralHypothesis:
    base: dict[str, object] = {"mde": 0.05, "funded_power_rung": PowerRung(mde=0.05)}
    base.update(overrides)
    return CentralHypothesis(**base)  # type: ignore[arg-type]


# --- package wiring -------------------------------------------------------


def test_reexport_is_same_class() -> None:
    assert CentralHypothesis is CH_direct


def test_models_are_frozen_models() -> None:
    for cls in (PrimaryMetric, PowerRung, CentralHypothesis):
        assert issubclass(cls, FrozenModel)


# --- enums ----------------------------------------------------------------


def test_arm_enum_values() -> None:
    assert {a.value for a in Arm} == {
        "foundry",
        "baseline_single_agent",
        "single_agent_plus_gates",
    }
    assert Arm.FOUNDRY and Arm.BASELINE_SINGLE_AGENT and Arm.SINGLE_AGENT_PLUS_GATES


def test_decision_classes_enum() -> None:
    assert {d.value for d in DecisionClass} == {
        "proceed",
        "hard_kill",
        "bounded_overhead_redesign",
    }


def test_equalizer_enum() -> None:
    assert Equalizer.EQUAL_USD.value == "equal_usd"
    assert Equalizer.EQUAL_EFFECTIVE_TASK_TOKEN.value == "equal_effective_task_token"


def test_provenance_enum() -> None:
    assert {p.value for p in Provenance} == {"dry_run", "pilot", "scored"}


# --- primary metric -------------------------------------------------------


def test_primary_metric_defaults() -> None:
    pm = PrimaryMetric()
    assert pm.name == "swe_evo_resolution_rate"
    assert pm.higher_is_better is True
    assert pm.oracle_scored is True  # §4: no LLM judge in the primary
    assert pm.equalizer is Equalizer.EQUAL_USD  # equal-USD primary
    assert pm.intent_to_treat is True  # §2 ITT


# --- construction + comparison --------------------------------------------


def test_central_hypothesis_construction() -> None:
    h = _hypothesis()
    assert h.treatment_arm is Arm.FOUNDRY
    assert h.control_arm is Arm.BASELINE_SINGLE_AGENT
    assert h.at_most_equal_cost is True
    assert h.primary_metric.equalizer is Equalizer.EQUAL_USD
    assert set(h.decision_classes) == set(DecisionClass)
    assert h.h0 and h.h1  # both stated


def test_alpha_and_mde_present() -> None:
    h = _hypothesis(alpha=0.01, mde=0.1)
    assert h.alpha == 0.01
    assert h.mde == 0.1
    assert h.funded_power_rung.mde == 0.05  # the rung the program funds


# --- field validation -----------------------------------------------------


@pytest.mark.parametrize("bad_alpha", [0.0, 1.0, -0.1, 1.5])
def test_alpha_must_be_open_unit_interval(bad_alpha: float) -> None:
    with pytest.raises(ValidationError):
        _hypothesis(alpha=bad_alpha)


@pytest.mark.parametrize("bad_mde", [0.0, -0.01])
def test_mde_must_be_positive(bad_mde: float) -> None:
    with pytest.raises(ValidationError):
        _hypothesis(mde=bad_mde)


@pytest.mark.parametrize("bad_mde", [0.0, -1.0])
def test_power_rung_mde_must_be_positive(bad_mde: float) -> None:
    with pytest.raises(ValidationError):
        PowerRung(mde=bad_mde)


@pytest.mark.parametrize("bad_power", [0.0, 1.0, -0.5])
def test_power_rung_target_power_in_open_unit_interval(bad_power: float) -> None:
    with pytest.raises(ValidationError):
        PowerRung(mde=0.05, target_power=bad_power)


@pytest.mark.parametrize(
    ("field", "val"),
    [("n_tasks", 0), ("k_repeats", 0), ("funded_budget_microusd", -1)],
)
def test_power_rung_count_bounds(field: str, val: int) -> None:
    with pytest.raises(ValidationError):
        PowerRung(mde=0.05, **{field: val})  # type: ignore[arg-type]


def test_power_rung_optional_defaults_none() -> None:
    r = PowerRung(mde=0.05)
    assert r.n_tasks is None and r.k_repeats is None and r.funded_budget_microusd is None


# --- frozenness + extra=forbid --------------------------------------------


def test_frozen() -> None:
    h = _hypothesis()
    with pytest.raises(ValidationError):
        h.alpha = 0.2  # type: ignore[misc]


def test_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        _hypothesis(unexpected_field=True)
    with pytest.raises(ValidationError):
        PowerRung(mde=0.05, extra=1)  # type: ignore[call-arg]


# --- dry-run / provenance honesty -----------------------------------------


def test_dry_run_default_is_honest() -> None:
    h = _hypothesis()
    assert h.provenance is Provenance.DRY_RUN
    assert h.dry_run is True


def test_provenance_can_be_overridden() -> None:
    h = _hypothesis(provenance=Provenance.SCORED, dry_run=False)
    assert h.provenance is Provenance.SCORED
    assert h.dry_run is False


# --- content_identity stability (base support) ----------------------------


def test_content_identity_stable_across_equal_construction() -> None:
    a = _hypothesis()
    b = _hypothesis()
    assert a.content_identity() == b.content_identity()


def test_content_identity_sensitive_to_change() -> None:
    a = _hypothesis(mde=0.05)
    b = _hypothesis(mde=0.10)
    assert a.content_identity() != b.content_identity()


def test_provenance_changes_identity() -> None:
    a = _hypothesis()
    b = _hypothesis(provenance=Provenance.SCORED, dry_run=False)
    assert a.content_identity() != b.content_identity()
