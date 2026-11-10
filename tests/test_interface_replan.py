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
from foundry.agents.implementation.tickets.contracts import SignatureChangeError
from foundry.agents.implementation.tickets.skeleton import revised_stubs
from foundry.agents.planner.interface import INTERFACE_SOP
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.supervisor import build_supervisor_graph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, Ticket
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


def _run(tmp_path: Path, adapter: _Guarded) -> tuple[FactoryState, list[str], MockTransport]:
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
    return app.get_state(config).values, nodes, tx


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
    final, nodes, tx = _run(tmp_path, adapter)

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
    final, nodes, tx = _run(tmp_path, adapter)

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
