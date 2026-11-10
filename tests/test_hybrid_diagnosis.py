"""Hybrid mode: one cloud diagnosis call before a local repair attempt (a fix pass
or a local ticket's retry), applied by the local model. Cloud and local modes,
and FOUNDRY_HYBRID_DIAGNOSE=0, make no such call."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

import tests.test_ticket_scheduler as sched
from foundry import telemetry
from foundry.agents import AgentRuntime
from foundry.agents.implementation import diagnosis
from foundry.agents.implementation.diagnosis import DIAGNOSE_ENV
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.tiered import TieredAdapter
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy.model_provider_proxy import ModelProviderProxy, ProxyUnavailable
from foundry.proxy.test_doubles import MockTransport
from foundry.state.factory_state import FactoryState
from tests.test_ticket_scheduler import _Recorder, _ticket, _wave_run

_DIAGNOSIS = {
    "root_cause": "app.py has a syntax error in broken().",
    "files_to_edit": ["app.py", "nope.py", "/etc/passwd"],
    "edits": [
        {"file": "app.py", "symbol": "broken", "change": "close the parameter list"},
        {"file": "nope.py", "symbol": "x", "change": "invent a file"},
    ],
    "do_not": ["rewrite the module"],
}
_HEAD = "DIAGNOSIS FROM A REVIEWER"


class _Calls:
    """The runtime's proxy, plus what diagnosis calls it saw."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
        self.transport = MockTransport.scripted(
            {"diagnosing a failed build": json.dumps(_DIAGNOSIS)}, default_text="{}"
        )
        self.fail = fail

        def runtime(adapter: Any, store: ArtifactStore) -> AgentRuntime:
            proxy = ModelProviderProxy(self.transport, signer=ZoneBSigner(b"k" * 16, "kid"))
            if self.fail:

                def down(*_a: Any, **_k: Any) -> Any:
                    raise ProxyUnavailable("cloud down")

                proxy.complete = down  # type: ignore[method-assign]
            return AgentRuntime(proxy=proxy, adapter=adapter, cas=store)

        monkeypatch.setattr(sched, "_runtime", runtime)

    def diagnoses(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.transport.requests
            if "diagnosing a failed build" in json.dumps(r.get("system"))
        ]


class _HybridLocalFixer(_Recorder):
    """Hybrid (a cloud escalation tier exists) with every pass routed to a local tier."""

    escalation_tier = "T1"

    def is_local(self, tier: str | None) -> bool:
        return True


class _HybridCloudFixer(_HybridLocalFixer):
    def is_local(self, tier: str | None) -> bool:
        return tier is not None  # a tierless fix pass runs on T1, the cloud level


class _LocalOnly(_Recorder):
    def is_local(self, tier: str | None) -> bool:
        return True


_BROKEN = {"T-001": {"app.py": "def broken(:\n"}}


def _journal(out: FactoryState) -> str:
    return json.dumps([j.model_dump(mode="json") for j in out.get("agent_journal") or ()])


def test_a_local_fix_pass_in_hybrid_gets_the_cloud_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(monkeypatch)
    rec = _HybridLocalFixer(writes=_BROKEN)
    with telemetry.counting() as counts:
        out, _, _ = _wave_run(tmp_path, rec)
    got = rec.packets["fix-1"].task["diagnosis"]
    assert got["advisory"].startswith("Advisory:")
    assert got["cause"] == "app.py has a syntax error in broken()."
    assert got["files"] == ["app.py"]  # nope.py and /etc/passwd are not in the tree
    assert got["change"] == ["app.py `broken`: close the parameter list"]
    assert got["do_not"] == ["rewrite the module"]
    assert "nope.py" not in json.dumps(got)
    assert "outside_fix_files" not in got  # no planned cluster: no fence to stay inside
    assert not any(line.startswith(_HEAD) for line in rec.packets["fix-1"].prior_failures)
    assert counts.snapshot().get("fix.diagnosis.used", 0) >= 1
    assert "fix.diagnosis.failed" not in counts.snapshot()
    sent = calls.diagnoses()
    prompts = [json.dumps(r["messages"]) for r in sent]
    assert "ATTEMPT: fix-1 (fix attempt 1)" in prompts[0]
    assert len(set(prompts)) == len(prompts)  # each pass is its own cache key
    assert (
        sent
        and sent[0]["model"]
        == ModelProviderProxy(calls.transport, signer=ZoneBSigner(b"k" * 16, "kid")).tier_models[
            "T1"
        ]
    )
    assert "current content of app.py" in json.dumps(sent[0]["messages"])
    assert "diagnosis from T1" in _journal(out)


def test_an_unavailable_cloud_leaves_the_fix_pass_as_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Calls(monkeypatch, fail=True)
    rec = _HybridLocalFixer(writes=_BROKEN)
    with telemetry.counting() as counts:
        _wave_run(tmp_path, rec)
    assert "fix-1" in rec.packets
    assert all("diagnosis" not in p.task for p in rec.packets.values())
    assert not any(line.startswith(_HEAD) for line in rec.packets["fix-1"].prior_failures)
    assert counts.snapshot().get("fix.diagnosis.failed") == 1
    assert "fix.diagnosis.used" not in counts.snapshot()


def test_a_failing_cloud_is_logged_once_per_run_and_counted_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = ModelProviderProxy(MockTransport.scripted({}), signer=ZoneBSigner(b"k" * 16, "kid"))

    def down(*_a: Any, **_k: Any) -> Any:
        raise ProxyUnavailable("cloud down")

    proxy.complete = down  # type: ignore[method-assign]
    rt = AgentRuntime(proxy=proxy)
    logged: list[str] = []
    monkeypatch.setattr(diagnosis.progress, "log", lambda msg, **_k: logged.append(msg))
    with telemetry.counting() as counts:
        for n in (1, 2):
            assert (
                diagnosis.diagnose(rt, "T1", label=f"fix-{n}", summary=("x",), files=(), tree={})
                is None
            )
    assert counts.snapshot() == {"fix.diagnosis.failed": 2}
    assert len(logged) == 1 and "fix-1" in logged[0]


def test_files_outside_the_fence_are_flagged_not_added() -> None:
    found = diagnosis.Diagnosis(
        root_cause="the route returns 500",
        files_to_edit=("app.py", "admin.py"),
        edits=(("app.py", "app.py: fix it"), ("models.py", "models.py: add a field")),
        do_not=(),
    )
    got = found.packet_field(("app.py", "tests/test_app.py"))
    assert got["files"] == ["app.py"]
    assert got["change"] == ["app.py: fix it", "models.py: add a field"]
    assert got["outside_fix_files"]["files"] == ["admin.py", "models.py"]
    assert "reason" in got["outside_fix_files"]["note"]
    assert "outside_fix_files" not in found.packet_field(())


def test_the_env_switch_turns_it_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DIAGNOSE_ENV, "0")
    calls = _Calls(monkeypatch)
    rec = _HybridLocalFixer(writes=_BROKEN)
    _wave_run(tmp_path, rec)
    assert not calls.diagnoses()
    assert "diagnosis" not in rec.packets["fix-1"].task


@pytest.mark.parametrize(
    "adapter",
    [_Recorder, _LocalOnly, _HybridCloudFixer],
    ids=["cloud-mode", "local-mode", "hybrid-cloud-fix"],
)
def test_no_diagnosis_without_a_local_attempt_in_hybrid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: type[_Recorder],
) -> None:
    calls = _Calls(monkeypatch)
    rec = adapter(writes=_BROKEN)
    _wave_run(tmp_path, rec)
    assert "fix-1" in rec.packets
    assert not calls.diagnoses()
    assert "diagnosis" not in rec.packets["fix-1"].task


def test_a_local_ticket_retry_in_hybrid_gets_the_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(monkeypatch)
    local = _Recorder(fail_on={"T-001": 1})
    cloud = _Recorder()
    adapter = TieredAdapter(
        cloud=cloud,
        local=local,
        local_tiers={"T2", "T3"},
        local_slot=threading.RLock,
        escalation_tier="T1",
    )
    adapter.name = "recorder"  # type: ignore[misc]
    store = ArtifactStore()
    rt = sched._runtime(adapter, store)
    out: FactoryState = (
        sched._graph(rt, store, tmp_path)
        .compile()
        .invoke(sched._state(store, [_ticket("T-001", 0, files=("app.py",))]), sched._config())
    )
    retry = local.packets["T-001"].prior_failures
    assert retry[0].startswith(_HEAD)
    # app.py is the ticket's own file: allowed though the failed attempt never wrote it.
    assert "files to edit: app.py" in retry
    assert any(line.startswith("attempt 1 failed") for line in retry)
    assert len(calls.diagnoses()) == 1
    assert "T-001 retry diagnosed on T1" in _journal(out)
