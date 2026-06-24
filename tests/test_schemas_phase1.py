"""Phase-1 schema gap audit (T-M1-02).

Audit result: every Phase-1 agent output artifact in docs/agents.md Layers 1
(Product), 3 (Implementation), and 4 (Quality) already had an adequate frozen
pydantic model in the matching foundry/schemas/* module, and tests/test_schemas.py
already exercises construction / frozenness / content_identity for each. The one
genuine gap was criterion traceability on the E2E layer: `E2EReport` carried the
flake-quarantine fields but no scenario->criterion mapping, unlike `TestPlan`.

This module covers the single append-only addition — `E2EReport.scenario_criteria`
and its `covered_criteria()` helper — for construction, frozenness, and
content_identity stability. The pre-existing models are left untouched and remain
covered by tests/test_schemas.py (asserted to still import cleanly below).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.quality import E2EReport
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="sdet", local=0)

E2E_KWARGS = {
    "results_uri": "cas://e2e/1",
    "run_against": "c" * 40,
    "scenarios_passed": 3,
    "scenarios_failed": 0,
    "pinned_seeds_note": "seed=42, clock frozen 2026-01-01T00:00Z, network mocked",
    "scenario_criteria": {"s_login": ("AC-1",), "s_checkout": ("AC-1", "AC-2")},
    "seq": SEQ,
}


def test_e2e_report_with_criterion_traceability_constructs() -> None:
    r = E2EReport(**E2E_KWARGS)
    assert r.seq == SEQ
    assert r.scenario_criteria == {"s_login": ("AC-1",), "s_checkout": ("AC-1", "AC-2")}
    assert r.flake_retry_budget == 2  # default preserved alongside the new field


def test_e2e_report_scenario_criteria_defaults_empty() -> None:
    """Append-only: existing call sites that omit the field still construct."""
    r = E2EReport(
        results_uri="cas://e2e/1",
        run_against="c" * 40,
        scenarios_passed=1,
        scenarios_failed=0,
        pinned_seeds_note="seed=42",
        seq=SEQ,
    )
    assert r.scenario_criteria == {}
    assert r.covered_criteria() == frozenset()


def test_e2e_report_covered_criteria_union() -> None:
    r = E2EReport(**E2E_KWARGS)
    assert r.covered_criteria() == frozenset({"AC-1", "AC-2"})


def test_e2e_report_is_frozen() -> None:
    r = E2EReport(**E2E_KWARGS)
    with pytest.raises(ValidationError):
        r.scenario_criteria = {"s_x": ("AC-9",)}


def test_e2e_report_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        E2EReport(**E2E_KWARGS, not_a_field="boom")


def test_e2e_report_content_identity_stable_and_field_sensitive() -> None:
    a = E2EReport(**E2E_KWARGS)
    # construction order irrelevant; dict key order irrelevant (canonical digest).
    reordered = dict(reversed(list(E2E_KWARGS.items())))
    reordered["scenario_criteria"] = {"s_checkout": ("AC-1", "AC-2"), "s_login": ("AC-1",)}
    b = E2EReport(**reordered)
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic

    mutated = a.model_copy(update={"scenario_criteria": {"s_login": ("AC-3",)}})
    assert mutated.content_identity() != a.content_identity()


def test_existing_schema_models_still_import() -> None:
    """The append-only edit must not break the pre-existing contracts."""
    from foundry.schemas.implementation import (  # noqa: F401
        ConflictResolution,
        FeatureDiff,
        ReviewRequest,
        Task,
    )
    from foundry.schemas.product import (  # noqa: F401
        PRD,
        AcceptanceCriterion,
        CriteriaCritique,
        IntentBrief,
        SprintPlan,
    )
    from foundry.schemas.quality import OracleBinding, TestPlan, TestReport  # noqa: F401
