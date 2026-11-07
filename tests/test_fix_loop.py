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
                            budget=BudgetLedger(fix_attempts_total=4))
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
    # The restore is the fix loop's diff, not the fixer's: the next summary is fix-2's.
    summary = rec.packets["fix-3"].prior_failures
    assert any(line.startswith("previous fix pass (fix-2)") for line in summary)
    assert not any("revert-1" in line for line in summary)
    assert {"T-001", "fix-1", "fix-2", "revert-1", "fix-3", "T-002"} <= {
        a.task_id for a in out["artifacts"] if a.kind == "feature_diff"}


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
