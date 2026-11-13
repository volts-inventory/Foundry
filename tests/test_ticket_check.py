"""A ticket session's gate check also judges its own tests and contract properties,
and the session that completes the app checks the launch (bench round 3: both
surfaced only at the final gates)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

import foundry.gates.self_check as self_check
from foundry.agents._common import emit_artifact
from foundry.gates.self_check import CheckSpec, SessionCheck
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, Ticket, scenario_ref
from tests.test_self_check import _criterion, _fake_base, _seq, _spec, _state, _steps

_CALC = "def double(n):\n    return n * 2\n"
_CALC_TESTS = (
    "from hypothesis import given, strategies as st\n\nfrom calc import double\n\n\n"
    "def test_double_two():\n    assert double(2) == {two}\n\n\n"
    "@given(st.integers())\ndef test_pr1_double_is_even(n):\n    assert double(n) % 2 == {rest}\n"
)


def test_a_ticket_check_fails_on_its_own_tests_and_properties() -> None:
    earlier = _criterion("AC-0", "T0")
    built = {
        "tests/test_t0.py": _steps("T0.feature", ok=False),
        "tests/test_other.py": "def test_other():\n    assert False\n",
    }
    files = ("calc.py", "tests/test_calc.py")
    with _spec(built, (earlier,), files=files) as plain:
        spec = dataclasses.replace(
            plain, own_tests=files, properties={"PR1": "double: the result is even"}
        )
        session = SessionCheck(spec)
        opening = session.opening()
        bad = session.check(
            {"calc.py": _CALC, "tests/test_calc.py": _CALC_TESTS.format(two=5, rest=1)}, ()
        )
        good = session.check(
            {"calc.py": _CALC, "tests/test_calc.py": _CALC_TESTS.format(two=4, rest=0)}, ()
        )
    theirs = scenario_ref("T0", "AC-0")
    assert any(line.startswith("property PR1 missing") for line in opening.yours), opening
    assert bad.red and any("test_double_two" in line for line in bad.yours), bad
    assert any(line.startswith("property PR1 failing") for line in bad.yours), bad
    # Another ticket's red scenario stays pre-existing; its red test never runs here.
    for v in (opening, bad, good):
        assert not any(theirs in line for line in v.yours), v
        assert any(theirs in line for line in v.pre_existing), v
        assert not any("test_other" in line for line in (*v.yours, *v.pre_existing)), v
    assert not good.red, good


class _FakeLaunch:
    name = "launch"

    def __init__(self, ran: list[Path]) -> None:
        self.ran = ran

    def execute(self, workdir: Path) -> Any:
        from foundry.gates.runner import observe

        self.ran.append(workdir)
        return observe("launch", findings=("launch: POST /notes with a hostile body: 500",))

    def decide(self, obs: Any) -> GateStatus:
        return GateStatus.FAIL


def test_a_launch_check_runs_the_launch_gate_once_the_app_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import foundry.graph.gate_wiring as gate_wiring

    ran: list[Path] = []
    monkeypatch.setattr(gate_wiring, "launch_gate", lambda *a, **k: _FakeLaunch(ran))
    with _spec({"app.py": "X = 1\n"}, files=("app.py",)) as plain:
        quiet = SessionCheck(plain).opening()
        session = SessionCheck(dataclasses.replace(plain, launch=True, own_smoke=True))
        opening = session.opening()
        broken = session.check({"app.py": "X = (\n"}, ())
    assert "launch" not in quiet.checked
    assert "launch" in opening.checked and "smoke" not in opening.checked
    assert any("hostile body" in line for line in opening.yours), opening
    # An app that does not build is not launched: the build failure is the report.
    assert broken.red and "launch" not in broken.checked
    assert len(ran) == 1


def test_the_checks_launch_gate_runs_inside_tighter_limits() -> None:
    from foundry.gates.launch import LaunchGate, LaunchTarget

    full = LaunchGate(LaunchTarget(kind="http", cmd="python app.py"))
    bounded = self_check._bounded(full, None)
    assert isinstance(bounded, LaunchGate) and bounded.target == full.target
    assert bounded._cfg["request_s"] == self_check.LAUNCH_REQUEST_S
    assert bounded._timeout_s < full._timeout_s


def _ticket(tid: str, *deps: str, **extra: Any) -> Ticket:
    extra.setdefault("specialty", Specialty.BACKEND)
    return Ticket(id=tid, title=tid, depends_on=deps, seq=_seq("planner", len(extra)), **extra)


def _ticket_spec(tmp_path: Path, ticket: Ticket, *others: Ticket) -> CheckSpec:
    from foundry.agents.implementation.tickets.constants import CONTRACT_KIND
    from foundry.agents.implementation.tickets.packet import _ticket_check
    from foundry.schemas.architecture import InterfaceContract
    from foundry.schemas.implementation import Task

    # The state's one diff is T0's: T0 is built.
    state, resolve, _ = _state({"app.py": "X = 1\n"}, tickets=(ticket, *others))
    contract = InterfaceContract(
        name="notes",
        spec_uri="cas://c",
        kind="function",
        properties=("PR1: titles stay unique",),
        seq=_seq("planner", 9),
    )
    handle = emit_artifact(contract, CONTRACT_KIND, "planner", contract.seq)
    state["artifacts"].append(handle)

    def resolving(a: Artifact) -> Any:
        return contract if a.uri == handle.uri else resolve(a)

    task = Task(
        id=ticket.id,
        ticket_id=ticket.id,
        spec_uri="cas://s",
        criterion_ids=(),
        branch=f"task/{ticket.id}",
        seq=_seq("planner"),
    )
    return _ticket_check(_fake_base(tmp_path), state, resolving, task, ticket, {}, (), {})


def test_the_ticket_check_launches_where_the_app_is_complete(tmp_path: Path) -> None:
    t0 = _ticket("T0", files=("app.py",))
    entry = _ticket(
        "T1",
        "T0",
        files=("main.py", "tests/test_main.py"),
        implements=("notes",),
        entrypoint=True,
    )
    tests = _ticket("T2", "T1", files=("tests/test_e2e.py",), specialty=Specialty.TEST)
    spec = _ticket_spec(tmp_path, entry, t0, tests)
    # The entrypoint, its dependencies built: a red launch is its own.
    assert spec.launch and spec.own_smoke and not spec.smoke
    assert spec.own_tests == entry.files
    assert spec.properties == {"PR1": "notes: titles stay unique"}
    # Not the entrypoint, with a ticket still unbuilt: no launch.
    assert not _ticket_spec(tmp_path, tests, t0, entry).launch
    # The last ticket to build completes the app, entrypoint or not: not its own.
    last = _ticket_spec(tmp_path, tests, t0)
    assert last.launch and not last.own_smoke
