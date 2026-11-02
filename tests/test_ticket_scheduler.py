"""Ticket-driven scheduler (plan §2 / §2b): tickets -> tasks -> waves.

Tickets are built by hand (``emit_artifact(ticket, "ticket", ...)``) into a real
:class:`ArtifactStore`, so the subgraph resolves them exactly as it will the
planner's. A recording adapter writes each ticket's ``files`` (plus paths outside
its scope) as a full-file snapshot, so wave order, overlay visibility, write-scope
trimming and resume are all observable.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry import cli
from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact, persist_prose
from foundry.agents.implementation.tickets import (
    cap_stopped,
    next_wave,
    pending_wave,
    task_from_ticket,
    tier_for,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.execution.sandbox import Sandbox
from foundry.execution.work_pool import ExecutionTask, LocalWorkPool
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.gate_wiring import (
    WaveBuildCheck,
    materialize_workdir,
    planned_paths,
    wave_typecheck,
)
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.graph.supervisor import build_supervisor_graph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.proxy.token_meter import TokenCapExceeded, TokenMeter
from foundry.schemas.architecture import ConstraintScope, InterfaceContract, PinnedConstraint
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import (
    Difficulty,
    FeatureDiff,
    Specialty,
    StackDecision,
    TaskEvent,
    Ticket,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq

CONSTRAINT = "MUST store money as integer cents, never floats."
CONTRACT_BODY = "def add(title: str) -> int:\n    '''Store a todo; return its id.'''"


def _seq(local: int, node: str = "planner") -> Seq:
    return Seq("run", 1, 1, node, local)


def _ticket(
    tid: str,
    local: int,
    *,
    specialty: Specialty = Specialty.BACKEND,
    files: tuple[str, ...] = (),
    deps: tuple[str, ...] = (),
    difficulty: Difficulty = Difficulty.STANDARD,
    **extra: Any,
) -> Ticket:
    return Ticket(
        id=tid, title=f"title {tid}", description=f"desc {tid}", specialty=specialty,
        difficulty=difficulty, depends_on=deps, files=files, seq=_seq(local), **extra,
    )


class _Recorder:
    """Writes each ticket's files (+ out-of-scope paths) as a full-file snapshot and
    records what it saw. Optionally raises the token cap once, on a chosen ticket."""

    supports_turn_snapshots = True
    name = "recorder"

    def __init__(
        self,
        cap_on: str | None = None,
        delay: float = 0.0,
        *,
        fail_on: dict[str, int] | None = None,
        writes: dict[str, dict[str, str]] | None = None,
        meter: TokenMeter | None = None,
        silent: frozenset[str] = frozenset(),
    ) -> None:
        self.silent = silent  # tids whose session writes none of the ticket's files
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.packets: dict[str, TaskPacket] = {}
        self.seen: dict[str, dict[str, str]] = {}
        self.cap_on = cap_on
        self.delay = delay
        self.fail_on = dict(fail_on or {})  # tid -> times to raise an ordinary error
        self.writes = writes or {}  # tid -> extra/overriding file contents
        self.meter = meter

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        if tid == self.cap_on:
            self.cap_on = None
            if self.meter is not None:
                self.meter.used = self.meter.cap  # this session spent the rest
            raise TokenCapExceeded("token cap spent")
        with self.lock:
            if self.fail_on.get(tid, 0) > 0:
                self.fail_on[tid] -= 1
                self.calls.append(f"{tid}!")
                raise RuntimeError(f"adapter crashed on {tid}")
        time.sleep(self.delay)
        root = Path(sandbox.worktree)
        seen = {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in root.rglob("*") if p.is_file()
        }
        with self.lock:
            self.calls.append(tid)
            self.packets[tid] = packet
            self.seen[tid] = seen
        files = {f: f"# written by {tid}\n" for f in packet.task.get("files", ())
                 if tid not in self.silent}
        files.update(self.writes.get(tid, {}))
        files["rogue.txt"] = "out of scope\n"
        files["features/T-001.feature"] = "Feature: edited\n"
        blob = json.dumps({"kind": "full-file-snapshot", "files": files}, sort_keys=True)
        return AdapterResult(
            diff_uri=sandbox.put_blob(blob.encode("utf-8")),
            snapshot_digest=sandbox.snapshot(),
        )


def _runtime(adapter: Any, store: ArtifactStore) -> AgentRuntime:
    proxy = ModelProviderProxy(MockTransport(default_text="{}"),
                               signer=ZoneBSigner(b"k" * 16, "kid"))
    return AgentRuntime(proxy=proxy, adapter=adapter, cas=store)


def _state(store: ArtifactStore, tickets: list[Ticket], **extra: Any) -> FactoryState:
    arts = [emit_artifact(t, "ticket", "planner", t.seq, store=store) for t in tickets]
    return {
        "request": "build a todo service",
        "artifacts": arts + list(extra.pop("artifacts", [])),
        "pinned_constraints": [PinnedConstraint(
            id="c1", text=CONSTRAINT, scope=ConstraintScope.GLOBAL, source="ADR-1",
            seq=_seq(0, "architect"),
        )],
        **extra,
    }


def _graph(rt: AgentRuntime, store: ArtifactStore, tmp_path: Path, **kw: Any) -> Any:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "README.md").write_text("base\n")
    return build_implementation_subgraph(
        rt, source_dir=str(src), resolve=store.resolve, resolve_blob=store.resolve_blob,
        max_slots=4, **kw,
    )


def _config(run_id: str = "run") -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": 1},
            "recursion_limit": 50}


def _diffs(out: FactoryState) -> dict[str, Any]:
    return {a.task_id: a for a in out.get("artifacts", []) if a.kind == "feature_diff"}


# ---- tickets -> tasks ---------------------------------------------------------


def test_ticket_maps_to_a_deterministic_task() -> None:
    t = _ticket("T-002", 1, specialty=Specialty.DATA, deps=("T-001", "T-404"),
                difficulty=Difficulty.COMPLEX, scenario_ids=("AC-1", "AC-2"))
    task = task_from_ticket(t, _seq(5, "senior_engineer"), known=("T-001", "T-002"))
    assert (task.id, task.ticket_id, task.branch) == ("T-002", "T-002", "task/T-002")
    assert task.specialty is Specialty.DATA
    assert task.tier == "T1"  # complex -> T1 by default
    assert task.depends_on == ("T-001",)  # the unknown dependency is dropped
    assert task.criterion_ids == ("AC-1", "AC-2")
    again = task_from_ticket(t, _seq(9, "senior_engineer"), known=("T-001",))
    assert again.spec_uri == task.spec_uri  # identity independent of when it is minted
    custom = task_from_ticket(t, _seq(5), ticket_tiers={"complex": "T2"})
    assert custom.tier == "T2"
    assert tier_for("trivial", None) == "T3" and tier_for(Difficulty.STANDARD, {}) == "T2"


def test_waves_respect_dependencies_and_never_share_a_file() -> None:
    a = _ticket("A", 0, files=("app.py",))
    b = _ticket("B", 1, files=("b.py",), deps=("A",))
    c = _ticket("C", 2, files=("app.py", "c.py"))  # collides with A, later by seq
    d = _ticket("D", 3, files=("d.py",), deps=("ghost",))  # unknown dep is ignored
    x = _ticket("X", 4, deps=("Y",))
    y = _ticket("Y", 5, deps=("X",))  # a cycle never becomes ready
    tickets = [a, b, c, d, x, y]
    assert [t.id for t in next_wave(tickets, set())] == ["A", "D"]
    assert [t.id for t in next_wave(tickets, {"A", "D"})] == ["B", "C"]
    assert [t.id for t in next_wave(tickets, {"A", "B", "C", "D"})] == []


def test_unrestricted_and_directory_scopes_collide() -> None:
    src = _ticket("SRC", 0, files=("src/",))
    app = _ticket("APP", 1, files=("src/app.py",))  # under SRC's directory
    other = _ticket("OTHER", 2, files=("docs/readme.md",))
    anything = _ticket("ANY", 3)  # empty files: may write anywhere
    tickets = [src, app, other, anything]
    assert [t.id for t in next_wave(tickets, set())] == ["SRC", "OTHER"]
    assert [t.id for t in next_wave(tickets, {"SRC", "OTHER"})] == ["APP"]
    assert [t.id for t in next_wave(tickets, {"SRC", "OTHER", "APP"})] == ["ANY"]
    first = _ticket("ANY0", 0)
    assert [t.id for t in next_wave([first, other], set())] == ["ANY0"]  # runs alone


def test_a_replanned_ticket_uses_its_newest_version(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    v1 = _ticket("T-001", 0, files=("a.py",), difficulty=Difficulty.TRIVIAL)
    v2 = _ticket("T-001", 5, files=("a.py",), difficulty=Difficulty.COMPLEX, version=2)
    gone = _ticket("T-002", 1, files=("b.py",))
    tomb = gone.model_copy(update={"supersedes": "T-002", "version": 2, "seq": _seq(6)})
    first_task = task_from_ticket(v1, _seq(0, "senior_engineer"))
    state = _state(store, [v1, gone, v2, tomb], tasks=[first_task.model_dump(mode="json")])
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(state, _config())
    assert rec.calls == ["T-001"]  # the retired ticket is never built
    assert rec.packets["T-001"].task["tier"] == "T1"  # complex, not v1's trivial T3
    assert set(_diffs(out)) == {"T-001"}


def test_an_unmaterializable_tree_is_noted_not_silently_dropped(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    bad = AcceptanceCriterion(id="AC-9", text="x", ticket_id="../escape", seq=_seq(9))
    state = _state(store, [_ticket("T-001", 0, files=("a.py",))], artifacts=[
        emit_artifact(bad, "acceptance_criterion", "planner", bad.seq, store=store),
    ])
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(state, _config())
    [note] = [t for t in out["gate_triage"] if t.name == "wave-overlay"]
    assert "bare base" in note.explanation and "escape" in note.explanation
    assert any("bare base" in c for j in out["agent_journal"] for c in j.concerns)


# ---- the engineering loop -----------------------------------------------------


def _three_tickets(store: ArtifactStore) -> tuple[list[Ticket], list[Any]]:
    spec_uri = persist_prose(CONTRACT_BODY, store=store)
    contract = InterfaceContract(name="TodoStore.add", spec_uri=spec_uri, seq=_seq(20))
    crit = AcceptanceCriterion(
        id="AC-1", text="Given an empty store When I add 'milk' Then it has id 1",
        given=("an empty store",), when=("I add 'milk'",), then=("it has id 1",),
        ticket_id="T-001", seq=_seq(21),
    )
    extra = [
        emit_artifact(contract, "interface_contract", "planner", contract.seq, store=store),
        emit_artifact(crit, "acceptance_criterion", "planner", crit.seq, store=store),
    ]
    tickets = [
        _ticket("T-001", 0, files=("todo/store.py",), scenario_ids=("AC-1",),
                implements=("TodoStore.add",), difficulty=Difficulty.TRIVIAL),
        _ticket("T-002", 1, specialty=Specialty.FRONTEND, files=("todo/cli.py",),
                deps=("T-001",), consumes=("TodoStore.add",)),
        _ticket("T-003", 2, specialty=Specialty.TEST, files=("tests/steps/test_t001.py",),
                deps=("T-001",), scenario_ids=("AC-1",)),
    ]
    return tickets, extra


def test_waves_build_in_dependency_order_on_the_integrated_tree(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    events: list[TaskEvent] = []
    lock = threading.Lock()

    def on_task(e: TaskEvent) -> None:
        with lock:
            events.append(e)

    tickets, extra = _three_tickets(store)
    app = _graph(_runtime(rec, store), store, tmp_path, on_task=on_task).compile()
    out = app.invoke(_state(store, tickets, artifacts=extra), _config())

    assert rec.calls[0] == "T-001" and set(rec.calls[1:]) == {"T-002", "T-003"}
    assert set(_diffs(out)) == {"T-001", "T-002", "T-003"}
    # Wave 2 reads everything wave 1 built, but never the criteria oracle.
    assert rec.seen["T-002"]["todo/store.py"] == "# written by T-001\n"
    assert "criteria.json" not in rec.seen["T-002"]
    waves = [j for j in out["agent_journal"] if j.node_id == "engineering"]
    assert len(waves) == 2
    tasks = {t["id"]: t for t in out["tasks"]}
    assert tasks["T-003"]["depends_on"] == ["T-001"] and tasks["T-001"]["tier"] == "T3"
    # Telemetry: exactly the TaskEvent shape, start then done for every ticket.
    assert all(set(e) == {"phase", "ticket_id", "title", "specialty", "tier"} for e in events)
    assert {(e["ticket_id"], e["phase"]) for e in events} == {
        (t, p) for t in ("T-001", "T-002", "T-003") for p in ("start", "done")
    }
    assert {"phase": "start", "ticket_id": "T-001", "title": "title T-001",
            "specialty": "backend", "tier": "T3"} in events


def test_packet_carries_ticket_scenarios_contracts_and_persona(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    tickets, extra = _three_tickets(store)
    _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, tickets, artifacts=extra), _config())

    p1 = rec.packets["T-001"]
    assert p1.pinned_constraints == (CONSTRAINT,)
    assert p1.task["title"] == "title T-001" and p1.task["description"] == "desc T-001"
    assert p1.task["tier"] == "T3" and p1.task["files"] == ["todo/store.py"]
    spans = "\n".join(p1.retrieved_spans)
    assert "Given an empty store" in spans and "When I add 'milk'" in spans
    assert "features/T-001.feature::AC-1" in spans
    assert CONTRACT_BODY in spans and "IMPLEMENTS" in spans
    assert CONTRACT_BODY in "\n".join(rec.packets["T-002"].retrieved_spans)
    assert "FRONTEND" in rec.packets["T-002"].persona
    p3 = rec.packets["T-003"]
    assert "pytest-bdd" in p3.persona and p3.task["feature_files"] == ["features/T-001.feature"]


def test_writes_outside_the_ticket_files_are_dropped_and_noted(tmp_path: Path) -> None:
    store = ArtifactStore()
    tickets = [_ticket("T-001", 0, files=("todo/store.py",))]
    out = _graph(_runtime(_Recorder(), store), store, tmp_path).compile().invoke(
        _state(store, tickets), _config())
    diff = store.resolve(_diffs(out)["T-001"])
    assert isinstance(diff, FeatureDiff)
    body = json.loads(store.resolve_blob(diff.diff_uri))
    assert body["files"] == {"todo/store.py": "# written by T-001\n"}
    notes = [t for t in out["gate_triage"] if t.name == "write-scope"]
    assert len(notes) == 1
    assert "rogue.txt" in notes[0].explanation
    assert "features/T-001.feature" in notes[0].explanation


def _wave_of_three() -> list[Ticket]:
    return [_ticket(f"T-00{i}", i, files=(f"m{i}.py",)) for i in (1, 2, 3)]


def test_a_failing_ticket_keeps_its_siblings_and_is_retried_then_skipped(
    tmp_path: Path,
) -> None:
    store = ArtifactStore()
    rec = _Recorder(fail_on={"T-002": 5})  # fails every attempt
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, _wave_of_three()), _config())
    assert set(_diffs(out)) == {"T-001", "T-003"}  # the siblings' diffs landed
    assert sorted(rec.calls) == ["T-001", "T-002!", "T-002!", "T-003"]  # retried once
    notes = {t.name for t in out["gate_triage"] if t.run_against == "task/T-002"}
    assert notes == {"ticket-failed:1", "ticket-failed:2", "ticket-skipped"}


def test_a_ticket_that_fails_once_is_built_on_the_retry(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder(fail_on={"T-002": 1})
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, _wave_of_three()), _config())
    assert set(_diffs(out)) == {"T-001", "T-002", "T-003"}
    assert rec.calls.count("T-001") == 1 and rec.calls[-1] == "T-002"


def test_a_token_cap_commits_the_siblings_and_resume_skips_them(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder(cap_on="T-002")
    app = _graph(_runtime(rec, store), store, tmp_path).compile()
    out = app.invoke(_state(store, _wave_of_three()), _config())
    # The subgraph exits cleanly with the siblings' writes and the stop marker; the
    # supervisor raises the cap a superstep later, once these writes are committed.
    assert set(_diffs(out)) == {"T-001", "T-003"}
    assert [n.run_against for n in cap_stopped(out)] == ["task/T-002"]
    rec.calls.clear()
    resumed = app.invoke(out, _config())
    assert rec.calls == ["T-002"]  # the completed siblings were not rebuilt
    assert set(_diffs(resumed)) == {"T-001", "T-002", "T-003"}
    assert not cap_stopped(resumed)


@pytest.mark.parametrize("resume", ["raise cap", "remove meter"])
def test_a_cap_stopped_supervisor_run_resumes(tmp_path: Path, resume: str) -> None:
    from tests.test_supervisor import _SCRIPTS, KEY, KEY_ID, _runner
    from tests.test_supervisor import _config as _sup_config

    store = ArtifactStore()
    meter = TokenMeter(10**9)
    rec = _Recorder(cap_on="T-002", meter=meter)
    proxy = ModelProviderProxy(MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve"),
                               signer=ZoneBSigner(KEY, KEY_ID))
    proxy.token_meter = meter
    rt = AgentRuntime(proxy=proxy, adapter=rec, cas=store)
    src = tmp_path / "src"
    src.mkdir()
    records = GlobalRecords()
    app = build_supervisor_graph(
        rt, runner=_runner(records), resolve=store.resolve, resolve_blob=store.resolve_blob,
        source_dir=str(src), max_fix_attempts=1,
    ).compile(checkpointer=MemorySaver())
    cfg = _sup_config("cap-run")
    seed = _state(store, _wave_of_three())
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        with pytest.raises(TokenCapExceeded):
            app.invoke(seed, cfg)
        committed = app.get_state(cfg)
        assert committed.next == ("token_cap",)
        # The scripted planner adds its own ticket (T-SPEC) beside the seeded wave.
        built = set(_diffs(committed.values))
        assert {"T-001", "T-003"} <= built and "T-002" not in built
        rec.calls.clear()
        if resume == "raise cap":
            with pytest.raises(TokenCapExceeded):  # still spent: stays stopped
                app.invoke(None, cfg)
            meter.cap = 2 * 10**9  # the operator raises the cap and resumes
        else:
            proxy.token_meter = None  # the operator resumes with the cap removed
        final = app.invoke(None, cfg)
    finally:
        reset_gate_write_verifier(token)
    assert rec.calls.count("T-002") == 1
    assert "T-001" not in rec.calls and "T-003" not in rec.calls
    assert {"T-001", "T-002", "T-003"} <= set(_diffs(final))
    assert not cap_stopped(final)


def test_a_ticket_that_writes_none_of_its_files_is_retried_then_skipped(
    tmp_path: Path,
) -> None:
    # A session that ends without writing its scope (e.g. its turns ran out) is a
    # failed attempt, not a built ticket: an empty diff must not unblock dependents.
    store = ArtifactStore()
    rec = _Recorder(silent=frozenset({"T-001"}))
    tickets = [_ticket("T-001", 0, files=("contracts.py",)),
               _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, tickets), _config())
    assert _diffs(out) == {}
    assert rec.calls == ["T-001", "T-001"]  # T-002 never builds on the empty contract
    notes = {t.name: t.explanation for t in out["gate_triage"]
             if t.run_against == "task/T-001" and t.produced_by == "engineering"}
    assert set(notes) == {"ticket-failed:1", "ticket-failed:2", "ticket-skipped"}
    assert "contracts.py" in notes["ticket-failed:1"]


def test_a_ticket_with_a_diff_is_not_rebuilt(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    tickets = [_ticket("T-001", 0, files=("a.py",)), _ticket("T-002", 1, files=("b.py",))]
    first = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, tickets[:1]), _config())
    rec.calls.clear()
    seeded = _state(store, tickets, artifacts=list(_diffs(first).values()))
    out = _graph(_runtime(rec, store), store, tmp_path).compile().invoke(seeded, _config())
    assert rec.calls == ["T-002"]
    assert set(_diffs(out)) == {"T-001", "T-002"}


def test_over_budget_ticket_is_skipped_and_the_loop_ends(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    tickets = [
        _ticket("T-001", 0, files=("a.py",)),
        _ticket("T-002", 1, files=("b.py",)),
        _ticket("T-003", 2, files=("c.py",), deps=("T-002",)),
    ]
    app = _graph(_runtime(rec, store), store, tmp_path,
                 cost_budget_microusd=100, cost_cap_microusd=100).compile()
    out = app.invoke(_state(store, tickets), _config())
    assert rec.calls == ["T-001"]
    skipped = {t.run_against for t in out["gate_triage"] if t.name == "ticket-skipped"}
    assert skipped == {"task/T-002"}  # T-003 waits on it and is never built


def test_fix_reentry_runs_the_fixer_not_the_tickets(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder()
    tickets = [_ticket("T-001", 0, files=("a.py",)), _ticket("T-002", 1, files=("b.py",))]
    app = _graph(_runtime(rec, store), store, tmp_path).compile()
    built = app.invoke(_state(store, tickets), _config())
    ran: list[str] = []
    for step in app.stream({**built, "phase": "build_test_fix"}, _config(),
                           stream_mode="updates"):
        ran.extend(step)
    assert ran == ["fixer"]
    assert rec.calls[-1] == "fix-1" and rec.seen["fix-1"]["a.py"] == "# written by T-001\n"
    assert "criteria.json" not in rec.seen["fix-1"]  # the oracle stays out of the worktree


# ---- per-wave integration check (§2b) ------------------------------------------

_WAVE_KEY = b"dev-zone-b-wave-key-0123456789abcdef"


def _wave_run(
    tmp_path: Path, rec: _Recorder, *, max_fix_attempts: int = 3,
    tickets: list[Ticket] | None = None, store: ArtifactStore | None = None, **extra: Any,
) -> tuple[FactoryState, list[str], ArtifactStore]:
    """Two waves (T-002 depends on T-001) with the REAL runner's build gate between."""
    store = store or ArtifactStore()
    rt = _runtime(rec, store)
    records = GlobalRecords()
    runner = TwoZoneGateRunner(ZoneBSigner(_WAVE_KEY, "wave-kid"), records, "s1", "l1")
    check = WaveBuildCheck(rt, runner, store.resolve, store.resolve_blob,
                           max_fix_attempts=max_fix_attempts)
    tickets = tickets or [
        _ticket("T-001", 0, files=("app.py",)),
        _ticket("T-002", 1, files=("app.py",), deps=("T-001",)),
    ]
    ran: list[str] = []
    token = set_gate_write_verifier(DevGateWriteVerifier(_WAVE_KEY, "wave-kid", records))
    try:
        app = _graph(rt, store, tmp_path, wave_check=check).compile(checkpointer=MemorySaver())
        cfg = _config("wave-run")
        for step in app.stream(_state(store, tickets, **extra), cfg, stream_mode="updates"):
            ran.extend(step)
        out: FactoryState = app.get_state(cfg).values
    finally:
        reset_gate_write_verifier(token)
    return out, ran, store


def _builds(out: FactoryState) -> list[GateStatus]:
    return [g.status for g in sorted(out.get("gates") or (), key=lambda g: g.seq)
            if g.name == "build"]


def test_a_passing_intermediate_build_proceeds_to_the_next_wave(tmp_path: Path) -> None:
    out, ran, _ = _wave_run(tmp_path, _Recorder())
    # Checked once, between the waves; never after the last (the final gate's job).
    assert ran == ["engineering", "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.PASS]
    assert set(_diffs(out)) == {"T-001", "T-002"}


def test_a_failing_intermediate_build_runs_the_fixer_before_the_next_wave(
    tmp_path: Path,
) -> None:
    rec = _Recorder(writes={
        "T-001": {"app.py": "def broken(:\n"},
        "fix-1": {"app.py": "def fixed():\n    return 1\n"},
        "T-002": {"app.py": "def fixed():\n    return 2\n"},
    })
    out, ran, store = _wave_run(tmp_path, rec)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering"]
    assert rec.calls == ["T-001", "fix-1", "T-002"]
    assert _builds(out) == [GateStatus.FAIL, GateStatus.PASS]
    assert rec.seen["T-002"]["app.py"] == "def fixed():\n    return 1\n"  # built on the fix
    # The passing re-check is green: the final gate keeps its whole fix budget.
    assert out["budget"].fix_attempts_since_all_green == 0
    # Integration applies wave 2 AFTER the fix, so its edit to the same file wins.
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "app.py").read_text() == "def fixed():\n    return 2\n"


def test_a_spent_fix_budget_halts_the_waves_in_order(tmp_path: Path) -> None:
    rec = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})  # the fixer can't fix
    out, ran, store = _wave_run(tmp_path, rec, max_fix_attempts=3)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate"]
    assert rec.calls == ["T-001", "fix-1", "fix-2"]  # wave 2 never started
    assert set(_builds(out)) == {GateStatus.FAIL}
    assert out["budget"].fix_attempts_since_all_green == 3  # the bound, as the final gate counts it
    assert [t.id for t in pending_wave(out, store.resolve)] == ["T-002"]  # supervisor halts


_EXISTS = "import os, sys; sys.exit(0 if all(map(os.path.exists, sys.argv[1:])) else 1)"


def _exists_check(tmp_path: Path) -> str:
    """A stand-in type checker that fails on a missing target, like `mypy app`."""
    script = tmp_path / "check.py"
    script.write_text(_EXISTS)
    return f"{sys.executable} {script}"


def test_the_wave_check_typechecks_only_targets_built_so_far(tmp_path: Path) -> None:
    # Wave 0 is the contracts ticket; the planned `app` package arrives in wave 1.
    # The stack's typecheck names both, so the wave-0 check must not demand `app`.
    stack = StackDecision(language="python", build="true",
                          typecheck=f"{_exists_check(tmp_path)} app contracts.py")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("app/__init__.py",), deps=("contracts",)),
    ]
    out, ran, _ = _wave_run(tmp_path, _Recorder(), tickets=tickets, stack=stack)
    assert ran == ["engineering", "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.PASS]
    assert set(_diffs(out)) == {"contracts", "T-001"}
    scope = [t for t in out["gate_triage"] if t.name == "wave-typecheck-scope"]
    assert len(scope) == 1 and "app" in scope[0].explanation


def test_the_wave_check_builds_only_targets_built_so_far(tmp_path: Path) -> None:
    # The finance-tracker trial: `compileall -q finance smoke.py` failed wave 0
    # because smoke.py belongs to a later ticket.
    stack = StackDecision(language="python",
                          build=f"{_exists_check(tmp_path)} contracts.py smoke.py")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("smoke.py",), deps=("contracts",)),
    ]
    out, ran, _ = _wave_run(tmp_path, _Recorder(), tickets=tickets, stack=stack)
    assert ran == ["engineering", "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.PASS]
    scope = [t for t in out["gate_triage"] if t.name == "wave-typecheck-scope"]
    assert len(scope) == 1 and "build" in scope[0].explanation


def test_the_wave_check_still_fails_a_missing_built_target(tmp_path: Path) -> None:
    # Scoping only drops planned-but-unbuilt targets: a path no ticket plans (a
    # typo'd target) still fails the check — the gate is not weakened.
    stack = StackDecision(language="python", build="true",
                          typecheck=f"{_exists_check(tmp_path)} contracts.py contract.py")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("app/__init__.py",), deps=("contracts",)),
    ]
    out, ran, _ = _wave_run(tmp_path, _Recorder(), tickets=tickets, stack=stack,
                            max_fix_attempts=1)
    assert ran == ["engineering", "wave_gate"]
    assert _builds(out) == [GateStatus.FAIL]


def test_contracts_that_fail_the_wave_zero_typecheck_are_fixable(tmp_path: Path) -> None:
    # The contracts are gate-owned, but not before wave 0 passes: the fixer's repair
    # of a stub the type checker rejects reaches the re-check, and wave 1 builds on it.
    script = tmp_path / "typecheck.py"
    script.write_text("import sys; sys.exit(any('BROKEN' in open(p).read() "
                      "for p in sys.argv[1:]))")
    stack = StackDecision(language="python", build="true",
                          typecheck=f"{sys.executable} {script} contracts.py")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("app.py",), deps=("contracts",)),
    ]
    rec = _Recorder(writes={"contracts": {"contracts.py": "BROKEN\n"},
                            "fix-1": {"contracts.py": "class Store: ...\n"}})
    out, ran, store = _wave_run(tmp_path, rec, tickets=tickets, stack=stack,
                                max_fix_attempts=2)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.FAIL, GateStatus.PASS]
    assert rec.seen["T-001"]["contracts.py"] == "class Store: ...\n"
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "contracts.py").read_text() == "class Store: ...\n"


def test_a_later_contract_fix_carries_forward_and_a_ticket_edit_does_not(
    tmp_path: Path,
) -> None:
    # After wave 0 the contracts stay fixable by the fixer (recorded in triage),
    # while a ticket's edit to them is still discarded at integration.
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("app.py",), deps=("contracts",)),
        _ticket("T-002", 2, files=("more.py", "contracts.py"), deps=("T-001",)),
    ]
    rec = _Recorder(writes={
        "T-001": {"app.py": "def broken(:\n"},
        "fix-1": {"app.py": "VALUE = 1\n", "contracts.py": "class Store: ...  # fixed\n"},
        "T-002": {"contracts.py": "class Store: ...  # ticket edit\n"},
    })
    out, ran, store = _wave_run(tmp_path, rec, tickets=tickets)
    assert ran == ["engineering", "wave_gate", "engineering", "wave_gate", "fixer",
                   "wave_gate", "engineering"]
    assert rec.seen["T-002"]["contracts.py"] == "class Store: ...  # fixed\n"
    notes = {t.name: t.explanation for t in out["gate_triage"]
             if t.run_against == "task/fix-1"}
    assert "contracts.py" in notes["contract-change:contracts.py"]
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "contracts.py").read_text() == "class Store: ...  # fixed\n"


def test_a_mid_wave_fix_keeps_writes_outside_the_built_files_and_notes_them(
    tmp_path: Path,
) -> None:
    # A repair often needs a file no built ticket owns (a missing __init__.py, a
    # manifest): the fixer's write is kept and flagged, never silently dropped.
    # Gate inputs stay out of reach: its feature-file edit is still discarded.
    rec = _Recorder(writes={
        "T-001": {"app.py": "def broken(:\n"},
        "fix-1": {"app.py": "def fixed():\n    return 1\n", "later.py": "x = 1\n"},
    })
    tickets = [_ticket("T-001", 0, files=("app.py",)),
               _ticket("T-002", 1, files=("later.py",), deps=("T-001",))]
    out, ran, store = _wave_run(tmp_path, rec, tickets=tickets)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering"]
    assert rec.seen["T-002"]["later.py"] == "x = 1\n"  # the fix reached the next wave
    assert "never build a later ticket's files" in rec.packets["fix-1"].persona
    notes = {t.name: t.explanation for t in out["gate_triage"]
             if t.run_against == "task/fix-1"}
    assert "later.py" in notes["fix-scope"] and "rogue.txt" in notes["fix-scope"]
    assert "app.py" not in notes["fix-scope"]
    assert notes["write-scope"].endswith("features/T-001.feature")  # dropped, not kept
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "rogue.txt").is_file()
    assert (tree / "later.py").read_text() == "# written by T-002\n"  # its owner wins


@pytest.mark.parametrize(("cmd", "want"), [
    ("mypy app contracts.py smoke.py", "mypy contracts.py"),
    ("mypy app/ smoke.py", ""),
    ("mypy ./app contracts.py && pyright contracts.py",
     "mypy contracts.py && pyright contracts.py"),
    ("mypy .", "mypy ."),
    ("mypy contracts.py lib", "mypy contracts.py lib"),  # `lib` is not planned
])
def test_wave_typecheck_drops_only_planned_unbuilt_targets(
    tmp_path: Path, cmd: str, want: str,
) -> None:
    (tmp_path / "contracts.py").write_text("")
    planned = planned_paths(["contracts.py", "app/db.py", "app/__init__.py", "smoke.py"])
    assert wave_typecheck(cmd, planned, tmp_path) == want


@pytest.mark.parametrize("phase", [None, "build_test_fix"])
def test_without_tickets_the_subgraph_builds_nothing(tmp_path: Path, phase: str | None) -> None:
    # The planner always emits tickets; with none there is no work and no fix target,
    # so START goes straight to END and the gates fail on the empty tree.
    store = ArtifactStore()
    rec = _Recorder()
    state = _state(store, [], **({"phase": phase} if phase else {}))
    ran: list[str] = []
    app = _graph(_runtime(rec, store), store, tmp_path).compile()
    for step in app.stream(state, _config(), stream_mode="updates"):
        ran.extend(step)
    assert ran == [] and rec.calls == []


def test_adapter_triage_is_forwarded_never_a_gate_result(tmp_path: Path) -> None:
    from foundry.execution.adapters.stub import StubAdapter
    from foundry.schemas.gates import GateResult, GateTriage

    store = ArtifactStore()
    rt = _runtime(StubAdapter(triage_note="needs a test"), store)
    out = _graph(rt, store, tmp_path).compile().invoke(
        _state(store, [_ticket("T-001", 0)]), _config())
    assert set(_diffs(out)) == {"T-001"}
    assert not out.get("gates")  # L5: agents never mint a GateResult
    triage = out["gate_triage"]
    assert any(t.explanation == "needs a test" for t in triage)
    assert all(isinstance(t, GateTriage) and not isinstance(t, GateResult) for t in triage)


class _ConstantDiffAdapter:
    """Every ticket writes IDENTICAL worktree content + diff bytes -> identical
    snapshot AND blob digests, so concurrent workers collide on the shared
    content-addressed store."""

    supports_turn_snapshots = True
    name = "constant-diff"

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        (Path(sandbox.worktree) / "same.txt").write_text("constant")
        return AdapterResult(diff_uri=sandbox.put_blob(b"identical-diff"),
                             snapshot_digest=sandbox.snapshot())


def test_concurrent_identical_writes_to_a_shared_store_are_safe(tmp_path: Path) -> None:
    """Race regression: a durable snapshot_store + rt.cas shared by pool workers must
    survive concurrent identical-digest writes. Pre-fix this raised FileExistsError /
    'Directory not empty' (a PID-only temp name shared across threads)."""
    store = ArtifactStore()
    tickets = [_ticket("T-001", 0, files=("a.py",)), _ticket("T-002", 1, files=("b.py",))]
    app = _graph(_runtime(_ConstantDiffAdapter(), store), store, tmp_path,
                 snapshot_store=str(tmp_path / "store")).compile()
    out = app.invoke(_state(store, tickets), _config())
    assert set(_diffs(out)) == {"T-001", "T-002"}  # one wave, no race crash
    digest = hashlib.sha256(b"identical-diff").hexdigest()
    assert store.resolve_blob(f"cas://sha256/{digest}") == b"identical-diff"


# ---- pool: per-specialty limits + events --------------------------------------


class _LiveByLineage:
    supports_turn_snapshots = True

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live: dict[str, int] = {}
        self.high: dict[str, int] = {}
        self.total = 0
        self.total_high = 0

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        sp = str(packet.task["specialty"])
        with self.lock:
            self.live[sp] = self.live.get(sp, 0) + 1
            self.high[sp] = max(self.high.get(sp, 0), self.live[sp])
            self.total += 1
            self.total_high = max(self.total_high, self.total)
        try:
            time.sleep(0.04)
            if packet.task["id"] == "boom":
                raise RuntimeError("adapter blew up")
            return AdapterResult(diff_uri=sandbox.put_blob(b""),
                                 snapshot_digest=sandbox.snapshot())
        finally:
            with self.lock:
                self.live[sp] -= 1
                self.total -= 1


def _etask(tid: str, specialty: str) -> ExecutionTask:
    packet = TaskPacket(
        task={"id": tid, "ticket_id": tid, "title": f"t {tid}", "specialty": specialty,
              "tier": "T2"},
        worktree_path=".", turn_budget=1, cost_budget_microusd=1,
    )
    return ExecutionTask(packet=packet, adapter="x", lineage=specialty)


def test_per_specialty_limit_is_never_exceeded(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    adapter = _LiveByLineage()
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(str(src)),
        adapter_factory=lambda _n: adapter,
        max_slots=4,
        limits={"backend": 1, "frontend": 2},
    )
    try:
        ids = [pool.submit(_etask(f"{sp}-{i}", sp))
               for i in range(5) for sp in ("backend", "frontend", "data")]
        for tid in ids:
            pool.result(tid)
    finally:
        pool.shutdown()
    assert adapter.high["backend"] == 1
    assert adapter.high["frontend"] <= 2
    assert adapter.total_high <= 4
    assert adapter.total_high >= 2  # and it did run concurrently


def test_a_task_waiting_on_its_specialty_never_holds_a_pool_thread(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    started: list[str] = []
    cond = threading.Condition()
    release = threading.Event()

    class _Gated(_LiveByLineage):
        def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
            with cond:
                started.append(str(packet.task["id"]))
                cond.notify_all()
            release.wait(5)
            return super().run(packet, sandbox)

    adapter = _Gated()
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(str(src)),
        adapter_factory=lambda _n: adapter,
        max_slots=3,
        limits={"backend": 1, "frontend": 2},
    )
    try:
        ids = [pool.submit(_etask(tid, sp)) for tid, sp in (
            ("B1", "backend"), ("B2", "backend"), ("B3", "backend"),
            ("F1", "frontend"), ("F2", "frontend"), ("F3", "frontend"),
        )]
        with cond:
            cond.wait_for(lambda: len(started) >= 3, timeout=2)
            first = sorted(started)
        release.set()
        for tid in ids:
            pool.result(tid)
    finally:
        release.set()
        pool.shutdown()
    assert first == ["B1", "F1", "F2"]  # B2/B3 wait without taking a thread
    assert adapter.high == {"backend": 1, "frontend": 2}
    assert adapter.total_high <= 3
    assert sorted(started) == ["B1", "B2", "B3", "F1", "F2", "F3"]


def test_on_task_reports_start_done_failed_and_is_fail_soft(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    events: list[TaskEvent] = []

    def hook(e: TaskEvent) -> None:
        events.append(e)
        raise ValueError("a broken listener must not break the build")

    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(str(src)),
        adapter_factory=lambda _n: _LiveByLineage(),
        on_task=hook,
    )
    try:
        ok = pool.submit(_etask("ok", "data"))
        bad = pool.submit(_etask("boom", "test"))
        pool.result(ok)
        with pytest.raises(RuntimeError):
            pool.result(bad)
    finally:
        pool.shutdown()
    assert events == [
        {"phase": "start", "ticket_id": "ok", "title": "t ok", "specialty": "data", "tier": "T2"},
        {"phase": "done", "ticket_id": "ok", "title": "t ok", "specialty": "data", "tier": "T2"},
        {"phase": "start", "ticket_id": "boom", "title": "t boom", "specialty": "test",
         "tier": "T2"},
        {"phase": "failed", "ticket_id": "boom", "title": "t boom", "specialty": "test",
         "tier": "T2"},
    ]


# ---- model per ticket ---------------------------------------------------------


def _packet(tier: str | None) -> TaskPacket:
    task: dict[str, Any] = {"id": "T-1"}
    if tier:
        task["tier"] = tier
    return TaskPacket(task=task, worktree_path=".", turn_budget=1, cost_budget_microusd=1)


def _model_arg(argv: list[str]) -> str | None:
    return argv[argv.index("--model") + 1] if "--model" in argv else None


def test_claude_code_resolves_the_packet_tier_to_model() -> None:
    adapter = ClaudeCodeAdapter(model="fallback-model",
                                tier_models={"T1": "opus-x", "T3": "haiku-x"})
    assert _model_arg(adapter._argv(_packet("T3"))) == "haiku-x"
    assert _model_arg(adapter._argv(_packet("T1"))) == "opus-x"
    assert _model_arg(adapter._argv(_packet("T2"))) == "fallback-model"  # unmapped tier
    assert _model_arg(adapter._argv(_packet(None))) == "fallback-model"
    assert _model_arg(ClaudeCodeAdapter()._argv(_packet(None))) is None


def test_cli_wires_tier_models_and_proxy_closures_use_the_packet_tier(
    tmp_path: Path,
) -> None:
    tiers: list[str] = []

    class _Proxy:
        def complete(self, _node: str, tier: str, *_a: Any, **_k: Any) -> Any:
            tiers.append(tier)
            return type("R", (), {"text": "```diff\n```\n```triage\nok\n```",
                                  "parsed": {"action": "finish", "summary": "ok"}})()

    proxy: Any = _Proxy()
    cc = cli.make_coding_adapter("claude-code", proxy=proxy, tier_models={"T3": "haiku-x"})
    assert _model_arg(cc._argv(_packet("T3"))) == "haiku-x"

    src = tmp_path / "src"
    src.mkdir()
    sandbox = Sandbox(str(src))
    try:
        cli.make_coding_adapter("raw-react", proxy=proxy).run(_packet("T3"), sandbox)
        cli.make_coding_adapter("raw-react", proxy=proxy).run(_packet(None), sandbox)
        cli.make_coding_adapter("proxy-agent", proxy=proxy).run(_packet("T2"), sandbox)
    finally:
        sandbox.destroy()
    assert tiers == ["T3", "T1", "T2"]


# ---- flags --------------------------------------------------------------------


def test_parse_workers_and_ticket_tiers() -> None:
    assert cli.parse_workers("") == {"backend": 4, "data": 4, "frontend": 4, "test": 4}
    assert cli.parse_workers("frontend=2, backend=3,data=1,test=2") == {
        "backend": 3, "data": 1, "frontend": 2, "test": 2}
    for bad in ("ops=2", "backend=0", "backend=x", "backend"):
        with pytest.raises(ValueError):
            cli.parse_workers(bad)
    assert cli.parse_ticket_tiers("trivial=t2") == {
        Difficulty.TRIVIAL: "T2", Difficulty.STANDARD: "T2", Difficulty.COMPLEX: "T1"}
    for bad in ("easy=T1", "complex=T9"):
        with pytest.raises(ValueError):
            cli.parse_ticket_tiers(bad)


def test_main_passes_flags_and_env_through_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(request: str, **kw: Any) -> Any:
        seen.update(kw)
        return type("R", (), {"summary": lambda self: "", "passed": True})()

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setenv("FOUNDRY_WORKERS", "data=5")
    monkeypatch.setenv("FOUNDRY_TICKET_TIERS", "complex=T2")
    assert cli.main(["build x"]) == 0
    assert seen["workers"]["data"] == 5 and seen["ticket_tiers"][Difficulty.COMPLEX] == "T2"
    assert cli.main(["--workers", "test=3", "--ticket-tiers", "trivial=T1", "build x"]) == 0
    assert seen["workers"] == {"backend": 4, "data": 4, "frontend": 4, "test": 3}
    assert seen["ticket_tiers"][Difficulty.TRIVIAL] == "T1"
    assert seen["ticket_tiers"][Difficulty.COMPLEX] == "T1"  # flag replaces the env
    assert cli.main(["--workers", "ops=1", "build x"]) == 2


# ---- per-wave scenario check: unbound scenarios are caught in their wave -----

_STEPS = '''from pytest_bdd import given, scenarios, then, when

scenarios("{path}")


@given("an empty store")
def _empty():
    pass


@when("I add 'milk'")
def _add():
    pass


@then("it has id 1")
def _id():
    from app import add
    assert add("milk") == 1
'''


def _bdd_wave_tickets(store: ArtifactStore) -> tuple[list[Ticket], list[Any]]:
    crit = AcceptanceCriterion(
        id="AC-1", text="add returns 1", given=("an empty store",),
        when=("I add 'milk'",), then=("it has id 1",), ticket_id="T-001", seq=_seq(21),
    )
    tickets = [
        _ticket("T-001", 0, files=("app.py",), scenario_ids=("AC-1",)),
        _ticket("T-002", 1, specialty=Specialty.TEST, files=("tests/steps/test_app.py",),
                deps=("T-001",), scenario_ids=("AC-1",)),
        _ticket("T-003", 2, files=("later.py",), deps=("T-002",)),
    ]
    return tickets, [emit_artifact(crit, "acceptance_criterion", "planner", crit.seq,
                                   store=store)]


def test_the_wave_check_catches_an_unbound_scenario_and_the_fixer_binds_it(
    tmp_path: Path,
) -> None:
    # The trial failure: a test ticket binds its OWN copy of the scenarios, so the
    # suite is green but the gate-owned feature never runs. Caught after the wave
    # that built both carriers, not all at once at the final gate.
    rec = _Recorder(writes={
        "T-001": {"app.py": "def add(title):\n    return 1\n"},
        "T-002": {"tests/features/app.feature": "Feature: copy\n",
                  "tests/steps/test_app.py": _STEPS.format(path="../features/app.feature")},
        "fix-1": {"tests/steps/test_app.py":
                  _STEPS.format(path="../../features/T-001.feature")},
    })
    store = ArtifactStore()
    tickets, extra = _bdd_wave_tickets(store)
    out, ran, store = _wave_run(tmp_path, rec, tickets=tickets, artifacts=extra, store=store)

    # T-001 alone leaves AC-1 waiting on its test ticket: build only, no scenario run.
    assert ran == ["engineering", "wave_gate", "engineering", "wave_gate", "fixer",
                   "wave_gate", "engineering"]
    tests = [g for g in sorted(out["gates"], key=lambda g: g.seq) if g.name == "test"]
    assert [g.status for g in tests] == [GateStatus.FAIL, GateStatus.PASS]
    assert any("features/T-001.feature::AC-1 was not executed" in f
               for f in tests[0].findings)
    # The fixer sees the failure grouped by feature file with the owning tickets.
    grouped = "\n".join(rec.packets["fix-1"].prior_failures)
    assert "[features/T-001.feature] scenarios AC-1" in grouped
    assert "T-002 (tests/steps/test_app.py)" in grouped
    assert "never write, copy or regenerate a .feature" in " ".join(
        rec.packets["fix-1"].task["rules"])
    # The agent's own feature file never reaches the tree.
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert not (tree / "tests/features/app.feature").exists()


def test_a_test_ticket_packet_names_its_feature_binding_and_verify_command(
    tmp_path: Path,
) -> None:
    store = ArtifactStore()
    tickets, extra = _bdd_wave_tickets(store)
    rec = _Recorder()
    stack = StackDecision(language="python", test="python3 -m pytest -q")
    _graph(_runtime(rec, store), store, tmp_path).compile().invoke(
        _state(store, tickets, artifacts=extra, stack=stack), _config())

    rules = " ".join(rec.packets["T-002"].task["rules"])
    assert "features/T-001.feature (Scenario: AC-1)" in rules
    assert 'in tests/steps/test_app.py: scenarios("../../features/T-001.feature")' in rules
    assert "Never write, copy or regenerate a .feature file" in rules
    assert rec.packets["T-002"].task["verify"]["scenarios"] == (
        "grep -q features/T-001.feature tests/steps/test_app.py && "
        "python3 -m pytest -q -rA tests/steps/test_app.py"
    )
    # The feature ticket names the file too, but leaves the binding to its test ticket.
    impl = " ".join(rec.packets["T-001"].task["rules"])
    assert "features/T-001.feature (Scenario: AC-1)" in impl
    assert "A paired test ticket binds these scenarios" in impl
    assert "scenarios" not in rec.packets["T-001"].task["verify"]
