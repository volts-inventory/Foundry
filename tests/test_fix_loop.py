"""The wave fix loop: each fixer pass gets a structured summary of the failure and
of the previous pass, and a pass that leaves the same checks failing halts the
wave instead of spending the remaining attempts."""

from __future__ import annotations

from pathlib import Path

from langchain_core.runnables import RunnableConfig

from foundry.agents.implementation.junior_engineer import _fix_summary, make_fixer_node
from foundry.agents.implementation.tickets import pending_wave
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.gates.test import failure_findings
from foundry.graph.gate_wiring import (
    BUILD_BOUNDARY,
    BUILD_TEST_BOUNDARY,
    NO_FIX_PROGRESS_TRIAGE,
    PIP_MANIFESTS,
    _unchanged_tree_recheck,
    failing_ids,
    failure_files,
    no_fix_progress,
)
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus, GateTriage, VerdictClass
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.test_ticket_scheduler import _config, _Recorder, _runtime, _ticket, _wave_run
from tests.test_ticket_scheduler import _state as _ticket_state

_CFG: RunnableConfig = {"configurable": {}}


def _gate(name: str, tree: str, local: int, status: GateStatus,
          *findings: str) -> GateResult:
    return GateResult(
        name=name, status=status, decision_deterministic=True,
        execution_deterministic=True, verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=findings, run_against=tree, seq=Seq("run", 1, local, "gate", 0),
    )


def _state(*gates: GateResult, attempts: int = 2) -> FactoryState:
    return {"gates": list(gates),
            "budget": BudgetLedger(fix_attempts_since_all_green=attempts)}


_FAIL_A = "FAILED tests/test_app.py::test_add - assert 1 == 2"
_FAIL_B = "FAILED tests/test_app.py::test_list - assert [] == [1]"
_BDD = "BDD criterion AC-1: scenario features/T-001.feature::AC-1 was not executed"
_OK = GateStatus.PASS
_RED = GateStatus.FAIL


def test_failing_ids_names_pytest_nodes_and_bdd_scenarios() -> None:
    assert failing_ids([_FAIL_A, "E   assert 1 == 2", _BDD, _FAIL_A]) == (
        "tests/test_app.py::test_add", "features/T-001.feature::AC-1")


def test_the_same_failing_tests_after_a_fix_is_no_progress() -> None:
    # The output around the failure may differ; the failing set is what counts.
    state = _state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A, "x"),
                   _gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _FAIL_A, "y"))
    assert no_fix_progress(state, _CFG, boundary=BUILD_TEST_BOUNDARY)


def test_a_changed_failing_set_is_progress() -> None:
    state = _state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A, _FAIL_B),
                   _gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _FAIL_A))
    assert not no_fix_progress(state, _CFG, boundary=BUILD_TEST_BOUNDARY)


def test_an_error_or_a_fresh_budget_is_never_no_progress() -> None:
    same = (_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A),
            _gate("build", "t2", 3, _OK))
    errored = _gate("test", "t2", 4, GateStatus.ERROR, _FAIL_A)
    assert not no_fix_progress(_state(*same, errored), _CFG, boundary=BUILD_TEST_BOUNDARY)
    # A resumed run's first re-check (budget reseeded) has had no fix pass yet.
    failed = _gate("test", "t2", 4, _RED, _FAIL_A)
    assert not no_fix_progress(_state(*same, failed, attempts=1), _CFG,
                               boundary=BUILD_TEST_BOUNDARY)


def _fix_diff_state(*gates: GateResult, cut_off: bool) -> tuple[FactoryState, ArtifactStore]:
    import json

    from foundry.agents._common import emit_artifact
    from foundry.schemas.implementation import FeatureDiff

    store = ArtifactStore()
    blob = store.put_blob(json.dumps({"kind": "full-file-snapshot",
                                      "files": {"services/_db.py": "X = 1\n"}}).encode())
    seq = Seq("run", 1, 9, "fixer", 0)
    diff = FeatureDiff(diff_uri=blob, task_id="fix-1", branch="task/fix-1",
                       base_tree_sha="b", seq=seq)
    art = emit_artifact(diff, "feature_diff", "fixer", seq, store=store).model_copy(
        update={"task_id": "fix-1"})
    report = ("turn budget exhausted before 'done' — partial work recorded" if cut_off
              else "moved the connection to services/_db.py")
    note = GateTriage(name="adapter-report", run_against="task/fix-1", explanation=report,
                      produced_by="proxy-agent", seq=Seq("run", 0, 0, "proxy-agent", 0))
    return {**_state(*gates), "artifacts": [art], "gate_triage": [note]}, store


_MYPY = 'tests/test_t3.py:6: error: Module "services.product" has no attribute "reset"'


def test_a_cut_off_pass_that_broke_a_passing_gate_is_named_unfinished() -> None:
    # The local shop run: fix-6 moved the DB helpers to services/_db.py, ran out of
    # turns before it updated the tests importing them, and broke a passing build.
    state, store = _fix_diff_state(
        _gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A),
        _gate("build", "t2", 3, _RED, _MYPY), cut_off=True)
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=2)
    assert ("previous fix pass (fix-1) touched services/_db.py and broke the build gate(s), "
            "which passed before it (it ran out of turns before it finished): its change is "
            "incomplete. Finish it first — update every caller and test of what it renamed, "
            "moved or removed — or undo it") in summary
    assert not any("try a different approach" in line for line in summary)


def test_a_cut_off_pass_without_a_regression_is_checked_before_a_new_approach() -> None:
    state, store = _fix_diff_state(
        _gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A),
        _gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _FAIL_B), cut_off=True)
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=2)
    assert ("previous fix pass (fix-1) touched services/_db.py and ran out of turns before "
            "it finished, leaving the gates above failing: check whether its edits are "
            "complete before trying a different approach") in summary


def test_a_finished_pass_that_failed_still_tries_a_different_approach() -> None:
    state, store = _fix_diff_state(
        _gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A),
        _gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _FAIL_B), cut_off=False)
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=2)
    assert ("previous fix pass (fix-1) touched services/_db.py and left the gates above "
            "failing: try a different approach") in summary


def test_the_summary_names_the_failing_gates_and_tests() -> None:
    state = _state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A, _BDD))
    summary = _fix_summary(state, lambda a: None, lambda u: b"", attempt=1)
    assert summary == (
        "[fix summary]",
        "failed gates: test",
        "failing tests/scenarios: tests/test_app.py::test_add, features/T-001.feature::AC-1",
        "gate output (tail):",
    )


def test_each_fix_pass_gets_the_failure_and_the_previous_pass_summarized(
    tmp_path: Path,
) -> None:
    rec = _Recorder(writes={
        "T-001": {"app.py": "def broken(:\n"},
        "fix-1": {"app.py": "def broken_1(:\n"},
        "fix-2": {"app.py": "def fixed():\n    return 1\n"},
    })
    _out, ran, _ = _wave_run(tmp_path, rec)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "engineering"]
    first = rec.packets["fix-1"].prior_failures
    assert first[:2] == ("[fix summary]", "failed gates: build")
    assert first[2].startswith("failing tests/scenarios: none named")
    assert not any(line.startswith("previous fix pass") for line in first)
    second = list(rec.packets["fix-2"].prior_failures)
    assert ("previous fix pass (fix-1) touched app.py, rogue.txt and left the gates above "
            "failing: try a different approach") in second
    assert any(line.startswith("previous fix pass reported: the fixer wrote outside")
               and "rogue.txt" in line for line in second)
    tail = second.index("gate output (tail):")
    assert second[tail + 1] == "[build gate FAILED]"
    assert any("broken_1" in line for line in second[tail:])  # this pass's output


def test_a_fix_pass_with_no_progress_halts_the_wave(tmp_path: Path) -> None:
    rec = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})  # the fixer can't fix
    out, ran, store = _wave_run(tmp_path, rec, max_fix_attempts=4)
    # Two attempts left, none spent: the same failure ends the wave.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate"]
    assert rec.calls == ["T-001", "fix-1"]
    assert {g.status for g in out["gates"]} == {GateStatus.FAIL}
    assert [t.id for t in pending_wave(out, store.resolve)] == ["T-002"]  # supervisor halts
    assert any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])


def test_a_fix_that_changes_nothing_is_no_progress() -> None:
    # The re-check on the same tree replaces the earlier verdict in state, so only
    # the check itself (old state + new verdicts) can see the unchanged tree.
    before = _state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, _FAIL_A),
                    attempts=1)
    recheck = {"gates": [_gate("build", "t1", 3, _OK), _gate("test", "t1", 4, _RED, _FAIL_A)]}
    assert _unchanged_tree_recheck(before, recheck, _CFG, boundary=BUILD_TEST_BOUNDARY)
    errored = {"gates": [_gate("build", "t1", 3, _OK),
                         _gate("test", "t1", 4, GateStatus.ERROR, _FAIL_A)]}
    assert not _unchanged_tree_recheck(before, errored, _CFG, boundary=BUILD_TEST_BOUNDARY)
    fresh = _state(*before["gates"], attempts=0)  # resumed run: no fix pass yet
    assert not _unchanged_tree_recheck(fresh, recheck, _CFG, boundary=BUILD_TEST_BOUNDARY)
    moved = {"gates": [_gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _FAIL_A)]}
    assert not _unchanged_tree_recheck(before, moved, _CFG, boundary=BUILD_TEST_BOUNDARY)


def test_an_empty_fix_diff_halts_the_wave(tmp_path: Path) -> None:
    broken = {"app.py": "def broken(:\n"}
    rec = _Recorder(writes={"T-001": broken, "fix-1": broken})  # fixer changes nothing
    # T-001 owns rogue.txt too, so the recorder's out-of-scope write is no change.
    tickets = [_ticket("T-001", 0, files=("app.py", "rogue.txt")),
               _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4, tickets=tickets)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate"]
    assert len({g.run_against for g in out["gates"]}) == 1
    assert {g.status for g in out["gates"]} == {GateStatus.FAIL}
    assert any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])


def test_raw_findings_compare_without_addresses_or_timings() -> None:
    first = _state(_gate("build", "t1", 1, _RED, "<Obj at 0x7f3a1c> broke in 1.23s"),
                   attempts=2)
    second = _gate("build", "t2", 2, _RED, "<Obj at 0x7f9e00> broke in 0.98s")
    assert no_fix_progress(_state(*first["gates"], second), _CFG, boundary=BUILD_BOUNDARY)


def test_the_failing_test_summary_survives_the_findings_cap() -> None:
    detail = "\n".join(f"E   assert {i} == {i + 1}" for i in range(200))
    tail = "\n".join(f"FAILED tests/test_app.py::test_{i} - assert" for i in range(5))
    findings = failure_findings(f"{detail}\n=== short test summary info ===\n{tail}\n")
    assert len(findings) == 50
    assert findings[0] == "E   assert 0 == 1"
    assert failing_ids(findings) == tuple(f"tests/test_app.py::test_{i}" for i in range(5))


_PIP = ("pip install command exited 1 (the project's declared dependencies cannot be "
        "installed — FAIL: fix requirements.txt / pyproject.toml); last 2 output line(s):",
        "ERROR: Could not find a version that satisfies the requirement sqlite3==2.6.0",
        "ERROR: No matching distribution found for sqlite3==2.6.0")


def test_failure_files_names_the_manifest_and_the_erroring_sources() -> None:
    assert failure_files([_gate("build", "t1", 1, _RED, *_PIP)]) == PIP_MANIFESTS
    build = _gate("build", "t1", 1, _RED, "app/models.py:12: error: bad type",
                  "./main.go:3:2: undefined: x", "src/a.ts(4,5): error TS2322",
                  '  File "./app.py", line 1', ".venv/lib/x.py:9: note",
                  "/usr/lib/python3/y.py:2: oops", "../out.py:1: no")
    assert failure_files([build]) == ("app/models.py", "main.go", "src/a.ts", "app.py")
    # A test traceback names the test file, not where the fix goes.
    assert failure_files([_gate("test", "t1", 1, _RED, "tests/test_a.py:3: AssertionError")]) == ()


def test_a_step_binding_error_names_the_step_module_to_edit_first() -> None:
    # pytest-bdd's error names the generated test, not a file:line: two local fix
    # passes got no "Edit these files first" for the step module they had to fix.
    test = _gate("test", "t1", 1, _RED,
                 "BDD criterion S-4: scenario features/T3.feature::S-4 failed",
                 "E       fixture 'note_for_delete' not found",
                 "FAILED tests/test_t3.py::test_s4", "FAILED tests/test_t3.py::test_s5")
    assert failure_files([test]) == ("tests/test_t3.py",)
    missing = _gate("test", "t1", 1, _RED, "E   pytest_bdd.exceptions.StepDefinitionNotFoundError:"
                    ' Step definition is not found: Given "x"', "FAILED tests/test_t1.py::test_a")
    assert failure_files([missing]) == ("tests/test_t1.py",)
    tree = {"tests/test_t3.py": "from pytest_bdd import given\n"}
    summary = _fix_summary(_state(test), lambda a: None, lambda u: b"", attempt=1, tree=tree)
    assert "Edit these files first: tests/test_t3.py (the failure names them)" in summary
    assert "current content of tests/test_t3.py:\nfrom pytest_bdd import given\n" in summary


def test_a_pip_manifest_failure_leads_with_the_manifest_inlined() -> None:
    state = _state(_gate("build", "t1", 1, _RED, *_PIP))
    tree = {"requirements.txt": "Flask==3.0.0\nsqlite3==2.6.0\n", "app.py": "x = 1\n"}
    summary = _fix_summary(state, lambda a: None, lambda u: b"", attempt=1, tree=tree)
    assert summary[3] == "Edit these files first: requirements.txt (the failure names them)"
    assert "cannot be re-run in this sandbox" in summary[4]
    assert "current content of requirements.txt:\nFlask==3.0.0\nsqlite3==2.6.0\n" in summary
    assert summary[-1] == "gate output (tail):"


def test_inlined_files_are_capped() -> None:
    lines = [f"m{i}.py:1: error: e" for i in range(5)]
    state = _state(_gate("build", "t1", 1, _RED, *lines))
    tree: dict[str, str | None] = {f"m{i}.py": f"# {i}\n" for i in range(5)}
    tree["m0.py"] = "x" * 9_000  # over the cap: left for the agent to read
    summary = _fix_summary(state, lambda a: None, lambda u: b"", attempt=1, tree=tree)
    inlined = [line.split(":")[0] for line in summary if line.startswith("current content")]
    assert inlined == ["current content of m1.py", "current content of m2.py"]


class _CappedFixer(_Recorder):
    """A fixer session that runs out of turns without editing app.py."""

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        result = super().run(packet, sandbox)
        tid = str(packet.task["id"])
        if not tid.startswith("fix-"):
            return result
        note = GateTriage(name="adapter-report", run_against=f"task/{tid}",
                          explanation="turn budget exhausted before 'done' — partial work",
                          produced_by="recorder", seq=Seq("run", 0, 0, "recorder", 0))
        return result.model_copy(update={"triage": (note,)})


def test_a_capped_pass_that_missed_the_failing_file_does_not_halt(tmp_path: Path) -> None:
    rec = _CappedFixer(writes={"T-001": {"app.py": "def broken(:\n"}})
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4)
    # Every pass is allowed, but the wave's fix budget (3 passes) still bounds it.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "fixer", "wave_gate"]
    assert not any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])
    assert ("previous fix pass never edited app.py, the file(s) the failure names: edit "
            "them before anything else") in rec.packets["fix-2"].prior_failures


class _LocalRecorder(_Recorder):
    def is_local(self, tier: str | None) -> bool:
        return True


def test_local_sessions_get_a_larger_turn_budget(tmp_path: Path) -> None:
    # A local ticket session takes one action per turn: on the local notes build 8
    # of 14 tickets hit 25 turns, one before it wrote the module binding its scenarios.
    rec = _LocalRecorder(writes={"T-001": {"app.py": "def broken(:\n"}})
    _wave_run(tmp_path, rec)
    assert rec.packets["T-001"].turn_budget == 40
    assert rec.packets["fix-1"].turn_budget == 40
    plain = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})
    (tmp_path / "cloud").mkdir()
    _wave_run(tmp_path / "cloud", plain)
    assert plain.packets["T-001"].turn_budget == 25
    assert plain.packets["fix-1"].turn_budget == 25


def _unbound_state(store: ArtifactStore) -> FactoryState:
    import json

    from foundry.agents._common import emit_artifact
    from foundry.schemas.implementation import FeatureDiff

    # The local notes run: E4-T04 ran out of turns after writing only its routes
    # module, so its scenarios were never bound and its step module never existed.
    ticket = _ticket("E4-T04", 0, files=("routes/notes.py", "templates/notes.html",
                                         "tests/test_e4_t04.py"),
                     scenario_ids=("E4-AC10", "E4-AC11"))
    other = _ticket("E4-T05", 1, files=("smoke.py",), scenario_ids=("E4-AC16",))
    seq = Seq("run", 1, 9, "junior_engineer", 0)
    blob = store.put_blob(json.dumps({"kind": "full-file-snapshot",
                                      "files": {"routes/notes.py": "x = 1\n"}}).encode())
    diff = emit_artifact(
        FeatureDiff(diff_uri=blob, task_id="E4-T04", branch="task/E4-T04",
                    base_tree_sha="b", seq=seq),
        "feature_diff", "junior_engineer", seq, store=store,
    ).model_copy(update={"task_id": "E4-T04"})
    unbound = [f"BDD criterion E4-AC1{i}: scenario features/E4-T04.feature::E4-AC1{i} was "
               "not executed — bind that exact gate-owned file" for i in (0, 1)]
    return {**_ticket_state(store, [ticket, other], artifacts=[diff]),
            **_state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, *unbound),
                     attempts=1)}


def test_a_fix_pass_is_told_to_create_the_files_an_unfinished_ticket_never_wrote() -> None:
    store = ArtifactStore()
    tree: dict[str, str | None] = {"routes/notes.py": "x = 1\n", "smoke.py": None}
    summary = _fix_summary(_unbound_state(store), store.resolve, store.resolve_blob,
                           attempt=1, tree=tree)
    assert ("Create these files first: templates/notes.html, tests/test_e4_t04.py. Ticket "
            "E4-T04 owns them and its failing scenarios need them, but its session ended "
            "before it wrote them: writing them is this fix, not a later ticket's work "
            '(bind the scenarios in tests/test_e4_t04.py: '
            'scenarios("../features/E4-T04.feature"))') in summary
    # E4-T05 is not built and fails nothing: its missing files are a later wave's.
    assert not any("smoke.py" in line for line in summary)


def test_an_empty_fix_pass_is_not_blamed_for_the_wave_that_broke_a_gate() -> None:
    import json

    from foundry.agents._common import emit_artifact
    from foundry.schemas.implementation import FeatureDiff

    # The test gate passed on the previous wave's tree and failed on the new wave's;
    # fix-1 changed nothing, so the tree it left is the one the wave built.
    store = ArtifactStore()
    blob = store.put_blob(json.dumps({"kind": "full-file-snapshot", "files": {}}).encode())
    seq = Seq("run", 1, 9, "fixer", 0)
    art = emit_artifact(FeatureDiff(diff_uri=blob, task_id="fix-1", branch="task/fix-1",
                                    base_tree_sha="b", seq=seq),
                        "feature_diff", "fixer", seq, store=store
                        ).model_copy(update={"task_id": "fix-1"})
    note = GateTriage(name="adapter-report", run_against="task/fix-1",
                      explanation="turn budget exhausted before 'done' — partial work recorded",
                      produced_by="proxy-agent", seq=Seq("run", 0, 0, "proxy-agent", 0))
    state: FactoryState = {
        **_state(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _OK),
                 _gate("build", "t2", 3, _OK), _gate("test", "t2", 4, _RED, _BDD)),
        "artifacts": [art], "gate_triage": [note]}
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=2)
    assert ("previous fix pass (fix-1) changed no file (it ran out of turns before its first "
            "edit): reading files changes nothing; make the edits this time") in summary
    assert not any("broke" in line or "different approach" in line for line in summary)


def test_a_fix_pass_with_no_ticket_diff_yet_starts_the_epochs(tmp_path: Path) -> None:
    # Every ticket failed, so the build failed on the scaffold alone: the fixer's diff
    # is the first one and has no epoch to lift past.
    store = ArtifactStore()
    rec = _Recorder(writes={"fix-1": {"app.py": "def fixed():\n    return 1\n"}})
    fixer = make_fixer_node(_runtime(rec, store), source_dir=str(tmp_path),
                            resolve=store.resolve, resolve_blob=store.resolve_blob)
    state = _ticket_state(store, [_ticket("T-001", 0, files=("app.py",))])
    state["gates"] = [_gate("build", "t1", 1, _RED, "SyntaxError in app.py")]
    out = fixer(state, _config())
    diffs = [a for a in out["artifacts"] if a.kind == "feature_diff"]
    assert [(d.task_id, d.seq.emission_epoch) for d in diffs] == [("fix-1", 1)]
