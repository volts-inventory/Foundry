"""Scenario binding: agents bind the gate-owned features/<ticket>.feature, never a
feature file of their own; a wave check runs only the scenarios it can judge; the
fixer sees BDD failures grouped by feature file; every script a stack command runs
has an owning ticket; a failed smoke run says why."""

from __future__ import annotations

import sys
from pathlib import Path

from foundry.agents.implementation.tickets import (
    group_failures_by_ticket,
    in_write_scope,
    scenario_rules,
    scenario_verify,
)
from foundry.agents.planner import _build_tickets, _own_stack_files, stack_files
from foundry.gates.smoke import SmokeGate
from foundry.gates.test import BDD_KEY, TestGate
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, StackDecision, Ticket
from foundry.state.seq import Seq
from tests.test_bdd import _STEPS, _project

_SEQ = Seq("run", 1, 1, "planner", 0)


def _ticket(tid: str, files: tuple[str, ...], **kw: object) -> Ticket:
    return Ticket(id=tid, title=tid, specialty=Specialty.BACKEND, files=files, seq=_SEQ,
                  **kw)  # type: ignore[arg-type]


# ---- agents never write feature files ------------------------------------------


def test_no_ticket_may_write_a_feature_file_anywhere() -> None:
    assert not in_write_scope("tests/features/store.feature", ("tests/",))
    assert not in_write_scope("features/T-1.feature", ())
    assert in_write_scope("tests/steps/test_store.py", ("tests/",))


def test_the_planner_drops_feature_files_from_ticket_scopes() -> None:
    raw = [{"id": "T1", "specialty": "test",
            "files": ["tests/features/store.feature", "tests/steps/test_store.py"]}]
    tickets, _, concerns = _build_tickets(raw, _SEQ, _SEQ, set())
    assert tickets[0].files == ("tests/steps/test_store.py",)
    assert any("tests/features/store.feature" in c for c in concerns)


# ---- packet rules per stack ------------------------------------------------------


def test_cucumber_tickets_run_their_scenarios_through_the_bdd_command() -> None:
    stack = StackDecision(language="typescript", test="npm test",
                          bdd="npx cucumber-js --format message:$FOUNDRY_BDD_REPORT")
    rules = " ".join(scenario_rules(("features/steps/t1.steps.ts",),
                                    {"features/T1.feature": ["AC-1"]}, stack))
    assert "features/T1.feature (Scenario: AC-1)" in rules
    assert "npx cucumber-js" in rules and "undefined or skipped step" in rules
    assert scenario_verify(("features/steps/t1.steps.ts",), {"features/T1.feature": ["AC-1"]},
                           stack) == {}


def test_a_test_ticket_is_never_told_a_paired_test_ticket_binds_its_scenarios() -> None:
    python = StackDecision(language="python", test="pytest")
    scenarios = {"features/T1.feature": ["AC-1"]}
    feature = " ".join(scenario_rules(("app.py",), scenarios, python))
    assert "A paired test ticket binds" in feature
    test = " ".join(scenario_rules(("conftest.py",), scenarios, python, test=True))
    assert "paired test ticket" not in test
    assert 'scenarios("../features/T1.feature")' in test


def test_a_ticket_without_scenarios_gets_no_scenario_rules() -> None:
    assert scenario_rules(("app.py",), {}, None) == []


# ---- the wave check's scoped Test gate -------------------------------------------


def test_a_scoped_gate_judges_only_its_scenarios(tmp_path: Path) -> None:
    root = _project(tmp_path, _STEPS.replace("{expected}", "n"))
    (root / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    gate = TestGate(scope=frozenset({"features/T1.feature::AC-1"}))
    obs = gate.execute(root)
    assert obs.structured[BDD_KEY] == {"features/T1.feature::AC-1": "passed"}
    # Only the step module that binds the feature ran: test_broken.py is not judged.
    assert "tests/test_t1.py" in obs.structured["cmd"]
    assert "test_broken.py" not in obs.structured["cmd"]
    assert gate.decide(obs) is GateStatus.PASS


def test_a_scoped_gate_fails_an_unbound_scenario_without_running_anything(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, None)
    gate = TestGate(scope=frozenset({"features/T1.feature::AC-2"}))
    obs = gate.execute(root)
    assert obs.structured[BDD_KEY] == {"features/T1.feature::AC-2": "missing"}
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("features/T1.feature::AC-2 was not executed" in f for f in obs.findings)


def test_a_binding_in_conftest_is_named_as_never_collected(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "conftest.py").write_text(_STEPS.replace("{expected}", "n"))
    gate = TestGate()
    obs = gate.execute(root)
    # (pytest-bdd may also fail to import the conftest: either way nothing ran.)
    assert set(obs.structured[BDD_KEY].values()) == {"missing"}
    assert any(
        f.startswith("tests/conftest.py call(s) pytest-bdd `scenarios(...)`")
        and "never collected as a test module" in f
        for f in obs.findings
    ), obs.findings
    assert any("module pytest collects, tests/test_<name>.py" in f for f in obs.findings)


def test_a_conftest_import_error_is_an_error_that_says_why(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "conftest.py").write_text("from app.store import Repo\n")
    gate = TestGate()
    obs = gate.execute(root)
    assert obs.exit_codes == (4,)
    assert gate.decide(obs) is GateStatus.ERROR
    why = [f for f in obs.findings if not f.startswith("BDD criterion")]
    assert why and why[0].startswith("pytest usage error"), obs.findings
    assert "test command exited 4" in why[0] and "app" in why[0]


def test_no_collected_test_module_fails_with_a_hint(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "test_plain.py").unlink()
    gate = TestGate()
    obs = gate.execute(root)
    assert obs.exit_codes == (5,)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any(f.startswith("no test modules collected: add tests/test_<name>.py")
               and "feature files" in f and "test command exited 5" in f
               for f in obs.findings), obs.findings


def test_a_scoped_scenario_absent_from_the_features_fails_closed(tmp_path: Path) -> None:
    gate = TestGate(scope=frozenset({"features/T9.feature::AC-9"}))
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "n")))
    assert obs.structured[BDD_KEY] == {"features/T9.feature::AC-9": "missing"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_the_scope_is_part_of_the_reuse_key() -> None:
    a = TestGate(scope=frozenset({"features/T1.feature::AC-1"}))
    b = TestGate(scope=frozenset({"features/T1.feature::AC-2"}))
    assert len({a.scanner_rule_version, b.scanner_rule_version,
                TestGate().scanner_rule_version}) == 3


# ---- the fixer sees failures grouped by feature file ---------------------------


def test_bdd_failures_are_grouped_by_feature_file_and_owning_tickets() -> None:
    why = "was not executed — bind it"
    failures = [
        "[build gate FAILED]", "build command exited 1",
        "[test gate FAILED]",
        *(f"BDD criterion E1-AC0{i}: scenario features/E1-T02.feature::E1-AC0{i} {why}"
          for i in (2, 3)),
        f"BDD criterion E2-AC01: scenario features/E2-T01.feature::E2-AC01 {why}",
        "FAILED tests/test_x.py::test_y",
    ]
    tickets = [
        _ticket("E1-T02", ("ft/validation.py",), scenario_ids=("E1-AC02", "E1-AC03")),
        _ticket("E1-T08", ("tests/steps/test_validation.py",),
                scenario_ids=("E1-AC02", "E1-AC03")),
    ]
    out = group_failures_by_ticket(failures, tickets)
    assert out == (
        "[build gate FAILED]", "build command exited 1", "[test gate FAILED]",
        "[BDD failures by feature file: 2 group(s)]",
        f"[features/E1-T02.feature] scenarios E1-AC02, E1-AC03 {why} — ticket(s): "
        "E1-T02 (ft/validation.py), E1-T08 (tests/steps/test_validation.py)",
        f"[features/E2-T01.feature] scenarios E2-AC01 {why} — ticket(s): no ticket",
        "FAILED tests/test_x.py::test_y",
    )
    assert group_failures_by_ticket(failures[:2], tickets) == tuple(failures[:2])


# ---- stack command files have an owner ------------------------------------------


def test_stack_files_are_the_scripts_the_commands_run() -> None:
    stack = StackDecision(language="python", test="python3 -m pytest -q",
                          run="python3 ./smoke.py --ticks 3 && sh scripts/check.sh")
    assert stack_files(stack) == ("smoke.py", "scripts/check.sh")


def test_an_unowned_run_script_goes_to_the_entrypoint_ticket() -> None:
    stack = StackDecision(language="python", run="python3 smoke.py")
    tickets = [_ticket("T1", ("app/__init__.py", "app/core.py")),
               _ticket("T2", ("app/main.py",), entrypoint=True)]
    out, concerns, findings = _own_stack_files(tickets, stack)
    assert out[1].files == ("app/main.py", "smoke.py") and "smoke.py" in out[1].description
    assert out[0] == tickets[0] and concerns and findings == ()
    # Already owned (here through a directory scope): nothing changes.
    owned = [_ticket("T1", ("scripts/",)), _ticket("T2", ("main.py",), entrypoint=True)]
    stack = StackDecision(language="python", run="sh scripts/smoke.sh")
    assert _own_stack_files(owned, stack) == (owned, [], ())


def test_an_unowned_run_script_without_an_entrypoint_is_a_coverage_finding() -> None:
    stack = StackDecision(language="python", run="python3 smoke.py")
    _, _, findings = _own_stack_files([_ticket("T1", ("app.py",))], stack)
    assert findings and "smoke.py" in findings[0]


def test_unowned_python_package_markers_get_an_owner() -> None:
    # The finance-tracker trial: no ticket wrote finance/__init__.py, so mypy saw
    # finance/contracts.py as both `contracts` and `finance.contracts`.
    stack = StackDecision(language="python")
    tickets = [_ticket("contracts", ("finance/contracts.py",)),
               _ticket("T1", ("finance/store/db.py", "tests/steps/test_db.py"))]
    out, concerns, _ = _own_stack_files(tickets, stack)
    assert out[0].files == ("finance/contracts.py", "finance/__init__.py",
                            "finance/store/__init__.py")
    assert out[1] == tickets[1] and concerns
    # Without a contracts ticket the first ticket writing there owns it; owned
    # markers and non-Python stacks are left alone.
    out, _, _ = _own_stack_files([tickets[1]], stack)
    assert out[0].files[-2:] == ("finance/__init__.py", "finance/store/__init__.py")
    done = [_ticket("T1", ("pkg/__init__.py", "pkg/a.py"))]
    assert _own_stack_files(done, stack)[0] == done
    ts = [_ticket("T1", ("src/a.ts",))]
    assert _own_stack_files(ts, StackDecision(language="typescript"))[0] == ts


# ---- a failed smoke run carries its reason ----------------------------------------


def test_a_failed_smoke_run_names_the_exit_code_and_error(tmp_path: Path) -> None:
    gate = SmokeGate((sys.executable, "smoke.py"))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.findings[0].startswith("smoke command exited 2")
    assert any("smoke.py" in f and "No such file" in f for f in obs.findings)


def test_a_smoke_command_that_never_exits_is_named_a_server(tmp_path: Path) -> None:
    (tmp_path / "serve.py").write_text("import time\nprint('listening', flush=True)\n"
                                       "time.sleep(60)\n")
    gate = SmokeGate((sys.executable, "serve.py"), timeout_s=2)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.ERROR
    assert "long-running server" in obs.findings[0]
    assert "port 0" in obs.findings[0] and "exits 0" in obs.findings[0]
    assert any("listening" in f for f in obs.findings)


def test_the_default_smoke_timeout_is_five_minutes() -> None:
    assert SmokeGate(("true",))._timeout_s == 300.0
