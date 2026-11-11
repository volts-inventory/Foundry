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
    SOP,
    _coverage_findings,
    _fold_test_bindings,
    _offline_scenario_findings,
    _one_step_module_per_ticket,
    _own_scenario_bindings,
    _python_deps_rule,
    _stack_constraints,
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


def _run(
    payload: dict[str, Any], state: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ArtifactStore, CriteriaCritique]:
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
        "must_haves": [
            "add todo",
            {"text": "list todos"},
            {"id": "mh1", "text": "dup"},
            None,
            42,
            {"id": "bad id!", "text": "odd"},
            {"id": "MH-9"},
            {"id": "SC-1", "text": "wrong prefix"},
        ],
        "success_criteria": "a user can add a todo",
        "deliverable": "spaceship",
    }
    scenarios = _scenarios_covering("mh-1", " MH-02 ", "sc1", "bogus", 7, None, " Odd ")
    writes, cas, critique = _run(
        _plan(
            intent=intent,
            tickets=_tickets_with(
                T_002={"scenarios": scenarios},
            ),
        )
    )
    [brief] = _bodies(cas, writes, "intent_brief")
    assert brief.must_have_ids == ("MH-1", "MH-2", "MH-3", "MH-4", "MH-5", "MH-6")
    assert brief.must_haves == ("add todo", "list todos", "dup", "42", "odd", "wrong prefix")
    assert brief.success_criterion_ids == ("SC-1",)
    assert brief.deliverable == ""  # unknown -> undeclared, treated as an app
    findings = critique.coverage_findings
    # Unknown covers entries are dropped with a note; a goal's exact text maps to its id.
    assert not any("covers unknown id" in f for f in findings)
    assert (
        "Scenario AC-001: dropped unknown covers id(s) 'bogus', '7' (declared ids: MH-1, "
        "MH-2, MH-3, MH-4, MH-5, MH-6, SC-1)."
    ) in writes["agent_journal"][0].concerns
    criteria = {c.id: c for c in _bodies(cas, writes, "acceptance_criterion")}
    assert criteria["AC-001"].covers == ("MH-1", "MH-2", "SC-1", "MH-5")
    uncovered = [f.split()[1] for f in findings if "has no covering scenario" in f]
    assert uncovered == ["MH-3", "MH-4", "MH-6"]
    assert any(f.startswith("stack.run is empty but the deliverable is an app") for f in findings)


# ---- 2. every scenario owned, every feature ticket specified -----------------------------


_SEQ = Seq("r", 1, 1, "planner", 0)


def _brief(**kw: Any) -> IntentBrief:
    return IntentBrief(intent="x", seq=_SEQ, **kw)


def test_a_scenario_on_no_live_ticket_is_an_orphan_and_covers_nothing() -> None:
    entry = Ticket(id="T-1", title="app", specialty=Specialty.BACKEND, entrypoint=True, seq=_SEQ)
    criteria = [
        AcceptanceCriterion(id="AC-1", text="t", ticket_id="T-1", seq=_SEQ),
        AcceptanceCriterion(id="AC-9", text="t", ticket_id="T-404", covers=("MH-1",), seq=_SEQ),
    ]
    findings = _coverage_findings(
        [entry],
        criteria,
        _brief(must_haves=("m",), must_have_ids=("MH-1",)),
        run=None,
    )
    assert findings == (
        "Scenario AC-9 is not owned by a live ticket (T-404 is not in the plan); put it "
        "on a ticket of the plan.",
        "Must-have MH-1 ('m') has no covering scenario; add one whose covers lists MH-1.",
    )


def test_a_feature_ticket_without_scenarios_is_a_finding_test_tickets_exempt() -> None:
    extra = {
        "id": "T-004",
        "title": "Export",
        "specialty": "backend",
        "difficulty": "standard",
        "depends_on": [],
        "files": ["todo/export.py"],
        "entrypoint": False,
        "scenarios": [],
    }
    tickets = _tickets_with(T_002={"depends_on": ["T-001", "T-004"]})
    findings = _coverage(_plan(tickets=[*tickets, extra]))
    assert findings == (
        "Feature ticket T-004 has no scenarios; give it at least one (or fold it into "
        "the ticket whose behaviour it serves).",
    )  # T-003 (test) and the generated tickets carry none and are exempt


# ---- 3. one entrypoint wires everything; an app has a run command ---------------------------


def test_an_entrypoint_must_depend_on_every_feature_ticket() -> None:
    findings = _coverage(
        _plan(
            tickets=_tickets_with(
                T_001={"entrypoint": True},
                T_002={"entrypoint": False},
            )
        )
    )
    assert findings == (
        "Entrypoint ticket T-001 does not depend (directly or transitively) on feature "
        "ticket(s) T-002; it must wire every feature in.",
    )
    # A test ticket never counts as the entrypoint.
    findings = _coverage(
        _plan(
            tickets=_tickets_with(
                T_002={"entrypoint": False},
                T_003={"entrypoint": True},
            )
        )
    )
    assert findings[0].startswith("No entrypoint ticket")


def _app(run: str) -> dict[str, Any]:
    return _plan(
        intent={**PLAN["intent"], "deliverable": "app"}, stack={**PYTHON_STACK, "run": run}
    )


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


def test_inline_cap_overflow_is_a_concern_naming_the_dropped_tickets() -> None:
    entry = _copy(PLAN["tickets"][1])
    filler = [
        {
            **_copy(PLAN["tickets"][0]),
            "id": f"T-{i:03d}",
            "title": f"part {i}",
            "files": [f"todo/p{i}.py"],
            "implements": [],
            "scenarios": [{**PLAN["tickets"][0]["scenarios"][0], "id": f"AC-{i:03d}"}],
        }
        for i in range(100, 100 + MAX_TICKETS + 1)
    ]
    filler[-1]["files"] = ["todo/p100.py"]  # T-140 shares a file with kept T-100
    writes, cas, critique = _run(_plan(tickets=[entry, *filler]))
    assert not any("exceed the cap" in f for f in critique.coverage_findings)
    concerns = writes["agent_journal"][0].concerns
    assert (
        f"{MAX_TICKETS + 2} tickets exceed the cap of {MAX_TICKETS}; dropped T-139 (part "
        "139), T-140 (part 140): consolidate tickets (or split a larger build into epics) "
        "so no work is lost."
    ) in concerns
    # A dropped ticket's scenarios move to the kept ticket owning its files, else drop.
    assert (
        "Moved scenario(s) AC-140 of dropped ticket T-140 to T-100, which owns its "
        "files." in concerns
    )
    assert (
        "Dropped scenario(s) AC-139 with ticket T-139: no kept ticket owns its files." in concerns
    )
    owner = {c.id: c.ticket_id for c in _bodies(cas, writes, "acceptance_criterion")}
    assert owner["AC-140"] == "T-100" and "AC-139" not in owner


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
    # Empty deltas fall back to repairs; the in-node repair and its fresh re-plan
    # return the same plan, so the finding reaches the gate.
    rt, tx = runtime_for(uncovered, {}, uncovered, uncovered, {}, PLAN, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 4
    assert "Plan check findings:\\n- Must-have MH-1" in json.dumps(tx.requests[2])
    assert "Plan check findings:\\n- Must-have MH-1" in json.dumps(tx.requests[3])

    doc = _criteria_payload({"artifacts": first["artifacts"]}, cas.resolve)
    assert isinstance(doc, dict)
    assert doc["critique"]["coverage_findings"] == [
        "Must-have MH-1 ('add todo') has no covering scenario; add one whose covers lists MH-1."
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
    assert "REPAIR PASS" in prompt
    assert "coverage: Must-have MH-1 ('add todo') has no covering scenario" in prompt
    assert "MH-1 (must-have): add todo" in prompt  # the ids to keep
    [critique] = [cas.resolve(a) for a in second["artifacts"] if a.kind == "criteria_findings"]
    assert critique.coverage_findings == ()  # the revision covered it


def test_an_unpaired_feature_ticket_gets_a_step_module_to_bind_its_scenarios() -> None:
    def ticket(
        tid: str,
        specialty: Specialty,
        scenarios: tuple[str, ...],
        files: tuple[str, ...] = ("app.py",),
        deps: tuple[str, ...] = (),
    ) -> Ticket:
        return Ticket(
            id=tid,
            title=tid,
            specialty=specialty,
            scenario_ids=scenarios,
            files=files,
            depends_on=deps,
            seq=_SEQ,
        )

    tickets = [
        ticket("E1-T01", Specialty.BACKEND, ("AC-1",)),  # unpaired
        ticket("E1-T02", Specialty.BACKEND, ("AC-2",)),  # paired below
        ticket("E1-T03", Specialty.TEST, ("AC-2",), ("tests/test_t02.py",), ("E1-T02",)),
        ticket("E1-T04", Specialty.BACKEND, ("AC-4",), ("app.py", "tests/test_own.py")),
        ticket("E1-T05", Specialty.BACKEND, ()),  # no scenarios
        ticket("E1-T06", Specialty.BACKEND, ("AC-6",), ()),  # acceptance-only
    ]
    python = StackDecision(language="python", test="pytest")
    out, concerns = _own_scenario_bindings(tickets, python)
    files = {t.id: t.files for t in out}
    assert files["E1-T01"] == ("app.py", "tests/test_e1_t01.py")
    assert files["E1-T02"] == files["E1-T05"] == ("app.py",)
    assert files["E1-T04"] == ("app.py", "tests/test_own.py")
    assert files["E1-T06"] == ()
    assert len(concerns) == 1
    cucumber = python.model_copy(update={"bdd": "behave"})
    assert _own_scenario_bindings(tickets, cucumber) == (tickets, [])


def test_a_test_ticket_with_scenarios_but_no_test_module_gets_one() -> None:
    # The plan's own bdd-harness, renamed bdd-harness-2, held scenario C1 as a test
    # ticket with no test_*.py: the feature ticket counted it as the binder, so
    # nothing bound C1.
    feature = Ticket(
        id="contracts-2",
        title="c",
        specialty=Specialty.BACKEND,
        scenario_ids=("C1",),
        files=("contracts_extra.py",),
        seq=_SEQ,
    )
    harness = Ticket(
        id="bdd-harness-2",
        title="h",
        specialty=Specialty.TEST,
        scenario_ids=("C1",),
        files=("conftest.py",),
        depends_on=("contracts-2",),
        seq=_SEQ,
    )
    no_scenarios = Ticket(id="T-9", title="t", specialty=Specialty.TEST, seq=_SEQ)
    out, concerns = _own_scenario_bindings(
        [feature, harness, no_scenarios], StackDecision(language="python", test="pytest")
    )
    files = {t.id: t.files for t in out}
    assert files["bdd-harness-2"] == ("conftest.py", "tests/test_bdd_harness_2.py")
    assert files["contracts-2"] == ("contracts_extra.py",)  # bound by the test ticket
    assert files["T-9"] == ()
    assert len(concerns) == 1


def _t(
    tid: str,
    specialty: Specialty,
    scenarios: tuple[str, ...] = (),
    files: tuple[str, ...] = ("app.py",),
    deps: tuple[str, ...] = (),
) -> Ticket:
    return Ticket(
        id=tid,
        title=tid,
        specialty=specialty,
        scenario_ids=scenarios,
        files=files,
        depends_on=deps,
        seq=_SEQ,
    )


def test_a_trailing_test_ticket_hands_over_only_what_its_last_feature_builds() -> None:
    # The local shop bench: E2-T04 bound all 30 scenarios of the chain E2-T01..T03
    # from four layer-named modules. E2-T03's lose nothing by moving to E2-T03; the
    # earlier tickets' would be judged before the code they exercise exists.
    tickets = [
        _t("E2-T01", Specialty.BACKEND, ("A1",), ("app/models/product.py",)),
        _t("E2-T02", Specialty.BACKEND, ("A2",), ("app/models/cart.py",), ("E2-T01",)),
        _t("E2-T03", Specialty.FRONTEND, ("A3",), ("app/routes/cart.py",), ("E2-T02",)),
        _t(
            "E2-T04",
            Specialty.TEST,
            ("A1", "A2", "A3"),
            ("tests/test_cart_routes.py", "tests/test_cart_service.py"),
            ("E2-T01", "E2-T02", "E2-T03"),
        ),
    ]
    python = StackDecision(language="python", test="pytest")
    folded, concerns = _fold_test_bindings(tickets, python)
    by_id = {t.id: t for t in folded}
    assert by_id["E2-T04"].scenario_ids == ("A1", "A2")
    assert len(concerns) == 1 and "A3" in concerns[0]
    bound, _ = _own_scenario_bindings(folded, python)
    trimmed, _ = _one_step_module_per_ticket(bound, python)
    files = {t.id: t.files for t in trimmed}
    assert files["E2-T03"] == ("app/routes/cart.py", "tests/test_e2_t03.py")
    assert files["E2-T01"] == ("app/models/product.py",)
    assert files["E2-T04"] == ("tests/test_cart_routes.py",)
    # A Cucumber stack keeps its paired test tickets.
    cucumber = python.model_copy(update={"bdd": "behave"})
    assert _fold_test_bindings(tickets, cucumber) == (tickets, [])


def test_a_models_tickets_user_level_scenario_stays_after_the_routes() -> None:
    # The coverage rule makes the models ticket carry a scenario too, e.g. "When I
    # GET /products": judged with the models ticket, no route would exist yet.
    tickets = [
        _t("M", Specialty.DATA, ("GET-products",), ("app/models.py",)),
        _t("R", Specialty.BACKEND, ("A2",), ("app/routes.py",), ("M",)),
        _t("T", Specialty.TEST, ("GET-products", "A2"), ("tests/test_steps.py",), ("M", "R")),
    ]
    out, _ = _fold_test_bindings(tickets, StackDecision(language="python"))
    by_id = {t.id: t for t in out}
    assert by_id["T"].scenario_ids == ("GET-products",)
    assert by_id["M"].files == ("app/models.py",)


def test_one_to_one_test_tickets_are_dropped_and_their_dependents_rewired() -> None:
    tickets = [
        _t("F1", Specialty.BACKEND, ("A1",)),
        _t("F2", Specialty.BACKEND, ("A2",), ("api.py",), ("F1",)),
        _t("P1", Specialty.TEST, ("A1",), ("tests/test_p1.py",), ("F1",)),
        _t("P2", Specialty.TEST, ("A2",), ("tests/test_p2.py",), ("F2",)),
        _t("F5", Specialty.BACKEND, ("A5",), ("cli.py",), ("P1", "P2")),
    ]
    out, concerns = _fold_test_bindings(tickets, StackDecision(language="python"))
    by_id = {t.id: t for t in out}
    assert set(by_id) == {"F1", "F2", "F5"}
    assert by_id["F5"].depends_on == ("F1", "F2")
    assert all("dropped the ticket" in c for c in concerns) and len(concerns) == 2


def test_a_test_ticket_keeps_what_no_code_ticket_can_bind_and_its_fixtures() -> None:
    smoke = StackDecision(language="python", test="pytest", run="python3 tests/test_smoke.py")
    tickets = [
        _t("F1", Specialty.BACKEND, ("A1",)),
        _t("F2", Specialty.BACKEND, ("A2",), ()),  # acceptance-only: nowhere to bind
        # Its own A9 (a criterion no code ticket owns) plus F1's and F2's.
        _t("T3", Specialty.TEST, ("A9", "A1", "A2"), ("tests/test_t3.py",), ("F1", "F2")),
        # Step modules that lean on fixtures, helpers or a stack-command file stay paired.
        _t("T4", Specialty.TEST, ("A1",), ("tests/conftest.py", "tests/test_t4.py"), ("F1",)),
        _t("T5", Specialty.TEST, ("A1",), ("tests/test_t5.py", "tests/test_helpers.py"), ("F1",)),
        _t("T6", Specialty.TEST, ("A1",), ("tests/test_smoke.py",), ("F1",)),
    ]
    out, concerns = _fold_test_bindings(tickets, smoke)
    by_id = {t.id: t for t in out}
    assert by_id["T3"].scenario_ids == ("A9", "A2")
    assert [by_id[t].scenario_ids for t in ("T4", "T5", "T6")] == [("A1",)] * 3
    assert {t.files for t in out} == {t.files for t in tickets}
    assert len(concerns) == 1


def test_a_scenario_its_feature_ticket_binds_itself_leaves_the_test_ticket() -> None:
    # Bound by F1's own step module and the test ticket's, it would run twice.
    tickets = [
        _t("F1", Specialty.BACKEND, ("A1",), ("app.py", "tests/test_f1.py")),
        _t("F2", Specialty.BACKEND, ("A2",), ("api.py",)),
        _t("T", Specialty.TEST, ("A1", "A2"), ("tests/conftest.py",), ("F1", "F2")),
    ]
    out, _ = _fold_test_bindings(tickets, StackDecision(language="python"))
    assert {t.id: t for t in out}["T"].scenario_ids == ("A2",)


def test_a_test_ticket_implementing_a_contract_is_flagged_not_emptied() -> None:
    contract = _t("T", Specialty.TEST, ("A1",), ("tests/test_t.py",), ("F1",))
    contract = contract.model_copy(update={"implements": ("Fixture",)})
    tickets = [_t("F1", Specialty.BACKEND, ("A1",)), contract]
    out, concerns = _fold_test_bindings(tickets, StackDecision(language="python"))
    assert out == tickets
    assert len(concerns) == 1 and "implements contract(s) Fixture" in concerns[0]


def test_a_ticket_with_scenarios_keeps_one_step_module() -> None:
    smoke = StackDecision(language="python", test="pytest", run="python3 tests/test_smoke.py")
    tickets = [
        _t(
            "F1",
            Specialty.BACKEND,
            ("A1",),
            ("app/cart.py", "tests/test_cart_routes.py", "tests/test_cart_service.py"),
        ),
        # The ticket-named module binds, wherever it is listed.
        _t("F2", Specialty.BACKEND, ("A2",), ("tests/test_a.py", "tests/test_f2.py")),
        # Else a module named for steps; helpers and stack-command files stay.
        _t(
            "F3",
            Specialty.BACKEND,
            ("A3",),
            (
                "tests/test_unit.py",
                "tests/test_cart_steps.py",
                "tests/test_helpers.py",
                "tests/test_smoke.py",
            ),
        ),
        # No scenarios: its test modules are plain unit tests.
        _t("F4", Specialty.BACKEND, (), ("app/x.py", "tests/test_u1.py", "tests/test_u2.py")),
    ]
    out, concerns = _one_step_module_per_ticket(tickets, smoke)
    files = {t.id: t.files for t in out}
    assert files["F1"] == ("app/cart.py", "tests/test_cart_routes.py")
    assert files["F2"] == ("tests/test_f2.py",)
    assert files["F3"] == (
        "tests/test_cart_steps.py",
        "tests/test_helpers.py",
        "tests/test_smoke.py",
    )
    assert files["F4"] == tickets[3].files
    assert len(concerns) == 3 and "test_cart_service.py" in concerns[0]
    js = StackDecision(language="javascript", bdd="npx cucumber-js")
    assert _one_step_module_per_ticket(tickets, js) == (tickets, [])


def test_the_ticket_rules_put_step_modules_with_the_feature_ticket() -> None:
    # The step module is added in code (_own_scenario_bindings): the model never writes it.
    assert "ONE step module tests/test_<ticket id>.py that is added to its files[] for you" in SOP
    assert "never list a test module for them" in SOP and "never on a models-only ticket" in SOP


def test_a_scenario_asserting_dependency_install_is_a_finding() -> None:
    # The gates run offline: "installing dependencies succeeds" can never pass.
    def c(cid: str, then: str) -> AcceptanceCriterion:
        return AcceptanceCriterion(
            id=cid,
            text="t",
            given=("requirements.txt exists",),
            when=("dependencies are pinned",),
            then=(then,),
            ticket_id="T-1",
            seq=_SEQ,
        )

    findings = _offline_scenario_findings(
        [
            c("C1", "installing dependencies succeeds with no version conflicts"),
            c("C2", "`pip install -r requirements.txt` exits 0"),
            c("C3", "every entry in requirements.txt is pinned with =="),
            c("C4", "the user sees the installation wizard"),
        ]
    )
    assert [f.split()[1] for f in findings] == ["C1", "C2"]
    assert "offline" in findings[0]


def test_the_python_deps_rule_forbids_standard_library_modules() -> None:
    # A model pinned sqlite3==2.6.0 in requirements.txt.
    rule = _python_deps_rule(StackDecision(language="python"), "requirements.txt")
    assert "standard-library" in rule and "sqlite3" in rule


def test_the_sop_adds_bdd_harness_only_for_a_bdd_stack_and_reserves_its_id() -> None:
    assert "only when stack.bdd is set" in SOP
    assert "reserved: never plan a ticket with either" in SOP
    assert "never that installing succeeds" in SOP


def test_a_python_sqlite_plan_pins_the_memory_connection_constraint() -> None:
    python = StackDecision(language="python")
    [pc] = _stack_constraints(python, {"design": {"body": "Store notes in SQLite"}})
    assert ":memory:" in pc["text"] and "ONE connection" in pc["text"]
    # That one database outlives each test: two modules inserting one username collided.
    assert "autouse fixture must give each test an empty database" in pc["text"]
    assert _stack_constraints(python, {"design": {"body": "Store notes in Postgres"}}) == []
    assert _stack_constraints(StackDecision(language="javascript"), {"x": "sqlite"}) == []


def test_a_python_sqlalchemy_plan_pins_the_typed_model_constraint() -> None:
    # Column(Integer) attributes typed as Column[int] failed 12 mypy checks in a run.
    python = StackDecision(language="python")
    pcs = _stack_constraints(python, {"design": {"body": "SQLAlchemy models over SQLite"}})
    assert [pc["id"] for pc in pcs] == [
        "PC-python-sqlite-memory",
        "PC-python-sqlalchemy-typed",
        "PC-python-sqlalchemy-relationships",
        "PC-python-sqlalchemy-sqlite",
    ]
    assert "Mapped[int] = mapped_column(" in pcs[1]["text"]
    # The local shop bench: the memory-connection constraint led auth.py to a raw
    # sqlite3.connect(':memory:') handed to Base.metadata.create_all.
    assert "StaticPool" in pcs[3]["text"] and "never through sqlite3.connect()" in pcs[3]["text"]
    assert _stack_constraints(python, {"design": {"body": "SQLAlchemy on Postgres"}}) == pcs[1:3]
    assert _stack_constraints(StackDecision(language="go"), {"x": "sqlalchemy"}) == []


def test_a_python_sqlalchemy_plan_pins_one_sided_relationships_across_tickets() -> None:
    # cart.py wrote relationship("User", back_populates="cart_items") but user.py was
    # another ticket's: no User.cart_items, so no mapper configured and every test failed.
    pcs = _stack_constraints(StackDecision(language="python"), {"x": "sqlalchemy"})
    [rel] = [pc for pc in pcs if pc["id"] == "PC-python-sqlalchemy-relationships"]
    assert "`back_populates=` or `backref=` only" in rel["text"]
    assert "BOTH mapped classes are in this ticket's files" in rel["text"]
    assert "one-sided `relationship()`" in rel["text"]


def test_manifest_and_app_install_scenarios_are_not_offline_findings() -> None:
    def c(cid: str, when: str, then: str) -> AcceptanceCriterion:
        return AcceptanceCriterion(
            id=cid, text="t", given=("x",), when=(when,), then=(then,), ticket_id="T-1", seq=_SEQ
        )

    findings = _offline_scenario_findings(
        [
            c(
                "C1",
                "requirements.txt is read",
                "every package in requirements.txt is pinned to an exact version "
                "for a reproducible install",
            ),
            c("C2", "the admin installs the analytics plugin package", "the plugin is listed"),
            c("C3", "I run pip install -r requirements.txt", "it succeeds"),
        ]
    )
    assert [f.split()[1] for f in findings] == ["C3"]
