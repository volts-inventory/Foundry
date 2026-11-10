"""Gate verdict reuse (foundry.gates.reuse + TwoZoneGateRunner.run_unit).

A boundary's gates re-run on provably identical inputs re-reference the ORIGINAL
attested verdicts (fresh Seq, same signature); any input change re-runs; ERROR is
never reused; a stored record that does not verify under the runner's key is never
reused; the emit guard still keys on an attested PASS for the newest tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry import telemetry
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.gates.pydeps import PythonBuildEnv
from foundry.gates.reuse import VerdictCache
from foundry.gates.runner import ZoneAObservation, observe
from foundry.graph.factory_graph import terminal_gates_pass
from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
TREE = "a" * 64
OTHER_TREE = "b" * 64


class FileGate:
    """PASS iff ``ok.txt`` reads ``ok``; counts its executions."""

    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True
    mutant_rotation_id = 0
    scanner_rule_version = "file-v1"

    def __init__(
        self, name: str = "build", *, flag: str = "x", pyenv: object = None, boom: bool = False
    ) -> None:
        self.name = name
        self.flag = flag
        self._pyenv = pyenv
        self._boom = boom
        self._seal_key = b"per-process"
        self.runs = 0

    def execute(self, workdir: Path) -> ZoneAObservation:
        self.runs += 1
        if self._boom:
            raise RuntimeError("sandbox crashed")
        body = (workdir / "ok.txt").read_text()
        return observe(self.name, exit_codes=(0 if body == "ok" else 1,))

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS if obs.exit_codes == (0,) else GateStatus.FAIL


def _runner(records: GlobalRecords, cache: VerdictCache | None, key: bytes = KEY):
    return TwoZoneGateRunner(ZoneBSigner(key, KEY_ID), records, "scan-1", "lin-1", verdicts=cache)


def _seq(epoch: int, node: str = "build_gate") -> Seq:
    return Seq(run_id="run-1", emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _tree(root: Path, body: str = "ok") -> Path:
    root.mkdir(parents=True)
    (root / "ok.txt").write_text(body)
    (root / "features").mkdir()
    (root / "features" / "a.feature").write_text("Feature: a\n")
    return root


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def verifier(records: GlobalRecords):
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        yield
    finally:
        reset_gate_write_verifier(token)


def test_identical_inputs_reuse_the_original_attested_record(tmp_path, records, verifier):
    runner = _runner(records, VerdictCache())
    gate = FileGate()
    (first,) = runner.run_unit([(gate, _seq(1))], TREE, _tree(tmp_path / "w1"))
    with telemetry.counting() as counts:
        (again,) = runner.run_unit([(FileGate(), _seq(2))], TREE, _tree(tmp_path / "w2"))
    assert gate.runs == 1
    assert again.status is GateStatus.PASS
    # The original signature, re-referenced: nothing was re-signed.
    assert again.runner_attestation == first.runner_attestation
    assert again.content_identity() == first.content_identity()
    assert again.seq == _seq(2)
    assert counts.snapshot() == {"gate.reuse.build": 1}
    # The reused record outranks the older one and is admitted by the real reducer.
    assert merge_gates([first], [again]) == [again]


def test_a_unit_reuses_all_or_nothing(tmp_path, records):
    runner = _runner(records, VerdictCache())
    runner.run_unit([(FileGate(), _seq(1))], TREE, _tree(tmp_path / "w1"))
    build, test = FileGate(), FileGate("test")
    runner.run_unit([(build, _seq(2)), (test, _seq(2, "test_gate"))], TREE, _tree(tmp_path / "w2"))
    assert (build.runs, test.runs) == (1, 1)  # a different unit: nothing reused


@pytest.mark.parametrize(
    "change",
    ["file_byte", "new_file", "exec_bit", "gate_flag", "deps", "env", "run_against"],
)
def test_any_input_change_reruns(tmp_path, records, monkeypatch, change):
    runner = _runner(records, VerdictCache())
    venv = PythonBuildEnv(tmp_path / "env", ("-r", "requirements.txt"), digest="deps-1")
    runner.run_unit([(FileGate(pyenv=venv), _seq(1))], TREE, _tree(tmp_path / "w1"))

    w2 = _tree(tmp_path / "w2")
    gate = FileGate(pyenv=venv)
    against = TREE
    if change == "file_byte":
        (w2 / "features" / "a.feature").write_text("Feature: b\n")
    elif change == "new_file":
        (w2 / "pyproject.toml").write_text("")
    elif change == "exec_bit":
        (w2 / "ok.txt").chmod(0o755)
    elif change == "gate_flag":
        gate = FileGate(pyenv=venv, flag="y")
    elif change == "deps":
        other = PythonBuildEnv(tmp_path / "env2", ("-r", "requirements.txt"), digest="deps-2")
        gate = FileGate(pyenv=other)
    elif change == "env":
        monkeypatch.setenv("FOUNDRY_GATE_REUSE_TEST", "1")
    else:
        against = OTHER_TREE
    runner.run_unit([(gate, _seq(2))], against, w2)
    assert gate.runs == 1


def test_unkeyable_inputs_never_reuse(tmp_path, records):
    runner = _runner(records, VerdictCache())
    local = PythonBuildEnv(tmp_path / "env", ("./vendored",), digest=None)
    for i, w in enumerate(("w1", "w2")):
        gate = FileGate(pyenv=local)
        runner.run_unit([(gate, _seq(i + 1))], TREE, _tree(tmp_path / w))
        assert gate.runs == 1


def test_error_is_never_reused(tmp_path, records):
    runner = _runner(records, VerdictCache())
    for i, w in enumerate(("w1", "w2")):
        gate = FileGate(boom=True)
        (result,) = runner.run_unit([(gate, _seq(i + 1))], TREE, _tree(tmp_path / w))
        assert result.status is GateStatus.ERROR and gate.runs == 1
    # A unit holding any ERROR is not stored, even beside a PASS.
    build, test = FileGate(), FileGate("test", boom=True)
    runner.run_unit([(build, _seq(3)), (test, _seq(3, "test_gate"))], TREE, _tree(tmp_path / "w3"))
    build2 = FileGate()
    runner.run_unit(
        [(build2, _seq(4)), (FileGate("test", boom=True), _seq(4, "test_gate"))],
        TREE,
        _tree(tmp_path / "w4"),
    )
    assert build2.runs == 1


def test_reused_fail_relatches(tmp_path, records):
    cache = VerdictCache()
    _runner(records, cache).run_unit([(FileGate(), _seq(1))], TREE, _tree(tmp_path / "w1", "no"))
    fresh = GlobalRecords()
    gate = FileGate()
    (again,) = _runner(fresh, cache).run_unit([(gate, _seq(2))], TREE, _tree(tmp_path / "w2", "no"))
    assert gate.runs == 0 and again.status is GateStatus.FAIL
    assert fresh.stable_fail_latched(TREE, "build")


def test_resume_reuses_from_disk_and_outranks_the_prior_record(tmp_path, records, verifier):
    store = tmp_path / "verdicts"
    gate = FileGate()
    (prior,) = _runner(records, VerdictCache(store)).run_unit(
        [(gate, _seq(1))], TREE, _tree(tmp_path / "w1")
    )
    # A new process: fresh runner and cache over the same run dir, epochs floored.
    resumed_gate = FileGate()
    (again,) = _runner(records, VerdictCache(store)).run_unit(
        [(resumed_gate, _seq(5))], TREE, _tree(tmp_path / "w2")
    )
    assert (gate.runs, resumed_gate.runs) == (1, 0)
    assert again.runner_attestation == prior.runner_attestation and again.seq > prior.seq
    assert merge_gates([prior, again], None) == [again]


def test_a_stored_record_that_does_not_verify_is_rerun(tmp_path, records):
    store = tmp_path / "verdicts"
    _runner(records, VerdictCache(store)).run_unit(
        [(FileGate(), _seq(1))], TREE, _tree(tmp_path / "w1", "no")
    )
    (path,) = store.glob("*.json")
    body = json.loads(path.read_text())
    body[0]["status"] = "pass"  # flip the stored FAIL
    path.write_text(json.dumps(body))
    gate = FileGate()
    (again,) = _runner(records, VerdictCache(store)).run_unit(
        [(gate, _seq(2))], TREE, _tree(tmp_path / "w2", "no")
    )
    assert gate.runs == 1 and again.status is GateStatus.FAIL
    # Nor does a record signed under another key reuse.
    gate = FileGate()
    _runner(records, VerdictCache(store), key=b"another-key-0123456789abcdef").run_unit(
        [(gate, _seq(3))], TREE, _tree(tmp_path / "w3", "no")
    )
    assert gate.runs == 1


def test_emit_guard_accepts_a_reused_attested_pass_and_refuses_without_one(
    tmp_path, records, verifier
):
    runner = _runner(records, VerdictCache())
    runs: list[FileGate] = []

    def unit(epoch: int, against: str, body: str, w: str):
        gates = [FileGate(n) for n in ("spec", "build", "test")]
        runs.extend(gates)
        return runner.run_unit(
            [(g, _seq(epoch, f"{g.name}_gate")) for g in gates],
            against,
            _tree(tmp_path / w, body),
        )

    first = unit(1, TREE, "ok", "w1")
    reused = unit(2, TREE, "ok", "w2")
    assert [g.runs for g in runs] == [1, 1, 1, 0, 0, 0]
    assert [r.runner_attestation for r in reused] == [r.runner_attestation for r in first]
    assert terminal_gates_pass({"gates": merge_gates(first, reused)})

    # The newer tree FAILs: an older tree's (reused) PASS never stands in for it.
    newer = unit(3, OTHER_TREE, "no", "w3")
    assert not terminal_gates_pass({"gates": merge_gates(merge_gates(first, reused), newer)})

    # A reused record with its status flipped loses its attestation: dropped.
    flipped = [r.model_copy(update={"status": GateStatus.PASS, "seq": _seq(9)}) for r in newer]
    folded = merge_gates(merge_gates(first, newer), flipped)
    assert not terminal_gates_pass({"gates": folded})
