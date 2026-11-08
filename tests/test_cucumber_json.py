"""The Test gate's legacy Cucumber JSON path (godog, cucumber-rs): the converter to
Cucumber Messages, the source step-pattern scan it relies on, and a live godog run
when the Go toolchain can fetch godog."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.gates.cucumber import (
    ReportError,
    convert_cucumber_json,
    read_verified_report,
    record_report,
    scenario_outcomes,
    source_step_patterns,
)
from foundry.gates.profiles import BDD_RUNNERS
from foundry.gates.test import BDD_KEY, TestGate, trusted_scenarios
from foundry.schemas.gates import GateStatus

REF = "features/T1.feature::"
FEATURE = """\
Feature: T1

  Scenario: AC-1
    Given an empty list
    When I add "milk"
    Then the list has 1 item
"""
GO_STEPS = """\
package bdd

func InitializeScenario(sc *godog.ScenarioContext) {
	sc.Step(`^an empty list$`, empty)
	sc.When("^I add \\"([^\\"]*)\\"$", add)
	sc.Then(regexp.MustCompile(`^the list has (\\d+) items?$`), has)
}
"""


def _go_project(tmp_path: Path, steps: str = GO_STEPS, feature: str = FEATURE) -> Path:
    wd = tmp_path / "wd"
    (wd / "features").mkdir(parents=True)
    (wd / "features" / "T1.feature").write_text(feature)
    (wd / "go.mod").write_text("module example.com/shop\n")
    (wd / "bdd").mkdir()
    (wd / "bdd" / "steps_test.go").write_text(steps)
    return wd


def _step(keyword: str, name: str, status: str) -> dict[str, Any]:
    return {"keyword": keyword, "name": name, "result": {"status": status}}


def _report(*elements: dict[str, Any]) -> bytes:
    return json.dumps(
        [
            {
                "uri": "features/T1.feature",
                "keyword": "Feature",
                "name": "T1",
                "elements": list(elements),
            }
        ]
    ).encode()


def _scenario(
    name: str, *statuses: str, texts: tuple[str, ...] | None = None, **extra: Any
) -> dict[str, Any]:
    texts = texts or ("an empty list", 'I add "milk"', "the list has 1 item")
    keywords = ("Given ", "When ", "Then ")
    return {
        "type": "scenario",
        "keyword": "Scenario",
        "name": name,
        **extra,
        "steps": [_step(k, t, s) for k, t, s in zip(keywords, texts, statuses, strict=True)],
    }


def _outcome(wd: Path, data: bytes) -> dict[str, str]:
    out, _ = scenario_outcomes(convert_cucumber_json(data, wd), trusted_scenarios(wd), wd)
    return out


@pytest.mark.parametrize(
    ("statuses", "want"),
    [
        (("passed", "passed", "passed"), "passed"),
        (("passed", "passed", "failed"), "failed"),
        (("passed", "undefined", "skipped"), "missing"),
        (("passed", "pending", "skipped"), "skipped"),
        (("passed", "skipped", "skipped"), "skipped"),
        (("passed", "ambiguous", "skipped"), "failed"),
        (("passed", "passed", "weird"), "missing"),
    ],
)
def test_step_statuses_map_like_messages(
    tmp_path: Path, statuses: tuple[str, ...], want: str
) -> None:
    wd = _go_project(tmp_path)
    assert _outcome(wd, _report(_scenario("AC-1", *statuses))) == {REF + "AC-1": want}


def test_forged_step_text_is_unmatched(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    forged = _scenario(
        "AC-1",
        "passed",
        "passed",
        "passed",
        texts=("an empty list", 'I add "eggs"', "the list has 1 item"),
    )
    assert _outcome(wd, _report(forged)) == {REF + "AC-1": "unmatched"}


def test_wrong_keyword_type_is_unmatched(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    el = _scenario("AC-1", "passed", "passed", "passed")
    el["steps"][2]["keyword"] = "And "  # inherits When, the trusted step is a Then
    assert _outcome(wd, _report(el)) == {REF + "AC-1": "unmatched"}


def test_background_element_and_failed_hook(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    background = {
        "type": "background",
        "keyword": "Background",
        "name": "",
        "steps": [_step("Given ", "an empty list", "passed")],
    }
    body = {
        "type": "scenario",
        "keyword": "Scenario",
        "name": "AC-1",
        "steps": [
            _step("When ", 'I add "milk"', "passed"),
            _step("Then ", "the list has 1 item", "passed"),
        ],
    }
    assert _outcome(wd, _report(background, body)) == {REF + "AC-1": "passed"}
    hooked = {**body, "after": [{"result": {"status": "failed"}}]}
    assert _outcome(wd, _report(background, hooked)) == {REF + "AC-1": "failed"}


def test_scenario_outline_rows_each_decide(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    rows = [
        _scenario("AC-1", "passed", "passed", "passed", keyword="Scenario Outline"),
        _scenario("AC-1", "passed", "passed", "failed", keyword="Scenario Outline"),
    ]
    assert _outcome(wd, _report(*rows)) == {REF + "AC-1": "failed"}
    assert _outcome(wd, _report(rows[0], rows[0])) == {REF + "AC-1": "passed"}


def test_catch_all_pattern_in_source_is_flagged(tmp_path: Path) -> None:
    steps = GO_STEPS.replace("`^the list has (\\d+) items?$`", "`(.*)`")
    wd = _go_project(tmp_path, steps)
    assert _outcome(wd, _report(_scenario("AC-1", "passed", "passed", "passed"))) == {
        REF + "AC-1": "catch-all"
    }


def test_step_no_source_pattern_matches_is_unchecked(tmp_path: Path) -> None:
    # A step registered where the scan cannot see it cannot be probed: catch-all.
    steps = GO_STEPS.replace("sc.Step(`^an empty list$`, empty)", "")
    wd = _go_project(tmp_path, steps)
    assert _outcome(wd, _report(_scenario("AC-1", "passed", "passed", "passed"))) == {
        REF + "AC-1": "catch-all"
    }


def test_go_and_rust_patterns_are_read_from_source(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    (wd / "vendor" / "x").mkdir(parents=True)
    (wd / "vendor" / "x" / "v.go").write_text("sc.Step(`.*`, f)")
    assert source_step_patterns(wd) == [
        ("REGULAR_EXPRESSION", "^an empty list$"),
        ("REGULAR_EXPRESSION", '^I add "([^"]*)"$'),
        ("REGULAR_EXPRESSION", r"^the list has (\d+) items?$"),
    ]
    rs = tmp_path / "rs"
    (rs / "tests").mkdir(parents=True)
    (rs / "Cargo.toml").write_text("[package]\n")
    (rs / "tests" / "cucumber.rs").write_text(
        '#[given("an empty list")]\n#[when(expr = "I add {string}")]\n'
        '#[then(regex = r#"^the "list" has (\\d+)$"#)]\n'
    )
    assert source_step_patterns(rs) == [
        ("REGULAR_EXPRESSION", "^an\\ empty\\ list$"),
        ("CUCUMBER_EXPRESSION", "I add {string}"),
        ("REGULAR_EXPRESSION", r'^the "list" has (\d+)$'),
    ]


def test_fifo_step_source_does_not_hang_and_fails_closed(tmp_path: Path) -> None:
    import os

    wd = _go_project(tmp_path)
    (wd / "bdd" / "steps_test.go").unlink()
    os.mkfifo(wd / "bdd" / "steps_test.go")
    assert source_step_patterns(wd) == []  # nothing read: every step unchecked
    assert _outcome(wd, _report(_scenario("AC-1", "passed", "passed", "passed"))) == {
        REF + "AC-1": "catch-all"
    }


def test_symlinked_step_source_is_not_read(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    outside = tmp_path / "outside.go"
    outside.write_text("sc.Step(`^elsewhere$`, f)")
    (wd / "bdd" / "link.go").symlink_to(outside)
    assert ("REGULAR_EXPRESSION", "^elsewhere$") not in (source_step_patterns(wd) or [])


def test_commented_out_registrations_are_not_read(tmp_path: Path) -> None:
    steps = GO_STEPS + "// sc.Step(`.*`, any)\n/* sc.Step(`(.*)`, any) */\n"
    wd = _go_project(tmp_path, steps + 'var u = "http://x" // sc.Step(`.+`, f)\n')
    pats = [src for _, src in source_step_patterns(wd) or []]
    assert pats == ["^an empty list$", '^I add "([^"]*)"$', r"^the list has (\d+) items?$"]
    rs = tmp_path / "rs"
    rs.mkdir()
    (rs / "Cargo.toml").write_text("[package]\n")
    (rs / "s.rs").write_text('// #[given(regex = ".*")]\n#[given("a // b")]\n')
    assert source_step_patterns(rs) == [("REGULAR_EXPRESSION", "^a\\ //\\ b$")]


def test_go_ignored_dirs_are_not_read(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    for d in ("_old", "testdata"):
        (wd / d).mkdir()
        (wd / d / "x.go").write_text("sc.Step(`.*`, f)\nsc.Step(pat, f)\n")
    assert len(source_step_patterns(wd) or []) == 3


def test_non_literal_godog_pattern_rejects_the_report(tmp_path: Path) -> None:
    steps = GO_STEPS.replace("sc.Step(`^an empty list$`, empty)", "sc.Step(pat, empty)")
    wd = _go_project(tmp_path, steps)
    with pytest.raises(ReportError, match=r"bdd/steps_test.go:4 .*not a string literal"):
        source_step_patterns(wd)
    # A one-argument .Step (a game loop's tick) is not a registration.
    wd2 = _go_project(tmp_path / "b", GO_STEPS + "func tick() { sim.Step(dt) }\n")
    assert len(source_step_patterns(wd2) or []) == 3


def test_cucumber_rs_undefined_step_reported_skipped_is_missing(tmp_path: Path) -> None:
    rs = tmp_path / "wd"
    (rs / "features").mkdir(parents=True)
    (rs / "features" / "T1.feature").write_text(FEATURE)
    (rs / "Cargo.toml").write_text("[package]\n")
    (rs / "cucumber.rs").write_text('#[given("an empty list")]\n')
    # cucumber-rs reports an unmatched step as skipped and drops the rest.
    el = _scenario("AC-1", "passed", "skipped", "skipped")
    el["steps"] = el["steps"][:2]
    assert _outcome(rs, _report(el)) == {REF + "AC-1": "missing"}


def test_json_report_from_a_messages_runner_is_rejected(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "package.json").write_text("{}")
    with pytest.raises(ReportError, match="message:"):
        convert_cucumber_json(_report(_scenario("AC-1", "passed", "passed", "passed")), wd)


def test_verified_read_converts_json(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    path = tmp_path / "r.json"
    path.write_bytes(b"\n " + _report(_scenario("AC-1", "passed", "passed", "passed")))
    envs = read_verified_report(path, record_report(path), wd)
    assert any("testRunFinished" in e for e in envs)
    with pytest.raises(ReportError, match="legacy Cucumber JSON"):
        read_verified_report(path, record_report(path))
    path.write_bytes(b"[{")
    with pytest.raises(ReportError, match="not valid Cucumber JSON"):
        read_verified_report(path, record_report(path), wd)


def test_rejected_report_names_the_stacks_runner(tmp_path: Path) -> None:
    wd = _go_project(tmp_path)
    gate = TestGate(
        cmd=("true",), min_passed=0, bdd_cmd="echo boom >&2; exit 3", profile_name="stack:go"
    )
    obs = gate.execute(wd)
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "missing"}
    assert any("bdd/godog_test.go MUST be exactly" in f for f in obs.findings)
    assert any("exited 3" in f and "boom" in f for f in obs.findings)


# ---- live godog --------------------------------------------------------------

_GODOG = "github.com/cucumber/godog@v0.16.0"
_LIVE_FEATURE = (
    FEATURE
    + """
  Scenario: AC-2
    Given an empty list
    When I remove "milk"
    Then the list has 0 items

  Scenario: AC-3
    Given an empty list
    When I add "milk"
    Then the list has 2 items
"""
)
_LIVE_STEPS = """\
package bdd

import (
	"fmt"

	"github.com/cucumber/godog"
)

func init() {
	register(func(sc *godog.ScenarioContext) {
		var items []string
		sc.Step(`^an empty list$`, func() error { items = nil; return nil })
		sc.Step(`^I add "([^"]*)"$`, func(x string) error { items = append(items, x); return nil })
		sc.Step(`^the list has (\\d+) items?$`, func(n int) error {
			if len(items) != n {
				return fmt.Errorf("want %d got %d", n, len(items))
			}
			return nil
		})
	})
}
"""


def _runner_file() -> str:
    text = BDD_RUNNERS["go"]
    start = text.index("package bdd")
    return text[start : text.index("\n}\n", start) + 3]


def test_live_godog_run_through_the_gate(tmp_path: Path, monkeypatch: Any) -> None:
    if shutil.which("go") is None:
        pytest.skip("go not installed")
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    wd = _go_project(tmp_path, _LIVE_STEPS, _LIVE_FEATURE)
    (wd / "bdd" / "godog_test.go").write_text(_runner_file())
    try:
        got = subprocess.run(["go", "get", _GODOG], cwd=wd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        pytest.skip("go get godog timed out")
    if got.returncode != 0:
        pytest.skip(f"cannot fetch godog: {got.stderr[-300:]!r}")
    gate = TestGate(
        cmd=("true",), min_passed=0, bdd_cmd="go test -count=1 ./bdd", profile_name="stack:go"
    )
    obs = gate.execute(wd)
    assert obs.structured[BDD_KEY] == {
        REF + "AC-1": "passed",
        REF + "AC-2": "missing",
        REF + "AC-3": "failed",
    }, obs.findings
    assert gate.decide(obs) is GateStatus.FAIL
    (wd / "features" / "T1.feature").write_text(FEATURE)
    obs = gate.execute(wd)
    assert obs.structured[BDD_KEY] == {REF + "AC-1": "passed"}, obs.findings
    assert gate.decide(obs) is GateStatus.PASS, obs.findings


def test_live_godog_runner_compiles_before_any_steps(tmp_path: Path) -> None:
    if shutil.which("go") is None:
        pytest.skip("go not installed")
    wd = tmp_path / "wd"
    (wd / "bdd").mkdir(parents=True)
    (wd / "go.mod").write_text("module example.com/shop\n")
    (wd / "bdd" / "godog_test.go").write_text(_runner_file())
    try:
        got = subprocess.run(["go", "get", _GODOG], cwd=wd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        pytest.skip("go get godog timed out")
    if got.returncode != 0:
        pytest.skip(f"cannot fetch godog: {got.stderr[-300:]!r}")
    vet = subprocess.run(
        ["go", "vet", "./..."], cwd=wd, capture_output=True, text=True, timeout=180
    )
    assert vet.returncode == 0, vet.stderr
