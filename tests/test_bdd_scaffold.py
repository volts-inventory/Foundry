"""The deterministic pytest-bdd step scaffold and the static step-module checks."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from foundry.gates.bdd_scaffold import parse_feature, step_scaffold
from foundry.gates.test import static_step_notes, step_module_notes

_FEATURE = """\
Feature: Basket

  Background:
    Given an empty basket

  Scenario: AC-1
    When I add "milk"
    Then the basket has 1 item

  Scenario Outline: AC-2
    When I add <count> of "<item>"
    And I add "milk"
    Then the basket has <total> item

    Examples:
      | count | item  | total |
      | 2     | bread | 3     |
      | 1     | {jam} | 2     |
"""


def _project(tmp_path: Path, module: str, body: str) -> Path:
    (tmp_path / "features").mkdir()
    (tmp_path / "features/T-1.feature").write_text(_FEATURE)
    (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / module).write_text(body)
    return tmp_path


def _pytest(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-rA", str(root)],
        cwd=root, capture_output=True, text=True, timeout=120,
    )


def test_parse_feature_resolves_and_background_and_outline_rows() -> None:
    plain, outline = parse_feature(_FEATURE)
    assert [(s.kind, s.text) for s in plain.steps] == [
        ("given", "an empty basket"), ("when", 'I add "milk"'), ("then", "the basket has 1 item"),
    ]
    rows = outline.rendered()
    assert [s.text for s in rows[0]] == [
        "an empty basket", 'I add 2 of "bread"', 'I add "milk"', "the basket has 3 item",
    ]
    assert rows[1][1].text == 'I add 1 of "{jam}"'


def test_scaffold_has_one_function_per_distinct_step_and_the_relative_binding() -> None:
    text = step_scaffold("tests/steps/test_t1.py", {"features/T-1.feature": _FEATURE})
    assert 'scenarios("../../features/T-1.feature")' in text
    tree = ast.parse(text)
    steps = [ast.unparse(d) for f in tree.body if isinstance(f, ast.FunctionDef)
             for d in f.decorator_list if "fixture" not in ast.unparse(d)]
    assert steps == [
        "given('an empty basket')",
        "when('I add \"milk\"')",
        "when(parsers.parse('I add {count} of \"{item}\"'))",
        "then(parsers.parse('the basket has {total} item'))",
    ]
    # The outline's pattern also matches the plain "the basket has 1 item" line: one
    # definition, not two that both match it.
    assert text.count("the basket has") == 1
    assert "def when_i_add_count_of_item(context, count, item):" in text
    assert 'target_fixture="name"' in text  # where to hand a value on


def test_scaffold_binds_every_step_under_real_pytest_bdd(tmp_path: Path) -> None:
    module = "tests/test_t1.py"
    scaffold = step_scaffold(module, {"features/T-1.feature": _FEATURE})
    root = _project(tmp_path, module, scaffold)
    unfilled = _pytest(root)
    assert unfilled.returncode == 1, unfilled.stdout
    assert "NotImplementedError" in unfilled.stdout
    assert "StepDefinitionNotFoundError" not in unfilled.stdout
    (root / module).write_text(scaffold.replace("raise NotImplementedError", "pass"))
    filled = _pytest(root)
    assert filled.returncode == 0, filled.stdout + filled.stderr
    assert "3 passed" in filled.stdout


_STEPS = '''\
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/T-1.feature")


@pytest.fixture
def shelf():
    return []


@given("an empty basket", target_fixture="basket")
def _empty(shelf):
    return []


@when(parsers.parse('I add "{name}"'))
def _add(basket, name):
    basket.append(name)


@when(parsers.parse('I add {count:d} of "{item}"'))
def _add_many(basket, count, item, request):
    basket.extend([item] * count)


@then(parsers.parse("the basket has {n:d} item"))
def _count(basket, n):
    assert len(basket) == n
'''


def test_a_clean_step_module_has_no_static_notes(tmp_path: Path) -> None:
    root = _project(tmp_path, "tests/test_t1.py", _STEPS)
    assert step_module_notes(root, "tests/test_t1.py") == ()
    assert _pytest(root).returncode == 0


def test_static_checks_name_duplicates_unmatched_texts_and_unprovided_params(
    tmp_path: Path,
) -> None:
    steps = _STEPS.replace('target_fixture="basket"', 'target_fixture="items"') + (
        '\n\n@then("the basket is empty")\ndef _empty_then(basket):\n    assert not basket\n'
        '\n\n@then(parsers.parse("the basket has {n:d} item"))\n'
        "def _count_again(basket, n):\n    assert basket\n"
    )
    root = _project(tmp_path, "tests/test_t1.py", steps)
    notes = step_module_notes(root, "tests/test_t1.py")
    dup = [n for n in notes if "is defined 2 times" in n]
    assert len(dup) == 1 and dup[0].startswith('`@then("the basket has {n:d} item")`')
    [unmatched] = [n for n in notes if "matches no" in n]
    assert unmatched.startswith('`@then("the basket is empty")` (tests/test_t1.py:')
    assert "matches no Then step line of features/T-1.feature" in unmatched
    assert "nearest Then line is" in unmatched
    unprovided = [n for n in notes if "is provided by nothing" in n]
    assert unprovided and all(n.startswith("step argument `basket`") for n in unprovided)
    # The gate's form skips the parameter check (the run's own output names those).
    gate = static_step_notes(root, {"features/T-1.feature"}, params=False)
    assert not any("provided by nothing" in n for n in gate)
    assert any("matches no" in n for n in gate) and any("2 times" in n for n in gate)


def test_a_conftest_fixture_or_target_counts_as_provided(tmp_path: Path) -> None:
    steps = _STEPS.replace('@given("an empty basket", target_fixture="basket")',
                           '@given("an empty basket")')
    root = _project(tmp_path, "tests/test_t1.py", steps)
    assert any("`basket`" in n for n in step_module_notes(root, "tests/test_t1.py"))
    (root / "tests/conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture(name='basket')\ndef _basket():\n    return []\n")
    assert step_module_notes(root, "tests/test_t1.py") == ()
