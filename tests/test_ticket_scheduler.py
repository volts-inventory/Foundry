"""Ticket-driven scheduler (plan §2 / §2b): tickets -> tasks -> waves.

Tickets are built by hand (``emit_artifact(ticket, "ticket", ...)``) into a real
:class:`ArtifactStore`, so the subgraph resolves them exactly as it will the
planner's. A recording adapter writes each ticket's ``files`` (plus paths outside
its scope) as a full-file snapshot, so wave order, overlay visibility, write-scope
trimming and resume are all observable.
"""

from __future__ import annotations

import json
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
    next_wave,
    task_from_ticket,
    tier_for,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.execution.sandbox import Sandbox
from foundry.execution.work_pool import ExecutionTask, LocalWorkPool
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.proxy.token_meter import TokenCapExceeded
from foundry.schemas.architecture import ConstraintScope, InterfaceContract, PinnedConstraint
from foundry.schemas.implementation import (
    Difficulty,
    FeatureDiff,
    Specialty,
    TaskEvent,
    Ticket,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.factory_state import FactoryState
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

    def __init__(self, cap_on: str | None = None, delay: float = 0.0) -> None:
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.packets: dict[str, TaskPacket] = {}
        self.seen: dict[str, dict[str, str]] = {}
        self.cap_on = cap_on
        self.delay = delay

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        if tid == self.cap_on:
            self.cap_on = None
            raise TokenCapExceeded("token cap spent")
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
        files = {f: f"# written by {tid}\n" for f in packet.task.get("files", ())}
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


def test_resume_after_token_cap_skips_built_tickets(tmp_path: Path) -> None:
    store = ArtifactStore()
    rec = _Recorder(cap_on="T-002")
    tickets = [
        _ticket("T-001", 0, files=("a.py",)),
        _ticket("T-002", 1, files=("b.py",), deps=("T-001",)),
    ]
    app = _graph(_runtime(rec, store), store, tmp_path).compile(checkpointer=MemorySaver())
    cfg = _config("resume-run")
    with pytest.raises(TokenCapExceeded):
        app.invoke(_state(store, tickets), cfg)
    assert rec.calls == ["T-001"]
    out = app.invoke(None, cfg)
    assert rec.calls == ["T-001", "T-002"]  # wave 1 was not rebuilt
    assert set(_diffs(out)) == {"T-001", "T-002"}


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
    assert rec.calls[-1] == "fix-0" and rec.seen["fix-0"]["a.py"] == "# written by T-001\n"


def test_without_tickets_the_ownership_lane_path_runs(tmp_path: Path) -> None:
    from foundry.agents.implementation.senior_engineer import DESIGN_KIND, OWNERSHIP_KIND
    from foundry.schemas.architecture import OwnershipMap, SystemDesign

    store = ArtifactStore()
    rec = _Recorder()
    own = OwnershipMap(owners={"api.py": "owner-a"}, seq=_seq(0))
    design = SystemDesign(title="t", design_uri="cas://d", components=("api",), seq=_seq(1))
    state = _state(store, [], artifacts=[
        emit_artifact(own, OWNERSHIP_KIND, "p", own.seq, store=store),
        emit_artifact(design, DESIGN_KIND, "p", design.seq, store=store),
    ])
    ran: list[str] = []
    app = _graph(_runtime(rec, store), store, tmp_path).compile()
    for step in app.stream(state, _config(), stream_mode="updates"):
        ran.extend(step)
    assert "engineering" not in ran and "backend_engineer" in ran


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
    assert cli.parse_workers("") == {"backend": 2, "data": 2, "frontend": 2, "test": 2}
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
    assert seen["workers"] == {"backend": 2, "data": 2, "frontend": 2, "test": 3}
    assert seen["ticket_tiers"][Difficulty.TRIVIAL] == "T1"
    assert seen["ticket_tiers"][Difficulty.COMPLEX] == "T1"  # flag replaces the env
    assert cli.main(["--workers", "ops=1", "build x"]) == 2
