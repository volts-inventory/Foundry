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
    return Ticket(id=tid, title=tid, specialty=Specialty.BACKEND, files=files, seq=_SEQ, **kw)  # type: ignore[arg-type]


# ---- agents never write feature files ------------------------------------------


def test_no_ticket_may_write_a_feature_file_anywhere() -> None:
    assert not in_write_scope("tests/features/store.feature", ("tests/",))
    assert not in_write_scope("features/T-1.feature", ())
    assert in_write_scope("tests/steps/test_store.py", ("tests/",))


def test_the_planner_drops_feature_files_from_ticket_scopes() -> None:
    raw = [
        {
            "id": "T1",
            "specialty": "test",
            "files": ["tests/features/store.feature", "tests/steps/test_store.py"],
        }
    ]
    tickets, _, concerns = _build_tickets(raw, _SEQ, _SEQ, set())
    assert tickets[0].files == ("tests/steps/test_store.py",)
    assert any("tests/features/store.feature" in c for c in concerns)


# ---- packet rules per stack ------------------------------------------------------


def test_cucumber_tickets_run_their_scenarios_through_the_bdd_command() -> None:
    stack = StackDecision(
        language="typescript",
        test="npm test",
        bdd="npx cucumber-js --format message:$FOUNDRY_BDD_REPORT",
    )
    rules = " ".join(
        scenario_rules(("features/steps/t1.steps.ts",), {"features/T1.feature": ["AC-1"]}, stack)
    )
    assert "features/T1.feature (Scenario: AC-1)" in rules
    assert "npx cucumber-js" in rules and "undefined or skipped step" in rules
    assert (
        scenario_verify(("features/steps/t1.steps.ts",), {"features/T1.feature": ["AC-1"]}, stack)
        == {}
    )


def test_a_test_ticket_is_never_told_a_paired_test_ticket_binds_its_scenarios() -> None:
    python = StackDecision(language="python", test="pytest")
    scenarios = {"features/T1.feature": ["AC-1"]}
    feature = " ".join(scenario_rules(("app.py",), scenarios, python))
    assert "A paired test ticket binds" in feature
    test = " ".join(scenario_rules(("conftest.py",), scenarios, python, test=True))
    assert "paired test ticket" not in test
    assert 'scenarios("../features/T1.feature")' in test


def test_each_feature_file_is_bound_from_exactly_one_step_module() -> None:
    # Told to bind from each of four modules, a model wrote one step module four
    # times and every scenario ran 4x.
    python = StackDecision(language="python", test="pytest")
    scenarios = {"features/T1.feature": ["AC-1"], "features/T2.feature": ["AC-2"]}
    files = ("tests/test_routes.py", "tests/test_t1.py", "tests/test_helpers.py")
    rules = " ".join(scenario_rules(files, scenarios, python, test=True, ticket_id="T1"))
    assert (
        "in tests/test_t1.py ONLY, the path relative to it: "
        'scenarios("../features/T1.feature") scenarios("../features/T2.feature")'
    ) in rules
    assert "tests/test_routes.py, tests/test_helpers.py must not call scenarios()" in rules
    verify = scenario_verify(files, scenarios, python, "T1")["scenarios"]
    assert verify.endswith("pytest -rA tests/test_t1.py")
    assert "test_routes.py" not in verify
    # Without a ticket-named module, the first one that is not a helper.
    first = scenario_verify(
        ("tests/test_helpers.py", "tests/test_a.py", "tests/test_b.py"), scenarios, python
    )["scenarios"]
    assert first.endswith("-rA tests/test_a.py")


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


def test_a_scoped_gate_runs_a_step_module_under_the_features_dir(tmp_path: Path) -> None:
    # A cloud plan put the steps in features/step_definitions/: the scoped run skipped
    # them and reported every scenario "not executed" though plain pytest passed them.
    root = _project(tmp_path, None)
    steps = root / "features" / "step_definitions" / "test_t1_steps.py"
    steps.parent.mkdir()
    steps.write_text(
        _STEPS.replace("{expected}", "n").replace("../features/T1.feature", "../T1.feature")
    )
    gate = TestGate(scope=frozenset({"features/T1.feature::AC-1"}))
    obs = gate.execute(root)
    assert "features/step_definitions/test_t1_steps.py" in obs.structured["cmd"]
    assert obs.structured[BDD_KEY] == {"features/T1.feature::AC-1": "passed"}, obs.stdout
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


def test_a_binding_module_that_fails_to_import_is_named_not_rebound(
    tmp_path: Path,
) -> None:
    # The notes build's step module bound the feature but imported bcrypt, which was
    # never declared: the gate said "bind that exact file", and the fixer rewrote the
    # binding that was already there instead of declaring the dependency.
    steps = "import bcrypt_not_installed_here\n" + _STEPS.replace("{expected}", "n")
    root = _project(tmp_path, steps)
    (root / "requirements.txt").write_text("pytest-bdd\n")
    for gate in (TestGate(), TestGate(scope=frozenset({"features/T1.feature::AC-1"}))):
        obs = gate.execute(root)
        assert set(obs.structured[BDD_KEY].values()) == {"missing"}
        ac1 = next(f for f in obs.findings if f.startswith("BDD criterion AC-1:"))
        assert "tests/test_t1.py binds it, but pytest failed to import that module" in ac1
        assert "binding is already in place" in ac1
        assert "bind that exact gate-owned file" not in ac1
        assert any(
            "imports `bcrypt_not_installed_here` but its declared dependencies "
            "do not include it" in f
            for f in obs.findings
        ), obs.findings


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


def test_a_conftest_that_only_mentions_scenarios_is_not_named(tmp_path: Path) -> None:
    # A local fixer's conftest said `scenarios(...)` in a comment; the gate read it as
    # a binding and told it to move a call that did not exist.
    from foundry.gates.test import conftest_binding_note

    (tmp_path / "conftest.py").write_text(
        '"""Fixtures; test modules call scenarios(...)."""\n'
        "# Individual test modules will use scenarios(...) to bind steps.\n"
    )
    assert conftest_binding_note(tmp_path) is None
    (tmp_path / "conftest.py").write_text(
        "import pytest_bdd\n\npytest_bdd.scenarios('features/T1.feature')\n"
    )
    assert conftest_binding_note(tmp_path) is not None


def test_a_test_function_named_like_the_generated_one_is_named_as_shadowing(
    tmp_path: Path,
) -> None:
    # A local fixer read `FAILED ...::test_sccart1` as a missing test and wrote one
    # after scenarios(): it replaced the scenario's test, pytest stayed green, and the
    # gate's only finding said to bind a file that was already bound.
    steps = _STEPS.replace("{expected}", "n") + (
        "\n\ndef test_ac1():\n    assert True\n"
        "\n\nfrom pytest_bdd import scenario\n"
        '\n\n@scenario("../features/T1.feature", "AC-2")\ndef test_ac2():\n    pass\n'
    )
    for scope in (None, frozenset({"features/T1.feature::AC-1"})):
        gate = TestGate(scope=scope)
        obs = gate.execute(_project(tmp_path / str(bool(scope)), steps))
        assert obs.structured[BDD_KEY]["features/T1.feature::AC-1"] == "missing"
        assert gate.decide(obs) is GateStatus.FAIL
        assert obs.findings[0].startswith(
            "tests/test_t1.py defines `test_ac1` after its `scenarios(...)` call"
        ), obs.findings
        assert "features/T1.feature::AC-1: the later definition replaces it" in obs.findings[0]
        # An explicit @scenario test of that name binds the scenario: not shadowing.
        assert not any("`test_ac2`" in f for f in obs.findings)
    assert obs.structured[BDD_KEY] == {"features/T1.feature::AC-1": "missing"}


def test_a_same_named_test_before_the_binding_is_not_flagged(tmp_path: Path) -> None:
    # Defined first, pytest-bdd generates test_ac1_1 instead: the scenario still runs.
    steps = "def test_ac1():\n    assert True\n\n\n" + _STEPS.replace("{expected}", "n")
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, steps))
    assert set(obs.structured[BDD_KEY].values()) == {"passed"}
    assert gate.decide(obs) is GateStatus.PASS


def test_a_failed_scenario_says_not_to_write_its_generated_test(tmp_path: Path) -> None:
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "99")))
    hit = next(f for f in obs.findings if f.startswith("BDD criterion AC-1"))
    assert "failed — its pytest test is the one `scenarios(...)` generates" in hit
    assert "never write a test function of that name yourself" in hit


def test_a_step_module_that_fails_to_import_is_a_fail_not_an_error(tmp_path: Path) -> None:
    """pytest exits 2 on a collection error; as an ERROR the fix loop scored the
    tree None, so a pass that broke the import was never judged worse or rolled back."""
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, "from admin import AdminServiceImpl\n" + _STEPS))
    assert obs.exit_codes == (2,)
    assert gate.decide(obs) is GateStatus.FAIL


def test_a_tree_syntax_error_at_collection_is_a_fail(tmp_path: Path) -> None:
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, "def (:\n" + _STEPS))
    assert obs.exit_codes == (2,)
    assert gate.decide(obs) is GateStatus.FAIL


def test_the_tree_importing_a_name_the_harness_lacks_is_a_fail(tmp_path: Path) -> None:
    """``from pytest_bdd import step_given`` names the harness, but the tree's own
    import is wrong: editing the tree fixes it."""
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, "from pytest_bdd import step_given\n" + _STEPS))
    assert obs.exit_codes == (2,)
    assert gate.decide(obs) is GateStatus.FAIL, obs.stdout


_HARNESS_BLOCK = (
    "____ ERROR collecting tests/test_t1.py ____\n"
    "ImportError while importing test module '/w/tests/test_t1.py'.\n"
    "Traceback:\n"
    "/usr/lib/python3.12/importlib/__init__.py:90: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_t1.py:1: in <module>\n"
    "    from pytest_bdd import scenarios\n"
    "{inner}"
    "E   {error}\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_t1.py\n"
)


def test_a_missing_harness_module_stays_an_error(tmp_path: Path) -> None:
    from foundry.gates.test import collect_error_owners

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t1.py").write_text("from pytest_bdd import scenarios\n")
    out = _HARNESS_BLOCK.format(inner="", error="ModuleNotFoundError: No module named 'pytest_bdd'")
    assert collect_error_owners(out, tmp_path) == {"tests/test_t1.py": "harness"}


def test_an_import_failing_inside_harness_code_stays_an_error(tmp_path: Path) -> None:
    from foundry.gates.test import collect_error_owners

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t1.py").write_text("from pytest_bdd import scenarios\n")
    inner = (
        "/venv/lib/python3.12/site-packages/pytest_bdd/__init__.py:5: in <module>\n"
        "    from pytest_bdd.parser import x\n"
    )
    for error in (
        "ImportError: cannot import name 'x' from 'pytest_bdd.parser'",
        "ImportError: cannot import name 'Parser' from 'gherkin'",
    ):
        out = _HARNESS_BLOCK.format(inner=inner, error=error)
        assert collect_error_owners(out, tmp_path) == {"tests/test_t1.py": "harness"}, error
    tree = _HARNESS_BLOCK.format(
        inner="", error="ImportError: cannot import name 'step_given' from 'pytest_bdd'"
    )
    assert collect_error_owners(tree, tmp_path) == {"tests/test_t1.py": "tree"}


def test_a_collect_path_under_a_subdir_rootdir_is_the_trees(tmp_path: Path) -> None:
    """A ``pytest.ini`` under ``tests/`` makes it pytest's rootdir: the block header
    names ``test_t1.py``, not ``tests/test_t1.py``."""
    from foundry.gates.test import collect_error_owners

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t1.py").write_text("from app import x\n")
    out = (
        "____ ERROR collecting test_t1.py ____\n"
        "tests/test_t1.py:1: in <module>\n"
        "    from app import x\n"
        "E   ModuleNotFoundError: No module named 'app'\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_t1.py\n"
    )
    want = {"test_t1.py": "tree", "tests/test_t1.py": "tree"}
    assert collect_error_owners(out, tmp_path) == want
    rooted = f"rootdir: {tmp_path / 'tests'}\n" + out
    assert collect_error_owners(rooted, tmp_path) == want
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "test_t1.py").write_text("")
    # Ambiguous without the rootdir line; the line pins it.
    assert collect_error_owners(out, tmp_path)["test_t1.py"] == "harness"
    assert collect_error_owners(rooted, tmp_path) == want


def test_a_tree_collection_error_under_a_subdir_rootdir_is_a_fail(tmp_path: Path) -> None:
    root = _project(tmp_path, "from admin import AdminServiceImpl\n" + _STEPS)
    (root / "tests" / "pytest.ini").write_text("[pytest]\n")
    gate = TestGate(cmd=(*TestGate().cmd, "tests"))
    obs = gate.execute(root)
    assert obs.exit_codes == (2,)
    assert "ERROR collecting test_t1.py" in obs.stdout
    assert gate.decide(obs) is GateStatus.FAIL, obs.stdout


def test_a_collection_error_outside_the_tree_stays_an_error(tmp_path: Path) -> None:
    out = (
        "____ ERROR collecting ../harness/gen_steps.py ____\n"
        "E   ModuleNotFoundError: No module named 'app'\n"
    )
    from foundry.gates.test import collect_error_owners

    assert collect_error_owners(out, tmp_path) == {"../harness/gen_steps.py": "harness"}


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
    assert any(
        f.startswith("no test modules collected: add tests/test_<name>.py")
        and "feature files" in f
        and "test command exited 5" in f
        for f in obs.findings
    ), obs.findings


def test_a_scoped_scenario_absent_from_the_features_fails_closed(tmp_path: Path) -> None:
    gate = TestGate(scope=frozenset({"features/T9.feature::AC-9"}))
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "n")))
    assert obs.structured[BDD_KEY] == {"features/T9.feature::AC-9": "missing"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_the_scope_is_part_of_the_reuse_key() -> None:
    a = TestGate(scope=frozenset({"features/T1.feature::AC-1"}))
    b = TestGate(scope=frozenset({"features/T1.feature::AC-2"}))
    assert (
        len({a.scanner_rule_version, b.scanner_rule_version, TestGate().scanner_rule_version}) == 3
    )


# ---- the fixer sees failures grouped by feature file ---------------------------


def test_bdd_failures_are_grouped_by_feature_file_and_owning_tickets() -> None:
    why = "was not executed — bind it"
    failures = [
        "[build gate FAILED]",
        "build command exited 1",
        "[test gate FAILED]",
        *(
            f"BDD criterion E1-AC0{i}: scenario features/E1-T02.feature::E1-AC0{i} {why}"
            for i in (2, 3)
        ),
        f"BDD criterion E2-AC01: scenario features/E2-T01.feature::E2-AC01 {why}",
        "FAILED tests/test_x.py::test_y",
    ]
    tickets = [
        _ticket("E1-T02", ("ft/validation.py",), scenario_ids=("E1-AC02", "E1-AC03")),
        _ticket("E1-T08", ("tests/steps/test_validation.py",), scenario_ids=("E1-AC02", "E1-AC03")),
    ]
    out = group_failures_by_ticket(failures, tickets)
    assert out == (
        "[build gate FAILED]",
        "build command exited 1",
        "[test gate FAILED]",
        "[BDD failures by feature file: 2 group(s)]",
        f"[features/E1-T02.feature] scenarios E1-AC02, E1-AC03 {why} — ticket(s): "
        "E1-T02 (ft/validation.py), E1-T08 (tests/steps/test_validation.py)",
        f"[features/E2-T01.feature] scenarios E2-AC01 {why} — ticket(s): no ticket",
        "FAILED tests/test_x.py::test_y",
    )
    assert group_failures_by_ticket(failures[:2], tickets) == tuple(failures[:2])


# ---- stack command files have an owner ------------------------------------------


def test_stack_files_are_the_scripts_the_commands_run() -> None:
    stack = StackDecision(
        language="python",
        test="python3 -m pytest -q",
        run="python3 ./smoke.py --ticks 3 && sh scripts/check.sh",
    )
    assert stack_files(stack) == ("smoke.py", "scripts/check.sh")


def test_an_unowned_run_script_goes_to_the_entrypoint_ticket() -> None:
    stack = StackDecision(language="python", run="python3 smoke.py")
    tickets = [
        _ticket("T1", ("app/__init__.py", "app/core.py")),
        _ticket("T2", ("app/main.py",), entrypoint=True),
    ]
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
    tickets = [
        _ticket("contracts", ("finance/contracts.py",)),
        _ticket("T1", ("finance/store/db.py", "tests/steps/test_db.py")),
    ]
    out, concerns, _ = _own_stack_files(tickets, stack)
    assert out[0].files == (
        "finance/contracts.py",
        "finance/__init__.py",
        "finance/store/__init__.py",
    )
    assert out[1] == tickets[1] and concerns
    # Without a contracts ticket the first ticket writing there owns it; owned
    # markers and non-Python stacks are left alone.
    out, _, _ = _own_stack_files([tickets[1]], stack)
    assert out[0].files[-2:] == ("finance/__init__.py", "finance/store/__init__.py")
    done = [_ticket("T1", ("pkg/__init__.py", "pkg/a.py"))]
    assert _own_stack_files(done, stack)[0] == done
    ts = [_ticket("T1", ("src/a.ts",))]
    assert _own_stack_files(ts, StackDecision(language="typescript"))[0] == ts


def test_a_module_a_planned_package_would_shadow_becomes_the_package() -> None:
    # The local shop bench: E1 planned models.py, E4 models/cart.py, and the contracts
    # ticket got an empty models/__init__.py in wave 0 — `from models import Base`
    # loaded the empty package in every later ticket.
    stack = StackDecision(language="python")
    tickets = [
        _ticket("contracts", ("contracts.py",)),
        _ticket("E1-T02", ("models.py", "tests/test_e1_t02.py")),
        _ticket("E2-T01", ("auth.py",), reads=("models.py",)),
        _ticket("E4-T01", ("models/cart.py", "services/cart_service.py")),
    ]
    out, concerns, _ = _own_stack_files(tickets, stack)
    by_id = {t.id: t for t in out}
    assert by_id["E1-T02"].files == ("models/__init__.py", "tests/test_e1_t02.py")
    assert "models/__init__.py" in by_id["E1-T02"].description
    assert by_id["E2-T01"].reads == ("models/__init__.py",)
    assert by_id["contracts"].files == ("contracts.py", "services/__init__.py")
    assert by_id["E4-T01"] == tickets[3]
    assert any("shadows the module" in c for c in concerns)
    # A base-tree module stays; so does a plan with no clashing directory.
    out, _, _ = _own_stack_files(tickets[1:2], stack, frozenset({"models/cart.py"}))
    assert out[0].files[0] == "models/__init__.py"
    out, _, _ = _own_stack_files(tickets[3:], stack, frozenset({"models.py"}))
    assert "models/__init__.py" in out[0].files
    plain = [_ticket("T1", ("models.py", "app/models.py"))]
    assert _own_stack_files(plain, stack)[0][0].files == (
        "models.py",
        "app/models.py",
        "app/__init__.py",
    )


# ---- a failed smoke run carries its reason ----------------------------------------


def test_a_failed_smoke_run_names_the_exit_code_and_error(tmp_path: Path) -> None:
    gate = SmokeGate((sys.executable, "smoke.py"))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.findings[0].startswith("smoke command exited 2")
    assert any("smoke.py" in f and "No such file" in f for f in obs.findings)


def test_a_smoke_command_that_never_exits_is_named_a_server(tmp_path: Path) -> None:
    (tmp_path / "serve.py").write_text(
        "import time\nprint('listening', flush=True)\ntime.sleep(60)\n"
    )
    gate = SmokeGate((sys.executable, "serve.py"), timeout_s=2)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.ERROR
    assert "long-running server" in obs.findings[0]
    assert "port 0" in obs.findings[0] and "exits 0" in obs.findings[0]
    assert any("listening" in f for f in obs.findings)


def test_the_default_smoke_timeout_is_five_minutes() -> None:
    assert SmokeGate(("true",))._timeout_s == 300.0


# ---- a database shared across test modules ----------------------------------------

_DB = (
    "import sqlite3\n"
    "conn = sqlite3.connect(':memory:')\n"
    "conn.execute('CREATE TABLE users (username TEXT UNIQUE)')\n"
)
_INSERT = (
    "from db import conn\n\n\n"
    "def test_{name}():\n"
    "    conn.execute(\"INSERT INTO users VALUES ('testuser')\")\n"
)

# The local shop bench's wave check: SQLAlchemy's echo of the colliding insert.
_SQLALCHEMY_UNIQUE = """\
__________________________________ test_scdb1 __________________________________
E   sqlite3.IntegrityError: UNIQUE constraint failed: users.username
E   sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: \
users.username
E   [SQL: INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)]
E   [parameters: ('testuser', 'hashed_secret', 'customer')]
FAILED tests/test_t2.py::test_scdb1 - sqlalchemy.exc.IntegrityError: (sqlite3...
"""


def test_a_unique_collision_between_test_modules_says_the_database_is_shared(
    tmp_path: Path,
) -> None:
    # Each module passes alone; run together the second insert collides, so a fixer
    # that reruns the failing module by itself sees nothing to fix.
    (tmp_path / "db.py").write_text(_DB)
    for name in ("a", "b"):
        (tmp_path / f"test_{name}.py").write_text(_INSERT.format(name=name))
    gate = TestGate()
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    hit = next(f for f in obs.findings if "unique key users.username" in f)
    assert "passes when its module runs alone" in hit
    assert "autouse fixture in conftest.py" in hit


def test_the_shared_state_hint_names_the_other_modules_writing_the_value(
    tmp_path: Path,
) -> None:
    from foundry.gates.test import shared_state_hints

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t2.py").write_text('UserModel(username="testuser")\n')
    (tmp_path / "tests" / "test_auth.py").write_text('username = "testuser"\n')
    (tmp_path / "auth.py").write_text('DEFAULT = "testuser"\n')  # not a test module
    (hint,) = shared_state_hints(_SQLALCHEMY_UNIQUE, tmp_path)
    assert "'testuser' (tests/test_auth.py, tests/test_t2.py)" in hint
    assert "(auth.py" not in hint and " auth.py" not in hint and "hashed_secret" not in hint
    assert shared_state_hints("E   AssertionError: assert 1 == 2\n", tmp_path) == ()
