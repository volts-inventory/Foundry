"""Executable BDD: feature rendering/materialization and the Test gate's scenario
accounting, run against a real pytest-bdd project in ``tmp_path``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from foundry.agents._common import emit_artifact
from foundry.gates.test import BDD_KEY, TestGate
from foundry.graph.gate_wiring import (
    CRITERION_KIND,
    FEATURE_DIFF_KIND,
    materialize_workdir,
    render_feature,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff, scenario_ref
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq


def _seq(node: str, local: int = 0) -> Seq:
    return Seq(run_id="run-1", emission_epoch=1, superstep_index=0, node_id=node, local=local)


def _ac(cid: str, ticket: str | None = "T1", **kw: Any) -> AcceptanceCriterion:
    steps: dict[str, Any] = {
        "given": ("an empty list",),
        "when": ('I add "milk"',),
        "then": ("the list has 1 item",),
    }
    steps.update(kw)
    return AcceptanceCriterion(
        id=cid,
        text="Given … When … Then …",
        ticket_id=ticket,
        oracle_hint=scenario_ref(ticket, cid) if ticket else "",
        seq=_seq("planner"),
        **steps,
    )


def _snapshot(files: dict[str, str]) -> str:
    import json

    return json.dumps({"kind": "full-file-snapshot", "files": files})


def _state(criteria: tuple[AcceptanceCriterion, ...], files: dict[str, str] | None = None):
    artifacts: list[Artifact] = []
    by_uri: dict[str, Any] = {}
    for c in criteria:
        h = emit_artifact(c, CRITERION_KIND, "planner", c.seq)
        artifacts.append(h)
        by_uri[h.uri] = c
    blobs: dict[str, str] = {}
    if files is not None:
        diff = FeatureDiff(
            diff_uri="cas://sha256/d1", task_id="T1", branch="task/T1",
            base_tree_sha="base", seq=_seq("junior_engineer"),
        )
        h = Artifact(kind=FEATURE_DIFF_KIND, uri="cas://fd/T1", produced_by="junior_engineer",
                     task_id="T1", seq=diff.seq)
        artifacts.append(h)
        by_uri[h.uri] = diff
        blobs[diff.diff_uri] = _snapshot(files)
    return {"artifacts": artifacts}, (lambda a: by_uri[a.uri]), (lambda u: blobs[u])


# ---- rendering ---------------------------------------------------------------


def test_render_feature_is_deterministic_gherkin() -> None:
    a = _ac("AC-2", given=("a list\nwith  newline", "a user"), when=("I look",), then=("ok",))
    b = _ac("AC-1")
    body = render_feature("T1", [a, b])
    assert body == render_feature("T1", [b, a])
    assert body == (
        "Feature: T1\n\n"
        "  Scenario: AC-1\n"
        "    Given an empty list\n"
        '    When I add "milk"\n'
        "    Then the list has 1 item\n\n"
        "  Scenario: AC-2\n"
        "    Given a list with newline\n"
        "    And a user\n"
        "    When I look\n"
        "    Then ok\n"
    )


def test_features_materialized_per_ticket_and_kept_out_of_gate_inputs(tmp_path: Path) -> None:
    criteria = (_ac("AC-1"), _ac("AC-2", ticket="T2"), _ac("AC-3", ticket=None))
    # An agent edit to a trusted feature and a stray feature are both overridden.
    files = {
        "app.py": "X = 1\n",
        "features/T1.feature": "Feature: T1\n",
        "features/fake.feature": "Feature: fake\n",
    }
    state, resolve, blob = _state(criteria, files)
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    feats = sorted(p.name for p in (tmp_path / "features").iterdir())
    assert feats == ["T1.feature", "T2.feature"]
    assert "Scenario: AC-1" in (tmp_path / "features/T1.feature").read_text()
    assert "AC-3" not in (tmp_path / "features/T2.feature").read_text()
    assert "pytest-bdd" in (tmp_path / "requirements.txt").read_text()


def test_output_tree_ships_features_but_not_criteria(tmp_path: Path) -> None:
    from foundry.cli import _write_output_tree

    state, resolve, blob = _state((_ac("AC-1"),), {"app.py": "X = 1\n"})
    out = Path(_write_output_tree(state, str(tmp_path / "out"), resolve=resolve, resolve_blob=blob))
    assert (out / "features/T1.feature").is_file()
    assert not (out / "criteria.json").exists()


def test_foreign_profile_gets_advisory_triage(tmp_path: Path) -> None:
    from foundry.agents import AgentRuntime
    from foundry.gates import TwoZoneGateRunner, ZoneBSigner
    from foundry.gates.profiles import VerificationProfile
    from foundry.graph.gate_wiring import run_build_test_gates
    from foundry.proxy import ModelProviderProxy
    from foundry.proxy.test_doubles import MockTransport
    from foundry.state.global_records import GlobalRecords

    key = b"zone-b-dev-key-0123456789abcdef"
    runner = TwoZoneGateRunner(ZoneBSigner(key, "k"), GlobalRecords(), "s", "l")
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport(), signer=ZoneBSigner(key, "k")))
    config: Any = {"configurable": {"thread_id": "run-1"}, "metadata": {"langgraph_step": 1}}
    state, resolve, blob = _state((_ac("AC-1"),), {"app.py": "X = 1\n"})
    profile = VerificationProfile(name="node-web", build_cmd=("true",), test_cmd=("true",))
    out = run_build_test_gates(
        state, config, rt=rt, runner=runner, resolve=resolve, resolve_blob=blob,
        workdir=tmp_path, profile=profile,
    )
    assert [g.status for g in out["gates"]] == [GateStatus.PASS, GateStatus.PASS]
    (triage,) = out["gate_triage"]
    assert "node-web" in triage.explanation and "not enforced" in triage.explanation


def test_no_ticketed_criteria_writes_no_features(tmp_path: Path) -> None:
    state, resolve, blob = _state((_ac("AC-1", ticket=None),), {"app.py": "X = 1\n"})
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    assert not (tmp_path / "features").exists()
    assert "pytest-bdd" not in (tmp_path / "requirements.txt").read_text()


# ---- the gate over a real pytest-bdd project ----------------------------------

_STEPS = '''\
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/T1.feature")


@given("an empty list", target_fixture="items")
def _empty():
    return []


@when(parsers.parse('I add "{x}"'))
def _add(items, x):
    items.append(x)


@then(parsers.parse("the list has {n:d} item"))
def _count(items, n):
    assert len(items) == {expected}
'''


def _project(tmp_path: Path, steps: str | None) -> Path:
    criteria = (_ac("AC-1"), _ac("AC-2"))
    files = {"test_plain.py": "def test_plain():\n    assert True\n"}
    if steps is not None:
        files["tests/test_t1.py"] = steps
    state, resolve, blob = _state(criteria, files)
    materialize_workdir(state, tmp_path, resolve=resolve, resolve_blob=blob)
    return tmp_path


def test_gate_passes_when_every_scenario_passes(tmp_path: Path) -> None:
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "n")))
    assert obs.structured[BDD_KEY] == {
        "features/T1.feature::AC-1": "passed",
        "features/T1.feature::AC-2": "passed",
    }, obs.stdout
    assert gate.decide(obs) is GateStatus.PASS


def test_gate_passes_inside_namespace_sandbox(tmp_path: Path, monkeypatch: Any) -> None:
    import pytest

    from foundry.gates.sandbox import unshare_available

    if not unshare_available():
        pytest.skip("no unprivileged user namespaces")
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "n")))
    assert gate.decide(obs) is GateStatus.PASS, obs.stdout + obs.stderr


def test_gate_fails_on_failing_scenario(tmp_path: Path) -> None:
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, _STEPS.replace("{expected}", "99")))
    assert gate.decide(obs) is GateStatus.FAIL
    assert set(obs.structured[BDD_KEY].values()) == {"failed"}
    assert any("BDD criterion AC-1" in f and "failed" in f for f in obs.findings)


def test_gate_fails_on_missing_step_definitions(tmp_path: Path) -> None:
    unbound = 'from pytest_bdd import scenarios\n\nscenarios("../features/T1.feature")\n'
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, unbound))
    assert gate.decide(obs) is GateStatus.FAIL
    assert set(obs.structured[BDD_KEY].values()) == {"failed"}


def test_gate_fails_when_scenarios_are_never_bound(tmp_path: Path) -> None:
    gate = TestGate()
    obs = gate.execute(_project(tmp_path, None))  # plain test passes; feature unbound
    assert obs.exit_codes == (0,)
    assert gate.decide(obs) is GateStatus.FAIL
    assert [f.split(":")[0] for f in obs.findings[:2]] == [
        "BDD criterion AC-1", "BDD criterion AC-2",
    ]
    assert all("not executed" in f for f in obs.findings[:2])


def test_gate_without_features_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_a():\n    assert True\n")
    gate = TestGate()
    obs = gate.execute(tmp_path)
    assert BDD_KEY not in obs.structured
    assert obs.structured["cmd"] == list(gate.cmd)
    assert gate.decide(obs) is GateStatus.PASS


def test_foreign_harness_ignores_features(tmp_path: Path) -> None:
    _project(tmp_path, None)
    gate = TestGate(cmd=("/bin/sh", "-c", "true"), min_passed=0)
    obs = gate.execute(tmp_path)
    assert BDD_KEY not in obs.structured
    assert gate.decide(obs) is GateStatus.PASS
