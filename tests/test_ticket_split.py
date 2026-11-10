"""A ticket that failed after its retry is split by the planner (ADaPT), once.

The engineering node asks for the split when a ticket with two or more scenarios
or work files fails its last attempt; the supervisor routes implementation ->
planner, whose children replace the ticket (after the plan's own assembly and
checks), and the next wave builds them. An invalid split is declined and the fix
loop's restart stays the ticket's fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.agents.implementation.tickets import pending_splits, read_tickets
from foundry.agents.implementation.tickets.constants import (
    PLANNER_NODE_ID,
    SPLIT_DECLINED_TRIAGE,
    SPLIT_REQUEST_TRIAGE,
    SPLIT_TRIAGE,
)
from foundry.agents.implementation.tickets.split import (
    may_split,
    split_after_restart,
    splittable,
)
from foundry.agents.planner.split import SPLIT_SOP, split_items
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.gate_wiring.ledger import RESTARTED_TRIAGE
from foundry.graph.gate_wiring.restarts import FailureCluster, unbuilt_restart
from foundry.graph.gate_wiring.wave_check import WaveBuildCheck
from foundry.graph.supervisor import build_supervisor_graph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import FixPass, GateTriage
from foundry.schemas.implementation import Specialty, Ticket
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
from foundry.telemetry import counting
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
    _resolve_blob_factory,
    _runner,
    _synthetic_resolve,
)

_SPLITTER_KEY = SPLIT_SOP.split(".")[0]
_SEQ = Seq("run", 1, 0, "planner", 0)


def _plan(*units: str) -> str:
    """One ticket per unit, the last the entrypoint after the others. One unit: a
    coarse ticket carrying both criteria. Two: one criterion each, the last ticket
    writing two work files."""
    split = {"app.py": ["AC-001", "AC-002"]} if len(units) == 1 else {}
    split = split or {units[0]: ["AC-001"], units[-1]: ["AC-002"]}
    payload = plan_payload(
        intent=INTENT_PAYLOAD,
        prd=PRD_PAYLOAD,
        critique=CRITIQUE_PAYLOAD,
        sprint=SPRINT_PAYLOAD,
        design={**DESIGN_PAYLOAD, "ownership": dict.fromkeys(units, "owner-a")},
        decomposition={"tasks": [{"unit": u, "criterion_ids": c} for u, c in split.items()]},
    )
    if len(units) > 1:
        payload["tickets"][-1]["files"].append("todo.py")
    return json.dumps(payload)


def _child(title: str, scenarios: list[str], after: list[int]) -> dict[str, Any]:
    return {
        "title": title,
        "description": title,
        "scenario_ids": scenarios,
        "files": ["app.py"],
        "implements": [],
        "consumes": [],
        "after": after,
    }


_GOOD = {
    "children": [_child("add todos", ["AC-001"], []), _child("list todos", ["AC-002"], [1])],
    "rationale": "one scenario per session",
}
# AC-002 is on no child: not a split of the ticket.
_BAD = {"children": [_child("add", ["AC-001"], []), _child("rest", [], [1])], "rationale": "x"}


class _Failing(StubAdapter):
    """Every session of the ``failing`` ticket itself fails; its children build."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing
        self.calls: list[str] = []

    def run(self, packet: Any, sandbox: Any) -> Any:
        tid = str(packet.task["id"])
        self.calls.append(tid)
        if tid.removeprefix("restart-") == self.failing:
            raise RuntimeError("the session could not finish the ticket")
        return super().run(packet, sandbox)


def _run(
    tmp_path: Path, reply: dict[str, Any], *units: str
) -> tuple[FactoryState, list[str], Any, Any]:
    units = units or ("app.py",)
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    scripts = {_SPLITTER_KEY: json.dumps(reply), **_SCRIPTS, PLANNER_SOP_KEY: _plan(*units)}
    tx = MockTransport.scripted(scripts, default_verdict="approve")
    cas = ArtifactStore()
    adapter = _Failing(f"T-{len(units):03d}")
    rt = AgentRuntime(
        proxy=ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID)), adapter=adapter, cas=cas
    )

    def _resolve(handle: Artifact) -> FrozenModel:
        if handle.kind == "feature_diff":
            return _synthetic_resolve(handle)
        body: FrozenModel = cas.resolve(handle)
        return body

    records = GlobalRecords()
    app = build_supervisor_graph(
        rt,
        runner=_runner(records),
        resolve=_resolve,
        resolve_blob=_resolve_blob_factory(_BDD_PASSING_DIFF),
        source_dir=str(src),
    ).compile(checkpointer=MemorySaver())
    config = _config(f"run-{tmp_path.name}")
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        with counting() as counts:
            nodes = [
                n
                for step in app.stream(
                    {"request": "build a todo app"}, config, stream_mode="updates"
                )
                for n in step
            ]
    finally:
        reset_gate_write_verifier(token)
    final = cast("FactoryState", app.get_state(config).values)
    return final, nodes, adapter, (counts.snapshot(), tx, _resolve)


@pytest.fixture(autouse=True)
def _build_only_wave_checks(monkeypatch: Any) -> None:
    # The synthetic diff's scenarios bind only under the final gates: the wave checks
    # between tickets judge the build alone.
    monkeypatch.setattr(WaveBuildCheck, "_scenarios", lambda self, state: frozenset())


def _names(state: FactoryState, tid: str) -> list[str]:
    return [t.name for t in state.get("gate_triage") or () if t.run_against == f"task/{tid}"]


def test_a_ticket_that_failed_its_retry_is_split_and_its_children_build(tmp_path: Path) -> None:
    final, nodes, adapter, (counts, tx, resolve) = _run(tmp_path, _GOOD)

    first = nodes.index("implementation")
    assert nodes[first : first + 4] == ["implementation", "planner", "spec_gate", "implementation"]
    assert sum(_SPLITTER_KEY in json.dumps(r) for r in tx.requests) == 1
    # Both attempts of the parent, then each child once.
    assert adapter.calls == ["T-001", "T-001", "T-001-1", "T-001-2"]
    assert SPLIT_REQUEST_TRIAGE in _names(final, "T-001")
    assert SPLIT_TRIAGE in _names(final, "T-001")
    live = {t.id: t for t in read_tickets(final, resolve)}
    assert "T-001" not in live
    # The children own exactly the parent's scenarios, the second after the first.
    assert live["T-001-1"].scenario_ids == ("AC-001",)
    assert live["T-001-2"].scenario_ids == ("AC-002",)
    assert "T-001-1" in live["T-001-2"].depends_on
    assert live["T-001-2"].entrypoint and not live["T-001-1"].entrypoint
    built = {a.task_id for a in final["artifacts"] if a.kind == "feature_diff"}
    assert {"T-001-1", "T-001-2"} <= built
    assert counts.get("planner.split.requested") == 1
    assert counts.get("planner.split.accepted") == 1
    assert "planner.split.rejected" not in counts
    assert pending_splits(final) == ()


def test_an_invalid_split_is_declined_and_the_restart_stays_the_fallback(tmp_path: Path) -> None:
    # T-001 (store.py) builds; T-002, the coarse one after it, fails.
    final, nodes, adapter, (counts, tx, resolve) = _run(tmp_path, _BAD, "store.py", "app.py")

    # Asked once: the decline answers the ticket for the rest of the run.
    assert sum(_SPLITTER_KEY in json.dumps(r) for r in tx.requests) == 1
    assert nodes.count("planner") == 2
    names = _names(final, "T-002")
    assert names.count(SPLIT_REQUEST_TRIAGE) == 1
    assert SPLIT_DECLINED_TRIAGE in names and SPLIT_TRIAGE not in names
    # The plan stands, and the skipped ticket stays the fix loop's to restart.
    assert {t.id for t in read_tickets(final, resolve)} == {"T-001", "T-002"}
    assert adapter.calls == ["T-001", "T-002", "T-002"]
    cluster = FailureCluster(key="ticket:T-002", stage=1, items=("test:x",), tickets=("T-002",))
    assert unbuilt_restart(final, resolve, cluster) == ("T-002",)
    assert counts.get("planner.split.rejected") == 1
    assert "planner.split.accepted" not in counts


def _ticket(tid: str, **kw: Any) -> Ticket:
    return Ticket(id=tid, title=tid, specialty=Specialty.BACKEND, seq=_SEQ, **kw)


def _state(*tickets: Ticket, notes: tuple[GateTriage, ...] = ()) -> tuple[FactoryState, Any]:
    handles = [
        Artifact(kind="ticket", uri=f"cas://{t.id}", produced_by="planner", seq=_SEQ)
        for t in tickets
    ]
    by_uri = {f"cas://{t.id}": t for t in tickets}
    return {"artifacts": handles, "gate_triage": list(notes)}, lambda h: by_uri[h.uri]


def _note(name: str, tid: str, by: str = "engineering") -> GateTriage:
    return GateTriage(
        name=name, run_against=f"task/{tid}", explanation="", produced_by=by, seq=_SEQ
    )


def test_only_a_ticket_with_two_scenarios_or_work_files_is_splittable() -> None:
    assert not splittable(_ticket("A", files=("app.py",), scenario_ids=("AC-1",)))
    # A test module and a package marker are not work files.
    assert not splittable(_ticket("A", files=("app.py", "tests/test_app.py", "pkg/__init__.py")))
    assert splittable(_ticket("A", files=("app.py",), scenario_ids=("AC-1", "AC-2")))
    assert splittable(_ticket("A", files=("app.py", "store.py")))
    assert not splittable(_ticket("contracts", files=("a.py", "b.py")))


def test_a_failure_a_dependency_owns_asks_for_no_split() -> None:
    dep = _ticket("A", files=("a.py",))
    mine = _ticket("B", files=("b.py", "c.py"), depends_on=("A",))
    state, resolve = _state(dep, mine)
    assert may_split(state, resolve, "B", set())
    assert may_split(state, resolve, "B", {"B"})
    assert not may_split(state, resolve, "B", {"A"})
    top = _ticket("C", files=("d.py", "e.py"), depends_on=("B",))
    state, resolve = _state(dep, mine, top)
    assert not may_split(state, resolve, "C", {"A"})  # transitively


def test_a_split_is_asked_for_once_per_ticket() -> None:
    mine = _ticket("B", files=("b.py", "c.py"))
    state, resolve = _state(mine, notes=(_note(SPLIT_REQUEST_TRIAGE, "B"),))
    assert pending_splits(state) == ("B",)
    assert not may_split(state, resolve, "B", set())
    answered: FactoryState = {
        **state,
        "gate_triage": [*state["gate_triage"], _note(SPLIT_DECLINED_TRIAGE, "B", PLANNER_NODE_ID)],
    }
    assert pending_splits(answered) == ()
    assert not may_split(answered, resolve, "B", set())


def _pass(n: int, fix: str | None, outcome: str, restart: tuple[str, ...] = ()) -> FixPass:
    return FixPass(
        restart_tickets=restart,
        window="w",
        boundary="build_test",
        pass_no=n,
        tree=f"t{n}",
        fix_task=fix,
        outcome=outcome,  # type: ignore[arg-type]
        cluster="ticket:B" if fix else None,
        seq=Seq("run", n, 0, "fix_pass", 0),
    )


def test_a_fix_loop_check_asks_to_split_a_ticket_only_after_its_restart() -> None:
    dep = _ticket("A", files=("a.py",))
    mine = _ticket("B", files=("b.py", "c.py"), depends_on=("A",))
    cluster = FailureCluster(key="ticket:B", stage=1, items=("test:x",), tickets=("B",))
    blamed = FailureCluster(key="ticket:A", stage=1, items=("test:y",), tickets=("A",))
    rt = AgentRuntime(proxy=None)  # type: ignore[arg-type]
    # fix-1's check planned the restart; a repair before it does not count.
    restarted = [
        _pass(0, None, "initial"),
        _pass(1, "fix-1", "no-change", restart=("B",)),
        _pass(2, "restart-B", "no-change"),
    ]
    repaired = [*restarted, _pass(3, "fix-3", "no-change")]

    def ask(
        state: FactoryState, resolve: Any, passes: list[FixPass], *more: FailureCluster
    ) -> tuple[str, ...]:
        return split_after_restart(state, resolve, rt, (cluster, *more), "ticket:B", passes)

    state, resolve = _state(dep, mine)
    assert ask(state, resolve, repaired) == ()  # no restart spent
    state, resolve = _state(dep, mine, notes=(_note(RESTARTED_TRIAGE, "B", "fix_pass"),))
    # The restart failed: one repair after it gets its turn first.
    assert ask(state, resolve, restarted) == ()
    assert ask(state, resolve, repaired) == ("B",)
    improved = [*restarted, _pass(3, "fix-3", "improved")]
    assert ask(state, resolve, improved) == ()
    # Its dependency fails too: that failure is the one to answer.
    assert ask(state, resolve, repaired, blamed) == ()


def _item(tid: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": tid,
        "description": "",
        "specialty": "backend",
        "difficulty": "standard",
        "depends_on": [],
        "files": [],
        "reads": [],
        "implements": [],
        "consumes": [],
        "entrypoint": False,
        "scenarios": [],
        **kw,
    }


def test_children_own_exactly_the_parents_work_and_take_over_its_dependents() -> None:
    sc = [{"id": f"AC-{i}", "text": "x"} for i in (1, 2, 3)]
    items = [
        _item("A", files=["a.py"]),
        _item(
            "B",
            depends_on=["A"],
            files=["b.py", "c.py"],
            implements=["make", "use"],
            consumes=["base"],
            scenarios=sc,
        ),
        _item("C", depends_on=["B"], files=["d.py"]),
    ]

    def reply(*children: dict[str, Any]) -> dict[str, Any]:
        return {"children": list(children), "rationale": "r"}

    def child(scenarios: list[str], files: list[str], implements: list[str], after: list[int]):
        return {
            "title": "t",
            "description": "",
            "scenario_ids": scenarios,
            "files": files,
            "implements": implements,
            "consumes": ["make"],
            "after": after,
        }

    # Independent children: the dependent waits on both.
    merged, ids, why = split_items(
        items,
        "B",
        reply(
            child(["AC-1"], ["b.py"], ["make"], []), child(["AC-2", "AC-3"], ["c.py"], ["use"], [])
        ),
        {"B-1"},
    )
    assert why == "" and merged is not None
    assert ids == ["B-2", "B-3"]  # B-1 is taken
    by = {i["id"]: i for i in merged}
    assert "B" not in by and [i["id"] for i in merged] == ["A", "B-2", "B-3", "C"]
    assert [s["id"] for s in by["B-2"]["scenarios"]] == ["AC-1"]
    assert by["B-2"]["depends_on"] == ["A"] and by["B-2"]["consumes"] == ["base"]
    assert by["B-3"]["consumes"] == ["base", "make"]
    assert by["C"]["depends_on"] == ["B-2", "B-3"]
    # A chain: the dependent waits on the last child.
    merged, ids, _ = split_items(
        items,
        "B",
        reply(
            child(["AC-1"], ["b.py"], ["make"], []), child(["AC-2", "AC-3"], ["c.py"], ["use"], [1])
        ),
        set(),
    )
    assert merged is not None
    assert {i["id"]: i for i in merged}["C"]["depends_on"] == ["B-2"]
    # Not a split of B: a scenario left out, one on two children, a foreign file,
    # a contract lost, a single child, a forward 'after'.
    bad = [
        reply(child(["AC-1"], ["b.py"], ["make"], []), child(["AC-2"], ["c.py"], ["use"], [])),
        reply(
            child(["AC-1", "AC-2"], ["b.py"], ["make"], []),
            child(["AC-2", "AC-3"], ["c.py"], ["use"], []),
        ),
        reply(
            child(["AC-1"], ["b.py", "x.py"], ["make"], []),
            child(["AC-2", "AC-3"], ["c.py"], ["use"], []),
        ),
        reply(child(["AC-1"], ["b.py"], [], []), child(["AC-2", "AC-3"], ["c.py"], ["use"], [])),
        reply(child(["AC-1", "AC-2", "AC-3"], ["b.py", "c.py"], ["make", "use"], [])),
        reply(
            child(["AC-1"], ["b.py"], ["make"], [2]), child(["AC-2", "AC-3"], ["c.py"], ["use"], [])
        ),
        None,
    ]
    for r in bad:
        assert split_items(items, "B", r, set())[0] is None
