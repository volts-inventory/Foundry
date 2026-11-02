"""Spec gate over structured Given/When/Then scenarios, and the criteria payload
the gate wiring materializes for it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.agents._common import emit_artifact
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates import SpecGate
from foundry.graph.gate_wiring import _criteria_list
from foundry.schemas.gates import GateStatus
from foundry.schemas.product import AcceptanceCriterion, render_scenario
from foundry.state.seq import Seq


def _gwt(cid: str, *, abuse: bool = True, **steps: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"given": ["a list"], "when": ["I add x"], "then": ["x is listed"]}
    base.update(steps)
    return {"id": cid, "text": "Given a list When I add x Then x is listed",
            "oracle": f"features/T-1.feature::{cid}", "security_abuse": abuse, **base}


def _run(tmp_path: Path, criteria: list[Any]) -> tuple[GateStatus, tuple[str, ...]]:
    (tmp_path / "criteria.json").write_text(json.dumps(criteria), encoding="utf-8")
    gate = SpecGate()
    obs = gate.execute(tmp_path)
    return gate.decide(obs), tuple(obs.findings)


def test_complete_scenarios_pass(tmp_path: Path) -> None:
    status, _ = _run(tmp_path, [_gwt("AC-1"), _gwt("AC-2", abuse=False)])
    assert status is GateStatus.PASS


@pytest.mark.parametrize("missing", ["given", "when", "then"])
def test_a_scenario_missing_a_step_kind_fails(tmp_path: Path, missing: str) -> None:
    status, findings = _run(tmp_path, [_gwt("AC-1", **{missing: []})])
    assert status is GateStatus.FAIL
    assert f"criterion AC-1: scenario has no {missing} step" in findings


def test_blank_or_mistyped_steps_fail_closed(tmp_path: Path) -> None:
    assert _run(tmp_path, [_gwt("AC-1", then=["  "])])[0] is GateStatus.FAIL
    assert _run(tmp_path, [_gwt("AC-1", given="a list")])[0] is GateStatus.FAIL


def test_text_only_criteria_still_validate_as_text(tmp_path: Path) -> None:
    legacy = {"id": "AC-1", "text": "Given a When b Then c", "oracle": "o",
              "security_abuse": True}
    assert _run(tmp_path, [legacy])[0] is GateStatus.PASS
    assert _run(tmp_path, [{**legacy, "text": ""}])[0] is GateStatus.FAIL


def test_security_abuse_and_oracle_are_still_required(tmp_path: Path) -> None:
    assert _run(tmp_path, [_gwt("AC-1", abuse=False)])[0] is GateStatus.FAIL
    assert _run(tmp_path, [{**_gwt("AC-1"), "oracle": ""}])[0] is GateStatus.FAIL


def _criterion(cid: str, local: int, **fields: Any) -> AcceptanceCriterion:
    return AcceptanceCriterion(id=cid, seq=Seq("r", 1, 1, "planner", local), **fields)


def test_criteria_payload_carries_steps_only_for_structured_scenarios() -> None:
    store = ArtifactStore()
    steps = {"given": ("a list",), "when": ("I add x",), "then": ("x is listed",)}
    structured = _criterion(
        "AC-1", 0, text=render_scenario(**steps), oracle_hint="features/T-1.feature::AC-1",
        security_abuse=True, ticket_id="T-1", **steps,
    )
    legacy = _criterion("AC-2", 1, text="plain", oracle_hint="o")
    handles = [
        emit_artifact(c, "acceptance_criterion", "planner", c.seq, store=store)
        for c in (structured, legacy)
    ]
    payload = _criteria_list({"artifacts": handles}, store.resolve)
    assert payload == [
        {"id": "AC-1", "text": "Given a list When I add x Then x is listed",
         "oracle": "features/T-1.feature::AC-1", "security_abuse": True,
         "given": ["a list"], "when": ["I add x"], "then": ["x is listed"]},
        {"id": "AC-2", "text": "plain", "oracle": "o", "security_abuse": False},
    ]
