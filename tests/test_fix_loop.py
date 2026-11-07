"""The wave fix loop: each fixer pass gets a structured summary of the failure and
of the previous pass, and a pass that leaves the same checks failing halts the
wave instead of spending the remaining attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    BUILD_TEST_SMOKE_BOUNDARY,
    NO_FIX_PROGRESS_TRIAGE,
    PIP_MANIFESTS,
    FixPolicy,
    WaveBuildCheck,
    _unchanged_tree_recheck,
    best_entry,
    failing_ids,
    failure_files,
    fix_window,
    no_fix_progress,
)
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.gates import FixPass, GateResult, GateStatus, GateTriage, VerdictClass
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.test_ticket_scheduler import _config, _Recorder, _runtime, _ticket, _wave_run
from tests.test_ticket_scheduler import _state as _ticket_state

_CFG: RunnableConfig = {"configurable": {}}
# For tests of the fixer passes themselves: no ticket restart takes a pass's place.
_NO_RESTART = FixPolicy(restarts=False)


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
    _out, ran, _ = _wave_run(tmp_path, rec, fix_policy=_NO_RESTART)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "engineering"]
    first = rec.packets["fix-1"].prior_failures
    assert first[:2] == ("[fix summary]", "failed gates: build")
    assert first[2].startswith("failing tests/scenarios: none named")
    assert not any(line.startswith("previous fix pass") for line in first)
    second = list(rec.packets["fix-2"].prior_failures)
    [line] = [x for x in second if x.startswith("pass 1 (fix-1): ")]
    assert line.startswith("pass 1 (fix-1): edited app.py, rogue.txt — lateral: ")
    assert "(build broken)" in line
    assert "the fixer wrote outside" in line and "rogue.txt" in line.split("reported: ")[1]
    assert second[second.index(line) + 1] == (
        "do not repeat an approach marked WORSE or no-change from this tree.")
    tail = second.index("gate output (tail):")
    assert second[tail + 1] == "[build gate FAILED]"
    assert any("broken_1" in line for line in second[tail:])  # this pass's output


def test_a_fix_pass_with_no_progress_halts_the_wave(tmp_path: Path) -> None:
    rec = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})  # the fixer can't fix
    out, ran, store = _wave_run(tmp_path, rec, max_fix_attempts=4, fix_policy=_NO_RESTART)
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
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4, tickets=tickets,
                            fix_policy=_NO_RESTART)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate"]
    assert len({g.run_against for g in out["gates"]}) == 1
    assert {g.status for g in out["gates"]} == {GateStatus.FAIL}
    assert any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])


def test_an_empty_fix_diff_halts_the_wave_after_earlier_waves_spent_fixes(
    tmp_path: Path,
) -> None:
    # The local shop run: earlier fixes had spent max_fix_attempts run-wide, the
    # check judged its run-wide cap off a state without the plan's tickets, saw HALT,
    # skipped the no-progress note, and the wave spent its last pass on the same tree.
    broken = {"app.py": "def broken(:\n"}
    rec = _Recorder(writes={"T-001": broken, "fix-1": broken})
    tickets = [_ticket("T-001", 0, files=("app.py", "rogue.txt")),
               _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4, tickets=tickets,
                            budget=BudgetLedger(fix_attempts_total=4),
                            fix_policy=_NO_RESTART)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate"]
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


def test_a_mass_failure_keeps_its_exception_message() -> None:
    cause = ("E   sqlalchemy.exc.InvalidRequestError: Mapper 'Mapper[User(user)]' has "
             "no property 'cart_items'")
    detail = "\n".join(f"_____ test_{i} _____\n>   session.query(User)\n{cause}"
                       for i in range(80))
    tail = "\n".join(f"FAILED tests/test_app.py::test_{i} - sqlalchemy.exc.Inv..."
                     for i in range(80))
    findings = failure_findings(f"{detail}\n=== short test summary info ===\n{tail}\n")
    assert findings.count(cause) == 1
    assert len(failing_ids(findings)) == 50


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
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4, fix_policy=_NO_RESTART)
    # Every pass is allowed, but the wave's fix budget (3 passes) still bounds it.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "fixer", "wave_gate"]
    assert not any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])
    assert ("the last fix pass (fix-1) never edited app.py, the file(s) the failure names: "
            "edit them before anything else") in rec.packets["fix-2"].prior_failures


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


# ---- keeping the fix loop's best tree -------------------------------------------


def _score(*gates: GateResult, names: frozenset[str] = frozenset({"build", "test"})
           ) -> tuple[int, int, int] | None:
    from foundry.graph.gate_wiring import fix_score

    return fix_score({g.name: g for g in gates}, names)


def test_a_broken_build_scores_worse_than_any_number_of_failing_tests() -> None:
    nine = [f"FAILED tests/test_app.py::test_{i} - assert" for i in range(9)]
    builds = _score(_gate("build", "t1", 1, _OK), _gate("test", "t1", 2, _RED, *nine))
    broken = _score(_gate("build", "t2", 1, _RED, "SyntaxError"), _gate("test", "t2", 2, _RED))
    six = _score(_gate("build", "t3", 1, _OK), _gate("test", "t3", 2, _RED, *nine[:6]))
    assert builds is not None and broken is not None and six is not None
    assert six < builds < broken
    assert _score(_gate("build", "t4", 1, _OK), _gate("test", "t4", 2, GateStatus.ERROR)) is None
    assert _score(_gate("build", "t5", 1, _OK)) is None  # a verdict missing: not judged


# Real mypy output (a stack build line `python -m compileall -q . && mypy app`).
_MYPY_OUT = (
    'app/models/user.py:19: error: Name "CartItem" is not defined  [name-defined]\n'
    'app/models/user.py:20: error: Name "Order" is not defined  [name-defined]\n'
    "Found 2 errors in 1 file (checked 9 source files)\n"
)
_USER_PY = (
    "from __future__ import annotations\n\n"
    "from sqlalchemy.orm import Mapped, relationship\n\n"
    "from app.models import Base\n\n\n"
    "class User(Base):\n"
    '    """ORM model for a user."""\n\n'
    '    __tablename__ = "user"\n\n'
    "    id: Mapped[int]\n"
    "    username: Mapped[str]\n"
    "    role: Mapped[str]\n"
    "    password_hash: Mapped[str]\n"
    "    email: Mapped[str]\n\n"
    '    cart_items: Mapped[list["CartItem"]] = relationship("CartItem", back_populates="user")\n'
    "    orders: Mapped[list[Order]] = relationship(Order.user)\n"
)


def _typecheck_build(tree: str, tmp: Path) -> GateResult:
    from foundry.gates.build import build_findings

    (tmp / "app" / "models").mkdir(parents=True, exist_ok=True)
    (tmp / "app" / "models" / "user.py").write_text(_USER_PY)
    return _gate("build", tree, 1, _RED, *build_findings(1, _MYPY_OUT, "", tmp))


def test_a_quoted_annotation_name_defined_error_gets_the_type_checking_hint(
    tmp_path: Path,
) -> None:
    from foundry.gates.build import TYPECHECK_ONLY, build_findings
    from foundry.graph.gate_wiring import typecheck_only

    build = _typecheck_build("t", tmp_path)
    assert TYPECHECK_ONLY in build.findings[0] and typecheck_only(build)
    hints = [f for f in build.findings if "TYPE_CHECKING" in f]
    # Order is used unquoted (a runtime use): a type-checking-only import would not do.
    assert hints == [
        "app/models/user.py:19: `CartItem` is used only in a quoted annotation: import it "
        "under `if TYPE_CHECKING:` (from typing import TYPE_CHECKING) in that module"]
    # A compile error stops the `&&` chain before mypy: not a typecheck-only build.
    compile_err = build_findings(1, "SyntaxError: invalid syntax\n", "", tmp_path)
    assert not typecheck_only(_gate("build", "t", 1, _RED, *compile_err))


def test_a_typecheck_only_build_is_scored_by_how_far_the_tests_got(tmp_path: Path) -> None:
    def tests(tree: str, n: int) -> GateResult:
        return _gate("test", tree, 2, _RED,
                     *(f"FAILED tests/test_cart.py::test_{i} - assert" for i in range(n)))

    start = _score(_gate("build", "t0", 1, _OK), tests("t0", 82))
    fixed = _score(_typecheck_build("t1", tmp_path), tests("t1", 40))
    clean = _score(_gate("build", "t2", 1, _OK), tests("t2", 40))
    broken = _score(_gate("build", "t3", 1, _RED, "SyntaxError"), tests("t3", 1))
    assert start is not None and fixed is not None and clean is not None
    assert broken is not None
    # Fewer failing tests plus a type error beat many more failing tests; equal tests
    # plus a type error lose; a build that does not compile still loses to both.
    assert clean < fixed < start < broken
    green = _score(_typecheck_build("t4", tmp_path), _gate("test", "t4", 2, _OK))
    assert green is not None and any(green[1:])  # a type error is never all-PASS


def test_a_type_error_alone_does_not_revert_a_pass_that_fixed_tests(tmp_path: Path) -> None:
    import json

    from foundry.graph.gate_wiring import keep_best

    def run(n: int) -> tuple[list[FixPass], list[FactoryState]]:
        store = ArtifactStore()
        ticket = _diff_art(store, "T-001", 1, {"app.py": "x = 1\n"})
        state: FactoryState = {"artifacts": [ticket]}
        window = fix_window(state, BUILD_TEST_BOUNDARY, frozenset())
        snap = store.put_blob(json.dumps({"kind": "full-file-snapshot",
                                          "files": {"app.py": "x = 0\n"}}).encode())
        failing = tuple(f"test:tests/test_cart.py::test_{i}" for i in range(3))
        best = FixPass(window=window, boundary=BUILD_TEST_BOUNDARY, pass_no=0,
                       tree="t-best", score=(-1, 1, 3), failing=failing, outcome="initial",
                       snapshot_uri=snap, seq=Seq("run", 1, 0, "fix_pass", 0))
        state = {**state, "fix_passes": [best],
                 "budget": BudgetLedger(fix_attempts_since_all_green=1)}
        out: dict[str, Any] = {"gates": [
            _typecheck_build("t-new", tmp_path),
            _gate("test", "t-new", 6, _RED,
                  *(f"FAILED tests/test_cart.py::test_{i} - assert" for i in range(n)))]}
        reran: list[FactoryState] = []

        def rerun(s: FactoryState) -> dict[str, Any]:
            reran.append(s)
            return {}

        keep_best(state, _config(), out, boundary=BUILD_TEST_BOUNDARY, scope=frozenset(),
                  rt=_runtime(None, store), resolve=store.resolve,
                  resolve_blob=store.resolve_blob, rerun=rerun)
        return out["fix_passes"], reran

    # Two of three failing tests fixed, one new type error: the new best, kept.
    [entry], reran = run(1)
    assert entry.outcome == "improved" and not reran and entry.snapshot_uri is not None
    # The same failing tests plus a new type error: worse, so the best tree is restored.
    [entry], reran = run(3)
    assert entry.outcome == "worse" and reran


def _entry(window: str, pass_no: int, tree: str, score: tuple[int, int, int] | None,
           epoch: int) -> FixPass:
    return FixPass(window=window, boundary=BUILD_BOUNDARY, pass_no=pass_no, tree=tree,
                   score=score, outcome="initial", seq=Seq("run", epoch, 0, "fix_pass", 0))


def test_the_best_pass_is_the_lowest_score_and_a_tie_goes_to_the_newest() -> None:
    passes = [_entry("w", 0, "t1", (0, 1, 5), 1), _entry("w", 1, "t2", (0, 1, 3), 2),
              _entry("w", 2, "t3", None, 3), _entry("w", 3, "t4", (0, 1, 3), 4),
              _entry("other", 0, "t5", (-1, 0, 0), 5)]
    best = best_entry(passes, "w")
    assert best is not None and best.tree == "t4"
    assert best_entry([], "w") is None


def _diff_art(store: ArtifactStore, task_id: str, epoch: int, files: dict[str, str],
              deleted: tuple[str, ...] = ()) -> Artifact:
    import json

    from foundry.agents._common import emit_artifact
    from foundry.schemas.implementation import FeatureDiff

    body: dict[str, object] = {"kind": "full-file-snapshot", "files": files}
    if deleted:
        body["deleted"] = list(deleted)
    blob = store.put_blob(json.dumps(body).encode())
    seq = Seq("run", epoch, 0, "junior_engineer", 0)
    return emit_artifact(
        FeatureDiff(diff_uri=blob, task_id=task_id, branch=f"task/{task_id}",
                    base_tree_sha="b", seq=seq),
        "feature_diff", "junior_engineer", seq, store=store,
    ).model_copy(update={"task_id": task_id})


def test_the_window_is_one_boundary_over_one_set_of_ticket_diffs() -> None:
    store = ArtifactStore()
    ticket = _diff_art(store, "T-001", 1, {"app.py": "x = 1\n"})
    state: FactoryState = {"artifacts": [ticket]}
    window = fix_window(state, BUILD_BOUNDARY, frozenset())
    assert fix_window(dict(state), BUILD_BOUNDARY, frozenset()) == window  # a resume
    fixed: FactoryState = {"artifacts": [
        ticket, _diff_art(store, "fix-1", 2, {"app.py": "x = 2\n"}),
        _diff_art(store, "revert-1", 3, {"app.py": "x = 1\n"})]}
    assert fix_window(fixed, BUILD_BOUNDARY, frozenset()) == window
    rebuilt: FactoryState = {"artifacts": [ticket, _diff_art(store, "T-001", 4,
                                                             {"app.py": "x = 3\n"})]}
    assert fix_window(rebuilt, BUILD_BOUNDARY, frozenset()) != window  # escalation
    wave: FactoryState = {"artifacts": [ticket, _diff_art(store, "T-002", 5, {"b.py": "y\n"})]}
    assert fix_window(wave, BUILD_BOUNDARY, frozenset()) != window
    assert fix_window(state, BUILD_TEST_SMOKE_BOUNDARY, frozenset()) != window  # final gate
    assert fix_window(state, BUILD_TEST_BOUNDARY, frozenset({"s"})) != window


def test_a_restore_diff_rebuilds_the_best_tree_exactly(tmp_path: Path) -> None:
    from foundry.graph.gate_wiring import (
        _snapshot_blob,
        integrated_files,
        materialize_workdir,
        restore_diff,
    )

    base = tmp_path / "base"
    base.mkdir()
    (base / "README.md").write_text("base readme\n")
    (base / "keep.py").write_text("KEEP = 1\n")
    store = ArtifactStore()
    tickets = [_ticket("contracts", 0, files=("api.py",)),
               _ticket("T-001", 1, files=("app.py",), deps=("contracts",))]
    best_diffs = [
        _diff_art(store, "contracts", 1, {"api.py": "API = 1\n"}),
        # A later ticket's edit to the contract never reaches the tree.
        _diff_art(store, "T-001", 2, {"app.py": "a = 1\n", "api.py": "API = 'ticket'\n"}),
        _diff_art(store, "fix-1", 3, {"api.py": "API = 2\n", "app.py": "a = 2\n"}),
    ]
    good: FactoryState = {**_ticket_state(store, tickets, artifacts=best_diffs),
                          "base_dir": str(base)}
    worse: FactoryState = {**good, "artifacts": [*good["artifacts"], _diff_art(
        store, "fix-2", 4, {"api.py": "API = 3\n", "app.py": "a = 3\n", "new.py": "n = 1\n"},
        deleted=("README.md",))]}

    def digest(state: FactoryState, name: str) -> str:
        return materialize_workdir(state, tmp_path / name, resolve=store.resolve,
                                   resolve_blob=store.resolve_blob)

    snapshot = store.put_blob(_snapshot_blob(integrated_files(
        good, resolve=store.resolve, resolve_blob=store.resolve_blob)))
    best = _entry("w", 0, digest(good, "good"), (0, 1, 1), 1).model_copy(
        update={"snapshot_uri": snapshot})
    assert digest(worse, "worse") != best.tree
    revert = restore_diff(worse, best, _config(), tree="t-worse", rt=_runtime(None, store),
                          resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert revert is not None and revert.task_id == "revert-1"
    assert revert.seq.emission_epoch == 5  # applied after every diff
    restored: FactoryState = {**worse, "artifacts": [*worse["artifacts"], revert]}
    assert digest(restored, "restored") == best.tree
    assert (tmp_path / "restored" / "README.md").read_text() == "base readme\n"
    assert not (tmp_path / "restored" / "new.py").exists()
    assert (tmp_path / "restored" / "api.py").read_text() == "API = 2\n"


def _keep_best_state(store: ArtifactStore) -> FactoryState:
    import json

    ticket = _diff_art(store, "T-001", 1, {"app.py": "x = 1\n"})
    state: FactoryState = {"artifacts": [ticket]}
    window = fix_window(state, BUILD_BOUNDARY, frozenset())
    snap = store.put_blob(json.dumps({"kind": "full-file-snapshot",
                                      "files": {"app.py": "x = 0\n"}}).encode())
    best = _entry(window, 0, "t-best", (0, 1, 1), 1).model_copy(update={"snapshot_uri": snap})
    return {**state, "fix_passes": [best],
            "budget": BudgetLedger(fix_attempts_since_all_green=1)}


def test_a_restore_whose_digest_is_not_the_best_tree_is_dropped() -> None:
    from foundry.graph.gate_wiring import REVERT_SKIPPED_TRIAGE, keep_best

    store = ArtifactStore()
    out: dict[str, Any] = {"gates": [_gate("build", "t-new", 5, _RED, "a", "b", "c")]}
    reverted = keep_best(_keep_best_state(store), _config(), out, boundary=BUILD_BOUNDARY,
                         scope=frozenset(), rt=_runtime(None, store), resolve=store.resolve,
                         resolve_blob=store.resolve_blob,
                         rerun=lambda s: {"gates": [_gate("build", "t-other", 6, _RED, "a")]})
    assert not reverted
    assert "artifacts" not in out and [g.run_against for g in out["gates"]] == ["t-new"]
    assert [t.name for t in out["gate_triage"]] == [REVERT_SKIPPED_TRIAGE]
    [entry] = out["fix_passes"]
    assert (entry.outcome, entry.pass_no, entry.tree) == ("worse", 1, "t-new")


def test_an_errored_check_is_never_reverted_from() -> None:
    from foundry.graph.gate_wiring import keep_best

    store = ArtifactStore()
    out: dict[str, Any] = {"gates": [_gate("build", "t-new", 5, GateStatus.ERROR, "infra")]}
    reran: list[FactoryState] = []

    def rerun(s: FactoryState) -> dict[str, Any]:
        reran.append(s)
        return {}

    assert not keep_best(_keep_best_state(store), _config(), out, boundary=BUILD_BOUNDARY,
                         scope=frozenset(), rt=_runtime(None, store), resolve=store.resolve,
                         resolve_blob=store.resolve_blob, rerun=rerun)
    assert not reran and [p.outcome for p in out["fix_passes"]] == ["error"]


_BROKEN = {"a.py": "def a(:\n", "b.py": "def b(:\n"}
_ONE_BROKEN = {"a.py": "def a():\n    return 1\n"}  # b.py still broken
_WORSE = {"c.py": "def c(:\n", "d.py": "def d(:\n"}  # b, c and d broken
_FIX_TICKETS = [_ticket("T-001", 0, files=("a.py", "b.py")),
                _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]


def test_a_pass_that_makes_the_tree_worse_is_reverted_to_the_best_tree(
    tmp_path: Path,
) -> None:
    rec = _Recorder(writes={"T-001": _BROKEN, "fix-1": _ONE_BROKEN, "fix-2": _WORSE,
                            "fix-3": {"b.py": "def b():\n    return 2\n"}})
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=4, tickets=_FIX_TICKETS)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "fixer", "wave_gate", "engineering"]
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert [(p.outcome, p.fix_task) for p in passes] == [
        ("initial", None), ("improved", "fix-1"), ("worse", "fix-2"),
        ("restored", "revert-1"), ("improved", "fix-3")]
    assert passes[3].tree == passes[1].tree
    assert passes[2].touched == ("c.py", "d.py", "rogue.txt")
    # The next fixer worked on fix-1's tree, not fix-2's.
    seen = rec.seen["fix-3"]
    assert "c.py" not in seen and "d.py" not in seen
    assert seen["a.py"] == _ONE_BROKEN["a.py"]
    assert not any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])
    # The restore is the fix loop's diff, not the fixer's: the log names fix-2 reverted.
    summary = rec.packets["fix-3"].prior_failures
    assert any(line.startswith("pass 2 (fix-2): edited c.py, d.py, rogue.txt — WORSE, "
                               "reverted (build broken)") for line in summary)
    assert not any("revert-1" in line for line in summary)
    # The gates judged the restored tree: fix-2 was undone, so nothing it left
    # unedited is on the tree any more.
    assert not any("never edited" in line for line in summary)
    # A worse pass and its restore are told apart by Seq, not by insertion order.
    assert len({p.seq.emission_epoch for p in passes}) == len(passes)
    assert {"T-001", "fix-1", "fix-2", "revert-1", "fix-3", "T-002"} <= {
        a.task_id for a in out["artifacts"] if a.kind == "feature_diff"}


def test_a_fix_pass_that_rebuilds_a_reverted_tree_is_no_progress(tmp_path: Path) -> None:
    # fix-3 makes fix-2's worse tree again: reverted again, and the wave stops.
    rec = _Recorder(writes={"T-001": _BROKEN, "fix-1": _ONE_BROKEN, "fix-2": _WORSE,
                            "fix-3": _WORSE})
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=6, tickets=_FIX_TICKETS)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "fixer", "wave_gate"]
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert [p.outcome for p in passes][-2:] == ["worse", "restored"]
    assert passes[-2].tree == passes[2].tree
    [note] = [t for t in out["gate_triage"] if t.name == NO_FIX_PROGRESS_TRIAGE]
    assert note.run_against == passes[-1].tree  # the restored, current tree


def test_the_draft_is_the_final_loops_best_after_many_waves() -> None:
    # Wave checks run inside the implementation subgraph, whose langgraph_step runs
    # far ahead of the supervisor's: the ledger must still order the final gate's
    # passes after every wave's.
    from foundry.graph.gate_wiring import draft_best, keep_best

    store = ArtifactStore()
    rt = _runtime(None, store)
    state: FactoryState = {"artifacts": [], "fix_passes": []}
    local = iter(range(1, 1000))

    def check(state: FactoryState, boundary: str, step: int, tree: str,
              *gates: tuple[str, GateStatus, tuple[str, ...]]) -> FactoryState:
        cfg: RunnableConfig = {"configurable": {"thread_id": "run"},
                               "metadata": {"langgraph_step": step}}
        out: dict[str, Any] = {"gates": [_gate(n, tree, next(local), s, *f)
                                         for n, s, f in gates]}
        keep_best(state, cfg, out, boundary=boundary, scope=frozenset(), rt=rt,
                  resolve=store.resolve, resolve_blob=store.resolve_blob,
                  rerun=lambda s: {})
        return {**state, "fix_passes": [*state["fix_passes"], *out["fix_passes"]]}

    for wave in range(5):
        state["artifacts"] = [*state["artifacts"], _diff_art(
            store, f"T-00{wave}", wave + 1, {f"w{wave}.py": "x = 1\n"})]
        state = check(state, BUILD_BOUNDARY, 40 + 4 * wave, f"w{wave}a",
                      ("build", _RED, ("SyntaxError",)))
        state = check(state, BUILD_BOUNDARY, 42 + 4 * wave, f"w{wave}b", ("build", _OK, ()))
    final = BUILD_TEST_SMOKE_BOUNDARY
    state = check(state, final, 6, "f0", ("build", _OK, ()),
                  ("test", _RED, (_FAIL_A, _FAIL_B)), ("smoke", _OK, ()))
    state["artifacts"] = [*state["artifacts"], _diff_art(store, "fix-1", 10, {"a.py": "1\n"})]
    state = check(state, final, 9, "f1", ("build", _OK, ()), ("test", _RED, (_FAIL_A,)),
                  ("smoke", _OK, ()))
    state["artifacts"] = [*state["artifacts"], _diff_art(store, "fix-2", 11, {"a.py": "(\n"})]
    state = check(state, final, 12, "f2", ("build", _RED, ("SyntaxError",)),
                  ("test", _RED, ()), ("smoke", _RED, ()))
    best = draft_best(state, store.resolve)
    assert best is not None and (best.boundary, best.tree) == (final, "f1")


def test_the_restored_tree_is_the_newest_checked_and_the_draft(tmp_path: Path) -> None:
    rec = _Recorder(writes={"T-001": _BROKEN, "fix-1": _ONE_BROKEN, "fix-2": _WORSE})
    # The budget runs out on the reverted pass.
    out, ran, store = _wave_run(tmp_path, rec, tickets=_FIX_TICKETS)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate"]
    best = sorted(out["fix_passes"], key=lambda p: p.seq)[1]
    builds = sorted((g for g in out["gates"] if g.name == "build"), key=lambda g: g.seq)
    assert builds[-1].run_against == best.tree
    check = WaveBuildCheck(_runtime(rec, store), None,  # type: ignore[arg-type]
                           store.resolve, store.resolve_blob)
    assert not check.capped_off_target(out)

    # A stop with an ungated fix diff on top still hands back the best tree.
    from foundry import cli

    ungated: FactoryState = {**out, "artifacts": [*out["artifacts"], _diff_art(
        store, "fix-3", 99, {"e.py": "def e(:\n"})]}
    draft = cli._write_draft_on_halt(ungated, str(tmp_path / "draft"), gate_findings={},
                                     resolve=store.resolve, resolve_blob=store.resolve_blob,
                                     stopped="token cap")
    assert draft is not None
    root = Path(draft)
    assert sorted(p.name for p in root.glob("*.py")) == ["a.py", "b.py"]
    assert (root / "a.py").read_text() == _ONE_BROKEN["a.py"]
    status = (root / "DRAFT-STATUS.md").read_text()
    assert "**build**: fail" in status
    assert "b.py" in status and "c.py" not in status


def test_a_pass_that_changes_nothing_after_a_restore_halts(tmp_path: Path) -> None:
    rec = _Recorder(writes={"T-001": _BROKEN, "fix-1": _ONE_BROKEN, "fix-2": _WORSE})
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=5, tickets=_FIX_TICKETS)
    # fix-3 writes only what fix-1's tree already holds: the restored tree, unchanged.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate",
                   "fixer", "wave_gate"]
    assert sorted(out["fix_passes"], key=lambda p: p.seq)[-1].outcome == "no-change"
    assert any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])


def test_the_final_gate_reverts_a_pass_that_breaks_the_build(tmp_path: Path) -> None:
    from foundry.gates import DevGateWriteVerifier
    from foundry.graph.supervisor import build_supervisor_graph
    from foundry.state.global_records import GlobalRecords
    from foundry.state.reducers import (
        merge_gates,
        reset_gate_write_verifier,
        set_gate_write_verifier,
        union_fix_passes,
    )
    from tests.test_supervisor import KEY, KEY_ID, _runner

    store = ArtifactStore()
    records = GlobalRecords()
    (tmp_path / "src").mkdir()
    graph = build_supervisor_graph(
        _runtime(_Recorder(), store), runner=_runner(records), resolve=store.resolve,
        resolve_blob=store.resolve_blob, source_dir=str(tmp_path / "src"))
    gate = graph.nodes["build_test_gate"].runnable
    tests = ("def test_a():\n    assert False\n\n\ndef test_b():\n    assert {}\n\n\n"
             "def test_c():\n    assert True\n").format
    state: FactoryState = _ticket_state(store, [_ticket("T-001", 0)], artifacts=[_diff_art(
        store, "T-001", 1, {"app.py": "X = 1\n", "test_app.py": tests("False")})])

    def check(state: FactoryState, *diffs: Artifact) -> tuple[FactoryState, dict[str, Any]]:
        state = {**state, "artifacts": [*state["artifacts"], *diffs]}
        out = gate.invoke(state, _config())
        return {**state, "artifacts": [*state["artifacts"], *out.get("artifacts", [])],
                "gates": merge_gates(state.get("gates"), out["gates"]),
                "fix_passes": union_fix_passes(state.get("fix_passes"), out["fix_passes"]),
                "budget": out.get("budget") or state.get("budget")}, out

    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        state, _ = check(state)
        state, _ = check(state, _diff_art(store, "fix-1", 2, {"test_app.py": tests("True")}))
        state, out = check(state, _diff_art(store, "fix-2", 3, {"app.py": "X = (\n"}))
    finally:
        reset_gate_write_verifier(token)
    passes = sorted(state["fix_passes"], key=lambda p: p.seq)
    assert [p.outcome for p in passes] == ["initial", "improved", "worse", "restored"]
    assert {p.boundary for p in passes} == {BUILD_TEST_SMOKE_BOUNDARY}
    assert [a.task_id for a in out["artifacts"]] == ["revert-1"]
    newest = {g.name: g for g in sorted(state["gates"], key=lambda g: g.seq)}
    assert {g.run_against for g in newest.values()} == {passes[1].tree}
    assert newest["build"].status is GateStatus.PASS
    assert state["budget"].fix_attempts_since_all_green == 3  # one per pass, not per run


def test_fix_passes_round_trip_the_checkpoint_serde() -> None:
    from foundry.durability.serde import foundry_serde

    serde = foundry_serde()
    entry = _entry("w", 0, "t1", (0, 1, 2), 1).model_copy(update={"failing": ("build:x",)})
    state = {"fix_passes": [entry]}
    assert serde.loads_typed(serde.dumps_typed(state)) == state
    # A checkpoint saved before the ledger existed reads as an empty one.
    old = serde.loads_typed(serde.dumps_typed({"gates": []}))
    assert best_entry(old.get("fix_passes") or (), "w") is None


# ---- the attempt-memory log ---------------------------------------------------

_T = "test:tests/test_app.py::"


def _log_state(store: ArtifactStore, upto: int) -> FactoryState:
    """A window's ledger: the built tree (a,b,c failing); fix-1 fixes a,b but breaks
    d,e,f and is reverted; fix-2 edits nothing; fix-3 trades c for d."""
    ticket = _diff_art(store, "T-001", 1, {"app.py": "x = 1\n"})
    fix1 = _diff_art(store, "fix-1", 2, {"models.py": "class Model:\n    pass\n"})
    state: FactoryState = {"artifacts": [ticket, fix1]}
    window = fix_window(state, BUILD_BOUNDARY, frozenset())

    def entry(n: int, tree: str, outcome: Any, failing: tuple[str, ...],
              fix: str | None = None, touched: tuple[str, ...] = ()) -> FixPass:
        return FixPass(window=window, boundary=BUILD_BOUNDARY, pass_no=n, tree=tree,
                       fix_task=fix, score=(-1, 1, len(failing)),
                       failing=tuple(_T + f for f in failing), outcome=outcome,
                       touched=touched, seq=Seq("run", n + 1, 0, "fix_pass", 0))

    passes = [
        entry(0, "t0", "initial", ("test_a", "test_b", "test_c")),
        entry(1, "t1", "worse", ("test_c", "test_d", "test_e", "test_f"), "fix-1",
              ("api.py", "models.py")),
        entry(2, "t0", "restored", ("test_a", "test_b", "test_c"), "revert-1"),
        entry(3, "t0", "no-change", ("test_a", "test_b", "test_c"), "fix-2"),
        entry(4, "t3", "lateral", ("test_a", "test_b", "test_d"), "fix-3", ("app.py",)),
    ][:upto]
    tree = passes[-1].tree
    note = GateTriage(name="adapter-report", run_against="task/fix-1",
                      explanation="renamed  Model\nto Item", produced_by="recorder",
                      seq=Seq("run", 1, 0, "recorder", 0))
    return {**state, "fix_passes": passes, "gate_triage": [note],
            "gates": [_gate("build", tree, 1, _OK),
                      _gate("test", tree, 2, _RED, _FAIL_A)],
            "budget": BudgetLedger(fix_attempts_since_all_green=1)}


def _log(state: FactoryState, store: ArtifactStore) -> list[str]:
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=1)
    start = summary.index("[fix passes so far in this loop]")
    return list(summary[start + 1:summary.index("gate output (tail):")])


def test_the_fix_log_names_what_each_pass_fixed_broke_and_whether_it_was_reverted() -> None:
    store = ArtifactStore()
    log = _log(_log_state(store, 5), store)
    assert log[0] == ("pass 1 (fix-1): edited api.py, models.py — WORSE, reverted: fixed "
                      "test_a,test_b; broke test_d,test_e,test_f (build ok) — reported: "
                      "renamed Model to Item")
    assert log[1] == "pass 3 (fix-2): no file edited"
    assert log[2] == ("pass 4 (fix-3): edited app.py — lateral: 3→3, now failing test_d "
                      "instead of test_c (build ok)")
    # The worse pass fixed two tests: its patch rides along to reapply that part.
    assert log[3].startswith("fix-1's change fixed test_a,test_b but was worse overall: "
                             "reapply this part without breaking test_d,test_e,test_f:\n"
                             "--- models.py\nclass Model:")
    assert log[4] == "do not repeat an approach marked WORSE or no-change from this tree."
    assert not any(line.startswith("previous fix pass") for line in log)


def test_two_passes_from_the_same_best_tree_get_different_prompts() -> None:
    store = ArtifactStore()
    after_revert = _fix_summary(_log_state(store, 3), store.resolve, store.resolve_blob,
                                attempt=2)
    after_no_edit = _fix_summary(_log_state(store, 4), store.resolve, store.resolve_blob,
                                 attempt=3)
    assert after_revert != after_no_edit
    assert "pass 3 (fix-2): no file edited" in after_no_edit
    assert "pass 3 (fix-2): no file edited" not in after_revert


def test_the_fix_log_is_capped_and_counts_the_passes_it_leaves_out() -> None:
    store = ArtifactStore()
    state = _log_state(store, 2)
    [initial, worse] = state["fix_passes"]
    many = [initial, *(worse.model_copy(update={
        "pass_no": n, "fix_task": f"fix-{n}", "outcome": "lateral",
        "touched": tuple(f"pkg/module_{n}_{i}.py" for i in range(12)),
        "seq": Seq("run", n + 1, 0, "fix_pass", 0)}) for n in range(1, 13))]
    log = _log({**state, "fix_passes": many}, store)
    assert log[0].startswith("(") and log[0].endswith("earlier pass(es) not shown)")
    shown = [line for line in log if line.startswith("pass ")]
    assert shown[-1].startswith("pass 12 (fix-12)") and len(shown) <= 8
    assert sum(len(line) for line in shown) <= 1_500


def test_a_fix_pass_records_its_wall_time_off_the_run_meter() -> None:
    from foundry.graph.gate_wiring import keep_best
    from foundry.proxy.token_meter import TokenMeter

    store = ArtifactStore()
    rt = _runtime(None, store)
    meter = TokenMeter(None)
    rt.proxy.token_meter = meter
    state: FactoryState = {"artifacts": [_diff_art(store, "T-001", 1, {"app.py": "x\n"})]}

    def check(state: FactoryState, tree: str, at: float) -> FactoryState:
        meter._elapsed_base = at - (meter.elapsed_s - meter._elapsed_base)
        out: dict[str, Any] = {"gates": [_gate("build", tree, 1, _RED, "SyntaxError")]}
        keep_best(state, _config(), out, boundary=BUILD_BOUNDARY, scope=frozenset(), rt=rt,
                  resolve=store.resolve, resolve_blob=store.resolve_blob,
                  rerun=lambda s: {})
        return {**state, "fix_passes": [*(state.get("fix_passes") or ()), *out["fix_passes"]]}

    state = check(state, "t0", 100.0)
    state = check(state, "t1", 400.0)
    first, second = sorted(state["fix_passes"], key=lambda p: p.seq)
    assert first.elapsed_s is None and first.clock_s is not None
    assert second.elapsed_s is not None and abs(second.elapsed_s - 300.0) < 1.0


# ---- one failure cluster per pass, superset acceptance, ticket restarts --------

_CLUSTER_TICKETS = [
    _ticket("T-001", 0, files=("a.py", "tests/test_a.py"), scenario_ids=("AC-1",)),
    _ticket("T-002", 1, files=("tests/test_b.py",)),
]
_A1 = "FAILED tests/test_a.py::test_alpha - assert 0"
_B1 = "FAILED tests/test_b.py::test_1 - assert 1"
_B2 = "FAILED tests/test_b.py::test_2 - assert 2"
_C9 = "FAILED tests/test_c.py::test_9 - assert 9"


def _plan_entry(n: int, outcome: Any, cluster: str | None) -> FixPass:
    return FixPass(window="w", boundary=BUILD_TEST_BOUNDARY, pass_no=n, tree=f"t{n}",
                   outcome=outcome, cluster=cluster, score=(-1, 1, 3),
                   seq=Seq("run", n + 1, 0, "fix_pass", 0))


def test_failures_cluster_build_first_then_the_biggest_cluster() -> None:
    from foundry.graph.gate_wiring import failure_clusters

    gates = [_gate("build", "t", 1, _RED, "a.py:3: error: Name 'x' is not defined"),
             _gate("test", "t", 2, _RED, _BDD.replace("T-001.feature", "f.feature"),
                   _B1, _B2, _C9)]
    clusters = failure_clusters(gates, _CLUSTER_TICKETS, {"T-001", "T-002"})
    assert [(c.key, c.tickets) for c in clusters] == [
        ("build", ("T-001",)),  # a.py is T-001's
        ("ticket:T-002", ("T-002",)),  # two failures
        ("file:tests/test_c.py", ()),  # nobody owns it
        ("ticket:T-001", ("T-001",)),  # AC-1's scenario
    ]
    assert clusters[1].items == ("test:tests/test_b.py::test_1", "test:tests/test_b.py::test_2")
    # An unbuilt ticket owns nothing yet; an ERROR is no failure to aim at.
    assert failure_clusters(gates, _CLUSTER_TICKETS, {"T-001"})[1].key == "file:tests/test_b.py"
    assert failure_clusters([_gate("test", "t", 3, GateStatus.ERROR, _B1)],
                            _CLUSTER_TICKETS, {"T-002"}) == ()


def test_a_pass_aims_at_one_cluster_and_rotates_after_two_tries() -> None:
    from foundry.graph.gate_wiring import FailureCluster, plan_next_pass

    a = FailureCluster(key="A", stage=1, items=("test:a::1", "test:a::2"), tickets=("T-001",))
    b = FailureCluster(key="B", stage=1, items=("test:b::1",))

    def plan(*passes: FixPass, clusters: tuple[FailureCluster, ...] = (a, b),
             **kw: Any) -> tuple[str | None, tuple[str, ...]]:
        return plan_next_pass(list(passes), clusters, restart_after=2,
                              restartable=lambda c: (), **kw)

    start = _plan_entry(0, "initial", None)
    assert plan(start) == ("A", ())
    once = _plan_entry(1, "lateral", "A")
    assert plan(start, once) == ("A", ())  # tried once: again
    twice = _plan_entry(2, "worse", "A")
    assert plan(start, once, twice) == ("B", ())  # tried twice without progress: rotate
    fixed_b = _plan_entry(3, "improved", "B")
    assert plan(start, once, twice, fixed_b, clusters=(a,)) == ("A", ())  # all spent: least
    # A pass that improved the tree is progress: the cluster keeps its turn.
    better = _plan_entry(2, "improved", "A")
    assert plan(start, once, better) == ("A", ())
    assert plan(start, clusters=()) == (None, ())


def test_a_restart_comes_after_two_passes_that_did_not_improve_the_cluster() -> None:
    from foundry.graph.gate_wiring import FailureCluster, plan_next_pass

    a = FailureCluster(key="A", stage=1, items=("test:a::1",), tickets=("T-001",))
    b = FailureCluster(key="B", stage=1, items=("test:b::1",))

    def plan(*passes: FixPass, after: int = 2, **kw: Any) -> tuple[str | None, tuple[str, ...]]:
        return plan_next_pass(list(passes), (a, b), restart_after=after,
                              restartable=lambda c: c.tickets, **kw)

    start = _plan_entry(0, "initial", None)
    once, twice = _plan_entry(1, "lateral", "A"), _plan_entry(2, "no-change", "A")
    assert plan(start, once) == ("A", ())
    assert plan(start, once, twice) == ("A", ("T-001",))
    # The legacy budget restarts after one pass, unless a pass improved the cluster.
    assert plan(start, once, after=1, unimproved_only=True) == ("A", ("T-001",))
    improved = _plan_entry(1, "improved", "A")
    later = _plan_entry(2, "lateral", "A")
    assert plan(start, improved, later, after=1, unimproved_only=True) == ("A", ())
    # A loop about to stop on a dead end restarts first.
    assert plan(start, dead_end=True) == ("A", ("T-001",))


def test_a_tree_that_breaks_a_passing_check_is_worse_even_with_fewer_failures() -> None:
    from foundry.graph.gate_wiring import _outcome, regressions

    best = _entry("w", 0, "t0", (-1, 1, 2), 1).model_copy(
        update={"failing": (_T + "test_a", _T + "test_b")})
    prior = [best]
    # One failure instead of two, but test_c passed on the best tree.
    assert regressions(best.failing, (_T + "test_c",)) == (_T + "test_c",)
    assert _outcome(prior, "t1", (-1, 1, 1), best, (_T + "test_c",)) == "worse"
    assert _outcome(prior, "t1", (-1, 1, 1), best, (_T + "test_a",)) == "improved"
    # A swap at the same count breaks a passing test: worse, not lateral.
    assert _outcome(prior, "t1", (-1, 1, 2), best, (_T + "test_a", _T + "test_c")) == "worse"
    assert _outcome(prior, "t1", (-1, 1, 2), best, best.failing) == "lateral"
    # A gate the best tree passed now failing is a regression; compiler lines are not
    # compared line by line (they shift with every edit).
    assert regressions(("build:x.py:1: error",), ("build:x.py:2: error",)) == ()
    assert regressions((_T + "test_a",), ("smoke:boom",)) == ("smoke:boom",)
    assert _outcome(prior, "t1", None, best, ()) == "error"  # ERROR handling unchanged
    # A worse pass is never the best, whatever its score.
    worse = _entry("w", 1, "t1", (-1, 1, 1), 2).model_copy(update={"outcome": "worse"})
    picked = best_entry([best, worse], "w")
    assert picked is not None and picked.tree == "t0"


def test_keep_best_reverts_a_pass_that_fixed_two_tests_and_broke_one() -> None:
    import json

    from foundry.graph.gate_wiring import keep_best

    store = ArtifactStore()
    state: FactoryState = {"artifacts": [_diff_art(store, "T-001", 1, {"app.py": "x = 1\n"})]}
    window = fix_window(state, BUILD_TEST_BOUNDARY, frozenset())
    snap = store.put_blob(json.dumps({"kind": "full-file-snapshot",
                                      "files": {"app.py": "x = 0\n"}}).encode())
    best = _entry(window, 0, "t-best", (-1, 1, 2), 1).model_copy(update={
        "snapshot_uri": snap, "failing": ("test:tests/test_app.py::test_add",
                                          "test:tests/test_app.py::test_list")})
    state = {**state, "fix_passes": [best], "budget": BudgetLedger(fix_attempts_since_all_green=1)}
    new = "FAILED tests/test_app.py::test_new - assert 0"
    out: dict[str, Any] = {"gates": [_gate("build", "t-new", 5, _OK),
                                     _gate("test", "t-new", 6, _RED, new)]}
    restored = keep_best(
        state, _config(), out, boundary=BUILD_TEST_BOUNDARY, scope=frozenset(),
        rt=_runtime(None, store), resolve=store.resolve, resolve_blob=store.resolve_blob,
        rerun=lambda s: {"gates": [_gate("build", "t-best", 7, _OK),
                                   _gate("test", "t-best", 8, _RED, _FAIL_A, _FAIL_B)]})
    assert restored
    assert [p.outcome for p in out["fix_passes"]] == ["worse", "restored"]


def _scoped_state(store: ArtifactStore, target: str | None) -> FactoryState:
    state = _ticket_state(store, _CLUSTER_TICKETS, artifacts=[
        _diff_art(store, "T-001", 1, {"a.py": "x\n"}),
        _diff_art(store, "T-002", 2, {"tests/test_b.py": "y\n"})])
    window = fix_window(state, BUILD_TEST_SMOKE_BOUNDARY, frozenset())
    entry = FixPass(window=window, boundary=BUILD_TEST_SMOKE_BOUNDARY, pass_no=0, tree="t1",
                    score=(-1, 1, 3), outcome="initial", target=target,
                    seq=Seq("run", 1, 0, "fix_pass", 0))
    detail_a = ["____ test_alpha ____", "E   assert 0 == 1"]
    return {**state, "fix_passes": [entry],
            "gates": [_gate("build", "t1", 1, _OK),
                      _gate("test", "t1", 2, _RED, *detail_a, _A1, _B1, _B2)],
            "budget": BudgetLedger(fix_attempts_since_all_green=1)}


def test_a_fix_pass_summary_lists_only_its_cluster() -> None:
    from foundry.agents.implementation.junior_engineer import fix_failures

    store = ArtifactStore()
    state = _scoped_state(store, "ticket:T-002")
    summary = _fix_summary(state, store.resolve, store.resolve_blob, attempt=1)
    assert summary[:5] == (
        "[fix summary]",
        "this pass fixes ONE failure cluster: ticket:T-002 (2 failure(s), listed below)",
        "1 other failure(s) are handled separately — don't touch them",
        "failed gates: test",
        "failing tests/scenarios: tests/test_b.py::test_1, tests/test_b.py::test_2",
    )
    tail = fix_failures(state, store.resolve)
    assert _B1 in tail and _B2 in tail
    assert _A1 not in tail and "E   assert 0 == 1" not in tail  # T-001's block is cut
    # Before a plan (or for an escalation's context) nothing is cut.
    whole = _scoped_state(store, None)
    assert _A1 in fix_failures(whole, store.resolve)
    assert not any("ONE failure cluster" in line for line in _fix_summary(
        state, store.resolve, store.resolve_blob, attempt=1, scoped=False))


def test_a_planned_restart_is_pending_until_the_ticket_is_restarted() -> None:
    from foundry.graph.gate_wiring import RESTARTED_TRIAGE, pending_restarts

    store = ArtifactStore()
    state = _scoped_state(store, "ticket:T-002")
    [entry] = state["fix_passes"]
    planned: FactoryState = {**state, "fix_passes": [entry.model_copy(
        update={"restart_tickets": ("T-002",)})]}
    assert pending_restarts(planned, store.resolve) == ("T-002",)  # also after a resume
    spent = GateTriage(name=RESTARTED_TRIAGE, run_against="task/T-002", explanation="x",
                       produced_by="engineering", seq=Seq("run", 5, 0, "engineering", 0))
    assert pending_restarts({**planned, "gate_triage": [spent]}, store.resolve) == ()


class _RestartRecorder(_Recorder):
    """A ``_Recorder`` whose second session on a ticket writes ``again[tid]``."""

    def __init__(self, *, again: dict[str, dict[str, str]], **kw: Any) -> None:
        super().__init__(**kw)
        self.again = again

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        if tid in self.again and tid in self.calls:
            self.writes[tid] = self.again[tid]
        return super().run(packet, sandbox)


_RESTART_TICKETS = [_ticket("T-001", 0, files=("a.py", "b.py")),
                    _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]


def test_a_fix_pass_that_did_not_improve_is_followed_by_a_ticket_restart(
    tmp_path: Path,
) -> None:
    from foundry.graph.gate_wiring import RESTARTED_TRIAGE, materialize_workdir

    rec = _RestartRecorder(
        writes={"T-001": {"a.py": "def a(:\n"}, "fix-1": {"a.py": "def a_1(:\n"}},
        again={"T-001": {"a.py": "def a():\n    return 1\n"}})
    out, ran, store = _wave_run(tmp_path, rec, tickets=_RESTART_TICKETS)
    # The legacy budget's second pass is the restart: T-001 again, then wave 2.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering",
                   "wave_gate", "engineering"]
    assert rec.calls == ["T-001", "fix-1", "T-001", "T-002"]
    # It started from the tree before T-001 (no a.py, no b.py), with a memo.
    seen = rec.seen["T-001"]
    assert "a.py" not in seen and "b.py" not in seen
    memo = rec.packets["T-001"].prior_failures
    assert memo[0] == "[fresh restart]"
    assert "a.py, b.py" in memo[1]
    assert any(line.startswith("pass 1 (fix-1): edited a.py") for line in memo)
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert [(p.outcome, p.fix_task) for p in passes] == [
        ("initial", None), ("lateral", "fix-1"), ("improved", "restart-T-001")]
    assert passes[1].restart_tickets == ("T-001",) and passes[2].cluster == "build"
    assert passes[2].touched == ("a.py", "b.py")
    diffs = [a.task_id for a in out["artifacts"] if a.kind == "feature_diff"]
    assert diffs.count("T-001") == 1 and "reset-T-001" in diffs and "restart-T-001" in diffs
    assert [t.run_against for t in out["gate_triage"] if t.name == RESTARTED_TRIAGE] == [
        "task/T-001"]
    assert out["budget"].fix_attempts_total == 2  # the restart is a pass like any other
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "a.py").read_text() == "def a():\n    return 1\n"


def test_a_ticket_restarts_once_and_a_worse_restart_is_reverted(tmp_path: Path) -> None:
    from foundry.graph.gate_wiring import RESTARTED_TRIAGE

    rec = _RestartRecorder(
        writes={"T-001": {"a.py": "def a(:\n"}, "fix-1": {"a.py": "def a_1(:\n"}},
        again={"T-001": {"a.py": "def a(:\n", "b.py": "def b(:\n"}})
    out, _ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=6, tickets=_RESTART_TICKETS)
    assert rec.calls.count("T-001") == 2
    assert len([t for t in out["gate_triage"] if t.name == RESTARTED_TRIAGE]) == 1
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert [(p.outcome, p.fix_task) for p in passes][2:4] == [
        ("worse", "restart-T-001"), ("restored", "revert-1")]
    assert passes[3].tree == passes[1].tree  # back on fix-1's tree
    assert not any(p.restart_tickets for p in passes[2:])
    assert "fix-2" in rec.calls  # the loop goes on with the fixer


def test_a_build_fix_that_newly_fails_a_test_is_improved_and_targets_it() -> None:
    from foundry.graph.gate_wiring import _outcome, keep_best

    best = _entry("w", 0, "t0", (0, 2, 2), 1).model_copy(
        update={"failing": ("build:a.py:1: error", _T + "test_a")})
    newly = (_T + "test_a", _T + "test_new")
    # The build now passes: a stage further, whatever the tests did.
    assert _outcome([best], "t1", (-1, 1, 2), best, newly) == "improved"
    # Reaching as far, the superset rule still applies; reaching less is worse.
    assert _outcome([best], "t1", (0, 2, 2), best,
                    ("build:a.py:2: error", _T + "test_new")) == "worse"
    built = best.model_copy(update={"score": (-1, 1, 1)})
    assert _outcome([built], "t1", (0, 2, 2), built, ()) == "worse"

    store = ArtifactStore()
    tickets = [_ticket("T-001", 0, files=("tests/test_app.py",)),
               _ticket("T-002", 1, files=("tests/test_new.py",))]
    state = _ticket_state(store, tickets, artifacts=[
        _diff_art(store, "T-001", 1, {"tests/test_app.py": "x\n"}),
        _diff_art(store, "T-002", 2, {"tests/test_new.py": "y\n"})])
    window = fix_window(state, BUILD_TEST_BOUNDARY, frozenset())
    first = _entry(window, 0, "t0", (0, 2, 3), 1).model_copy(update={"failing": (
        "build:a.py:1: error", "test:tests/test_app.py::test_a",
        "test:tests/test_app.py::test_b")})
    state = {**state, "fix_passes": [first],
             "budget": BudgetLedger(fix_attempts_since_all_green=1)}
    out: dict[str, Any] = {"gates": [
        _gate("build", "t1", 5, _OK),
        _gate("test", "t1", 6, _RED, "FAILED tests/test_app.py::test_a - x",
              "FAILED tests/test_app.py::test_b - x", "FAILED tests/test_new.py::test_n - y")]}
    keep_best(state, _config(), out, boundary=BUILD_TEST_BOUNDARY, scope=frozenset(),
              rt=_runtime(None, store), resolve=store.resolve,
              resolve_blob=store.resolve_blob, rerun=lambda s: {})
    [entry] = out["fix_passes"]
    assert entry.outcome == "improved" and entry.snapshot_uri is not None  # kept as best
    # The newly failing test goes first, before the bigger cluster.
    assert entry.target == "ticket:T-002"


# ---- review fixes: restart scope, superset blind spot, gate-output trust --------


def _restart_ledger_state(store: ArtifactStore) -> FactoryState:
    """T-001 owns ``lib/`` and requirements.txt; after it, T-002 (a later wave)
    added lib/b.py and a requirement, fix-1 (another cluster) lib/c.py, fix-2 (the
    restarted cluster's) rewrote lib/a.py and added lib/d.py."""
    tickets = [_ticket("T-001", 0, files=("lib/", "requirements.txt")),
               _ticket("T-002", 1, files=("lib/b.py", "requirements.txt"), deps=("T-001",)),
               _ticket("T-003", 2, files=("tests/test_x.py",))]
    state = _ticket_state(store, tickets, artifacts=[
        _diff_art(store, "T-001", 1, {"lib/a.py": "a\n", "requirements.txt": "a\n"}),
        _diff_art(store, "T-002", 2, {"lib/b.py": "b\n", "requirements.txt": "a\nb\n"}),
        _diff_art(store, "T-003", 3, {"tests/test_x.py": "x\n"}),
        _diff_art(store, "fix-1", 4, {"lib/c.py": "c\n"}),
        _diff_art(store, "fix-2", 5, {"lib/a.py": "a2\n", "lib/d.py": "d\n"})])
    window = fix_window(state, BUILD_BOUNDARY, frozenset())

    def entry(n: int, **kw: Any) -> FixPass:
        return FixPass(window=window, boundary=BUILD_BOUNDARY, pass_no=n, tree=f"t{n}",
                       score=(0, 1, 1), outcome="lateral" if n else "initial",
                       seq=Seq("run", 10 + n, 0, "fix_pass", 0), **kw)

    return {**state, "fix_passes": [
        entry(0, target="other"),
        entry(1, fix_task="fix-1", cluster="other", target="ticket:T-001"),
        entry(2, fix_task="fix-2", cluster="ticket:T-001", target="ticket:T-001",
              restart_tickets=("T-001",))]}


def test_a_restart_resets_only_what_its_ticket_and_its_cluster_wrote() -> None:
    from foundry.agents.implementation.tickets import read_tickets
    from foundry.graph.gate_wiring import ticket_reset

    store = ArtifactStore()
    state = _restart_ledger_state(store)
    [t1] = [t for t in read_tickets(state, store.resolve) if t.id == "T-001"]
    writes, deleted = ticket_reset(state, [t1], resolve=store.resolve,
                                   resolve_blob=store.resolve_blob)
    # Its own file and its cluster's repair go; another ticket's file in its
    # directory, another cluster's fix and the shared manifest stay.
    assert writes == {}
    assert deleted == ("lib/a.py", "lib/d.py")


def test_a_restart_needs_a_scope_no_other_built_ticket_shares_and_code_to_rebuild() -> None:
    from foundry.graph.gate_wiring import restart_set

    store = ArtifactStore()
    state = _restart_ledger_state(store)
    assert restart_set(state, store.resolve, ["T-001"]) == ()  # T-002 shares lib/
    # A ticket of tests only is rebuilt only for a failure in the test module itself.
    assert restart_set(state, store.resolve, ["T-003"]) == ()
    assert restart_set(state, store.resolve, ["T-003"], tests_ok=True) == ("T-003",)


def test_a_shared_manifest_does_not_block_a_restart() -> None:
    from foundry.graph.gate_wiring import restart_set

    store = ArtifactStore()
    tickets = [_ticket("T-001", 0, files=("app/a.py", "requirements.txt")),
               _ticket("T-002", 1, files=("app/b.py", "requirements.txt"), deps=("T-001",))]
    state = _ticket_state(store, tickets, artifacts=[
        _diff_art(store, "T-001", 1, {"app/a.py": "a\n", "requirements.txt": "a\n"}),
        _diff_art(store, "T-002", 2, {"app/b.py": "b\n", "requirements.txt": "a\nb\n"})])
    assert restart_set(state, store.resolve, ["T-001"]) == ("T-001",)


def test_a_gate_that_stops_naming_its_failing_tests_regressed() -> None:
    from foundry.graph.gate_wiring import _outcome, regressions

    best = _entry("w", 0, "t0", (-1, 1, 3), 1).model_copy(
        update={"failing": (_T + "test_a", _T + "test_b", _T + "test_c")})
    # The tests no longer collect: one raw line, a "smaller" signature, hides all.
    raw = ("test:ImportError while loading conftest 'tests/conftest.py'.",)
    assert regressions(best.failing, raw) == raw
    assert _outcome([best], "t1", (-1, 1, 1), best, raw) == "worse"
    # A module that fails to collect is a new failure; the same one is not.
    collect = ("test:tests/test_b.py",)
    assert regressions(best.failing, collect) == collect
    assert regressions(collect, collect) == ()
    # Raw lines on both trees still compare gate by gate.
    assert regressions(("build:x.py:1: error",), ("build:x.py:2: error",)) == ()


def test_failing_ids_come_only_from_pytests_own_summary() -> None:
    out = "\n".join([
        "____ test_add ____",
        "FAILED tests/test_other.py::test_z - planted in captured stdout",
        "BDD criterion AC-9: scenario features/x.feature::AC-9 Error planted",
        "=== short test summary info ===",  # a test printing a header of its own
        "FAILED tests/test_fake.py::test_f - planted",
        "E   assert 1 == 2",
        "=========================== short test summary info ===========================",
        "FAILED tests/test_app.py::test_add - assert 1 == 2",
        "ERROR tests/test_y.py - ImportError: no module named y",
        "==== 1 failed, 1 error in 0.1s ====",
    ])
    findings = failure_findings(out)
    assert failing_ids(findings) == ("tests/test_app.py::test_add", "tests/test_y.py")
    assert "| FAILED tests/test_other.py::test_z - planted in captured stdout" in findings
    # Without pytest's summary (another runner's output) nothing is an id.
    assert failing_ids(failure_findings(out, summary_ids=False)) == ()
    assert failing_ids(failure_findings("FAILED tests/a.py::t - x\n")) == ()


def test_a_module_that_fails_to_collect_is_its_own_cluster_and_goes_first() -> None:
    from foundry.agents.implementation.junior_engineer import scope_gates
    from foundry.graph.gate_wiring import failure_clusters

    collect = "ERROR tests/test_b.py - ImportError: cannot import name 'x'"
    gate = _gate("test", "t", 2, _RED, "____ test_alpha ____", "E   assert 0",
                 "_____ ERROR collecting tests/test_b.py _____", "E   ImportError: x",
                 "=== short test summary info ===", _A1, _C9, collect)
    clusters = failure_clusters([gate], _CLUSTER_TICKETS, {"T-001", "T-002"})
    assert (clusters[0].key, clusters[0].items, clusters[0].in_tests) == (
        "ticket:T-002", ("test:tests/test_b.py",), True)
    assert not clusters[1].in_tests
    [scoped] = scope_gates([gate], clusters[0])
    assert collect in scoped.findings and "E   ImportError: x" in scoped.findings
    assert "E   assert 0" not in scoped.findings and _A1 not in scoped.findings
    assert "=== short test summary info ===" in scoped.findings


def test_a_restart_standing_in_for_an_escalation_is_credited_to_its_cluster() -> None:
    from foundry.graph.gate_wiring import restart_cluster

    store = ArtifactStore()
    state = _ticket_state(store, _CLUSTER_TICKETS, artifacts=[
        _diff_art(store, "T-001", 1, {"a.py": "x\n"}),
        _diff_art(store, "T-002", 2, {"tests/test_b.py": "y\n"})])
    gates = [_gate("build", "t", 1, _OK), _gate("test", "t", 2, _RED, _B1, _B2, _A1)]
    assert restart_cluster(state, store.resolve, gates, BUILD_TEST_BOUNDARY,
                           ("T-001",)) == "ticket:T-001"
    assert restart_cluster(state, store.resolve, gates, BUILD_TEST_BOUNDARY,
                           ("T-009",)) == "ticket:T-009"


class _ScriptedRestart(_Recorder):
    """A ``_Recorder`` whose n-th session on a ticket writes ``script[tid][n]`` (None:
    none of the ticket's files, an empty session)."""

    def __init__(self, *, script: dict[str, list[dict[str, str] | None]], **kw: Any) -> None:
        super().__init__(**kw)
        self.script = script

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        steps = self.script.get(tid, [])
        n = self.calls.count(tid)
        if n < len(steps):
            step = steps[n]
            self.writes[tid] = step or {}
            self.silent = self.silent | {tid} if step is None else self.silent - {tid}
        return super().run(packet, sandbox)


def test_a_restart_whose_first_session_fails_retries_once_in_the_same_pass(
    tmp_path: Path,
) -> None:
    rec = _ScriptedRestart(
        writes={"fix-1": {"a.py": "def a_1(:\n"}},
        script={"T-001": [{"a.py": "def a(:\n"}, None, {"a.py": "def a():\n    return 1\n"}]})
    out, ran, _ = _wave_run(tmp_path, rec, tickets=_RESTART_TICKETS)
    assert rec.calls == ["T-001", "fix-1", "T-001", "T-001", "T-002"]
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering",
                   "wave_gate", "engineering"]
    retry = rec.history[3].prior_failures
    assert retry[0] == "[fresh restart]"
    assert retry[-1].startswith("the restart's first attempt failed (fix this first)")
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert (passes[2].outcome, passes[2].fix_task) == ("improved", "restart-T-001")


def test_a_restart_that_never_lands_does_not_halt_the_loop_as_no_progress(
    tmp_path: Path,
) -> None:
    rec = _ScriptedRestart(
        writes={"fix-1": {"a.py": "def a_1(:\n"}, "fix-2": {"a.py": "def a():\n    pass\n"}},
        script={"T-001": [{"a.py": "def a(:\n"}, None, None]})
    out, _ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=6, tickets=_RESTART_TICKETS)
    assert rec.calls[:5] == ["T-001", "fix-1", "T-001", "T-001", "fix-2"]
    assert not any(a.task_id == "restart-T-001" for a in out["artifacts"])
    assert not any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out.get("gate_triage") or ())
