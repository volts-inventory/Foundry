"""The step-definition lint: a scenario whose steps cannot fail where they should is
FAILed (pytest-bdd by AST, Cucumber step files by regex)."""

from __future__ import annotations

from pathlib import Path

from foundry.gates.test import (
    BDD_KEY,
    WEAK_STEPS,
    TestGate,
    cucumber_weak_step_notes,
    weak_step_notes,
)
from foundry.schemas.gates import GateStatus
from tests.test_bdd import _STEPS, _project

_REF = "features/T1.feature::AC-1"
_GOOD = _STEPS.replace("{expected}", "n")
_TRUSTED = {
    _REF: [("given", "an empty list"), ("when", 'I add "milk"'), ("then", "the list has 1 item")]
}


def _then(body: str) -> str:
    return _GOOD.replace("    assert len(items) == n\n", body)


def _notes(tmp_path: Path, steps: str) -> tuple[str, ...]:
    return weak_step_notes(_project(tmp_path, steps), _TRUSTED).get(_REF, ())


def test_a_specific_then_step_is_clean(tmp_path: Path) -> None:
    assert _notes(tmp_path, _GOOD) == ()


def test_an_assert_in_a_when_step_fails_the_scenario(tmp_path: Path) -> None:
    steps = _GOOD.replace("    items.append(x)\n", "    items.append(x)\n    assert items\n")
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, steps))
    assert obs.structured[BDD_KEY][_REF] == WEAK_STEPS
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("asserts inside a When step" in f and _REF in f for f in obs.findings)


def test_an_empty_or_unchecked_then_step_is_flagged(tmp_path: Path) -> None:
    (empty,) = _notes(tmp_path / "a", _then("    pass\n"))
    assert "empty Then step" in empty
    (none,) = _notes(tmp_path / "b", _then("    print(items)\n"))
    assert "no assert" in none


def test_a_then_step_checking_only_truthiness_or_exit_code_is_flagged(tmp_path: Path) -> None:
    for i, body in enumerate(
        (
            "    assert items\n",
            "    assert items is not None\n",
            "    assert len(items) > 0\n",
            "    assert items[0].returncode == 0\n",
            '    assert items["exit_code"] == 0\n',
        )
    ):
        (note,) = _notes(tmp_path / str(i), _then(body))
        assert "only check is truthiness" in note, body


def test_an_expected_value_from_the_function_under_test_is_flagged(tmp_path: Path) -> None:
    steps = (
        _GOOD.replace("from pytest_bdd", "def total(xs):\n    return len(xs)\n\n\nfrom pytest_bdd")
        .replace("    items.append(x)\n", "    items.append(x)\n    total(items)\n")
        .replace(
            "    assert len(items) == n\n",
            "    expected = total(items)\n    assert total(items) == expected\n",
        )
    )
    (note,) = _notes(tmp_path, steps)
    assert "calling `total`, the function under test" in note


def _then_says(tmp_path: Path, then: str, body: str) -> tuple[str, ...]:
    steps = _GOOD.replace(
        '@then(parsers.parse("the list has {n:d} item"))\ndef _count(items, n):\n'
        "    assert len(items) == n\n",
        f"@then({then!r})\ndef _count(items):\n{body}",
    )
    trusted = {_REF: [*_TRUSTED[_REF][:2], ("then", then)]}
    return weak_step_notes(_project(tmp_path, steps), trusted).get(_REF, ())


def test_an_exit_code_or_none_check_the_then_line_states_exactly_is_clean(
    tmp_path: Path,
) -> None:
    assert (
        _then_says(
            tmp_path / "a",
            "the command exits with code 2",
            ("    result = items\n    assert result.returncode == 2\n"),
        )
        == ()
    )
    assert _then_says(tmp_path / "b", "no user is found", "    assert items is None\n") == ()
    (more,) = _then_says(
        tmp_path / "c",
        'the command exits with code 2 and prints "bad id"',
        "    assert items.returncode == 2\n",
    )
    assert "only check is truthiness" in more
    (shows,) = _then_says(
        tmp_path / "d", "no user is found and a message shows", ("    assert items is None\n")
    )
    assert "only check is truthiness" in shows


def test_rereading_the_actual_through_the_sut_against_a_literal_is_clean(
    tmp_path: Path,
) -> None:
    steps = (
        _GOOD.replace(
            "from pytest_bdd", "def search(xs, q):\n    return list(xs)\n\n\nfrom pytest_bdd"
        )
        .replace("    items.append(x)\n", "    items.append(x)\n    search(items, x)\n")
        .replace(
            "    assert len(items) == n\n",
            '    hits = search(items, "x")\n    assert [h for h in hits] == ["milk"]\n'
            '    assert search(items, "x")[0] == "milk"\n',
        )
    )
    assert _notes(tmp_path, steps) == ()


def test_both_sides_from_sut_calls_are_flagged(tmp_path: Path) -> None:
    steps = (
        _GOOD.replace(
            "from pytest_bdd", "def search(xs, q):\n    return list(xs)\n\n\nfrom pytest_bdd"
        )
        .replace("    items.append(x)\n", "    items.append(x)\n    search(items, x)\n")
        .replace(
            "    assert len(items) == n\n",
            '    hits = search(items, "x")\n    assert [h for h in hits] == search(items, "x")\n',
        )
    )
    (note,) = _notes(tmp_path, steps)
    assert "calling `search`, the function under test" in note


def test_cucumber_js_and_go_step_files_are_linted(tmp_path: Path) -> None:
    js = tmp_path / "js" / "features" / "step_definitions"
    js.mkdir(parents=True)
    (js / "list.steps.js").write_text(
        "const assert = require('assert');\n"
        "Given('an empty list', function () { this.items = []; });\n"
        "When('I add {string}', function (x) { this.items.push(x); assert.ok(this.items); });\n"
        "Then('the list has {int} item', function (n) { assert.ok(this.items); });\n"
    )
    notes = cucumber_weak_step_notes(tmp_path / "js", _TRUSTED, "javascript")[_REF]
    assert any("When step" in n for n in notes) and any("truthiness" in n for n in notes)
    go = tmp_path / "go" / "bdd"
    go.mkdir(parents=True)
    (go / "list_steps_test.go").write_text(
        "package bdd\n\n"
        "func added(x string) error { items = append(items, x); return nil }\n"
        "func counted(n int) error { return nil }\n"
        "func init() { register(func(sc *godog.ScenarioContext) {\n"
        '\tsc.Step(`^I add "([^"]*)"$`, added)\n'
        "\tsc.Step(`^the list has (\\d+) item$`, counted)\n"
        "}) }\n"
    )
    (note,) = cucumber_weak_step_notes(tmp_path / "go", _TRUSTED, "go")[_REF]
    assert "checks nothing" in note and "the list has 1 item" in note
    (go / "list_steps_test.go").write_text(
        (go / "list_steps_test.go")
        .read_text()
        .replace(
            "func counted(n int) error { return nil }",
            "func counted(n int) error { if len(items) != n { "
            'return fmt.Errorf("got %d", len(items)) }; return nil }',
        )
    )
    assert cucumber_weak_step_notes(tmp_path / "go", _TRUSTED, "go") == {}
