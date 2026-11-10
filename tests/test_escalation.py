"""Escalation cascade (Phase 5): in hybrid mode a local ticket that can't finish
re-runs once on the cloud level, with what was tried: at once when its session
wrote none of its files; on the first failing wave check when its session stopped
early (turn cap, repeat/read-thrash detector) and kept its partial work; else when
its wave check failed twice. Cloud and local modes never escalate."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from foundry.agents import AgentRuntime
from foundry.agents.implementation.tickets import (
    escalation_candidates,
    escalation_pick,
    make_engineering_node,
    pending_escalations,
    session_digests,
    stopped_tickets,
    task_from_ticket,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.tiered import (
    DIGEST_TRIAGE,
    ESCALATE_FALLBACK_TRIAGE,
    ESCALATED_TRIAGE,
    STOPPED_DIGEST,
    TieredAdapter,
)
from foundry.execution.sandbox import Sandbox
from foundry.graph.gate_wiring import (
    BUILD_BOUNDARY,
    FixPolicy,
    failing_streak,
    materialize_workdir,
)
from foundry.modes import escalation_tier, preset_levels
from foundry.proxy.model_provider_proxy import TIER_MODELS
from foundry.proxy.token_meter import TokenMeter
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, GateTriage, VerdictClass
from foundry.schemas.implementation import Ticket
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.test_model_modes import CALL, RecordingTransport, _hybrid
from tests.test_ticket_scheduler import (
    _builds,
    _config,
    _diffs,
    _graph,
    _Recorder,
    _runtime,
    _state,
    _ticket,
    _wave_run,
)

TURN_CAP = "turn budget exhausted before 'done' — partial work recorded"
REPEAT = "stopped early: identical action repeated 5 times (run 'pytest') — partial work recorded"
THRASH = "stopped early: read app.py 6 times without changing it — partial work recorded"


class _Session(_Recorder):
    """A ``_Recorder`` whose session reports ``stop[tid]`` the first ``times`` runs,
    the way proxy-agent reports a turn cap or an early stop, and can make one model
    call per session through ``proxy`` at the packet's tier. A stopped session
    reports ``digest[tid]`` as its summary, the way proxy-agent does."""

    def __init__(
        self,
        *,
        stop: dict[str, str] | None = None,
        times: int = 99,
        proxy: Any = None,
        digest: dict[str, str] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.stop = dict(stop or {})
        self.digest = dict(digest or {})
        self.times = times
        self.proxy = proxy
        self.tiers: list[tuple[str, str | None]] = []

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        tier = packet.task.get("tier")
        self.tiers.append((tid, tier))
        if self.proxy is not None:
            self.proxy.complete(CALL[0], str(tier or "T1"), *CALL[1:], purpose="ideation")
        result = super().run(packet, sandbox)
        if tid in self.stop and self.times > 0:
            self.times -= 1
            note = GateTriage(
                name="adapter-report",
                run_against=f"task/{tid}",
                explanation=self.stop[tid],
                produced_by="proxy-agent",
                seq=Seq("run", 0, 0, "proxy-agent", 0),
            )
            triage: tuple[GateTriage, ...] = (note,)
            if tid in self.digest:
                head = note.model_copy(
                    update={"name": DIGEST_TRIAGE, "explanation": self.digest[tid]}
                )
                triage = (head, note)
            result = result.model_copy(update={"triage": triage})
        return result


def _tiered(cloud: Any, local: Any, *, target: str | None = "T1") -> TieredAdapter:
    adapter = TieredAdapter(
        cloud=cloud,
        local=local,
        local_tiers={"T2", "T3"},
        local_slot=threading.RLock,
        escalation_tier=target,
    )
    adapter.name = "recorder"  # type: ignore[misc]
    return adapter


def _build(tmp_path: Path, adapter: Any, tickets: list[Ticket]) -> FactoryState:
    store = ArtifactStore()
    out: FactoryState = (
        _graph(_runtime(adapter, store), store, tmp_path)
        .compile()
        .invoke(_state(store, tickets), _config())
    )
    return out


def _escalated(out: FactoryState) -> dict[str, str]:
    return {
        t.run_against: t.explanation
        for t in out.get("gate_triage") or ()
        if t.name == ESCALATED_TRIAGE
    }


# ---- which modes escalate ----------------------------------------------------------


def test_only_a_mixed_level_table_has_an_escalation_tier() -> None:
    def table(mode: str) -> Any:
        return preset_levels(mode, cloud_backend="mock", cloud_models=TIER_MODELS)

    assert escalation_tier(table("hybrid")) == "T1"
    assert escalation_tier(table("cloud")) is None
    assert escalation_tier(table("local")) is None


# ---- in-session triggers: turn cap, early stop, an empty session ----------------


def _local_tree(out: FactoryState, store: ArtifactStore, tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    return tree


@pytest.mark.parametrize("reason", [TURN_CAP, REPEAT, THRASH])
def test_a_stopped_local_session_keeps_its_work_and_is_marked(
    tmp_path: Path,
    reason: str,
) -> None:
    # No wave check follows (none configured): the stop is left to the supervisor's
    # gates and its cloud fixer, never re-run on the cloud level here.
    store = ArtifactStore()
    local = _Session(stop={"T-001": reason}, writes={"T-001": {"a.py": "local\n"}})
    cloud = _Session()
    out = (
        _graph(_runtime(_tiered(cloud, local), store), store, tmp_path)
        .compile()
        .invoke(
            _state(
                store, [_ticket("T-001", 0, files=("a.py",)), _ticket("T-002", 1, files=("b.py",))]
            ),
            _config(),
        )
    )
    assert cloud.tiers == []
    assert _escalated(out) == {}
    assert set(_diffs(out)) == {"T-001", "T-002"}
    assert (_local_tree(out, store, tmp_path) / "a.py").read_text() == "local\n"
    assert stopped_tickets(out) == {"T-001": reason}
    concerns = [c for j in out["agent_journal"] if j.node_id == "engineering" for c in j.concerns]
    assert any(c.startswith("T-001 stopped early on T2, its partial work kept") for c in concerns)


def test_a_session_that_wrote_none_of_its_files_escalates_at_once(tmp_path: Path) -> None:
    local = _Session(silent=frozenset({"T-001"}))
    cloud = _Session(writes={"T-001": {"a.py": "cloud\n"}})
    store = ArtifactStore()
    out = (
        _graph(_runtime(_tiered(cloud, local), store), store, tmp_path)
        .compile()
        .invoke(_state(store, [_ticket("T-001", 0, files=("a.py",))]), _config())
    )
    assert local.tiers == [("T-001", "T2")]
    assert cloud.tiers == [("T-001", "T1")]
    # The local level's larger turn budget never carries over to the cloud re-run.
    assert (local.packets["T-001"].turn_budget, cloud.packets["T-001"].turn_budget) == (40, 25)
    context = cloud.packets["T-001"].prior_failures
    assert context[0] == "[escalation]"
    assert any("wrote none of the ticket's files" in line and "T2" in line for line in context)
    assert any("writes were discarded" in line for line in context)
    assert "from T2 to T1" in _escalated(out)["task/T-001"]
    assert stopped_tickets(out) == {}
    assert (_local_tree(out, store, tmp_path) / "a.py").read_text() == "cloud\n"


def test_a_failed_escalation_retries_on_the_cloud_and_never_escalates_again(
    tmp_path: Path,
) -> None:
    # The cloud attempt crashes. Its escalation is spent and the local attempt was
    # discarded, so the retry stays on the cloud level instead of demoting to local.
    local = _Session(silent=frozenset({"T-001"}))
    cloud = _Session(fail_on={"T-001": 1})
    out = _build(tmp_path, _tiered(cloud, local), [_ticket("T-001", 0, files=("a.py",))])
    assert cloud.calls == ["T-001!", "T-001"]
    assert cloud.tiers == [("T-001", "T1"), ("T-001", "T1")]
    assert local.tiers == [("T-001", "T2")]
    assert set(_diffs(out)) == {"T-001"}
    assert len([t for t in out["gate_triage"] if t.name == ESCALATED_TRIAGE]) == 1


def test_a_cloud_ticket_and_a_finished_local_ticket_never_escalate(tmp_path: Path) -> None:
    from foundry.schemas.implementation import Difficulty

    local = _Session()
    cloud = _Session(stop={"T-002": TURN_CAP})  # a cloud session stopping stays as is
    out = _build(
        tmp_path,
        _tiered(cloud, local),
        [
            _ticket("T-001", 0, files=("a.py",)),
            _ticket("T-002", 1, files=("b.py",), difficulty=Difficulty.COMPLEX),
        ],
    )
    assert local.tiers == [("T-001", "T2")]
    assert cloud.tiers == [("T-002", "T1")]
    assert _escalated(out) == {}


def test_local_mode_never_escalates(tmp_path: Path) -> None:
    # Local mode's table has no cloud level: the adapter declares no escalation tier.
    local = _Session(stop={"T-001": TURN_CAP})
    out = _build(
        tmp_path, _tiered(local, local, target=None), [_ticket("T-001", 0, files=("a.py",))]
    )
    assert local.tiers == [("T-001", "T2")]
    assert _escalated(out) == {}


def test_cloud_mode_never_escalates(tmp_path: Path) -> None:
    # Cloud mode runs one adapter, no level table: nothing to escalate from.
    rec = _Session(stop={"T-001": TURN_CAP})
    out = _build(tmp_path, rec, [_ticket("T-001", 0, files=("a.py",))])
    assert rec.tiers == [("T-001", "T2")]
    assert _escalated(out) == {}


def test_escalated_calls_are_charged_to_the_cloud_budget(tmp_path: Path) -> None:
    cloud_tx, local_tx = RecordingTransport(), RecordingTransport()
    proxy = _hybrid(cloud_tx, local_tx)
    proxy.token_meter = TokenMeter(10_000)
    local = _Session(silent=frozenset({"T-001"}), proxy=proxy)
    cloud = _Session(proxy=proxy)
    store = ArtifactStore()
    rt = AgentRuntime(proxy=proxy, adapter=_tiered(cloud, local), cas=store)
    _graph(rt, store, tmp_path).compile().invoke(
        _state(store, [_ticket("T-001", 0, files=("a.py",))]), _config()
    )
    assert len(local_tx.requests) == 1 and len(cloud_tx.requests) == 1
    assert cloud_tx.requests[0]["model"] == TIER_MODELS["T1"]
    assert proxy.token_meter.used == 110  # the cloud call only; the local one is free


# ---- the wave-check trigger: two failing checks in a row ------------------------


def _two_waves() -> list[Ticket]:
    return [
        _ticket("T-001", 0, files=("app.py",)),
        _ticket("T-002", 1, files=("app.py",), deps=("T-001",)),
    ]


def test_a_wave_check_that_fails_twice_reruns_its_local_ticket_on_the_cloud(
    tmp_path: Path,
) -> None:
    local = _Session(
        writes={
            "T-001": {"app.py": "def broken(:\n"},
            "T-002": {"app.py": "def two():\n    return 2\n"},
        }
    )
    cloud = _Session(
        writes={
            "fix-1": {"app.py": "def broken_1(:\n"},
            "T-001": {"app.py": "def one():\n    return 1\n"},
        }
    )
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    # Restart once on its own tier first, then escalate: the restart rebuilt it
    # (still broken) locally, and only then the cloud level rebuilt it.
    assert ran == [
        "engineering",
        "wave_gate",
        "fixer",
        "wave_gate",
        "engineering",
        "wave_gate",
        "engineering",
        "wave_gate",
        "engineering",
    ]
    assert [t for t, _ in local.tiers] == ["T-001", "T-001", "T-002"]
    assert local.packets["T-001"].prior_failures[0] == "[fresh restart]"
    assert cloud.tiers == [("fix-1", None), ("T-001", "T1")]
    assert _builds(out) == [GateStatus.FAIL, GateStatus.FAIL, GateStatus.FAIL, GateStatus.PASS]
    assert {t.run_against for t in out["gate_triage"] if t.name == "ticket-restarted"} == {
        "task/T-001"
    }
    context = cloud.packets["T-001"].prior_failures
    assert context[0] == "[escalation]"
    assert "[fix summary]" in context and "failed gates: build" in context
    assert any("twice" in line for line in context)
    assert any(
        line.startswith("the earlier attempt touched:") and "app.py" in line for line in context
    )
    assert set(_escalated(out)) == {"task/T-001"}
    assert cloud.seen["T-001"]["app.py"] == "def broken(:\n"  # built on the restart
    # A standard ticket thinks at medium; its restart and escalation one level up;
    # the fix loop's first fixer pass at medium.
    assert local.packets["T-002"].task["effort"] == "medium"
    assert local.packets["T-001"].task["effort"] == "xhigh"
    assert cloud.packets["T-001"].task["effort"] == "xhigh"
    assert cloud.packets["fix-1"].task["effort"] == "medium"


def test_a_wave_check_escalation_happens_once(tmp_path: Path) -> None:
    # The cloud rebuild fails the check too: no second request, the bound decides.
    local = _Session(writes={"T-001": {"app.py": "def broken(:\n"}})
    cloud = _Session(
        writes={
            "fix-1": {"app.py": "def broken_1(:\n"},
            "T-001": {"app.py": "def broken_2(:\n"},
            "fix-2": {"app.py": "def broken_3(:\n"},
        }
    )
    out, ran, _ = _wave_run(
        tmp_path,
        _tiered(cloud, local),
        tickets=_two_waves(),  # type: ignore[arg-type]
        fix_policy=FixPolicy(restarts=False),
    )
    assert [t for t, _ in cloud.tiers].count("T-001") == 1
    assert len([t for t in out["gate_triage"] if t.name == "escalate-request"]) == 1
    # The rebuild is an extra attempt, not a fix pass: the wave keeps its second fixer
    # pass, and only the check after that halts.
    assert ran == [
        "engineering",
        "wave_gate",
        "fixer",
        "wave_gate",
        "engineering",
        "wave_gate",
        "fixer",
        "wave_gate",
    ]
    assert [t for t, _ in cloud.tiers] == ["fix-1", "T-001", "fix-2"]
    assert "T-002" not in [t for t, _ in local.tiers]


def test_without_an_escalation_tier_a_twice_failing_check_never_escalates(
    tmp_path: Path,
) -> None:
    local = _Session(writes={"T-001": {"app.py": "def broken(:\n"}})
    cloud = _Session(
        writes={"fix-1": {"app.py": "def broken_1(:\n"}, "fix-2": {"app.py": "def broken_2(:\n"}}
    )
    out, ran, _ = _wave_run(
        tmp_path,
        _tiered(cloud, local, target=None),  # type: ignore[arg-type]
        tickets=_two_waves(),
        fix_policy=FixPolicy(restarts=False),
    )
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate"]
    assert not [t for t in out["gate_triage"] if t.name.startswith("escalate")]


# ---- who a twice-failing wave check escalates ------------------------------------


def _check_state(
    store: ArtifactStore,
    tickets: list[Ticket],
    *findings: str,
    escalated: tuple[str, ...] = (),
    tasks: list[dict[str, Any]] | None = None,
    fell_back: tuple[str, ...] = (),
) -> FactoryState:
    diffs = [
        Artifact(
            kind="feature_diff",
            uri=f"cas://feature_diff/{t.id}",
            produced_by="junior_engineer",
            task_id=t.id,
            seq=Seq("run", 1, 1, "e", i),
        )
        for i, t in enumerate(tickets)
    ]
    gate = GateResult(
        name="build",
        status=GateStatus.FAIL,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=findings,
        run_against="tree-2",
        seq=Seq("run", 1, 9, "g", 0),
    )
    notes = [
        GateTriage(
            name=ESCALATED_TRIAGE,
            run_against=f"task/{tid}",
            explanation="x",
            produced_by="engineering",
            seq=Seq("run", 1, 1, "e", 0),
        )
        for tid in escalated
    ]
    notes += [
        GateTriage(
            name=ESCALATE_FALLBACK_TRIAGE,
            run_against=f"task/{tid}",
            explanation="x",
            produced_by="build_gate",
            seq=Seq("run", 1, 1, "g", 0),
        )
        for tid in fell_back
    ]
    return _state(
        store, tickets, artifacts=diffs, gates=[gate], gate_triage=notes, tasks=tasks or []
    )


def _candidates(state: FactoryState, store: ArtifactStore, **kw: Any) -> tuple[str, ...]:
    rt = AgentRuntime(proxy=None, adapter=_tiered(_Session(), _Session()), cas=store)  # type: ignore[arg-type]
    return escalation_candidates(state, store.resolve, rt, **kw)


def _pair() -> list[Ticket]:
    return [_ticket("T-001", 0, files=("app.py",)), _ticket("T-002", 1, files=("webapp.py",))]


def test_the_whole_wave_fallback_runs_at_most_once() -> None:
    store = ArtifactStore()
    unnamed = "SyntaxError: invalid syntax"
    rt = AgentRuntime(proxy=None, adapter=_tiered(_Session(), _Session()), cas=store)  # type: ignore[arg-type]
    first = _check_state(store, _pair(), unnamed)
    assert escalation_pick(first, store.resolve, rt) == (("T-001", "T-002"), True)
    # Once the fallback has run on the wave, an unnamed failure escalates nobody else.
    after = _check_state(store, _pair(), unnamed, escalated=("T-001",), fell_back=("T-001",))
    assert _candidates(after, store) == ()
    # A ticket the failures name still can, and that is no fallback.
    named = _check_state(
        store, _pair(), "webapp.py:3: error", escalated=("T-001",), fell_back=("T-001",)
    )
    assert escalation_pick(named, store.resolve, rt) == (("T-002",), False)


def test_a_named_or_stopped_escalation_leaves_the_fallback_to_the_rest_of_the_wave() -> None:
    # Wave {T-001 stopped, T-002}: the first failure escalated T-001 (stopped early,
    # named). A second failure that names nobody still falls back to T-002, once.
    store = ArtifactStore()
    rt = AgentRuntime(proxy=None, adapter=_tiered(_Session(), _Session()), cas=store)  # type: ignore[arg-type]
    stop = GateTriage(
        name="stopped-early",
        run_against="task/T-001",
        explanation=TURN_CAP,
        produced_by="engineering",
        seq=Seq("run", 1, 1, "e", 1),
    )
    state = _check_state(store, _pair(), "SyntaxError: invalid syntax", escalated=("T-001",))
    state = {**state, "gate_triage": [*state["gate_triage"], stop]}
    assert escalation_pick(state, store.resolve, rt) == (("T-002",), True)
    # The settled T-001 is never picked again, and the fallback does not repeat.
    done = _check_state(
        store, _pair(), "SyntaxError", escalated=("T-001", "T-002"), fell_back=("T-002",)
    )
    assert _candidates(done, store) == ()


@pytest.mark.parametrize(
    ("finding", "named"),
    [
        ("webapp.py:3: SyntaxError", ("T-002",)),
        ("FAILED tests/test_app.py::test_add - assert 1 == 2", ("T-001", "T-002")),  # fallback
        ('  File "/tmp/w/src/app.py", line 2', ("T-001",)),
        ("app.py:1: error: Name 'x' is not defined", ("T-001",)),
    ],
)
def test_a_ticket_is_named_only_by_a_whole_path(finding: str, named: tuple[str, ...]) -> None:
    store = ArtifactStore()
    assert _candidates(_check_state(store, _pair(), finding), store) == named


def test_candidates_read_the_tier_the_engineering_node_uses() -> None:
    from foundry.schemas.implementation import Difficulty

    store = ArtifactStore()
    tickets = [_ticket("T-001", 0, files=("app.py",), difficulty=Difficulty.COMPLEX)]
    # A stale tasks-channel record says T2; the ticket's difficulty says T1 (cloud).
    stale = [
        {
            **task_from_ticket(tickets[0], Seq("run", 1, 1, "s", 0)).model_dump(mode="json"),
            "tier": "T2",
        }
    ]
    state = _check_state(store, tickets, "app.py:1: error", tasks=stale)
    assert _candidates(state, store) == ()
    assert _candidates(state, store, ticket_tiers={"complex": "T2"}) == ("T-001",)


def _verdict(tree: str, local: int, status: GateStatus) -> GateResult:
    return GateResult(
        name="build",
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=("x",),
        run_against=tree,
        seq=Seq("run", 1, local, "g", 0),
    )


def test_only_code_failures_count_toward_the_failing_streak() -> None:
    fail, error, ok = GateStatus.FAIL, GateStatus.ERROR, GateStatus.PASS
    assert (
        failing_streak(
            {"gates": [_verdict("a", 1, fail), _verdict("b", 2, fail)]}, boundary=BUILD_BOUNDARY
        )
        == 2
    )
    # Infra errors / flake quarantines are not code failures.
    assert (
        failing_streak(
            {"gates": [_verdict("a", 1, error), _verdict("b", 2, error)]}, boundary=BUILD_BOUNDARY
        )
        == 0
    )
    assert (
        failing_streak(
            {"gates": [_verdict("a", 1, fail), _verdict("b", 2, error)]}, boundary=BUILD_BOUNDARY
        )
        == 0
    )
    # A FAIL superseded by a newer PASS on the same tree no longer counts.
    assert (
        failing_streak(
            {"gates": [_verdict("a", 1, fail), _verdict("b", 2, fail), _verdict("b", 3, ok)]},
            boundary=BUILD_BOUNDARY,
        )
        == 0
    )


# ---- a requested rebuild and the work pool's quota -------------------------------


def _requested(tmp_path: Path, **node_kw: Any) -> tuple[FactoryState, _Session]:
    store = ArtifactStore()
    local, cloud = _Session(), _Session()
    rt = _runtime(_tiered(cloud, local), store)
    built = (
        _graph(rt, store, tmp_path)
        .compile()
        .invoke(_state(store, [_ticket("T-001", 0, files=("a.py",))]), _config())
    )
    ask = GateTriage(
        name="escalate-request",
        run_against="task/T-001",
        explanation="the wave check failed twice in a row (build)",
        produced_by="build_gate",
        seq=Seq("run", 1, 1, "build_gate", 0),
    )
    state = {**built, "gate_triage": [*built["gate_triage"], ask]}
    node = make_engineering_node(
        rt,
        source_dir=str(tmp_path / "src"),
        resolve=store.resolve,
        resolve_blob=store.resolve_blob,
        **node_kw,
    )
    out = node(state, _config())
    merged: FactoryState = {
        **state,
        "gate_triage": [*state["gate_triage"], *out.get("gate_triage", [])],
    }
    assert pending_escalations(merged, store.resolve) == ()  # settled either way
    return out, cloud


def test_a_rebuild_replaces_the_tickets_quota_estimate(tmp_path: Path) -> None:
    # The cap fits one ticket: the rebuild must not be counted on top of the build.
    out, cloud = _requested(tmp_path, cost_budget_microusd=100, cost_cap_microusd=100)
    assert cloud.tiers == [("T-001", "T1")]
    assert [t.name for t in out["gate_triage"]].count(ESCALATED_TRIAGE) == 1


def test_a_rebuild_the_quota_refuses_is_declined_not_spent(tmp_path: Path) -> None:
    out, cloud = _requested(tmp_path, cost_budget_microusd=100, cost_cap_microusd=50)
    assert cloud.tiers == []
    names = {t.name for t in out.get("gate_triage", [])}
    assert names == {"escalate-declined"}  # no ESCALATED, no SKIP for a built ticket


# ---- a stopped session and the wave check ---------------------------------------


def test_a_stopped_ticket_whose_wave_check_passes_never_escalates(tmp_path: Path) -> None:
    local = _Session(
        stop={"T-001": TURN_CAP},
        writes={
            "T-001": {"app.py": "def one():\n    return 1\n"},
            "T-002": {"app.py": "def two():\n    return 2\n"},
        },
    )
    cloud = _Session()
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    assert ran == ["engineering", "wave_gate", "engineering"]
    assert cloud.tiers == []
    assert _builds(out) == [GateStatus.PASS]
    assert stopped_tickets(out) == {"T-001": TURN_CAP}
    assert _escalated(out) == {}
    assert not [t for t in out["gate_triage"] if t.name.startswith("escalate")]
    # T-002 built on T-001's kept local work.
    assert local.seen["T-002"]["app.py"] == "def one():\n    return 1\n"


def test_a_stopped_ticket_escalates_on_the_first_failing_wave_check(tmp_path: Path) -> None:
    local = _Session(
        stop={"T-001": TURN_CAP},
        writes={
            "T-001": {"app.py": "def broken(:\n"},
            "T-002": {"app.py": "def two():\n    return 2\n"},
        },
    )
    cloud = _Session(writes={"T-001": {"app.py": "def one():\n    return 1\n"}})
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    # No fixer pass first: the failing check escalates straight away.
    assert ran == ["engineering", "wave_gate", "engineering", "wave_gate", "engineering"]
    assert cloud.tiers == [("T-001", "T1")]
    assert _builds(out) == [GateStatus.FAIL, GateStatus.PASS]
    context = cloud.packets["T-001"].prior_failures
    assert context[0] == "[escalation]"
    assert any("stopped early" in line and TURN_CAP[:30] in line for line in context)
    assert any("holds that attempt" in line for line in context)
    assert any(
        line.startswith("the earlier attempt touched:") and "app.py" in line for line in context
    )
    assert "failed gates: build" in context
    assert cloud.seen["T-001"]["app.py"] == "def broken(:\n"  # built on the kept work
    assert set(_escalated(out)) == {"task/T-001"}
    assert len([t for t in out["gate_triage"] if t.name == "escalate-request"]) == 1


def test_a_stopped_sessions_digest_reaches_its_escalation_whole(tmp_path: Path) -> None:
    digest = (
        f"{STOPPED_DIGEST} the session ended before done; files written: app.py; "
        "files read: app.py, tests/test_app.py; last check: run 'pytest -q': FAILED: exit 1 "
        "| E   AssertionError: assert 2 == 1; last actions: "
        + " -> ".join(["read_file app.py"] * 5)
        + "; tail-marker"
    )
    assert len(digest) > 300
    local = _Session(
        stop={"T-001": TURN_CAP},
        digest={"T-001": digest},
        writes={
            "T-001": {"app.py": "def broken(:\n"},
            "T-002": {"app.py": "def two():\n    return 2\n"},
        },
    )
    cloud = _Session(writes={"T-001": {"app.py": "def one():\n    return 1\n"}})
    _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    context = cloud.packets["T-001"].prior_failures
    # The stop note and the digest are separate records: neither hides the other.
    assert f"the earlier attempt reported: {digest}" in context
    assert any("reported: " + TURN_CAP[:30] in line for line in context)


def test_a_finished_session_supersedes_its_tasks_stopped_digest(tmp_path: Path) -> None:
    digest = f"{STOPPED_DIGEST} the session ended before done; files written: app.py"
    local = _Session(
        stop={"T-001": TURN_CAP},
        digest={"T-001": digest},
        writes={
            "T-001": {"app.py": "def broken(:\n"},
            "T-002": {"app.py": "def two():\n    return 2\n"},
        },
    )
    cloud = _Session(writes={"T-001": {"app.py": "def one():\n    return 1\n"}})
    out, _ran, _store = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    assert f"the earlier attempt reported: {digest}" in cloud.packets["T-001"].prior_failures
    # The escalated session ended with done: a later escalation or restart of T-001
    # reads no digest, and the stopped session's record is not left on the channel.
    assert "T-001" not in session_digests(out)
    assert not any(t.explanation == digest for t in out["gate_triage"])


def test_a_final_wave_stop_keeps_its_work_and_never_escalates(tmp_path: Path) -> None:
    # No wave check runs after the last wave: the supervisor's gates (and its cloud
    # fixer, on T1 in hybrid) judge the kept work.
    local = _Session(
        stop={"T-002": TURN_CAP},
        writes={
            "T-001": {"app.py": "def one():\n    return 1\n"},
            "T-002": {"app.py": "def two(:\n"},
        },
    )
    cloud = _Session()
    out, ran, store = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    assert ran == ["engineering", "wave_gate", "engineering"]
    assert cloud.tiers == []
    assert _escalated(out) == {}
    assert stopped_tickets(out) == {"T-002": TURN_CAP}
    assert (_local_tree(out, store, tmp_path) / "app.py").read_text() == "def two(:\n"


def test_the_stop_marker_survives_a_resume(tmp_path: Path) -> None:
    # Wave 1 builds and the process ends before its check; a new process resuming
    # the checkpoint re-runs that check, which still knows T-001 stopped early.
    store = ArtifactStore()
    local = _Session(stop={"T-001": TURN_CAP}, writes={"T-001": {"app.py": "def broken(:\n"}})
    first_cloud = _Session()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "README.md").write_text("base\n")
    node = make_engineering_node(
        _runtime(_tiered(first_cloud, local), store),
        source_dir=str(tmp_path / "src"),
        resolve=store.resolve,
        resolve_blob=store.resolve_blob,
    )
    built = node(_state(store, _two_waves()), _config())
    assert stopped_tickets(built) == {"T-001": TURN_CAP}  # type: ignore[arg-type]
    assert first_cloud.tiers == []

    cloud = _Session(writes={"T-001": {"app.py": "def one():\n    return 1\n"}})
    out, ran, _ = _wave_run(
        tmp_path,
        _tiered(cloud, _Session()),
        tickets=_two_waves(),
        store=store,  # type: ignore[arg-type]
        artifacts=built["artifacts"],
        gate_triage=built["gate_triage"],
        tasks=built["tasks"],
        phase="resume",
    )
    assert ran == ["wave_gate", "engineering", "wave_gate", "engineering"]
    assert cloud.tiers == [("T-001", "T1")]
    assert set(_escalated(out)) == {"task/T-001"}


def test_a_first_failure_picks_only_named_stopped_tickets() -> None:
    store = ArtifactStore()
    stop = GateTriage(
        name="stopped-early",
        run_against="task/T-001",
        explanation=TURN_CAP,
        produced_by="engineering",
        seq=Seq("run", 1, 1, "e", 1),
    )

    def first(*findings: str, tickets: list[Ticket] | None = None) -> tuple[str, ...]:
        state = _check_state(store, tickets or _pair(), *findings)
        state = {**state, "gate_triage": [*state["gate_triage"], stop]}
        return _candidates(state, store, stopped_only=True)

    assert first("app.py:1: error") == ("T-001",)
    assert first("webapp.py:3: error") == ()  # T-002 did not stop: it waits for a second
    assert first("SyntaxError: invalid syntax") == ()  # unnamed, two tickets in the wave
    # Alone in its wave, a stopped ticket escalates whatever the failure says.
    assert first("SyntaxError", tickets=[_ticket("T-001", 0, files=("app.py",))]) == ("T-001",)


def test_an_empty_session_error_carries_the_adapter_report(tmp_path: Path) -> None:
    from foundry.agents.implementation.tickets import EmptyTicketError, _require_scope_written

    src = tmp_path / "src"
    src.mkdir()
    with Sandbox(src) as sb:
        blob = b'{"kind": "full-file-snapshot", "files": {}}'
        note = GateTriage(
            name="adapter-report",
            run_against="task/T-001",
            explanation="claude reported is_error (subtype=error_max_turns, num_turns=40): None",
            produced_by="claude-code",
            seq=Seq("r", 0, 0, "n", 0),
        )
        result = AdapterResult(
            diff_uri=sb.put_blob(blob), triage=(note,), turns_used=40, snapshot_digest=sb.snapshot()
        )
        with pytest.raises(EmptyTicketError) as err:
            _require_scope_written(result, sb, ("go.mod",), "T-001")
    msg = str(err.value)
    assert "wrote none of the ticket's files (go.mod)" in msg
    assert "after 40 turn(s)" in msg and "subtype=error_max_turns" in msg


def test_a_later_finished_session_retires_a_stopped_early_note() -> None:
    # T-001's local session stopped early (its own diff, then the stop note); a
    # later session for T-001 (a restart, an escalation, a rebuild) finished. The
    # stale stop must not make the next first failure escalate T-001.
    store = ArtifactStore()
    stop = GateTriage(
        name="stopped-early",
        run_against="task/T-001",
        explanation=TURN_CAP,
        produced_by="engineering",
        seq=Seq("run", 1, 1, "e", 1),
    )

    def diff(task_id: str, step: int) -> Artifact:
        return Artifact(
            kind="feature_diff",
            uri=f"cas://feature_diff/{task_id}-{step}",
            produced_by="junior_engineer",
            task_id=task_id,
            seq=Seq("run", 1, step, "e", 0),
        )

    def first(*later: Artifact) -> tuple[dict[str, str], tuple[str, ...]]:
        state = _check_state(store, _pair(), "app.py:1: error")
        state = {
            **state,
            "artifacts": [*state["artifacts"], *later],
            "gate_triage": [*state["gate_triage"], stop],
        }
        return stopped_tickets(state), _candidates(state, store, stopped_only=True)

    assert first() == ({"T-001": TURN_CAP}, ("T-001",))
    assert first(diff("T-001", 3)) == ({}, ())
    assert first(diff("restart-T-001", 3)) == ({}, ())
    # Another ticket's newer diff leaves T-001's stop standing.
    assert first(diff("T-002", 3)) == ({"T-001": TURN_CAP}, ("T-001",))
