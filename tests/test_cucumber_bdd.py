"""The Test gate's Cucumber Messages path: the NDJSON parser against reports recorded
from a real cucumber-js run (``tests/fixtures/cucumber``), the gate's report channel
and integrity checks, and live cucumber-js runs when npm can install it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.gates import cucumber
from foundry.gates.cucumber import (
    ReportError,
    expression_to_regex,
    parse_messages,
    read_verified_report,
    record_report,
    scenario_outcomes,
)
from foundry.gates.test import BDD_EXIT_KEY, BDD_KEY, TestGate, trusted_scenarios
from foundry.schemas.gates import GateStatus

FIXTURES = Path(__file__).parent / "fixtures" / "cucumber"
REF = "features/T1.feature::"


def _workdir(tmp_path: Path, feature: str | None = None) -> Path:
    wd = tmp_path / "wd"
    (wd / "features").mkdir(parents=True)
    body = feature if feature is not None else (FIXTURES / "T1.feature").read_text()
    (wd / "features" / "T1.feature").write_text(body)
    return wd


def _envelopes(name: str) -> list[dict[str, Any]]:
    return parse_messages((FIXTURES / f"{name}.ndjson").read_bytes())


# ---- parser over recorded cucumber-js reports ---------------------------------


def test_recorded_run_maps_pass_fail_and_undefined(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    out, notes = scenario_outcomes(_envelopes("mixed"), trusted_scenarios(wd), wd)
    assert out == {
        REF + "AC-1": "passed",
        REF + "AC-2": "failed",  # assertion error in a step
        REF + "AC-3": "missing",  # "I remove ..." has no step definition
        REF + "AC-4": "passed",  # regex with a JS named group, not a catch-all
    }
    assert notes == ()


def test_recorded_catch_all_run_is_flagged(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    out, _ = scenario_outcomes(_envelopes("catchall"), trusted_scenarios(wd), wd)
    assert set(out.values()) == {"catch-all"}


def test_step_text_must_match_the_trusted_feature(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    trusted = trusted_scenarios(wd)
    trusted[REF + "AC-1"] = [("given", "an empty list"), ("when", 'I add "eggs"'),
                             ("then", "the list has 1 item")]
    trusted[REF + "AC-4"] = [("when", t) for _, t in trusted[REF + "AC-4"]]  # keyword type
    out, _ = scenario_outcomes(_envelopes("mixed"), trusted, wd)
    assert out[REF + "AC-1"] == "unmatched"
    assert out[REF + "AC-4"] == "unmatched"


def test_unreported_scenario_is_missing_and_foreign_ones_ignored(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    trusted = {REF + "AC-1": trusted_scenarios(wd)[REF + "AC-1"], REF + "AC-9": []}
    out, _ = scenario_outcomes(_envelopes("mixed"), trusted, wd)
    assert out == {REF + "AC-1": "passed", REF + "AC-9": "missing"}


def test_absolute_file_uri_maps_to_the_workdir_ref(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    envs = _envelopes("mixed")
    for env in envs:
        if "pickle" in env:
            env["pickle"]["uri"] = (wd / "features" / "T1.feature").resolve().as_uri()
    out, _ = scenario_outcomes(envs, trusted_scenarios(wd), wd)
    assert out[REF + "AC-1"] == "passed"


def test_any_failed_attempt_decides_the_scenario(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    envs = _envelopes("mixed")
    # Replay AC-2's failed attempt under AC-1's test case: a retried pass is not a pass.
    ac1_pickle = next(e["pickle"]["id"] for e in envs if e.get("pickle", {}).get("name") == "AC-1")
    case = next(e["testCase"] for e in envs
                if e.get("testCase", {}).get("pickleId") == ac1_pickle)
    envs.append({"testCaseStarted": {"id": "retry-0", "testCaseId": case["id"], "attempt": 1}})
    envs.append({"testStepFinished": {
        "testCaseStartedId": "retry-0", "testStepId": case["testSteps"][0]["id"],
        "testStepResult": {"status": "FAILED"},
    }})
    out, _ = scenario_outcomes(envs, trusted_scenarios(wd), wd)
    assert out[REF + "AC-1"] == "failed"


def test_expression_translation() -> None:
    rx = expression_to_regex(r"I have {int} cat(s)/dog in \{x} {word}", {})
    import re

    assert re.fullmatch(rx, "I have 3 cats in {x} home")
    assert re.fullmatch(rx, "I have -1 dog in {x} home")
    assert not re.fullmatch(rx, "I have many cats in {x} home")
    custom = expression_to_regex("the {color} light", {"color": ["red|blue"]})
    assert re.fullmatch(custom, "the red light") and not re.fullmatch(custom, "the x light")


@pytest.mark.parametrize(
    ("ptype", "source", "params", "flagged"),
    [
        ("CUCUMBER_EXPRESSION", "{}", {}, True),
        ("CUCUMBER_EXPRESSION", "{word} {word} {word}", {}, True),
        ("CUCUMBER_EXPRESSION", "{anything}", {"anything": [".*"]}, True),
        ("CUCUMBER_EXPRESSION", "{mystery}", {}, True),  # unknown type: assume the worst
        ("CUCUMBER_EXPRESSION", "I add {string}", {}, False),
        ("REGULAR_EXPRESSION", "^(.*)$", {}, True),
        ("REGULAR_EXPRESSION", "", {}, True),  # unanchored empty regex matches anything
        ("REGULAR_EXPRESSION", r"\p{L}+", {}, True),  # untranslatable: fail closed
        ("REGULAR_EXPRESSION", "^the list has (\\d+) items?$", {}, False),
    ],
)
def test_catch_all_detection(ptype: str, source: str, params: dict, flagged: bool) -> None:
    defs = {"d": {"id": "d", "pattern": {"type": ptype, "source": source}}}
    probes = cucumber.make_probes(len(source.split()) or None)  # the step's shape
    got, _ = cucumber._catch_all_ids(defs, params, probes)
    assert ("d" in got) is flagged


def test_probes_are_random_with_no_fixed_literal_to_dodge() -> None:
    runs = [cucumber.make_probes() for _ in range(20)]
    assert all(len(r) == 3 for r in runs) and len({p for r in runs for p in r}) == 60
    assert len({len(p) for r in runs for p in r}) > 5
    assert all(len(p.split(" ")) == 4 for p in cucumber.make_probes(4))
    # A matcher that accepts everything except the old fixed probe literals.
    dodge = {"d": {"pattern": {"type": "REGULAR_EXPRESSION",
                               "source": r"^(?!.*Zq-9 !)(?!wV\? ).*$"}}}
    got, _ = cucumber._catch_all_ids(dodge, {}, cucumber.make_probes())
    assert got == {"d"}


def _rebind(envs: list[dict[str, Any]], scenario: str, step_type: str, def_id: str) -> None:
    """Bind ``scenario``'s ``step_type`` step(s) to step definition ``def_id``."""
    pickle = next(e["pickle"] for e in envs if e.get("pickle", {}).get("name") == scenario)
    ids = {s["id"] for s in pickle["steps"] if s["type"] == step_type}
    case = next(e["testCase"] for e in envs
                if e.get("testCase", {}).get("pickleId") == pickle["id"])
    for ts in case["testSteps"]:
        if ts.get("pickleStepId") in ids:
            ts["stepDefinitionIds"] = [def_id]


def test_a_catch_all_then_step_alone_grades_the_scenario_catch_all(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    envs = _envelopes("mixed")
    envs.insert(0, {"stepDefinition": {
        "id": "any", "pattern": {"type": "CUCUMBER_EXPRESSION", "source": "{}"}}})
    _rebind(envs, "AC-1", "Outcome", "any")  # Given/When stay specific
    _rebind(envs, "AC-4", "Context", "any")  # a catch-all Given alone is not enough
    out, _ = scenario_outcomes(envs, trusted_scenarios(wd), wd)
    assert out[REF + "AC-1"] == "catch-all"
    assert out[REF + "AC-4"] == "passed"


def test_an_undeclared_step_definition_counts_as_catch_all(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    envs = _envelopes("mixed")
    _rebind(envs, "AC-1", "Context", "never-declared")
    out, _ = scenario_outcomes(envs, trusted_scenarios(wd), wd)
    assert out[REF + "AC-1"] == "catch-all"


def test_backtracking_pattern_cannot_hang_the_gate(monkeypatch: Any) -> None:
    monkeypatch.setattr(cucumber, "_PATTERN_EVAL_TIMEOUT_S", 1.0)
    defs = {
        "a": {"pattern": {"type": "REGULAR_EXPRESSION", "source": "^specific$"}},
        "b": {"pattern": {"type": "REGULAR_EXPRESSION", "source": "^(\\w|\\w)*$"}},
    }
    probes = ("a" * 40 + "!", "b" * 40 + "!")
    got, notes = cucumber._catch_all_ids(defs, {}, probes)
    assert got == {"b"} and notes


# ---- report integrity --------------------------------------------------------


def test_report_file_is_exclusive_and_unpredictable(tmp_path: Path) -> None:
    a, b = cucumber.create_report_file(tmp_path), cucumber.create_report_file(tmp_path)
    assert a != b and a.stat().st_size == 0 and (a.stat().st_mode & 0o777) == 0o600


def test_tampered_report_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "r.ndjson"
    shutil.copy(FIXTURES / "mixed.ndjson", path)
    digest = record_report(path)
    assert len(read_verified_report(path, digest)) > 10
    path.write_bytes(path.read_bytes().replace(b'"FAILED"', b'"PASSED"'))
    with pytest.raises(ReportError, match="changed"):
        read_verified_report(path, digest)


@pytest.mark.parametrize("case", ["absent", "symlink", "empty", "garbage", "incomplete"])
def test_unusable_reports_are_rejected(tmp_path: Path, case: str) -> None:
    path = tmp_path / "r.ndjson"
    if case == "symlink":
        path.symlink_to(FIXTURES / "mixed.ndjson")
    elif case == "empty":
        path.write_bytes(b"")
    elif case == "garbage":
        path.write_bytes(b"{not json\n")
    elif case == "incomplete":
        lines = (FIXTURES / "mixed.ndjson").read_text().splitlines()
        path.write_text("\n".join(x for x in lines if "testRunFinished" not in x))
    with pytest.raises(ReportError):
        read_verified_report(path, record_report(path))


# ---- the gate's report channel (shell bdd commands, no node needed) -------------


def _gate_with(bdd_cmd: str) -> TestGate:
    return TestGate(cmd=("true",), min_passed=0, bdd_cmd=bdd_cmd)


def test_gate_runs_bdd_cmd_with_a_gate_owned_report_path(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    fixture = FIXTURES / "mixed.ndjson"
    gate = _gate_with(
        f'test -f "$FOUNDRY_BDD_REPORT" && test ! -s "$FOUNDRY_BDD_REPORT" && '
        f'echo "$FOUNDRY_BDD_REPORT" > seen.txt && cp {fixture} "$FOUNDRY_BDD_REPORT"; exit 1'
    )
    obs = gate.execute(wd)
    seen = Path((wd / "seen.txt").read_text().strip())
    assert wd.resolve() not in seen.resolve().parents
    assert not seen.exists()  # the gate's temp dir is gone after the run
    assert obs.structured[BDD_KEY][REF + "AC-1"] == "passed"
    assert obs.structured[BDD_KEY][REF + "AC-2"] == "failed"
    assert obs.structured[BDD_EXIT_KEY] == 1
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("AC-3" in f and "undefined steps" in f for f in obs.findings)
    assert cucumber.CUCUMBER_RESIDUAL in obs.findings


def test_gate_report_rewritten_after_runner_exit_is_rejected(tmp_path: Path) -> None:
    wd = _workdir(tmp_path)
    good = tmp_path / "good.ndjson"
    _all_passing(good)
    # A process left behind by the bdd command rewrites the report while the unit
    # test command runs; the digest taken at runner exit catches it.
    bad = FIXTURES / "mixed.ndjson"
    gate = TestGate(
        cmd=("sleep", "1.5"), min_passed=0,
        bdd_cmd=(f'cp {bad} "$FOUNDRY_BDD_REPORT"; '
                 f'(sleep 0.5; cp {good} "$FOUNDRY_BDD_REPORT") >/dev/null 2>&1 &'),
    )
    obs = gate.execute(wd)
    assert set(obs.structured[BDD_KEY].values()) == {"missing"}
    assert any("changed after the runner exited" in f for f in obs.findings)
    assert gate.decide(obs) is GateStatus.FAIL


@pytest.mark.parametrize(
    "bdd_cmd", ['rm "$FOUNDRY_BDD_REPORT"', "true", 'echo junk > "$FOUNDRY_BDD_REPORT"']
)
def test_gate_without_a_usable_report_marks_all_missing(tmp_path: Path, bdd_cmd: str) -> None:
    wd = _workdir(tmp_path)
    gate = _gate_with(bdd_cmd)
    obs = gate.execute(wd)
    assert set(obs.structured[BDD_KEY].values()) == {"missing"}
    assert any(f.startswith("BDD report rejected") for f in obs.findings)
    assert gate.decide(obs) is GateStatus.FAIL


def _all_passing(dest: Path) -> None:
    """The recorded run with every step reported PASSED (a forged-looking report)."""
    out = []
    for line in (FIXTURES / "mixed.ndjson").read_text().splitlines():
        env = json.loads(line)
        if "testStepFinished" in env:
            env["testStepFinished"]["testStepResult"] = {"status": "PASSED"}
        out.append(json.dumps(env))
    dest.write_text("\n".join(out) + "\n")


def test_gate_checks_step_text_and_digest_is_deterministic(tmp_path: Path) -> None:
    good = tmp_path / "good.ndjson"
    _all_passing(good)
    wd = _workdir(tmp_path, (FIXTURES / "T1.feature").read_text().replace(
        "    When I remove \"milk\"\n", "    When I add \"milk\"\n"))
    # AC-3 now reads 'I add "milk"' in the trusted file but the report ran 'I remove'.
    gate = _gate_with(f'cp {good} "$FOUNDRY_BDD_REPORT"')
    a, b = gate.execute(wd), gate.execute(wd)
    assert a.structured[BDD_KEY][REF + "AC-3"] == "unmatched"
    assert a.structured[BDD_KEY][REF + "AC-1"] == "passed"
    assert a.results_digest == b.results_digest


def test_no_features_leaves_the_bdd_command_unrun(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    gate = _gate_with("touch ran.txt")
    obs = gate.execute(wd)
    assert not (wd / "ran.txt").exists() and BDD_KEY not in obs.structured
    assert gate.decide(obs) is GateStatus.PASS


# ---- live cucumber-js -------------------------------------------------------

_CUCUMBER_VERSION = "13.3.0"
_FEATURE = """\
Feature: T1

  Scenario: AC-1
    Given an empty list
    When I add "milk"
    Then the list has 1 item

  Scenario: AC-2
    Given an empty list
    And another "bread"
    Then the list has 1 item
"""
_STEPS = """\
const { Given, When, Then } = require('@cucumber/cucumber');
const assert = require('assert');
Given('an empty list', function () { this.items = []; });
When('I add {string}', function (x) { this.items.push(x); });
Given('another {string}', function (x) { this.items.push(x); });
Then('the list has {int} item(s)', function (n) { assert.strictEqual(this.items.length, n); });
"""
_RUN = (
    'node node_modules/@cucumber/cucumber/bin/cucumber.js --require "steps/*.js" '
    '--format "message:$FOUNDRY_BDD_REPORT"'
)


@pytest.fixture(scope="session")
def cucumber_js(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("node") is None or shutil.which("npm") is None:
        pytest.skip("node/npm not installed")
    root = tmp_path_factory.mktemp("cucumber-js")
    try:
        proc = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--prefix", str(root),
             f"@cucumber/cucumber@{_CUCUMBER_VERSION}"],
            capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("npm install of @cucumber/cucumber timed out")
    if proc.returncode != 0:
        pytest.skip(f"cannot npm install @cucumber/cucumber: {proc.stderr[-300:]!r}")
    return root / "node_modules"


def _js_project(tmp_path: Path, modules: Path, steps: str = _STEPS) -> Path:
    wd = _workdir(tmp_path, _FEATURE)
    (wd / "steps").mkdir()
    (wd / "steps" / "steps.js").write_text(steps)
    os.symlink(modules, wd / "node_modules")
    return wd


def _run_js(wd: Path, bdd_cmd: str = _RUN) -> tuple[TestGate, Any]:
    gate = _gate_with(bdd_cmd)
    obs = gate.execute(wd)
    return gate, obs


def test_cucumber_js_passing_run(tmp_path: Path, cucumber_js: Path) -> None:
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js))
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "passed", REF + "AC-2": "passed"}, obs.stdout
    assert gate.decide(obs) is GateStatus.PASS


def test_cucumber_js_passing_run_in_namespace_sandbox(
    tmp_path: Path, cucumber_js: Path, monkeypatch: Any
) -> None:
    from foundry.gates.sandbox import unshare_available

    if not unshare_available():
        pytest.skip("no unprivileged user namespaces")
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    wd = _js_project(tmp_path, cucumber_js)
    # The sandbox shadows host /tmp, so the shared install must live in the workdir —
    # as a real install step puts it.
    (wd / "node_modules").unlink()
    shutil.copytree(cucumber_js, wd / "node_modules", symlinks=True)
    gate, obs = _run_js(wd)
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "passed", REF + "AC-2": "passed"}, obs.stdout
    assert gate.decide(obs) is GateStatus.PASS, obs.stdout + obs.stderr


def test_cucumber_js_failing_step(tmp_path: Path, cucumber_js: Path) -> None:
    steps = _STEPS.replace("this.items.push(x); });\nGiven", "});\nGiven")
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js, steps))
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "failed", REF + "AC-2": "passed"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_cucumber_js_missing_step_definitions(tmp_path: Path, cucumber_js: Path) -> None:
    steps = _STEPS.replace("Given('another {string}'", "Given('unused {string}'")
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js, steps))
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "passed", REF + "AC-2": "missing"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_cucumber_js_agent_edited_feature_text(tmp_path: Path, cucumber_js: Path) -> None:
    # The bdd command rewrites the trusted feature before the runner parses it.
    edit = "sed -i 's/the list has 1 item/the list has 1 items/' features/T1.feature && "
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js), edit + _RUN)
    assert set(obs.structured[BDD_KEY].values()) == {"unmatched"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_cucumber_js_catch_all(tmp_path: Path, cucumber_js: Path) -> None:
    steps = "const { Given } = require('@cucumber/cucumber');\nGiven('{}', function (x) {});\n"
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js, steps))
    assert set(obs.structured[BDD_KEY].values()) == {"catch-all"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_cucumber_js_tampered_report(tmp_path: Path, cucumber_js: Path) -> None:
    forged = tmp_path / "forged.ndjson"
    forged.write_text("{}\n")
    tamper = f'; (sleep 0.5; cp {forged} "$FOUNDRY_BDD_REPORT") >/dev/null 2>&1 &'
    gate = TestGate(cmd=("sleep", "1.5"), min_passed=0, bdd_cmd=_RUN + tamper)
    obs = gate.execute(_js_project(tmp_path, cucumber_js))
    assert set(obs.structured[BDD_KEY].values()) == {"missing"}
    assert gate.decide(obs) is GateStatus.FAIL


def test_cucumber_js_report_written_elsewhere(tmp_path: Path, cucumber_js: Path) -> None:
    gate, obs = _run_js(_js_project(tmp_path, cucumber_js), _RUN.replace(
        "$FOUNDRY_BDD_REPORT", "own-report.ndjson"))
    assert set(obs.structured[BDD_KEY].values()) == {"missing"}
    assert gate.decide(obs) is GateStatus.FAIL
