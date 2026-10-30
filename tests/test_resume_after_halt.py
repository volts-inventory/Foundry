"""A halted build resumes at the boundary that stopped it (plan §3).

Rerunning the same thread after a HALT re-enters the failed per-wave check, the
final gates, or the Spec gate's re-plan with a fresh fix budget — never a fresh
plan, never a no-op — while a new thread or a new request still plans.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.agents.implementation.tickets import pending_wave
from foundry.cli import _max_epoch, main, run
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates import DevGateWriteVerifier
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.supervisor import (
    RESUME_FINAL,
    RESUME_PHASE,
    RESUME_SPEC,
    RESUME_WAVE,
    build_supervisor_graph,
    resume_boundary,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateStatus, GateTriage
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import EpochCounter, Seq
from tests.planner_payload import PLANNER_SOP_KEY
from tests.test_cli_smoke import _ERROR_DIFF
from tests.test_supervisor import _SCRIPTS, KEY, KEY_ID, _config, _respec_app, _runner
from tests.test_ticket_scheduler import _diffs, _Recorder, _state, _ticket

# Binds the planner payload's scenarios by file name, as the scoped wave check needs.
_STEPS = """from pytest_bdd import scenarios, then

import app

scenarios('features/T-001.feature', 'features/T-SPEC.feature')


@then('Given a user, When they add a todo, Then it appears.')
def _todo_appears():
    assert app.VALUE == 42


@then('Given an unauthenticated request, When it hits /todos, Then 401.')
def _unauthenticated_rejected():
    assert app.VALUE != 401
"""
_RESUME: FactoryState = {"request": "build a todo service", "phase": RESUME_PHASE}


def _stream(app: Any, seed: FactoryState, cfg: Any, records: GlobalRecords) -> list[str]:
    ran: list[str] = []
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        for step in app.stream(seed, cfg, stream_mode="updates"):
            ran.extend(step)
    finally:
        reset_gate_write_verifier(token)
    return ran


def _planner_calls(tx: MockTransport) -> int:
    return sum(PLANNER_SOP_KEY in json.dumps(r) for r in tx.requests)


def test_resume_after_a_wave_check_halt_reruns_the_check_and_continues(
    tmp_path: Path,
) -> None:
    store = ArtifactStore()
    rec = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    rt = AgentRuntime(proxy=ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID)),
                      adapter=rec, cas=store)
    src = tmp_path / "src"
    src.mkdir()
    records, saver = GlobalRecords(), MemorySaver()

    def app(max_fix_attempts: int) -> Any:
        return build_supervisor_graph(
            rt, runner=_runner(records), resolve=store.resolve,
            resolve_blob=store.resolve_blob, source_dir=str(src),
            max_fix_attempts=max_fix_attempts,
        ).compile(checkpointer=saver)

    cfg = _config("wave-halt")
    tickets = [_ticket("T-001", 0, files=("app.py",)),
               _ticket("T-002", 1, files=("later.py",), deps=("T-001",))]
    _stream(app(1), _state(store, tickets), cfg, records)
    halted = app(1).get_state(cfg).values
    assert [t.id for t in pending_wave(halted, store.resolve)] == ["T-002"]
    assert resume_boundary(halted, store.resolve) == RESUME_WAVE
    plans = _planner_calls(tx)

    # The operator reruns with more fix attempts; the fixer can now repair it.
    rec.writes["fix-1"] = {"app.py": "VALUE = 42\n", "test_steps.py": _STEPS}
    rec.calls.clear()
    ran = _stream(app(2), _RESUME, cfg, records)
    assert ran[:2] == ["triage", "implementation"]
    assert "planner" not in ran and _planner_calls(tx) == plans
    assert rec.calls[:2] == ["fix-1", "T-002"]  # re-checked, fixed, then the next wave
    assert "T-001" not in rec.calls  # the built wave is reused
    final = app(2).get_state(cfg).values
    assert {"T-001", "T-002"} <= set(_diffs(final))


def test_resume_after_a_spec_halt_reenters_the_planner_revision(tmp_path: Path) -> None:
    records = GlobalRecords()
    app, tx = _respec_app(tmp_path, records, max_fix_attempts=1)
    cfg = _config("spec-halt")
    _stream(app, {"request": "build a todo app"}, cfg, records)
    halted = app.get_state(cfg).values
    assert resume_boundary(halted, lambda h: None) == RESUME_SPEC
    assert _planner_calls(tx) == 1

    ran = _stream(app, {"request": "build a todo app", "phase": RESUME_PHASE}, cfg, records)
    assert ran[:3] == ["triage", "spec_fix", "planner"]
    prompts = [json.dumps(r) for r in tx.requests if PLANNER_SOP_KEY in json.dumps(r)]
    assert len(prompts) == 2 and "REVISION PASS" in prompts[1]  # a revision, not a re-plan
    spec = [g for g in app.get_state(cfg).values["gates"] if g.name == "spec"]
    assert max(spec, key=lambda g: g.seq).status is GateStatus.PASS


def test_rerun_after_a_final_gate_halt_reenters_the_final_gates(tmp_path: Path) -> None:
    request = "build a todo cli"
    halted = run(request, thread_id="final-halt", state_dir=str(tmp_path),
                 diff_body=_ERROR_DIFF, max_fix_attempts=1)
    assert not halted.passed
    assert halted.resume_from == RESUME_FINAL and halted.resumable
    assert "rerun the same command to resume from the final gates" in halted.summary()

    nodes: list[str] = []
    resumed = run(request, thread_id="final-halt", state_dir=str(tmp_path),
                  progress_cb=lambda node, scope, **_k: nodes.append(node))
    assert resumed.passed
    assert "planner" not in nodes
    assert "build_test_gate" in nodes


def test_a_new_thread_or_request_still_plans(tmp_path: Path) -> None:
    run("build a todo cli", thread_id="t-old", state_dir=str(tmp_path),
        diff_body=_ERROR_DIFF, max_fix_attempts=1)
    for thread, request in (("t-new", "build a todo cli"), ("t-old", "build a notes cli")):
        nodes: list[str] = []
        run(request, thread_id=thread, state_dir=str(tmp_path),
            progress_cb=lambda node, scope, _n=nodes, **_k: _n.append(node))
        assert "planner" in nodes, (thread, request)


def test_the_cli_halt_message_says_how_to_resume(
    tmp_path: Path, capsys: Any, monkeypatch: Any,
) -> None:
    import foundry.cli as cli

    real_run = cli.run
    monkeypatch.setattr(cli, "run", lambda *a, **k: real_run(*a, **k, diff_body=_ERROR_DIFF))
    assert main(["--state-dir", str(tmp_path), "--max-fix-attempts", "1",
                 "build a thing"]) == 1
    assert "rerun the same command to resume from the final gates" in capsys.readouterr().out


def test_an_epoch_floor_outranks_a_resumed_runs_records() -> None:
    epochs = EpochCounter()
    epochs.raise_floor("run", 7)
    assert epochs.bump("run", "k") == 8
    assert epochs.bump("other", "k") == 1


def test_a_resumed_process_seeds_the_floor_from_the_runs_newest_record() -> None:
    def note(run: str, epoch: int) -> GateTriage:
        return GateTriage(name=f"n{epoch}", run_against="x", explanation="", produced_by="p",
                          seq=Seq(run, epoch, 1, "p", 0))

    values = {"gate_triage": [note("run", 3), note("run", 9), note("other", 50)], "phase": "x"}
    assert _max_epoch(values, "run") == 9
