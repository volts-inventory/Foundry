"""The planner's deterministic coverage check: the tickets cover the whole app.

Every must-have and success criterion is covered by a scenario on a live ticket,
every feature ticket carries scenarios, one entrypoint ticket wires every feature
in, an app-like deliverable has a run command, and work dropped at a cap is named.
Each violation is a ``coverage_findings`` entry that FAILS the Spec gate, so the
existing spec_fix revision pass closes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundry.agents.planner import (
    MAX_TICKETS,
    _coverage_findings,
    make_planner_node,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates import SpecGate
from foundry.graph.gate_wiring import _criteria_payload
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, StackDecision, Ticket
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique, IntentBrief
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.planner_payload import PYTHON_STACK, multi_epic_plan
from tests.test_planner import (
    PLAN,
    _bodies,
    _epic_of,
    _epic_runtime,
    _epic_scripts,
    _plan,
    _prompt,
    _spec_verdict,
    config_for,
    runtime_for,
)


def _copy(x: Any) -> Any:
    return json.loads(json.dumps(x))


def _tickets_with(**changes: dict[str, Any]) -> list[dict[str, Any]]:
    """PLAN's tickets with per-id field overrides (``T_002={...}`` for T-002)."""
    tickets = _copy(PLAN["tickets"])
    for t in tickets:
        t.update(changes.get(t["id"].replace("-", "_"), {}))
    return tickets


def _run(payload: dict[str, Any], state: dict[str, Any] | None = None) -> tuple[
    dict[str, Any], ArtifactStore, CriteriaCritique
]:
    cas = ArtifactStore()
    rt, _ = runtime_for(payload, cas=cas)
    writes = make_planner_node(rt)({"request": "x", **(state or {})}, config_for("r"))
    [critique] = _bodies(cas, writes, "criteria_findings")
    return writes, cas, critique


def _coverage(payload: dict[str, Any], state: dict[str, Any] | None = None) -> tuple[str, ...]:
    return _run(payload, state)[2].coverage_findings


def _scenarios_covering(*covers: str) -> list[dict[str, Any]]:
    scenarios = _copy(PLAN["tickets"][1]["scenarios"])
    scenarios[0]["covers"] = list(covers)
    return scenarios


# ---- the clean plan ----------------------------------------------------------------


def test_a_fully_covered_plan_has_no_coverage_findings() -> None:
    writes, cas, critique = _run(PLAN)
    assert critique.coverage_findings == ()
    assert not critique.spec_gate_blocking()
    [brief] = _bodies(cas, writes, "intent_brief")
    assert brief.must_have_ids == ("MH-1",) and brief.success_criterion_ids == ("SC-1",)
    assert brief.deliverable == "library"
    criteria = {c.id: c for c in _bodies(cas, writes, "acceptance_criterion")}
    assert criteria["AC-001"].covers == ("MH-1", "SC-1")
    tickets = {t.id: t for t in _bodies(cas, writes, "ticket")}
    assert tickets["T-002"].entrypoint and not tickets["T-001"].entrypoint


# ---- 1. traceability ------------------------------------------------------------------


def test_bad_goal_and_covers_ids_are_tolerated_and_reported() -> None:
    intent = {
        **PLAN["intent"],
        "must_haves": ["add todo", {"text": "list todos"}, {"id": "mh1", "text": "dup"},
                       None, 42, {"id": "bad id!", "text": "odd"}, {"id": "MH-9"},
                       {"id": "SC-1", "text": "wrong prefix"}],
        "success_criteria": "a user can add a todo",
        "deliverable": "spaceship",
    }
    scenarios = _scenarios_covering("mh-1", " MH-02 ", "sc1", "bogus", 7, None)
    writes, cas, critique = _run(_plan(intent=intent, tickets=_tickets_with(
        T_002={"scenarios": scenarios},
    )))
    [brief] = _bodies(cas, writes, "intent_brief")
    assert brief.must_have_ids == ("MH-1", "MH-2", "MH-3", "MH-4", "MH-5", "MH-6")
    assert brief.must_haves == ("add todo", "list todos", "dup", "42", "odd", "wrong prefix")
    assert brief.success_criterion_ids == ("SC-1",)
    assert brief.deliverable == ""  # unknown -> undeclared, treated as an app
    findings = critique.coverage_findings
    known = "(MH-1, MH-2, MH-3, MH-4, MH-5, MH-6, SC-1)"
    assert f"Scenario AC-001 covers unknown id 'bogus'; cite must-have / success-criterion " \
        f"ids {known}." in findings
    assert any("covers unknown id '7'" in f for f in findings)
    uncovered = [f.split()[1] for f in findings if "has no covering scenario" in f]
    assert uncovered == ["MH-3", "MH-4", "MH-5", "MH-6"]
    assert any(f.startswith("stack.run is empty but the deliverable is an app") for f in findings)


# ---- 2. every scenario owned, every feature ticket specified -----------------------------


_SEQ = Seq("r", 1, 1, "planner", 0)


def _brief(**kw: Any) -> IntentBrief:
    return IntentBrief(intent="x", seq=_SEQ, **kw)


def test_a_scenario_on_no_live_ticket_is_an_orphan_and_covers_nothing() -> None:
    entry = Ticket(id="T-1", title="app", specialty=Specialty.BACKEND, entrypoint=True,
                   seq=_SEQ)
    criteria = [
        AcceptanceCriterion(id="AC-1", text="t", ticket_id="T-1", seq=_SEQ),
        AcceptanceCriterion(id="AC-9", text="t", ticket_id="T-404", covers=("MH-1",),
                            seq=_SEQ),
    ]
    findings = _coverage_findings(
        [entry], criteria, _brief(must_haves=("m",), must_have_ids=("MH-1",)), run=None,
    )
    assert findings == (
        "Scenario AC-9 is not owned by a live ticket (T-404 is not in the plan); put it "
        "on a ticket of the plan.",
        "Must-have MH-1 ('m') has no covering scenario; add one whose covers lists MH-1.",
    )


def test_a_feature_ticket_without_scenarios_is_a_finding_test_tickets_exempt() -> None:
    extra = {"id": "T-004", "title": "Export", "specialty": "backend",
             "difficulty": "standard", "depends_on": [], "files": ["todo/export.py"],
             "entrypoint": False, "scenarios": []}
    tickets = _tickets_with(T_002={"depends_on": ["T-001", "T-004"]})
    findings = _coverage(_plan(tickets=[*tickets, extra]))
    assert findings == (
        "Feature ticket T-004 has no scenarios; give it at least one (or fold it into "
        "the ticket whose behaviour it serves).",
    )  # T-003 (test) and the generated tickets carry none and are exempt


# ---- 3. one entrypoint wires everything; an app has a run command ---------------------------


def test_an_entrypoint_must_depend_on_every_feature_ticket() -> None:
    findings = _coverage(_plan(tickets=_tickets_with(
        T_001={"entrypoint": True}, T_002={"entrypoint": False},
    )))
    assert findings == (
        "Entrypoint ticket T-001 does not depend (directly or transitively) on feature "
        "ticket(s) T-002; it must wire every feature in.",
    )
    # A test ticket never counts as the entrypoint.
    findings = _coverage(_plan(tickets=_tickets_with(
        T_002={"entrypoint": False}, T_003={"entrypoint": True},
    )))
    assert findings[0].startswith("No entrypoint ticket")


def _app(run: str) -> dict[str, Any]:
    return _plan(intent={**PLAN["intent"], "deliverable": "app"},
                 stack={**PYTHON_STACK, "run": run})


def test_an_app_deliverable_needs_a_run_command_a_library_does_not() -> None:
    assert _coverage(_app("")) == (
        "stack.run is empty but the deliverable is app: give a run command that boots the "
        "real app headless and exits 0, or declare deliverable 'library' if nothing runs.",
    )
    assert _coverage(_app("python -m todo --check")) == ()
    assert _coverage(PLAN) == ()  # PLAN is a library with an empty run


def test_an_operator_pinned_profile_is_exempt_from_the_run_check() -> None:
    pinned = StackDecision(language="python", test="pytest", profile="python")
    writes, _, critique = _run(_app(""), {"stack": pinned})
    assert critique.coverage_findings == ()
    assert "stack" not in writes


def test_a_replan_may_add_the_run_command_to_the_planner_chosen_stack() -> None:
    chosen = StackDecision(language="python", test="pytest")  # an earlier pass, no run
    assert _coverage(_app(""), {"stack": chosen})  # still missing: a finding
    writes, _, critique = _run(_app("python -m todo --check"), {"stack": chosen})
    assert critique.coverage_findings == ()
    assert writes["stack"] == chosen.model_copy(update={"run": "python -m todo --check"})


# ---- 4. caps never drop work silently -------------------------------------------------------


def test_inline_cap_overflow_is_a_finding_naming_the_dropped_tickets() -> None:
    entry = _copy(PLAN["tickets"][1])
    filler = [
        {**_copy(PLAN["tickets"][0]), "id": f"T-{i:03d}", "title": f"part {i}",
         "files": [f"todo/p{i}.py"], "implements": [],
         "scenarios": [{**PLAN["tickets"][0]["scenarios"][0], "id": f"AC-{i:03d}"}]}
        for i in range(100, 100 + MAX_TICKETS + 1)
    ]
    _, _, critique = _run(_plan(tickets=[entry, *filler]))
    [overflow] = [f for f in critique.coverage_findings if "exceed the cap" in f]
    assert overflow == (
        f"{MAX_TICKETS + 2} tickets exceed the cap of {MAX_TICKETS}; dropped T-139 (part "
        "139), T-140 (part 140): consolidate tickets (or split a larger build into epics) "
        "so no work is lost."
    )
    assert critique.spec_gate_blocking()


# ---- epic mode: coverage is checked across epics ---------------------------------------------


def test_epic_mode_coverage_spans_every_epic() -> None:
    plan, replies = multi_epic_plan()
    cas = ArtifactStore()
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    [critique] = _bodies(cas, writes, "criteria_findings")
    assert critique.coverage_findings == ()  # MH-1 in E1, MH-2 in E2, SC-1 in E3

    prompts = {_epic_of(r): json.dumps(r, ensure_ascii=False) for r in tx.requests}
    assert "MH-2 (must-have): notes are served over HTTP" in prompts["E1"]
    assert "YOUR EPIC OWNS THE ENTRYPOINT" in prompts["E3"]
    assert "does NOT own the entrypoint" in prompts["E1"]

    replies["E3"]["tickets"][0]["scenarios"][0]["covers"] = []
    replies["E3"]["tickets"][0]["entrypoint"] = False
    cas = ArtifactStore()
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    [critique] = _bodies(cas, writes, "criteria_findings")
    assert critique.coverage_findings == (
        "Success criterion SC-1 ('a user sees a note in the browser') has no covering "
        "scenario; add one whose covers lists SC-1.",
        "No entrypoint ticket: mark one feature ticket entrypoint=true that wires the app "
        "together end to end and depends_on every other feature ticket.",
    )


# ---- the Spec gate fails on them and the revision pass is handed them ------------------------


def test_coverage_findings_fail_the_spec_gate_and_reach_the_revision(tmp_path: Path) -> None:
    uncovered = _plan(tickets=_tickets_with(T_002={"scenarios": _scenarios_covering("SC-1")}))
    cas = ArtifactStore()
    rt, tx = runtime_for(uncovered, PLAN, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))

    doc = _criteria_payload({"artifacts": first["artifacts"]}, cas.resolve)
    assert isinstance(doc, dict)
    assert doc["critique"]["coverage_findings"] == [
        "Must-have MH-1 ('add todo') has no covering scenario; add one whose covers lists "
        "MH-1."
    ]
    (tmp_path / "criteria.json").write_text(json.dumps(doc), encoding="utf-8")
    gate = SpecGate()
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.findings == (
        "coverage: Must-have MH-1 ('add todo') has no covering scenario; add one whose "
        "covers lists MH-1.",
    )

    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, tuple(obs.findings), epoch=1)],
    }
    second = node(state, config_for("r", step=5))
    prompt = _prompt(tx)
    assert "REVISION PASS" in prompt
    assert "coverage: Must-have MH-1 ('add todo') has no covering scenario" in prompt
    assert "MH-1 (must-have): add todo" in prompt  # the ids to keep
    [critique] = [cas.resolve(a) for a in second["artifacts"] if a.kind == "criteria_findings"]
    assert critique.coverage_findings == ()  # the revision covered it
