"""Escalation cascade (Phase 5): in hybrid mode a local ticket that can't finish —
its session hit the turn cap, the repeat/read-thrash detector stopped it, or its
wave check failed twice — re-runs once on the cloud level, with what was tried.
Cloud and local modes never escalate."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from foundry.agents import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.tiered import ESCALATED_TRIAGE, TieredAdapter
from foundry.execution.sandbox import Sandbox
from foundry.graph.gate_wiring import materialize_workdir
from foundry.modes import escalation_tier, preset_levels
from foundry.proxy.model_provider_proxy import TIER_MODELS
from foundry.proxy.token_meter import TokenMeter
from foundry.schemas.gates import GateStatus, GateTriage
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
REPEAT = ("stopped early: identical action repeated 5 times (run 'pytest') — "
          "partial work recorded")
THRASH = "stopped early: read app.py 6 times without changing it — partial work recorded"


class _Session(_Recorder):
    """A ``_Recorder`` whose session reports ``stop[tid]`` the first ``times`` runs,
    the way proxy-agent reports a turn cap or an early stop, and can make one model
    call per session through ``proxy`` at the packet's tier."""

    def __init__(self, *, stop: dict[str, str] | None = None, times: int = 99,
                 proxy: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.stop = dict(stop or {})
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
            note = GateTriage(name="adapter-report", run_against=f"task/{tid}",
                              explanation=self.stop[tid], produced_by="proxy-agent",
                              seq=Seq("run", 0, 0, "proxy-agent", 0))
            result = result.model_copy(update={"triage": (note,)})
        return result


def _tiered(cloud: Any, local: Any, *, target: str | None = "T1") -> TieredAdapter:
    adapter = TieredAdapter(cloud=cloud, local=local, local_tiers={"T2", "T3"},
                            local_slot=threading.RLock, escalation_tier=target)
    adapter.name = "recorder"  # type: ignore[misc]
    return adapter


def _build(tmp_path: Path, adapter: Any, tickets: list[Ticket]) -> FactoryState:
    store = ArtifactStore()
    out: FactoryState = _graph(_runtime(adapter, store), store, tmp_path).compile().invoke(
        _state(store, tickets), _config())
    return out


def _escalated(out: FactoryState) -> dict[str, str]:
    return {t.run_against: t.explanation for t in out.get("gate_triage") or ()
            if t.name == ESCALATED_TRIAGE}


# ---- which modes escalate ----------------------------------------------------------


def test_only_a_mixed_level_table_has_an_escalation_tier() -> None:
    def table(mode: str) -> Any:
        return preset_levels(mode, cloud_backend="mock", cloud_models=TIER_MODELS)

    assert escalation_tier(table("hybrid")) == "T1"
    assert escalation_tier(table("cloud")) is None
    assert escalation_tier(table("local")) is None


# ---- in-session triggers: turn cap, early stop ---------------------------------


@pytest.mark.parametrize("reason", [TURN_CAP, REPEAT, THRASH])
def test_a_local_session_that_cannot_finish_reruns_once_on_the_cloud(
    tmp_path: Path, reason: str,
) -> None:
    local = _Session(stop={"T-001": reason}, writes={"T-001": {"a.py": "local\n"}})
    cloud = _Session(writes={"T-001": {"a.py": "cloud\n"}})
    out = _build(tmp_path, _tiered(cloud, local),
                 [_ticket("T-001", 0, files=("a.py",)), _ticket("T-002", 1, files=("b.py",))])
    assert sorted(local.tiers) == [("T-001", "T2"), ("T-002", "T2")]
    assert cloud.tiers == [("T-001", "T1")]  # only the stopped ticket, on the cloud level
    context = cloud.packets["T-001"].prior_failures
    assert context[0] == "[escalation]"
    assert any(reason[:40] in line and "T2" in line for line in context)
    assert any(line.startswith("the earlier attempt touched:") and "a.py" in line
               for line in context)
    assert any(line.startswith("the earlier attempt reported:") for line in context)
    # The cloud attempt's diff replaces the local one.
    assert set(_diffs(out)) == {"T-001", "T-002"}
    assert "from T2 to T1" in _escalated(out)["task/T-001"]
    assert set(_escalated(out)) == {"task/T-001"}
    concerns = [c for j in out["agent_journal"] if j.node_id == "engineering"
                for c in j.concerns]
    assert any(c.startswith("T-001 escalated from T2 to T1") for c in concerns)


def test_the_escalated_diff_is_the_one_integrated(tmp_path: Path) -> None:
    store = ArtifactStore()
    local = _Session(stop={"T-001": TURN_CAP}, writes={"T-001": {"a.py": "local\n"}})
    cloud = _Session(writes={"T-001": {"a.py": "cloud\n"}})
    out = _graph(_runtime(_tiered(cloud, local), store), store, tmp_path).compile().invoke(
        _state(store, [_ticket("T-001", 0, files=("a.py",))]), _config())
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "a.py").read_text() == "cloud\n"


def test_a_ticket_escalates_at_most_once(tmp_path: Path) -> None:
    # The cloud attempt crashes; the retry runs locally, stops again, and is kept
    # as it is rather than escalating a second time.
    local = _Session(stop={"T-001": TURN_CAP})
    cloud = _Session(fail_on={"T-001": 1})
    out = _build(tmp_path, _tiered(cloud, local), [_ticket("T-001", 0, files=("a.py",))])
    assert cloud.calls == ["T-001!"]
    assert local.tiers == [("T-001", "T2"), ("T-001", "T2")]
    assert set(_diffs(out)) == {"T-001"}
    assert len([t for t in out["gate_triage"] if t.name == ESCALATED_TRIAGE]) == 1


def test_a_cloud_ticket_and_a_finished_local_ticket_never_escalate(tmp_path: Path) -> None:
    from foundry.schemas.implementation import Difficulty

    local = _Session()
    cloud = _Session(stop={"T-002": TURN_CAP})  # a cloud session stopping stays as is
    out = _build(tmp_path, _tiered(cloud, local), [
        _ticket("T-001", 0, files=("a.py",)),
        _ticket("T-002", 1, files=("b.py",), difficulty=Difficulty.COMPLEX),
    ])
    assert local.tiers == [("T-001", "T2")]
    assert cloud.tiers == [("T-002", "T1")]
    assert _escalated(out) == {}


def test_local_mode_never_escalates(tmp_path: Path) -> None:
    # Local mode's table has no cloud level: the adapter declares no escalation tier.
    local = _Session(stop={"T-001": TURN_CAP})
    out = _build(tmp_path, _tiered(local, local, target=None),
                 [_ticket("T-001", 0, files=("a.py",))])
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
    local = _Session(stop={"T-001": TURN_CAP}, proxy=proxy)
    cloud = _Session(proxy=proxy)
    store = ArtifactStore()
    rt = AgentRuntime(proxy=proxy, adapter=_tiered(cloud, local), cas=store)
    _graph(rt, store, tmp_path).compile().invoke(
        _state(store, [_ticket("T-001", 0, files=("a.py",))]), _config())
    assert len(local_tx.requests) == 1 and len(cloud_tx.requests) == 1
    assert cloud_tx.requests[0]["model"] == TIER_MODELS["T1"]
    assert proxy.token_meter.used == 110  # the cloud call only; the local one is free


# ---- the wave-check trigger: two failing checks in a row ------------------------


def _two_waves() -> list[Ticket]:
    return [_ticket("T-001", 0, files=("app.py",)),
            _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]


def test_a_wave_check_that_fails_twice_reruns_its_local_ticket_on_the_cloud(
    tmp_path: Path,
) -> None:
    local = _Session(writes={"T-001": {"app.py": "def broken(:\n"},
                             "T-002": {"app.py": "def two():\n    return 2\n"}})
    cloud = _Session(writes={"fix-1": {"app.py": "def broken_1(:\n"},
                             "T-001": {"app.py": "def one():\n    return 1\n"}})
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate",
                   "engineering", "wave_gate", "engineering"]
    assert cloud.tiers == [("fix-1", None), ("T-001", "T1")]
    assert _builds(out) == [GateStatus.FAIL, GateStatus.FAIL, GateStatus.PASS]
    context = cloud.packets["T-001"].prior_failures
    assert context[0] == "[escalation]"
    assert "[fix summary]" in context and "failed gates: build" in context
    assert any("twice" in line for line in context)
    assert any(line.startswith("the earlier attempt touched:") and "app.py" in line
               for line in context)
    assert set(_escalated(out)) == {"task/T-001"}
    assert cloud.seen["T-001"]["app.py"] == "def broken_1(:\n"  # built on the fix


def test_a_wave_check_escalation_happens_once(tmp_path: Path) -> None:
    # The cloud rebuild fails the check too: no second request, the bound decides.
    local = _Session(writes={"T-001": {"app.py": "def broken(:\n"}})
    cloud = _Session(writes={"fix-1": {"app.py": "def broken_1(:\n"},
                             "T-001": {"app.py": "def broken_2(:\n"},
                             "fix-2": {"app.py": "def broken_3(:\n"}})
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local), tickets=_two_waves())  # type: ignore[arg-type]
    assert [t for t, _ in cloud.tiers].count("T-001") == 1
    assert len([t for t in out["gate_triage"] if t.name == "escalate-request"]) == 1
    # The rebuild spent the wave's last fix attempt: the third failing check halts.
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "engineering", "wave_gate"]
    assert "T-002" not in [t for t, _ in local.tiers]


def test_without_an_escalation_tier_a_twice_failing_check_never_escalates(
    tmp_path: Path,
) -> None:
    local = _Session(writes={"T-001": {"app.py": "def broken(:\n"}})
    cloud = _Session(writes={"fix-1": {"app.py": "def broken_1(:\n"},
                             "fix-2": {"app.py": "def broken_2(:\n"}})
    out, ran, _ = _wave_run(tmp_path, _tiered(cloud, local, target=None),  # type: ignore[arg-type]
                            tickets=_two_waves())
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate"]
    assert not [t for t in out["gate_triage"] if t.name.startswith("escalate")]
