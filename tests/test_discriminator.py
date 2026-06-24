"""Held-out-mutant + behavioral-invariance discriminator. (T-M4-04, gov §1c)

The malicious/weak-test defense: a green pytest run is NECESSARY but NOT
SUFFICIENT. These exercise the discriminator standalone (kill score, behavioral
invariance, gate-class-awareness) and through the real Test gate / two-zone
runner (a gamed-green suite that survives a held-out mutant is FAILed; the
existing adequacy floor + exit-code semantics are preserved).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.gates import (
    DevGateWriteVerifier,
    TestGate,
    TwoZoneGateRunner,
    ZoneBSigner,
)
from foundry.gates.discriminator import (
    BehavioralInvariant,
    HeldOutMutant,
    TestSuiteDiscriminator,
    required_kill_ratio,
)
from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"

# --- the code under test: a tiny adder the agent was asked to implement -------
CALC_SRC = "def add(a, b):\n    return a + b\n"

# A GENUINE suite: exercises real behavior, so a mutated `a + b -> a - b` fails.
GENUINE_SUITE = (
    "import calc\n"
    "def test_add_basic():\n"
    "    assert calc.add(2, 3) == 5\n"
    "def test_add_commutes():\n"
    "    assert calc.add(7, 4) == calc.add(4, 7)\n"
    "def test_add_zero():\n"
    "    assert calc.add(0, 9) == 9\n"
)

# A GAMED-GREEN suite: passes pytest but tests nothing about `add`. It is green
# on the real tree AND stays green when `add` is mutated -> the mutant survives.
GAMED_SUITE = "import calc\ndef test_vacuous():\n    assert calc is not None\n    assert True\n"

# Held-out mutant: a + b -> a - b. A genuine suite kills it; the gamed one does not.
ADD_MUTANT = HeldOutMutant(mutant_id="add->sub", target="calc.py", find="a + b", replace="a - b")


def _seq(node: str, against: str) -> Seq:
    return Seq(run_id="run-1", emission_epoch=1, superstep_index=0, node_id=node, local=0)


def _mk_tree(tmp_path: Path, suite: str, src: str = CALC_SRC) -> Path:
    (tmp_path / "calc.py").write_text(src, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(suite, encoding="utf-8")
    return tmp_path


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, "scanner-rev-1", "lineage-rev-1")


@pytest.fixture
def verifier(records: GlobalRecords):
    v = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(v)
    yield v
    reset_gate_write_verifier(token)


def _assert_admitted(result) -> None:
    assert merge_gates([result], None) == [result]


# ---- standalone: held-out-mutant kill score --------------------------------


def test_genuine_suite_kills_held_out_mutant(tmp_path):
    tree = _mk_tree(tmp_path, GENUINE_SUITE)
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
    report = disc.evaluate(tree)
    assert report.passed
    assert report.kill_score == 1.0
    assert report.mutants_killed == 1
    assert report.survived == ()


def test_gamed_green_suite_lets_mutant_survive(tmp_path):
    """A vacuous suite is green but does NOT kill the mutant -> discriminator FAIL."""
    tree = _mk_tree(tmp_path, GAMED_SUITE)
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
    report = disc.evaluate(tree)
    assert not report.passed
    assert report.kill_score == 0.0
    assert report.survived == ("add->sub",)
    assert any("SURVIVED" in f for f in report.findings)


def test_inapplicable_mutant_is_not_counted_killed(tmp_path):
    tree = _mk_tree(tmp_path, GENUINE_SUITE)
    bad = HeldOutMutant(mutant_id="nope", target="calc.py", find="ZZZ", replace="WWW")
    report = TestSuiteDiscriminator(mutants=[bad]).evaluate(tree)
    assert not report.passed
    assert "inapplicable" in " ".join(report.findings)


# ---- standalone: behavioral invariance -------------------------------------


def test_behavioral_invariance_satisfied(tmp_path):
    tree = _mk_tree(tmp_path, GENUINE_SUITE)
    inv = BehavioralInvariant(
        invariant_id="add-3-4", module="calc.py", call="add", args=(3, 4), expected=7
    )
    report = TestSuiteDiscriminator(invariants=[inv]).evaluate(tree)
    assert report.passed
    assert report.invariants_violated == ()


def test_behavioral_invariance_violation_fails(tmp_path):
    # The produced code is WRONG (subtracts) even though the agent's suite (which
    # only ever asserts on its own buggy outputs) might be green -> invariant catches it.
    tree = _mk_tree(tmp_path, GENUINE_SUITE, src="def add(a, b):\n    return a - b\n")
    inv = BehavioralInvariant(
        invariant_id="add-3-4", module="calc.py", call="add", args=(3, 4), expected=7
    )
    report = TestSuiteDiscriminator(invariants=[inv]).evaluate(tree)
    assert not report.passed
    assert report.invariants_violated == ("add-3-4",)
    assert any("violated" in f for f in report.findings)


# ---- gate-class-awareness ---------------------------------------------------


def test_required_kill_ratio_is_class_aware():
    assert required_kill_ratio(VerdictClass.EXECUTION_DETERMINISTIC) == 1.0
    # Config-evaluable + statistical carry no held-out-mutant discriminator.
    assert required_kill_ratio(VerdictClass.CONFIG_EVALUABLE) == 0.0
    assert required_kill_ratio(VerdictClass.STATISTICAL) == 0.0


def test_class_awareness_changes_verdict_on_surviving_mutant(tmp_path):
    """SAME gamed suite + surviving mutant: class 2 FAILs, class 3 does not."""
    tree = _mk_tree(tmp_path, GAMED_SUITE)
    class2 = TestSuiteDiscriminator(
        mutants=[ADD_MUTANT], verdict_class=VerdictClass.EXECUTION_DETERMINISTIC
    ).evaluate(tree)
    class3 = TestSuiteDiscriminator(
        mutants=[ADD_MUTANT], verdict_class=VerdictClass.STATISTICAL
    ).evaluate(tree)
    assert not class2.passed  # class 2: a surviving mutant is fatal
    assert class3.passed  # class 3: discriminator carries no kill bar (vacuously ok)
    assert class3.required_kill_ratio == 0.0


# ---- through the Test gate / two-zone runner --------------------------------


def test_test_gate_fails_gamed_green_suite(tmp_path, runner, verifier):
    """End to end: gamed-green suite is green in pytest but the gate FAILs it."""
    tree = _mk_tree(tmp_path, GAMED_SUITE)
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
    r = runner.run(TestGate(discriminator=disc), "task/1", _seq("test", "task/1"), tree)
    assert r.status is GateStatus.FAIL
    assert any("SURVIVED" in f for f in r.findings)
    _assert_admitted(r)


def test_test_gate_passes_genuine_suite(tmp_path, runner, verifier):
    tree = _mk_tree(tmp_path, GENUINE_SUITE)
    inv = BehavioralInvariant(
        invariant_id="add-2-3", module="calc.py", call="add", args=(2, 3), expected=5
    )
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT], invariants=[inv])
    r = runner.run(TestGate(discriminator=disc), "task/1", _seq("test", "task/1"), tree)
    assert r.status is GateStatus.PASS
    _assert_admitted(r)


def test_test_gate_fails_invariant_violation(tmp_path, runner, verifier):
    tree = _mk_tree(tmp_path, GENUINE_SUITE, src="def add(a, b):\n    return a - b\n")
    # The suite passes (it only asserts buggy-self outputs would differ — use a
    # vacuous suite so pytest is green) and the invariant catches the wrong code.
    (tree / "test_calc.py").write_text(GAMED_SUITE, encoding="utf-8")
    inv = BehavioralInvariant(
        invariant_id="add-2-3", module="calc.py", call="add", args=(2, 3), expected=5
    )
    disc = TestSuiteDiscriminator(invariants=[inv])
    r = runner.run(TestGate(discriminator=disc), "task/1", _seq("test", "task/1"), tree)
    assert r.status is GateStatus.FAIL
    _assert_admitted(r)


def test_gate_mutant_rotation_mirrors_discriminator():
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT], mutant_rotation_id=7)
    assert TestGate(discriminator=disc).mutant_rotation_id == 7


# ---- existing Test-gate behavior preserved ---------------------------------


def test_clean_green_run_still_passes_without_discriminator(tmp_path, runner, verifier):
    """No discriminator wired: a real green suite still PASSes (legacy behavior)."""
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    r = runner.run(TestGate(), "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r)


def test_vacuous_zero_test_run_still_fails_via_floor(tmp_path, runner, verifier):
    """No tests collected (exit 5) -> adequacy floor FAIL, discriminator or not."""
    (tmp_path / "calc.py").write_text(CALC_SRC, encoding="utf-8")  # no test_*.py
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
    r = runner.run(TestGate(discriminator=disc), "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL


def test_red_suite_still_fails_by_exit_code(tmp_path, runner, verifier):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
    r = runner.run(TestGate(discriminator=disc), "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL
