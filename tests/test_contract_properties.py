"""Contract properties: planned, handed to the implementing ticket, enforced by Test."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from foundry.agents.implementation.tickets.packet import (
    _ticket_packet,
    conflicted_properties,
    live_contracts,
    property_rule,
    ticket_properties,
)
from foundry.agents.planner import SOP, make_planner_node
from foundry.agents.planner.build import _build_interfaces
from foundry.agents.planner.checks import _property_findings, finding_kind
from foundry.agents.planner.folds import (
    _drop_unsatisfiable_properties,
    _lift_described_properties,
    unsatisfiable_property,
)
from foundry.agents.planner.schema import PLAN_SCHEMA
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.tiered import SPEC_CONFLICT_TRIAGE
from foundry.gates.properties import property_findings
from foundry.gates.test import PROPERTIES_KEY, TestGate
from foundry.graph.gate_wiring.materialize import _ship_test_manifest
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus, GateTriage
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
    assert "properties" in schema["properties"] and "properties" in schema["required"]
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


def test_python_gate_counts_a_skipped_property_test_as_not_run(tmp_path: Path) -> None:
    skips = _PROPERTY_TEST.format(rest=0).replace(
        "from hypothesis", "import pytest\nfrom hypothesis"
    ) + (
        "\n\ndef test_plain():\n    pytest.skip('not a property')\n"
        "\n\n@given(st.integers())\ndef test_pr2_skips_zero(n):\n"
        "    if n == 0:\n        pytest.skip('zero')\n    assert total(n) % 2 == 0\n"
    )
    gate = TestGate(properties={"PR1": "total: even", "PR2": "total: even again"})
    obs = gate.execute(_project(tmp_path, skips))
    assert obs.structured[PROPERTIES_KEY] == {"PR1": "covered", "PR2": "skipped"}
    assert gate.decide(obs) is GateStatus.FAIL
    [finding] = [f for f in obs.findings if f.startswith("property PR2 not run")]
    assert "assume()" in finding


def test_a_skip_line_naming_a_property_marks_it_skipped(tmp_path: Path) -> None:
    (tmp_path / "rapid_test.go").write_text('import "testing/quick"\nfunc TestPR1Sum(t *T) {}\n')
    ran = "=== RUN   TestPR1Sum\n--- SKIP: TestPR1Sum (0.00s)\nok  \tx\t0.01s\n"
    status, [finding] = property_findings(tmp_path, {"PR1": "sum: s"}, "go", ran=ran)
    assert status == {"PR1": "skipped"} and "not run" in finding
    assert property_findings(tmp_path, {"PR1": "sum: s"}, "go", ran="1 skipped")[0] == {
        "PR1": "covered"
    }


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


# ---- properties the planner wrote into a ticket description --------------------


def _contract(name: str, *props: str, kind: str = "function", sig: str = "") -> InterfaceContract:
    return InterfaceContract(
        name=name,
        spec_uri="cas://x",
        kind=kind,
        signature=sig or f"def {name}(s: str) -> int: ...",
        properties=props,
        seq=_SEQ,
    )


def test_described_properties_are_lifted_onto_the_named_contract() -> None:
    roll, parse = _contract("roll"), _contract("parse_notation")
    total = _contract("total", "PR1: the total is even")
    dice = _ticket(
        "dice",
        files=("dice.py", "tests/test_dice.py"),
        implements=("roll", "parse_notation"),
        description=(
            "Roll dice from notation.\n"
            "Properties:\n"
            "- PR1: for any notation parse_notation(format(x)) == x\n"
            "PR2: for any mixed expression the roll lies in [min, max]; "
            "PR3 — the total is even"
        ),
    )
    out, interfaces, [c1, c2] = _lift_described_properties([dice], [roll, parse, total])
    assert out[0].description == "Roll dice from notation."
    by_name = {c.name: c for c in interfaces}
    # PR1 is taken, so renumbered; the text naming parse_notation goes there, the
    # rest to the first contract; the property `total` already states is dropped.
    assert by_name["parse_notation"].properties == (
        "PR2: for any notation parse_notation(format(x)) == x",
    )
    assert by_name["roll"].properties == (
        "PR3: for any mixed expression the roll lies in [min, max]",
    )
    assert by_name["total"].properties == ("PR1: the total is even",)
    assert "PR2" in c1 and "parse_notation" in c1 and "PR3" in c2


def test_the_planner_emits_the_lifted_property_on_its_contract() -> None:
    from tests.test_planner import PLAN, _bodies, config_for, runtime_for

    plan = json.loads(json.dumps(PLAN))
    plan["interfaces"][0].update(kind="function", signature="def add(title: str) -> int: ...")
    plan["tickets"][0]["description"] = "Store todos.\nPR1: for any title add returns a new id"
    cas = ArtifactStore()
    rt, _ = runtime_for(plan, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    contracts = {c.name: c for c in _bodies(cas, writes, "interface_contract")}
    assert contracts["TodoStore.add"].properties == ("PR1: for any title add returns a new id",)
    [store] = [t for t in _bodies(cas, writes, "ticket") if t.id == "T-001"]
    assert store.description == "Store todos."


def test_a_paired_test_ticket_lifts_onto_its_dependency_and_data_types_never_carry() -> None:
    record = _contract("Roll", kind="type", sig="class Roll: total: int")
    roll = _contract("roll")
    feature = _ticket("dice", files=("dice.py",), implements=("Roll", "roll"))
    tests = _ticket(
        "dice-tests",
        "dice",
        files=("tests/test_dice.py",),
        specialty=Specialty.TEST,
        description="PR1: any roll is within range",
    )
    out, interfaces, _ = _lift_described_properties([feature, tests], [record, roll])
    assert out[1].description == ""
    assert [c.properties for c in interfaces] == [(), ("PR1: any roll is within range",)]
    # A ticket implementing no code contract keeps its prose.
    lone = _ticket("docs", description="PR1: x")
    assert _lift_described_properties([lone], [record]) == ([lone], [record], [])


def test_a_plan_with_no_property_is_a_finding_only_for_a_real_input_domain() -> None:
    roll = _contract("roll", sig="def roll(notation: str, rng: Random | None = None) -> int")
    clock = _contract("now", sig="def now() -> datetime")
    store = _contract(
        "Store", kind="class", sig="class Store:\n    def __init__(self, db: Db) -> None: ..."
    )
    go = _contract("Sum", sig="func Sum(xs []int) (int, error)")
    # A store's CRUD method and the entrypoint's main(argv) are never judged.
    recent = _contract("recent", sig="def recent(self, n: int) -> list[Todo]")
    main = _contract("main", sig="def main(argv: list[str]) -> int")
    tickets = [
        _ticket("t", implements=("roll", "now", "Store", "Sum", "recent")),
        _ticket("cli", implements=("main",), entrypoint=True),
    ]
    findings = _property_findings(tickets, [roll, clock, store, go, recent, main])
    assert [f.split()[1] for f in findings] == ["roll", "Sum"]
    assert "notation: str" in findings[0] and "mixed operators" in findings[0]
    assert finding_kind(findings[0]) == "contract-no-properties"
    # Any property in the plan silences it; an unimplemented contract is not judged.
    assert _property_findings(tickets, [roll, _contract("x", "PR1: y")]) == ()
    assert _property_findings([], [roll]) == ()


def test_the_plan_sop_puts_properties_on_contracts_over_feature_combinations() -> None:
    assert "never in its description or a ticket's" in SOP
    assert "never only one feature or a single-term case" in SOP
    assert "trivial and edge inputs of its domain" in SOP
    assert "expressible through the contracts' types" in SOP
    assert "stated upper bound" in SOP


def test_a_substring_universal_with_no_length_floor_is_unsatisfiable() -> None:
    """Round 4 (notes PR7): a one-character password is in its own hex hash."""
    assert unsatisfiable_property(
        "For any password, hash_password(password) returns a string that does not "
        "contain the plaintext password as a substring."
    )
    assert unsatisfiable_property("for every token t, the log line never contains t as a substring")
    # A stated length floor, no universal, or no string containment: left alone.
    assert not unsatisfiable_property(
        "for any password of at least 8 characters the hash does not contain it as a substring"
    )
    assert not unsatisfiable_property("for any password whose length >= 12, no substring leaks")
    assert not unsatisfiable_property("hash('hunter2') does not contain 'hunter2' as a substring")
    assert not unsatisfiable_property("for any list, dedupe(xs) does not contain duplicates")
    assert not unsatisfiable_property("for any notation the total lies within [min, max]")


def test_the_planner_drops_an_unsatisfiable_property_without_a_repair() -> None:
    leak = "PR7: for any password, hash(password) does not contain password as a substring"
    hashing = _contract("hash_password", "PR6: verify(p, hash(p)) is true", leak)
    roll = _contract("roll", "PR1: in range")
    out, [concern] = _drop_unsatisfiable_properties([hashing, roll])
    assert out[0].properties == ("PR6: verify(p, hash(p)) is true",)
    assert out[1] is roll
    assert "hash_password" in concern and "PR7" in concern
    assert _drop_unsatisfiable_properties([roll]) == ([roll], [])


def test_the_planner_emits_no_unsatisfiable_property() -> None:
    from tests.test_planner import PLAN, _bodies, config_for, runtime_for

    plan = json.loads(json.dumps(PLAN))
    plan["interfaces"][0].update(
        kind="function",
        signature="def add(title: str) -> int: ...",
        properties=[
            {"id": "PR1", "text": "for any title add returns a new id"},
            {"id": "PR2", "text": "for any title the id does not contain the title as a substring"},
        ],
    )
    cas = ArtifactStore()
    rt, _ = runtime_for(plan, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    contracts = {c.name: c for c in _bodies(cas, writes, "interface_contract")}
    assert contracts["TodoStore.add"].properties == ("PR1: for any title add returns a new id",)


def test_a_spec_conflict_naming_a_property_drops_it_from_the_live_contracts() -> None:
    contract = _contract("hash_password", "PR6: verify round-trips", "PR7: no substring leak")
    roll = _contract("roll", "PR1: in range")
    auth = _ticket("auth", files=("auth.py", "tests/test_auth.py"), implements=("hash_password",))
    dice = _ticket("dice", files=("dice.py", "tests/test_dice.py"), implements=("roll",))
    bodies: dict[str, object] = {"cas://c": contract, "cas://r": roll, "cas://a": auth}
    bodies["cas://d"] = dice
    handles = [
        Artifact(kind="interface_contract", uri="cas://c", produced_by="planner", seq=_SEQ),
        Artifact(kind="interface_contract", uri="cas://r", produced_by="planner", seq=_SEQ),
        Artifact(kind="ticket", uri="cas://a", produced_by="planner", seq=_SEQ),
        Artifact(kind="ticket", uri="cas://d", produced_by="planner", seq=_SEQ),
    ]
    state: dict[str, object] = {"artifacts": handles}

    def resolve(a: Artifact) -> object:
        return bodies[a.uri]

    def note(name: str, task: str, text: str) -> GateTriage:
        return GateTriage(
            name=name, run_against=f"task/{task}", explanation=text, produced_by="p", seq=_SEQ
        )

    assert live_contracts(state, resolve)["hash_password"] is contract  # type: ignore[arg-type]
    state["gate_triage"] = [
        note(SPEC_CONFLICT_TRIAGE, "auth", "pr7 says the hash never holds it; 'a' is in hex"),
        note("adapter-report", "auth", "PR6 is fine"),
        # Another ticket cannot waive a property it does not prove.
        note(SPEC_CONFLICT_TRIAGE, "dice", "PR6 contradicts SC-2"),
    ]
    contracts = {"hash_password": contract, "roll": roll}
    assert conflicted_properties(state, [auth, dice], contracts) == {"PR7"}  # type: ignore[arg-type]
    live = live_contracts(state, resolve)  # type: ignore[arg-type]
    assert live["hash_password"].properties == ("PR6: verify round-trips",)
    assert live["roll"].properties == ("PR1: in range",)


# ---- guarded imports and the shipped manifest ------------------------------------


def _lock_version(name: str) -> str:
    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text())
    [pkg] = [p for p in lock["package"] if p["name"] == name]
    return str(pkg["version"])


def test_the_property_rule_forbids_a_guarded_import() -> None:
    rule = property_rule(("roll: PR1: x",), None)
    assert rule is not None and "try/except ImportError" in rule
    assert "Never skip inside a property" in rule and "assume()" in rule
    assert f"hypothesis=={_lock_version('hypothesis')}" in rule


_GUARDED = """\
import pytest

try:
    from hypothesis import given, strategies as st
except ImportError:  # pragma: no cover
    pytest.skip("hypothesis missing", allow_module_level=True)

from dice import total


@given(st.integers(min_value=0, max_value=100))
def test_pr1_total_is_even(n):
    assert total(n) % 2 == 0
"""


def test_python_gate_does_not_count_a_guarded_property_test(tmp_path: Path) -> None:
    _project(tmp_path, _GUARDED)
    status, [finding] = property_findings(tmp_path, {"PR1": "total: even"}, "python")
    assert status == {"PR1": "missing"}
    assert finding.startswith("property PR1 not covered (tests/test_dice.py)")
    skip = "import pytest\nhypothesis = pytest.importorskip('hypothesis')\n" + _PROPERTY_TEST
    (tmp_path / "tests" / "test_dice.py").write_text(skip.format(rest=0))
    assert property_findings(tmp_path, {"PR1": "t: e"}, "python")[0] == {"PR1": "missing"}


def test_the_shipped_manifest_declares_hypothesis_at_the_lock_pin(tmp_path: Path) -> None:
    pin = f"hypothesis=={_lock_version('hypothesis')}"
    _project(tmp_path, _PROPERTY_TEST.format(rest=0))
    (tmp_path / "app.py").write_text("import flask\n")
    (tmp_path / "requirements.txt").write_text("flask==3.0.0")
    _ship_test_manifest(tmp_path)
    assert (tmp_path / "requirements.txt").read_text() == f"flask==3.0.0\n{pin}\n"
    _ship_test_manifest(tmp_path)  # already declared: unchanged
    assert (tmp_path / "requirements.txt").read_text().count("hypothesis") == 1
    # No property test imports it: nothing is added.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("import flask\n")
    (plain / "requirements.txt").write_text("flask==3.0.0\n")
    _ship_test_manifest(plain)
    assert (plain / "requirements.txt").read_text() == "flask==3.0.0\n"


def test_the_shipped_manifest_declares_the_harness_libraries_the_tests_import(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("import flask\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_steps.py").write_text(
        "import pytest\nfrom pytest_bdd import scenarios\nimport mypy\n"
    )
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n")
    _ship_test_manifest(tmp_path)
    assert (tmp_path / "requirements.txt").read_text().splitlines() == [
        "flask==3.0.0",
        f"pytest=={_lock_version('pytest')}",
        f"pytest-bdd=={_lock_version('pytest-bdd')}",
    ]


def test_the_shipped_manifest_never_drops_a_requirement(tmp_path: Path) -> None:
    # A package can be used with no import (a server, a driver, a plugin): keep all.
    (tmp_path / "app.py").write_text("import flask\n")
    text = "# runtime\nFlask==3.0.0\nSQLAlchemy==2.0.30\ngunicorn==22.0.0\n-e .\n"
    (tmp_path / "requirements.txt").write_text(text)
    _ship_test_manifest(tmp_path)
    assert (tmp_path / "requirements.txt").read_text() == text


# ---- a failing property: the invariant's fault, or the test's own? ---------------

_BROKEN_STRATEGY = """\
from hypothesis import given, strategies as st

from dice import total


@st.composite
def evens(draw):
    n = draw(st.integers(0, 10))
    return n + "x"


@given(evens())
def test_pr1_total_is_even(n):
    assert total(n) % 2 == 0
"""


def test_a_strategy_that_raises_while_generating_blames_the_test(tmp_path: Path) -> None:
    """The dice run: "TypeError … while generating 'Draw 1' from
    valid_notation_with_terms()" was reported as "fix the implementation"."""
    gate = TestGate(properties={"PR1": "total: the total is even"})
    obs = gate.execute(_project(tmp_path, _BROKEN_STRATEGY))
    assert gate.decide(obs) is GateStatus.FAIL
    [finding] = [f for f in obs.findings if f.startswith("property PR1 failing")]
    assert "the TEST is wrong" in finding and "while generating" in finding, finding
    assert "fix the implementation, not the test" not in finding


def _pytest_block(*body: str) -> str:
    return "\n".join(
        (
            "=================================== FAILURES ===================================",
            "____________________________ test_pr1_signup_is_unique ____________________________",
            *body,
            "=========================== short test summary info ============================",
            "FAILED tests/test_users.py::test_pr1_signup_is_unique - Error",
        )
    )


def _users(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_users.py").write_text(
        "from hypothesis import given\n\ndef test_pr1_signup_is_unique(): ...\n"
    )
    return tmp_path


def _finding(tmp_path: Path, output: str) -> str:
    _, findings = property_findings(tmp_path, {"PR1": "signup: unique"}, "python", output)
    [finding] = findings
    return finding


def test_state_shared_across_examples_blames_the_tests_fixture(tmp_path: Path) -> None:
    """The notes run: a tmp_path DB shared across Hypothesis examples raised
    DuplicateUsernameError from the store on the second example."""
    out = _pytest_block(
        "tests/test_users.py:12: in test_pr1_signup_is_unique",
        "    store.signup(name)",
        "app/store.py:40: in signup",
        "    raise DuplicateUsernameError(name)",
        "E   app.store.DuplicateUsernameError: Username '0' is taken",
        "E   Falsifying example: test_pr1_signup_is_unique(tmp_path=..., name='0')",
    )
    finding = _finding(_users(tmp_path), out)
    assert "the TEST is wrong" in finding and "earlier generated example" in finding
    assert "function-scoped fixtures" in finding and "per example" in finding


def test_an_error_only_in_the_tests_frames_blames_the_test(tmp_path: Path) -> None:
    out = _pytest_block(
        "tests/test_users.py:12: in test_pr1_signup_is_unique",
        "    names = sorted(names, key=len) + None",
        'E   TypeError: can only concatenate list (not "NoneType") to list',
    )
    assert "every frame of the traceback is in the test" in _finding(_users(tmp_path), out)


def test_a_broken_invariant_still_blames_the_implementation(tmp_path: Path) -> None:
    root = _users(tmp_path)
    asserted = _pytest_block(
        "tests/test_users.py:12: in test_pr1_signup_is_unique",
        "    assert store.count() == 1",
        "E   assert 2 == 1",
    )
    raised = _pytest_block(
        "tests/test_users.py:12: in test_pr1_signup_is_unique",
        "    store.signup(name)",
        "app/store.py:40: in signup",
        "    return name.lower() + 1",
        'E   TypeError: can only concatenate str (not "int") to str',
    )
    for out in (asserted, raised):
        finding = _finding(root, out)
        assert "fix the implementation, not the test" in finding, finding


def test_a_jest_test_failing_in_its_own_code_blames_the_test(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "users.test.ts").write_text(
        "import fc from 'fast-check';\ntest('PR1 signup is unique', () => {});\n"
    )
    out = (
        "  ✕ PR1 signup is unique (5 ms)\n\n"
        "  ● PR1 signup is unique\n\n"
        "    TypeError: Cannot read properties of undefined (reading 'map')\n\n"
        "      at Object.<anonymous> (test/users.test.ts:7:21)\n"
        "      at node_modules/fast-check/lib/check/runner.js:44:9\n"
    )
    _, [finding] = property_findings(tmp_path, {"PR1": "signup: unique"}, "typescript", out)
    assert "the TEST is wrong" in finding, finding


# ---- a scoped check judges the session's own tests and properties ----------------


def test_a_scoped_gate_fails_on_its_own_tests_and_properties(tmp_path: Path) -> None:
    """Round 3: a scoped (wave or ticket) Test gate passed on its scenarios alone, so
    the property-test bugs of both apps surfaced only at the final gate."""
    root = _project(tmp_path, _PROPERTY_TEST.format(rest=1))
    (root / "tests" / "test_other.py").write_text("def test_other():\n    assert False\n")
    scoped = TestGate(scope=frozenset())
    own = scoped.owning(("dice.py", "tests/test_dice.py"), {"PR1": "total: the total is even"})
    assert own.scanner_rule_version != scoped.scanner_rule_version
    obs = own.execute(root)
    assert own.decide(obs) is GateStatus.FAIL
    own_red = "this ticket's own tests failed (tests/test_dice.py"
    assert any(f.startswith(own_red) for f in obs.findings), obs.findings
    assert any(f.startswith("property PR1 failing") for f in obs.findings), obs.findings
    # Another ticket's red test is not run by this session's check.
    assert not any("test_other" in f for f in obs.findings), obs.findings
    # (A same-size rewrite in the same second would import the stale bytecode.)
    shutil.rmtree(root / "tests" / "__pycache__", ignore_errors=True)
    (root / "tests" / "test_dice.py").write_text(_PROPERTY_TEST.format(rest=0))
    green = own.execute(root)
    assert own.decide(green) is GateStatus.PASS, green.findings
    # A property no test names fails the scoped gate too.
    missing = scoped.owning(("tests/test_dice.py",), {"PR2": "total: the total is positive"})
    obs = missing.execute(root)
    assert obs.structured[PROPERTIES_KEY] == {"PR2": "missing"}
    assert missing.decide(obs) is GateStatus.FAIL
    # A subtree entry runs every test module under it.
    subtree = scoped.owning(("tests/",), {})
    assert subtree.decide(subtree.execute(root)) is GateStatus.FAIL


_STEP_MODULE = """\
from hypothesis import given, strategies as st
from pytest_bdd import given, scenarios, then

from dice import total

scenarios("../features/T1.feature")


@given("a die")
def die():
    pass


@then("it totals")
def totals():
    assert total(1) == 2


class TestDiceProperties:
    @given(st.integers(min_value=0, max_value=100))
    def test_pr1_total_is_even(self, n):
        assert total(n) % 2 == 0
"""


def _step_project(tmp_path: Path, module: str) -> Path:
    root = _project(tmp_path, module)
    (root / "features").mkdir()
    (root / "features" / "T1.feature").write_text(
        "Feature: dice\n  Scenario: AC-1\n    Given a die\n    Then it totals\n"
    )
    return root


def test_a_scoped_gate_judges_the_other_tests_of_an_own_step_module(tmp_path: Path) -> None:
    """Round 4 (notes): T1's `from pytest_bdd import given` shadowed hypothesis's, so
    its property tests in the step module errored, yet its check and the wave checks
    passed on the module's scenarios: the own run left step modules out."""
    root = _step_project(tmp_path, _STEP_MODULE)
    gate = TestGate(scope=frozenset({"features/T1.feature::AC-1"})).owning(
        ("tests/test_dice.py",), {"PR1": "total: the total is even"}
    )
    obs = gate.execute(root)
    assert obs.structured["bdd"] == {"features/T1.feature::AC-1": "passed"}
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("tests/test_dice.py::TestDiceProperties" in f for f in obs.findings), obs.findings
    # Fixed, the module's scenario is not run again (or failed) by the own run.
    shutil.rmtree(root / "tests" / "__pycache__", ignore_errors=True)
    (root / "tests" / "test_dice.py").write_text(
        _STEP_MODULE.replace("from pytest_bdd import given,", "from pytest_bdd import")
        .replace('@given("a die")', "@pytest_bdd.given('a die')")
        .replace("from dice", "import pytest_bdd\nfrom dice")
    )
    green = gate.execute(root)
    assert gate.decide(green) is GateStatus.PASS, green.findings
    assert green.structured["own_exit_code"] == 0


def test_an_import_error_in_an_own_step_module_fails_the_scoped_gate(tmp_path: Path) -> None:
    root = _step_project(tmp_path, "from dice import nothing\n" + _STEP_MODULE)
    # Out of scope: the BDD run does not run the module; the own run still does.
    gate = TestGate(scope=frozenset()).owning(("tests/test_dice.py",), {})
    obs = gate.execute(root)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any(f.startswith("this ticket's own tests failed") for f in obs.findings)
