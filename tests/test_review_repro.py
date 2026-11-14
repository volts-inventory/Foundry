"""A review finding backed by a reproduction command is re-run by Foundry on the
final tree: a reproduced one is an attested ``repro`` FAIL that gets one fix pass;
one that does not reproduce stays advisory."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, ZoneBSigner
from foundry.gates.repro import Reproduction, ReproGate, reproduces
from foundry.graph.factory_graph import terminal_gates_pass
from foundry.graph.gate_wiring.review_repro import reproductions, review_candidates
from foundry.graph.supervisor import build_supervisor_graph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
from tests.planner_payload import PLANNER_SOP_KEY, plan_payload
from tests.test_supervisor import (
    _BDD_PASSING_DIFF,
    _SCRIPTS,
    CRITIQUE_PAYLOAD,
    DESIGN_PAYLOAD,
    INTENT_PAYLOAD,
    KEY,
    KEY_ID,
    PRD_PAYLOAD,
    SPRINT_PAYLOAD,
    _config,
    _runner,
    _synthetic_resolve,
)

# ---- the comparison -----------------------------------------------------------


def test_a_run_that_shows_the_observed_result_and_not_the_correct_one_reproduces() -> None:
    run = {"rc": 0, "output": "HTTP 200 OK\nnotes list goes here"}
    assert reproduces("[]", "notes list goes here", run) is True
    assert reproduces("notes list goes here", "[]", run) is False
    # The correct result inside the observed one does not count against it.
    assert reproduces("notes", "stub notes", {"rc": 0, "output": "stub notes"}) is True
    # Whole tokens: an observed "0" is not in a 200 status line.
    assert reproduces("1", "0", {"rc": 0, "output": "HTTP 200 OK\n1"}) is False


def test_json_is_compared_as_values_and_a_crash_by_its_signal() -> None:
    run = {"rc": 0, "output": 'HTTP 200 OK\n{"b": 2, "a": 1}'}
    assert reproduces('{"a": 1, "b": 3}', '{"a":1,"b":2}', run) is True
    assert reproduces('{"a": 1, "b": 2}', '{"a":1,"b":3}', run) is False
    crash = "Traceback (most recent call last): KeyError"
    assert reproduces("0", crash, {"rc": 1, "output": "boom"}) is True
    assert reproduces("0", crash, {"rc": 0, "output": "0"}) is False


def test_a_run_that_cannot_tell_is_inconclusive() -> None:
    assert reproduces("a", "b", {"rc": None, "output": "b"}) is None
    assert reproduces("a", "b", {"rc": 127, "output": "sh: curl: not found"}) is None
    # A command that prints the claim itself proves nothing about the app.
    assert reproduces("ok", "broken", {"rc": 0, "output": "broken"}, "echo broken") is None


def test_the_reviewers_evidence_is_parsed_and_advisory_findings_are_not() -> None:
    findings = (
        "GET /notes is shadowed [evidence: `curl -s http://localhost:5000/notes` "
        "expected '[]', got \"stub, it's a list\"]",
        "[advisory: no failing check or differing reproduction] style nit",
        "test fails [evidence: failing test]",
    )
    (r,) = reproductions(findings)
    assert r == Reproduction(
        finding="GET /notes is shadowed",
        command="curl -s http://localhost:5000/notes",
        correct="[]",
        observed="stub, it's a list",
    )


# ---- the gate, against a launched http app ---------------------------------------

_SHADOWED = """\
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"notes: TODO stub" if self.path.startswith("/notes") else b"home"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""


def test_the_gate_reruns_findings_against_the_launched_app(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_SHADOWED)
    gate = ReproGate(
        (
            Reproduction("GET /notes is a stub", "GET /notes", '[{"id": 1}]', "TODO stub"),
            Reproduction(
                "the stub again, via a URL",
                f'{sys.executable} -c "import urllib.request as u; '
                "print(u.urlopen('http://localhost:8000/notes').read().decode())\"",
                '[{"id": 1}]',
                "notes: TODO stub",
            ),
            Reproduction("home is wrong", "GET /", "home", "500"),
        ),
        tag="review verdict #1",
        app_cmd=f"{sys.executable} app.py",
    )
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.findings
    assert obs.findings[0] == "[review reproduction of review verdict #1]"
    reproduced = [f for f in obs.findings if f.startswith("REPRODUCED")]
    assert len(reproduced) == 2
    assert "advisory, did not reproduce: home is wrong" in obs.findings
    # The reproduced lines carry the evidence, so a later check can re-run them.
    assert len(reproductions(obs.findings)) == 2


# ---- the supervisor: one bounded fix pass for a reproduced finding ---------------

_COMMAND = 'python -c "import app; print(app.NAME)"'
_FIXED_DIFF = _BDD_PASSING_DIFF.replace(
    "@@ -0,0 +1,1 @@\n+VALUE = 42\n", "@@ -0,0 +1,2 @@\n+VALUE = 42\n+NAME = 'notes'\n"
)
_STUB_DIFF = _BDD_PASSING_DIFF.replace(
    "@@ -0,0 +1,1 @@\n+VALUE = 42\n", "@@ -0,0 +1,2 @@\n+VALUE = 42\n+NAME = 'stub'\n"
)


def _verdict(observed: str) -> str:
    return json.dumps(
        {
            "status": "request-changes",
            "findings": [
                {
                    "finding": "GET /notes returns a stub: the route is shadowed",
                    "failing_check": "",
                    "command": _COMMAND,
                    "correct_result": "notes",
                    "observed_result": observed,
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def _plan_blind_diff(monkeypatch: Any) -> None:
    # The canned diff writes test_app.py and test_steps.py whatever files the plan
    # lists, so the file-ownership check would FAIL every tree.
    monkeypatch.setattr("foundry.graph.gate_wiring.runs.owned_paths", lambda *_: None)


class _RecordingAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.fixes: list[tuple[str, ...]] = []

    def run(self, packet: Any, sandbox: Any) -> Any:
        if str(packet.task.get("id", "")).startswith("fix-"):
            self.fixes.append(tuple(packet.prior_failures))
        return super().run(packet, sandbox)


def _run(
    tmp_path: Path, verdicts: list[str], *, fixes_work: bool
) -> tuple[FactoryState, list[str], _RecordingAdapter]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    adapter = _RecordingAdapter()
    plan = plan_payload(
        intent=INTENT_PAYLOAD,
        prd=PRD_PAYLOAD,
        critique=CRITIQUE_PAYLOAD,
        sprint=SPRINT_PAYLOAD,
        design=DESIGN_PAYLOAD,
        # One ticket with both criteria: one wave, so only the final gates judge it.
        decomposition={"tasks": [{"unit": "app.py", "criterion_ids": ["AC-001", "AC-002"]}]},
    )
    tx = MockTransport.scripted(
        {**_SCRIPTS, PLANNER_SOP_KEY: json.dumps(plan)},
        verdicts=verdicts,
        default_verdict="approve",
    )
    cas = ArtifactStore()
    rt = AgentRuntime(
        proxy=ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID)), adapter=adapter, cas=cas
    )

    def _resolve(handle: Artifact) -> FrozenModel:
        if handle.kind == "feature_diff":
            return _synthetic_resolve(handle)
        body: FrozenModel = cas.resolve(handle)
        return body

    def _blob(uri: str) -> bytes:
        return (_FIXED_DIFF if fixes_work and adapter.fixes else _STUB_DIFF).encode()

    records = GlobalRecords()
    app = build_supervisor_graph(
        rt,
        runner=_runner(records),
        resolve=_resolve,
        resolve_blob=_blob,
        source_dir=str(src),
    ).compile(checkpointer=MemorySaver())
    config = _config(f"run-repro-{tmp_path.name}")
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        nodes = [
            n
            for step in app.stream({"request": "build a todo app"}, config, stream_mode="updates")
            for n in step
        ]
        final: FactoryState = app.get_state(config).values
    finally:
        reset_gate_write_verifier(token)
    return final, nodes, adapter


def _newest_repro(final: FactoryState) -> Any:
    return max((g for g in final["gates"] if g.name == "repro"), key=lambda g: g.seq)


def test_a_reproducing_finding_gets_a_fix_pass_and_its_fix_releases(tmp_path: Path) -> None:
    final, nodes, adapter = _run(tmp_path, [_verdict("stub")], fixes_work=True)

    assert nodes.count("build_test_fix") == 1
    (failures,) = adapter.fixes
    assert any("REPRODUCED on the final tree" in f and "shadowed" in f for f in failures)
    repros = [g for g in final["gates"] if g.name == "repro"]
    assert {g.status for g in repros} == {GateStatus.FAIL, GateStatus.PASS}
    # The re-review approved; the carried finding was re-run and no longer reproduces.
    assert _newest_repro(final).status is GateStatus.PASS
    assert final["budget"].review_fixes == 1
    assert terminal_gates_pass(final)


def test_a_finding_that_does_not_reproduce_stays_advisory(tmp_path: Path) -> None:
    # The reviewer claims "broken"; Foundry's run prints "stub", not "broken".
    final, nodes, adapter = _run(tmp_path, [_verdict("broken")], fixes_work=False)

    assert nodes.count("review_check") == 1
    assert "build_test_fix" not in nodes
    assert not adapter.fixes
    repro = _newest_repro(final)
    assert repro.status is GateStatus.PASS
    assert any("did not reproduce" in f for f in repro.findings)
    assert terminal_gates_pass(final)


def test_the_review_fix_cap_holds(tmp_path: Path) -> None:
    # Every review repeats the finding and the fix changes nothing.
    final, nodes, adapter = _run(tmp_path, [_verdict("stub")] * 6, fixes_work=False)

    assert nodes.count("build_test_fix") == 1
    assert len(adapter.fixes) == 1
    assert nodes.count("review_check") == 2
    assert nodes[-1] == "review_check"
    assert _newest_repro(final).status is GateStatus.FAIL
    assert final["budget"].review_fixes == 2
    assert not terminal_gates_pass(final)


def _repro(status: GateStatus, epoch: int, findings: tuple[str, ...]) -> GateResult:
    return GateResult(
        name="repro",
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=findings,
        run_against=f"tree-{epoch}",
        seq=Seq("run", epoch, 1, "review_check", 0),
    )


def test_a_check_that_errored_keeps_the_last_reproduced_finding() -> None:
    reproduced = (
        "[review reproduction of review verdict #1]",
        f"REPRODUCED on the final tree: shadowed [evidence: `{_COMMAND}` "
        "expected 'notes', got 'stub']",
    )
    errored = ("review reproduction driver exited 1 with no result",)
    state: FactoryState = {
        "gates": [_repro(GateStatus.FAIL, 1, reproduced), _repro(GateStatus.ERROR, 2, errored)]
    }
    _, carried = review_candidates(state, None)
    assert [r.finding for r in carried] == ["shadowed"]
    assert not terminal_gates_pass(state)


# ---- a Python app with dependencies: the check installs them --------------------


def _tinydep_app(tmp_path: Path) -> Path:
    from tests.test_python_deps import _wheel

    whl = _wheel(tmp_path / "wheels", "tinydep", {"tinydep/__init__.py": "NAME = 'stub'\n"})
    app = tmp_path / "app"
    app.mkdir()
    (app / "requirements.txt").write_text(f"tinydep @ {whl.as_uri()}\n")
    (app / "app.py").write_text("import tinydep\n\nNAME = tinydep.NAME\n")
    return app


def _review_check(
    tmp_path: Path, monkeypatch: Any, app: Path, *, rt: AgentRuntime | None = None
) -> list[GateResult]:
    import shutil

    import foundry.graph.gate_wiring.materialize as materialize
    import foundry.graph.gate_wiring.review_repro as review_repro

    def _materialize(state: Any, workdir: Path, **_: Any) -> str:
        shutil.copytree(app, workdir, dirs_exist_ok=True)
        return "tree-1"

    monkeypatch.setattr(materialize, "materialize_workdir", _materialize)
    repro = Reproduction("NAME is a stub", _COMMAND, "notes", "stub")
    monkeypatch.setattr(review_repro, "review_candidates", lambda *_: ("review #1", (repro,)))
    rt = rt or AgentRuntime(proxy=ModelProviderProxy(MockTransport.scripted({})))
    out = review_repro.run_review_check(
        {},
        _config("run-deps"),
        rt=rt,
        runner=_runner(GlobalRecords()),
        resolve=lambda a: a,  # type: ignore[arg-type,return-value]
        resolve_blob=lambda u: b"",
        workdir=tmp_path / "work",
    )
    return list(out["gates"])


@pytest.fixture()
def _allow_install(monkeypatch: Any) -> None:
    # The suite runs the gates in-process, where the networked install is refused
    # unless accepted (the dependency is a local wheel: nothing leaves the host).
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")


def test_the_check_installs_a_python_apps_dependencies(
    tmp_path: Path, monkeypatch: Any, _allow_install: None
) -> None:
    # f4c550b's check opened the build env but never installed into it, so every
    # Python app with a requirements.txt ERRORed ("the build's Python dependency
    # install did not complete") and shipped with its backed findings never re-run.
    (gate,) = _review_check(tmp_path, monkeypatch, _tinydep_app(tmp_path))
    assert gate.status is GateStatus.FAIL, gate.findings
    assert any(f.startswith("REPRODUCED") for f in gate.findings)


def test_the_check_reuses_the_final_gates_install(
    tmp_path: Path, monkeypatch: Any, _allow_install: None
) -> None:
    import foundry.gates.pydeps as pydeps

    # A file: URL is never cached (local content can change): pin a digest instead.
    monkeypatch.setattr(pydeps, "manifest_digest", lambda *_: "d" * 64)
    app = _tinydep_app(tmp_path)
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport.scripted({})))
    with pydeps.python_build_env(app, python=True, cache=rt.python_envs) as pyenv:
        assert pyenv is not None
        assert pyenv.install(app, timeout_s=120)[0] == 0

    def _no_pip(*_: Any, **__: Any) -> tuple[int, str, str]:
        raise AssertionError("the check re-ran pip instead of reusing the install")

    monkeypatch.setattr(pydeps, "ground_untrusted", _no_pip)
    (gate,) = _review_check(tmp_path, monkeypatch, app, rt=rt)
    assert gate.status is GateStatus.FAIL, gate.findings


def test_a_check_that_cannot_run_is_retried_once_then_holds_the_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import foundry.gates.repro as repro_mod
    from foundry.graph.gate_wiring.review_repro import route_review_check

    calls: list[int] = []

    def _no_driver(*_: Any, **__: Any) -> tuple[int, str, str]:
        calls.append(1)
        return 1, "", "boom"

    monkeypatch.setattr(repro_mod, "ground_untrusted", _no_driver)
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text("NAME = 'stub'\n")
    (gate,) = _review_check(tmp_path, monkeypatch, app)
    assert gate.status is GateStatus.ERROR
    assert len(calls) == 2

    build = gate.model_copy(update={"name": "build", "status": GateStatus.PASS})
    state: FactoryState = {"gates": [build, gate]}
    assert route_review_check(state, max_review_fixes=1) == "halt"
    # Every other gate green: still held, the backed finding was never re-run.
    monkeypatch.setattr("foundry.graph.factory_graph._gates_all_pass", lambda *_: True)
    assert not terminal_gates_pass(state)
    # A later tree the check has not judged is not held by the old ERROR.
    newer = build.model_copy(update={"run_against": "tree-2"})
    assert terminal_gates_pass({"gates": [newer, gate]})
