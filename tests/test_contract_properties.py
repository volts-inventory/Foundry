"""Contract properties: planned, handed to the implementing ticket, enforced by Test."""

from __future__ import annotations

from pathlib import Path

from foundry.agents.implementation.tickets.packet import (
    _ticket_packet,
    property_rule,
    ticket_properties,
)
from foundry.agents.planner.build import _build_interfaces
from foundry.agents.planner.schema import PLAN_SCHEMA
from foundry.gates.properties import property_findings
from foundry.gates.test import PROPERTIES_KEY, TestGate
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, StackDecision, Task, Ticket
from foundry.state.seq import Seq

_SEQ = Seq("r", 1, 1, "planner", 0)


def _iface(name: str, kind: str, *props: tuple[str, str]) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "signature": f"def {name}(s: str) -> int: ...",
        "properties": [{"id": i, "text": t} for i, t in props],
    }


def test_planner_parses_properties_with_unique_ids() -> None:
    schema = PLAN_SCHEMA["properties"]["interfaces"]["items"]
    assert "properties" in schema["properties"] and "properties" not in schema["required"]
    raw = [
        _iface("roll", "function", ("PR1", "total within [min, max]"), ("pr2", "same seed")),
        # A repeated id and a free-form one are renumbered past every planned id.
        _iface("parse", "function", ("PR1", "parse(format(x)) == x"), ("x", "bad input raises")),
        _iface("Roll", "type", ("PR9", "ignored: a data type has none")),
        {"name": "old", "kind": "function", "signature": "def old() -> None: ..."},
    ]
    roll, parse, data, old = _build_interfaces(raw, _SEQ)
    assert roll.properties == ("PR1: total within [min, max]", "PR2: same seed")
    assert parse.properties == ("PR3: parse(format(x)) == x", "PR4: bad input raises")
    assert data.properties == () and old.properties == ()


def _ticket(tid: str, *deps: str, **extra: object) -> Ticket:
    extra.setdefault("specialty", Specialty.BACKEND)
    return Ticket(id=tid, title=tid, depends_on=deps, seq=_SEQ, **extra)  # type: ignore[arg-type]


def _contracts() -> dict[str, InterfaceContract]:
    c = InterfaceContract(
        name="roll", spec_uri="cas://x", kind="function", properties=("PR1: in range",), seq=_SEQ
    )
    return {"roll": c}


def test_the_packet_carries_the_property_rule() -> None:
    contracts = _contracts()
    dice = _ticket("dice", files=("dice.py", "tests/test_dice.py"), implements=("roll",))
    assert ticket_properties(dice, [dice], contracts) == ("roll: PR1: in range",)
    # A ticket with no test file leaves its properties to its paired test ticket.
    bare = _ticket("dice", files=("dice.py",), implements=("roll",))
    tests = _ticket("dice-tests", "dice", files=("tests/test_dice.py",), specialty=Specialty.TEST)
    assert ticket_properties(bare, [bare, tests], contracts) == ()
    assert ticket_properties(tests, [bare, tests], contracts) == ("roll: PR1: in range",)

    task = Task(
        id="task-dice",
        ticket_id="dice",
        spec_uri="cas://s",
        criterion_ids=(),
        branch="task/dice",
        seq=_SEQ,
    )
    packet = _ticket_packet(
        task,
        dice,
        (),
        spans=(),
        feature_files=(),
        prior_failures=(),
        turn_budget=10,
        cost_budget_microusd=0,
        properties=ticket_properties(dice, [dice], contracts),
    )
    [rule] = [r for r in packet.task["rules"] if r.startswith("PROPERTY TESTS")]
    assert "hypothesis" in rule and "roll: PR1: in range" in rule
    ts = property_rule(("roll: PR1: x",), StackDecision(language="typescript", test="npm test"))
    assert ts is not None and "fast-check" in ts and "devDependencies" in ts
    assert property_rule(("roll: PR1: x",), StackDecision(language="lua")) is None


_DICE = "def total(n: int) -> int:\n    return n * 2\n"
_PROPERTY_TEST = """\
from hypothesis import given, strategies as st

from dice import total


@given(st.integers(min_value=0, max_value=100))
def test_pr1_total_is_even(n):
    assert total(n) % 2 == {rest}
"""


def _project(tmp_path: Path, test: str) -> Path:
    (tmp_path / "dice.py").write_text(_DICE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_dice.py").write_text(test)
    return tmp_path


def test_python_gate_fails_a_missing_property_test(tmp_path: Path) -> None:
    plain = "from dice import total\n\n\ndef test_total():\n    assert total(2) == 4\n"
    gate = TestGate(properties={"PR1": "total: the total is even"})
    obs = gate.execute(_project(tmp_path, plain))
    assert obs.structured[PROPERTIES_KEY] == {"PR1": "missing"}
    assert gate.decide(obs) is GateStatus.FAIL
    [finding] = [f for f in obs.findings if f.startswith("property PR1 missing")]
    assert "hypothesis" in finding


def test_python_gate_passes_a_present_property_test(tmp_path: Path) -> None:
    gate = TestGate(properties={"PR1": "total: the total is even"})
    obs = gate.execute(_project(tmp_path, _PROPERTY_TEST.format(rest=0)))
    assert obs.structured[PROPERTIES_KEY] == {"PR1": "covered"}
    assert gate.decide(obs) is GateStatus.PASS, obs.stdout + obs.stderr


def test_python_gate_names_a_failing_property(tmp_path: Path) -> None:
    gate = TestGate(properties={"PR1": "total: the total is even"})
    obs = gate.execute(_project(tmp_path, _PROPERTY_TEST.format(rest=1)))
    assert gate.decide(obs) is GateStatus.FAIL
    assert any(f.startswith("property PR1 failing (tests/test_dice.py)") for f in obs.findings)


def test_non_python_profiles_detect_property_tests(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "dice.test.ts").write_text(
        "import fc from 'fast-check';\ntest('PR1: in range', () => fc.assert(fc.property()));\n"
    )
    (tmp_path / "dice_test.go").write_text(
        'package dice\n\nimport (\n\t"testing"\n\t"testing/quick"\n)\n\n'
        "func TestPR2Total(t *testing.T) { _ = quick.Check }\n"
        # PR10 is not PR1, and a file without the library proves nothing.
        "func TestPR10(t *testing.T) {}\n"
    )
    (tmp_path / "other_test.go").write_text("package dice\n\nfunc TestPR3(t *T) {}\n")
    props = {"PR1": "roll: a", "PR2": "total: b", "PR3": "c: c"}
    ts, _ = property_findings(tmp_path, props, "typescript")
    assert ts == {"PR1": "covered", "PR2": "missing", "PR3": "missing"}
    go, findings = property_findings(tmp_path, props, "go")
    assert go == {"PR1": "missing", "PR2": "covered", "PR3": "missing"}
    assert any(f.startswith("property PR3 missing") and "testing/quick" in f for f in findings)
    # The stack's own test command runs them; the gate FAILs on a missing one.
    gate = TestGate(
        cmd=("/bin/sh", "-c", "true"),
        min_passed=0,
        stack_harness=True,
        properties={"PR1": "roll: a"},
        language="typescript",
    )
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.PASS
    gate = TestGate(
        cmd=("/bin/sh", "-c", "true"),
        min_passed=0,
        stack_harness=True,
        properties={"PR3": "c: c"},
        language="go",
    )
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.FAIL
