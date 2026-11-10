"""A ticket the stub-signature guard leaves to a re-plan gets its interface revised.

The supervisor routes implementation -> planner when the engineering node writes a
``ticket-replan`` note; the planner revises only that ticket's contracts, the Spec
gate re-checks the plan, and the next wave builds the ticket again, once per run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.agents.implementation.stubs import python_skeleton
from foundry.agents.implementation.tickets import pending_interface_replans
from foundry.agents.implementation.tickets.constants import PLANNER_NODE_ID
from foundry.agents.implementation.tickets.contracts import SignatureChangeError
from foundry.agents.implementation.tickets.skeleton import restub_revised, revised_stubs
from foundry.agents.planner.interface import INTERFACE_SOP
from foundry.agents.planner.schema import NODE_ID as PLANNER_ID
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.supervisor import build_supervisor_graph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact, BudgetLedger, FrozenModel
from foundry.schemas.gates import GateStatus, GateTriage
from foundry.schemas.implementation import Specialty, StackDecision, Ticket
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
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

_REVISER_KEY = INTERFACE_SOP.split(".")[0]
_PLANNED = "POST /todos {title} -> Todo"
_REVISED = "POST /todos {title, due} -> Todo"


def _plan() -> str:
    payload = plan_payload(
        intent=INTENT_PAYLOAD,
        prd=PRD_PAYLOAD,
        critique=CRITIQUE_PAYLOAD,
        sprint=SPRINT_PAYLOAD,
        design=DESIGN_PAYLOAD,
        # One ticket carries both criteria: one wave, so only the final gates judge it.
        decomposition={"tasks": [{"unit": "app.py", "criterion_ids": ["AC-001", "AC-002"]}]},
    )
    payload["interfaces"] = [
        {"name": "add_todo", "kind": "http", "signature": _PLANNED, "description": "add one"}
    ]
    payload["tickets"][0]["implements"] = ["add_todo"]
    return json.dumps(payload)


class _Guarded(StubAdapter):
    """T-001's sessions change the planned signature until the packet carries the
    revised one (``always``: they never stop)."""

    def __init__(self, *, always: bool = False) -> None:
        super().__init__()
        self.always = always
        self.calls: list[str] = []

    def run(self, packet: Any, sandbox: Any) -> Any:
        tid = str(packet.task["id"])
        self.calls.append(tid)
        if tid == "T-001" and (self.always or _REVISED not in packet.model_dump_json()):
            raise SignatureChangeError("app.py: add_todo's parameters changed")
        return super().run(packet, sandbox)


def _run(tmp_path: Path, adapter: _Guarded) -> tuple[FactoryState, list[str], MockTransport, Any]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    revision = {
        "contracts": [{"name": "add_todo", "signature": _REVISED, "description": "add one"}],
        "rationale": "every session needed a due date",
    }
    scripts = {_REVISER_KEY: json.dumps(revision), **_SCRIPTS, PLANNER_SOP_KEY: _plan()}
    tx = MockTransport.scripted(scripts, default_verdict="approve")
    cas = ArtifactStore()
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
        nodes = [
            n
            for step in app.stream({"request": "build a todo app"}, config, stream_mode="updates")
            for n in step
        ]
    finally:
        reset_gate_write_verifier(token)
    spec_gate = app.builder.nodes["spec_gate"].runnable

    def _spec(state: FactoryState) -> dict[str, Any]:
        token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
        try:
            out: dict[str, Any] = spec_gate.invoke(state, config)
        finally:
            reset_gate_write_verifier(token)
        return out

    return app.get_state(config).values, nodes, tx, _spec


def _notes(state: FactoryState, tid: str) -> dict[str, str]:
    return {
        t.name: t.explanation
        for t in state.get("gate_triage") or ()
        if t.run_against == f"task/{tid}"
    }


def _revisions(tx: MockTransport) -> int:
    return sum(_REVISER_KEY in json.dumps(r) for r in tx.requests)


def test_a_ticket_left_to_a_replan_is_rebuilt_on_its_revised_interface(tmp_path: Path) -> None:
    adapter = _Guarded()
    final, nodes, tx, _ = _run(tmp_path, adapter)

    # Built once on the plan (both attempts rejected), then re-planned mid-build.
    first = nodes.index("implementation")
    assert nodes[first : first + 4] == ["implementation", "planner", "spec_gate", "implementation"]
    assert _revisions(tx) == 1
    # The third session got the revised contract in its packet, so it built.
    assert adapter.calls == ["T-001", "T-001", "T-001"]

    notes = _notes(final, "T-001")
    assert "ticket-replan" in notes
    assert "revised add_todo" in notes["interface-revised"]
    spec = [g for g in final["gates"] if g.name == "spec"]
    assert {g.status for g in spec} == {GateStatus.PASS}
    assert {a.task_id for a in final["artifacts"] if a.kind == "feature_diff"} == {"T-001"}
    newest = {g.name: g for g in sorted(final["gates"], key=lambda g: g.seq)}
    assert newest["test"].status is GateStatus.PASS


def test_an_interface_is_revised_at_most_once_per_ticket(tmp_path: Path) -> None:
    adapter = _Guarded(always=True)
    final, nodes, tx, _ = _run(tmp_path, adapter)

    assert _revisions(tx) == 1
    assert nodes.count("planner") == 2  # the plan, then the one revision
    # Rebuilt on the revised interface, rejected twice again, then left to the fix loop.
    notes = _notes(final, "T-001")
    assert "interface-revised" in notes and "ticket-replan" in notes
    assert "T-001" not in {a.task_id for a in final["artifacts"] if a.kind == "feature_diff"}
    assert adapter.calls.count("T-001") >= 4


def test_a_revised_ticket_is_stubbed_from_its_revised_contract() -> None:
    seq = Seq("run", 1, 0, "planner", 0)
    ticket = Ticket(
        id="T-001",
        title="todo",
        specialty=Specialty.BACKEND,
        files=("todo.py",),
        implements=("add_todo",),
        seq=seq,
    )
    planned = InterfaceContract(
        name="add_todo",
        spec_uri="cas://c",
        kind="function",
        signature="def add_todo(title: str) -> Todo: ...",
        seq=seq,
    )
    revised = planned.model_copy(
        update={"signature": "def add_todo(title: str, due: str) -> Todo: ...", "version": 2}
    )
    texts = {
        "contracts.py": "from dataclasses import dataclass\n\n"
        "@dataclass\nclass Todo:\n    title: str\n"
    }
    before = python_skeleton([planned], [ticket], texts).files["todo.py"]
    after = revised_stubs([ticket], {"add_todo": revised}, texts)
    assert "due: str" not in before
    assert "def add_todo(title: str, due: str) -> Todo" in after["todo.py"]


def test_a_restart_of_a_revised_ticket_is_held_to_its_revised_stubs() -> None:
    seq = Seq("run", 1, 0, "planner", 0)
    ticket = Ticket(
        id="T-001",
        title="todo",
        specialty=Specialty.BACKEND,
        files=("todo.py",),
        implements=("add_todo",),
        seq=seq,
    )
    planned = InterfaceContract(
        name="add_todo",
        spec_uri="cas://c",
        kind="function",
        signature="def add_todo(title: str) -> Todo: ...",
        seq=seq,
    )
    revised = planned.model_copy(
        update={"signature": "def add_todo(title: str, due: str) -> Todo: ...", "version": 2}
    )
    texts = {
        "contracts.py": "from dataclasses import dataclass\n\n"
        "@dataclass\nclass Todo:\n    title: str\n"
    }
    planned_stub = python_skeleton([planned], [ticket], texts).files["todo.py"]
    # The tree before the ticket holds the plan-time stub: the reset would restore it.
    restart: tuple[tuple[str, ...], tuple[dict[str, str], tuple[str, ...]]] = (
        ("T-001",),
        ({"todo.py": planned_stub}, ()),
    )
    escalation: tuple[tuple[str, ...], tuple[dict[str, str], tuple[str, ...]]] = ((), ({}, ()))
    for restarts, reset in (restart, escalation):
        skeleton = {"todo.py": planned_stub}
        restubbed, out = restub_revised(
            [ticket], {"T-001": 3}, restarts, skeleton, {"add_todo": revised}, texts, reset
        )
        assert "due: str" in skeleton["todo.py"]  # the guard's reference
        assert restubbed["T-001"] == {"todo.py": skeleton["todo.py"]}
        assert out == (({"todo.py": skeleton["todo.py"]}, ()) if restarts else reset)
    # A ticket whose interface was never revised keeps the skeleton's stubs.
    skeleton = {"todo.py": planned_stub}
    unrevised = restub_revised([ticket], {}, ("T-001",), skeleton, {}, texts, ({}, ()))
    assert unrevised == ({}, ({}, ()))
    assert skeleton == {"todo.py": planned_stub}


def test_only_an_unbuilt_ticket_waits_on_the_planner() -> None:
    assert PLANNER_NODE_ID == PLANNER_ID  # the notes filter on it

    def note(name: str, by: str, epoch: int) -> GateTriage:
        seq = Seq("run", epoch, 0, by, 0)
        return GateTriage(
            name=name, run_against="task/T-001", explanation="", produced_by=by, seq=seq
        )

    left: FactoryState = {"gate_triage": [note("ticket-replan", "engineering", 1)]}
    assert pending_interface_replans(left) == ("T-001",)
    answered: FactoryState = {
        "gate_triage": [*left["gate_triage"], note("interface-declined", PLANNER_ID, 2)]
    }
    assert pending_interface_replans(answered) == ()
    diff = Artifact(
        kind="feature_diff",
        uri="cas://d",
        produced_by="engineering",
        task_id="T-001",
        seq=Seq("run", 1, 0, "engineering", 0),
    )
    # A built ticket keeps its build: revising its interface would rebuild nothing.
    assert pending_interface_replans({**left, "artifacts": [diff]}) == ()


def test_a_spec_pass_mid_build_keeps_the_fix_budget_the_build_spent(tmp_path: Path) -> None:
    final, _, _, spec = _run(tmp_path, _Guarded())
    spent: FactoryState = {**final, "budget": BudgetLedger(fix_attempts_since_all_green=2)}
    assert "budget" not in spec(spent)
    # Before anything is built a Spec PASS still hands the build the whole budget.
    planning: FactoryState = {
        **spent,
        "artifacts": [a for a in final["artifacts"] if a.kind != "feature_diff"],
    }
    assert spec(planning)["budget"].fix_attempts_since_all_green == 0


def test_a_spec_fail_mid_build_halts_instead_of_re_planning(tmp_path: Path) -> None:
    from tests.test_supervisor import _respec_app

    records = GlobalRecords()
    app, _ = _respec_app(tmp_path, records)
    built = Artifact(
        kind="feature_diff",
        uri="cas://feature_diff/T-001",
        produced_by="engineering",
        task_id="T-001",
        seq=Seq("run-midbuild", 1, 0, "engineering", 0),
    )
    config = _config("run-midbuild")
    seed: FactoryState = {"request": "build a todo app", "artifacts": [built]}
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        nodes = [n for step in app.stream(seed, config, stream_mode="updates") for n in step]
    finally:
        reset_gate_write_verifier(token)
    assert nodes == ["triage", "planner", "spec_gate"]
    assert not app.get_state(config).next


def test_a_method_of_a_class_the_contracts_file_declares_is_not_revised() -> None:
    from foundry.agents.planner.interface import _revised

    seq = Seq("run", 1, 0, "planner", 0)
    contract = InterfaceContract(
        name="TodoStore.add",
        spec_uri="cas://c",
        kind="function",
        signature="def add(self, title: str) -> int: ...",
        seq=seq,
    )
    reply = {"contracts": [{"name": "TodoStore.add", "signature": "def add(self) -> int: ..."}]}
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport.scripted({})), adapter=None)
    out, problems = _revised(
        rt, _config("run"), [contract], reply, {contract.name: contract}, None, {"TodoStore"}
    )
    assert out == []
    assert "class TodoStore is declared in the built contracts file" in problems[0]


def test_a_type_the_contracts_file_declares_is_not_revised() -> None:
    from foundry.agents.planner.interface import _revised

    seq = Seq("run", 1, 0, "planner", 0)
    contract = InterfaceContract(
        name="Todo",
        spec_uri="cas://c",
        kind="type",
        signature="class Todo:\n    title: str",
        seq=seq,
    )
    reply = {"contracts": [{"name": "Todo", "signature": "class Todo:\n    due: str"}]}
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport.scripted({})), adapter=None)
    out, problems = _revised(
        rt, _config("run"), [contract], reply, {contract.name: contract}, None, {"Todo"}
    )
    assert out == []
    assert problems == [
        "Todo is declared in the built contracts file, which a revision cannot change"
    ]


def test_a_revision_compiles_strict_only_for_the_exact_strict_flag(monkeypatch: Any) -> None:
    from foundry.agents.plan_contracts import CompileResult
    from foundry.agents.planner import interface

    seen: list[bool] = []

    def fake(_code: Any, _language: str, *, strict: bool = False) -> CompileResult:
        seen.append(strict)
        return CompileResult()

    monkeypatch.setattr(interface, "check_contracts", fake)
    seq = Seq("run", 1, 0, "planner", 0)
    add = InterfaceContract(
        name="add_todo",
        spec_uri="cas://a",
        kind="function",
        signature="def add_todo(title: str) -> int: ...",
        seq=seq,
    )
    for typecheck, strict in (("mypy --strict-optional .", False), ("mypy --strict .", True)):
        seen.clear()
        stack = StackDecision(language="python", typecheck=typecheck)
        assert interface._new_compile_findings({add.name: add}, [add], stack) == []
        assert seen == [strict, strict]


def test_a_revision_that_adds_a_compile_error_is_refused(monkeypatch: Any) -> None:
    from foundry.agents.planner.interface import _new_compile_findings

    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    seq = Seq("run", 1, 0, "planner", 0)
    todo = InterfaceContract(
        name="Todo",
        spec_uri="cas://t",
        kind="class",
        signature="class Todo:\n    title: str",
        seq=seq,
    )
    add = InterfaceContract(
        name="add_todo",
        spec_uri="cas://a",
        kind="function",
        signature="def add_todo(title: str) -> Todo: ...",
        seq=seq,
    )
    planned = {"Todo": todo, "add_todo": add}
    good = add.model_copy(update={"signature": "def add_todo(title: str, due: str) -> Todo: ..."})
    bad = add.model_copy(update={"signature": "def add_todo(title: str = 1) -> Todo: ..."})
    assert _new_compile_findings(planned, [good], None) == []
    assert any("add_todo" in f for f in _new_compile_findings(planned, [bad], None))
