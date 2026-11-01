"""Model modes — Cloud · Hybrid · Local (Phase 4b): per-level routing in the proxy,
per-ticket substrate, the single local slot, local structured output, the
wall-clock cap and the local server check. No network: local servers are fakes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import foundry.cli as cli
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.execution.adapters.proxy_agent import ACTION_SCHEMA, ProxyAgentAdapter
from foundry.execution.adapters.tiered import TieredAdapter
from foundry.gates.attestation import ZoneBSigner
from foundry.modes import (
    DEFAULT_FIX_ATTEMPTS,
    Level,
    build_level_proxy,
    parse_levels,
    preset_levels,
    resolve_levels,
)
from foundry.proxy import ModelProviderProxy, ProviderResponse
from foundry.proxy import token_meter as tm
from foundry.proxy.local_transport import (
    DEFAULT_LOCAL_MODEL,
    LocalOpenAITransport,
    check_local_server,
)
from foundry.proxy.model_provider_proxy import TIER_MODELS
from foundry.proxy.test_doubles import MockTransport
from foundry.proxy.token_meter import TimeCapExceeded, TokenMeter

SIGNER = ZoneBSigner(b"k" * 16, "kid")
CALL = ("node", "SOP", [{"role": "user", "content": "x"}])
USAGE = {"input_tokens": 100, "output_tokens": 10}


class RecordingTransport:
    """A fake egress that records requests and measures its own concurrency."""

    def __init__(self, text: str = "ok", delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        with self._lock:
            self.requests.append(request)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
            return ProviderResponse(served_model=request["model"], request_id="r",
                                    text=self.text, usage=dict(USAGE), stop_reason="stop")
        finally:
            with self._lock:
                self.in_flight -= 1


def _hybrid(cloud: Any, local: Any) -> ModelProviderProxy:
    levels = preset_levels("hybrid", cloud_backend="mock", cloud_models=TIER_MODELS)
    return build_level_proxy(levels, cloud_transport=lambda _b: cloud,
                             local_transport=lambda _u: local, signer=SIGNER)


# ---- presets + level tables ---------------------------------------------------


def test_presets_put_the_levels_where_the_plan_says() -> None:
    kw: dict[str, Any] = {"cloud_backend": "claude-cli", "cloud_models": TIER_MODELS}
    assert all(not lvl.local for lvl in preset_levels("cloud", **kw).values())
    hybrid = preset_levels("hybrid", **kw)
    assert hybrid["T1"] == Level("claude-cli", TIER_MODELS["T1"])
    assert hybrid["T2"].local and hybrid["T3"].local
    assert hybrid["T2"].model == DEFAULT_LOCAL_MODEL
    assert hybrid["T2"].base_url == "http://localhost:1234/v1"
    assert all(lvl.local for lvl in preset_levels("local", **kw).values())
    assert DEFAULT_FIX_ATTEMPTS == {"cloud": 3, "hybrid": 4, "local": 5}


def test_no_mode_and_cloud_mode_keep_todays_routing() -> None:
    kw: dict[str, Any] = {"levels": None, "backend": "claude-cli",
                          "cloud_models": TIER_MODELS, "cloud_backends": cli.BACKENDS}
    assert resolve_levels(model_mode=None, **kw) is None
    assert resolve_levels(model_mode="cloud", **kw) is None
    with pytest.raises(ValueError, match="unknown model mode"):
        resolve_levels(model_mode="orbit", **kw)


def test_an_explicit_table_overrides_and_omitted_tiers_run_on_the_backend() -> None:
    table = resolve_levels(
        model_mode="hybrid", backend="claude-cli", cloud_models=TIER_MODELS,
        cloud_backends=cli.BACKENDS,
        levels={"T3": {"backend": "local", "model": "qwen/qwen3.6-27b",
                       "base_url": "http://box:1234/v1"}},
    )
    assert table is not None
    assert table["T3"] == Level("local", "qwen/qwen3.6-27b", "http://box:1234/v1")
    assert table["T1"] == Level("claude-cli", TIER_MODELS["T1"])
    assert table["T2"] == Level("claude-cli", TIER_MODELS["T2"])


@pytest.mark.parametrize("raw, message", [
    ({"T4": {"backend": "local", "model": "m"}}, "unknown tier"),
    ({"T1": {"backend": "bogus", "model": "m"}}, "unknown backend"),
    ({"T1": {"backend": "local", "model": "m; rm -rf /"}}, "invalid model id"),
    ({"T1": {"backend": "local", "model": "m", "base_url": "file:///etc/passwd"}}, "http"),
])
def test_level_tables_are_validated_at_the_boundary(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_levels(raw, cloud_backends=cli.BACKENDS)


# ---- per-level routing in the proxy ---------------------------------------------


def test_each_tier_routes_to_its_levels_transport() -> None:
    cloud, local = RecordingTransport(), RecordingTransport()
    proxy = _hybrid(cloud, local)
    proxy.complete(CALL[0], "T1", *CALL[1:], purpose="ideation")
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation")
    proxy.complete(CALL[0], "T3", *CALL[1:], purpose="gate")
    assert [r["model"] for r in cloud.requests] == [TIER_MODELS["T1"]]
    assert [r["model"] for r in local.requests] == [DEFAULT_LOCAL_MODEL] * 2
    assert [rec.get("local", False) for rec in proxy.manifest] == [False, True, True]
    # The default local model resolves to a curated lineage, not a guess.
    assert proxy.manifest[1]["served_family"] == ["alibaba-qwen"]


def test_a_single_backend_proxy_is_unchanged() -> None:
    transport = RecordingTransport()
    proxy = ModelProviderProxy(transport, signer=SIGNER)
    for tier in ("T1", "T2", "T3"):
        proxy.complete(CALL[0], tier, *CALL[1:], purpose="ideation")
    assert len(transport.requests) == 3
    assert proxy.local_tiers == frozenset()
    assert all("local" not in rec for rec in proxy.manifest)


def test_an_unknown_local_model_is_pinned_to_its_own_family() -> None:
    local = RecordingTransport()
    levels = {t: Level("local", "acme-coder-7b", "http://localhost:1234/v1")
              for t in ("T1", "T2", "T3")}
    proxy = build_level_proxy(levels, cloud_transport=lambda _b: RecordingTransport(),
                              local_transport=lambda _u: local, signer=SIGNER)
    proxy.complete(CALL[0], "T1", *CALL[1:], purpose="ideation")
    assert proxy.manifest[0]["served_family"] == ["local:acme-coder-7b"]


def test_the_token_meter_counts_cloud_tokens_only() -> None:
    proxy = _hybrid(RecordingTransport(), RecordingTransport())
    proxy.token_meter = TokenMeter(10_000)
    proxy.complete(CALL[0], "T1", *CALL[1:], purpose="ideation")
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation")
    proxy.complete(CALL[0], "T3", *CALL[1:], purpose="ideation")
    assert proxy.token_meter.used == 110


# ---- local scheduling --------------------------------------------------------------


def test_at_most_one_local_call_in_flight_while_cloud_calls_overlap() -> None:
    cloud, local = RecordingTransport(delay=0.05), RecordingTransport(delay=0.02)
    proxy = _hybrid(cloud, local)

    def call(tier: str) -> None:
        proxy.complete(CALL[0], tier, *CALL[1:], purpose="ideation")

    threads = [threading.Thread(target=call, args=(t,))
               for t in ["T2", "T3"] * 4 + ["T1"] * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(local.requests) == 8
    assert local.max_in_flight == 1
    assert cloud.max_in_flight > 1


def test_a_local_session_holds_the_slot_for_its_whole_run(tmp_path: Path) -> None:
    local = RecordingTransport(delay=0.0)
    proxy = _hybrid(RecordingTransport(), local)
    sessions_in_flight: list[int] = []
    active = [0]
    guard = threading.Lock()

    class Session:
        supports_turn_snapshots = True

        def run(self, packet: TaskPacket, sandbox: Any) -> AdapterResult:
            with guard:
                active[0] += 1
                sessions_in_flight.append(active[0])
            # The session's own calls re-enter the slot it holds.
            for _ in range(3):
                proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation")
                time.sleep(0.01)
            with guard:
                active[0] -= 1
            return AdapterResult(diff_uri="cas://x", triage=(), turns_used=1, snapshot_digest="s")

    adapter = TieredAdapter(cloud=Session(), local=Session(), local_tiers={"T2", "T3"},
                            local_slot=proxy.local_slot)
    packet = TaskPacket(task={"id": "t", "tier": "T2"}, worktree_path=str(tmp_path),
                        turn_budget=1, cost_budget_microusd=1)
    threads = [threading.Thread(target=adapter.run, args=(packet, None)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(sessions_in_flight) == 1
    assert len(local.requests) == 12


# ---- per-ticket substrate ----------------------------------------------------------


class _Named:
    supports_turn_snapshots = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.ran: list[str | None] = []

    def available(self) -> bool:
        return True

    def run(self, packet: TaskPacket, sandbox: Any) -> AdapterResult:
        self.ran.append(packet.task.get("tier"))
        return AdapterResult(diff_uri="cas://x", triage=(), turns_used=1, snapshot_digest="s")


def test_hybrid_runs_cloud_tickets_on_the_cloud_adapter_and_local_ones_locally(
    tmp_path: Path,
) -> None:
    cloud, local = _Named("cloud"), _Named("local")
    adapter = TieredAdapter(cloud=cloud, local=local, local_tiers={"T2", "T3"},
                            local_slot=threading.RLock)
    for tier in ("T1", "T2", "T3", None):
        task: dict[str, Any] = {"id": "t"} | ({"tier": tier} if tier else {})
        adapter.run(TaskPacket(task=task, worktree_path=str(tmp_path), turn_budget=1,
                               cost_budget_microusd=1), None)  # type: ignore[arg-type]
    assert cloud.ran == ["T1", None]  # no tier (the fixer) runs on T1
    assert local.ran == ["T2", "T3"]


def test_a_local_sessions_sandbox_gets_no_cloud_credential(tmp_path: Path) -> None:
    from foundry.agents.implementation.junior_engineer import _sandbox_for

    adapter = TieredAdapter(cloud=ClaudeCodeAdapter(),
                            local=ProxyAgentAdapter(complete_fn=lambda m, s: ("", None)),
                            local_tiers={"T2", "T3"}, local_slot=threading.RLock)
    rt = SimpleNamespace(adapter=adapter)
    local_box = _sandbox_for(rt, str(tmp_path), None, "T3")  # type: ignore[arg-type]
    cloud_box = _sandbox_for(rt, str(tmp_path), None, "T1")  # type: ignore[arg-type]
    try:
        assert local_box._credential_grant == ()
        assert local_box._home_passthrough is False
        assert cloud_box._credential_grant == ClaudeCodeAdapter.credential_grant
    finally:
        local_box.destroy()
        cloud_box.destroy()


class FakeLocalServer:
    """An OpenAI-compatible local server stand-in: scripted planner/verdicts like
    the mock backend, and a proxy-agent loop that finishes on its first turn."""

    def __init__(self) -> None:
        self.mock = MockTransport.scripted(dict(cli._SCRIPTS), default_verdict="approve")
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.requests.append(request)
        schema = request.get("output_config", {}).get("format", {}).get("schema")
        if schema == ACTION_SCHEMA:
            return ProviderResponse(served_model=request["model"], request_id="r",
                                    text=json.dumps({"action": "done", "summary": "ok"}),
                                    usage=dict(USAGE), stop_reason="stop")
        return self.mock.send(request)


def test_a_hybrid_run_routes_tickets_and_calls_by_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeLocalServer()

    def _local(url: str) -> FakeLocalServer:
        server.urls.append(url)
        return server

    monkeypatch.setattr(cli, "_local_transport", _local)
    seen: dict[str, Any] = {}
    real_graph = cli.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen["max_fix_attempts"] = kw["max_fix_attempts"]
        seen["adapter"] = rt.adapter
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli, "build_factory_graph", _graph)
    result = cli.run("build a thing", model_mode="hybrid", local_url="http://box:1234/v1",
                     local_model="qwen/qwen3.6-27b", ticket_tiers={"standard": "T1"})

    assert result.passed
    assert result.model_mode == "hybrid"
    assert result.levels is not None and result.levels["T2"]["backend"] == "local"
    assert seen["max_fix_attempts"] == 4
    assert server.urls == ["http://box:1234/v1"]
    assert {r["model"] for r in server.requests} == {"qwen/qwen3.6-27b"}
    # The trivial ticket (T3, local) ran the proxy-agent loop on the local server;
    # the standard ticket (pinned to T1, cloud) ran the cloud substrate (stub).
    def prompt(r: dict[str, Any]) -> str:
        return str(r["messages"][0]["content"])

    action_calls = [r for r in server.requests
                    if r.get("output_config", {}).get("format", {}).get("schema")
                    == ACTION_SCHEMA]
    assert action_calls
    assert all("TEST / QA ENGINEER" in prompt(r) for r in action_calls)
    assert not any("BACKEND ENGINEER" in prompt(r) for r in server.requests)
    # The planner is T1 (cloud); the reviewer is T2 (local).
    assert not any("You are the Planner" in json.dumps(r) for r in server.requests)
    assert any(r["messages"][0]["content"].startswith("Review the artifact")
               for r in server.requests)
    adapter = seen["adapter"]
    assert isinstance(adapter, TieredAdapter)
    assert adapter.cloud.name == "stub" and adapter.local.name == "proxy-agent"


def test_a_run_without_a_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_local(url: str) -> Any:
        raise AssertionError("no local server without a model mode")

    monkeypatch.setattr(cli, "_local_transport", _no_local)
    seen: dict[str, Any] = {}
    real_graph = cli.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen["max_fix_attempts"] = kw["max_fix_attempts"]
        seen["adapter"] = rt.adapter
        seen["proxy"] = rt.proxy
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli, "build_factory_graph", _graph)
    result = cli.run("build a thing")
    assert result.passed and result.levels is None and result.model_mode is None
    assert seen["max_fix_attempts"] == 3
    assert seen["adapter"].name == "stub"
    assert seen["proxy"].local_tiers == frozenset()
    assert "models:" not in result.summary()


def test_local_mode_defaults_to_five_fix_attempts_and_one_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeLocalServer()
    monkeypatch.setattr(cli, "_local_transport", lambda url: server)
    seen: dict[str, Any] = {}
    real_graph = cli.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli, "build_factory_graph", _graph)
    # The fake local agent writes nothing, so the run reaches the gates and halts
    # there; what matters is that every call went to the local server.
    result = cli.run("build a thing", model_mode="local")
    assert result.phase == "halted" and result.model_mode == "local"
    assert seen["max_fix_attempts"] == 5
    assert seen["max_slots"] == 1
    assert any("You are the Planner" in json.dumps(r) for r in server.requests)


# ---- structured output on the local server ----------------------------------------


class _FakeCompletions:
    def __init__(self, reject_schema: bool, text: str) -> None:
        self.reject_schema = reject_schema
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        if self.reject_schema and "response_format" in params:
            raise RuntimeError("Error code: 400 - 'response_format' json_schema not supported")
        message = SimpleNamespace(content=self.text)
        return SimpleNamespace(
            model=params["model"], id="cmpl-1",
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )


def _local_client(reject_schema: bool, text: str) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(reject_schema, text)))


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"]}


def test_local_server_gets_a_json_schema_request_when_it_supports_one() -> None:
    client = _local_client(False, '{"answer": "42"}')
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(), local_transport=lambda _u: transport,
        signer=SIGNER)
    result = proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation", schema=SCHEMA)
    assert result.parsed == {"answer": "42"}
    sent = client.chat.completions.calls
    assert len(sent) == 1
    assert sent[0]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert transport.schema_supported is True


def test_local_schema_rejection_falls_back_to_prompt_and_parse() -> None:
    client = _local_client(True, '<think>{maybe}</think>Sure: {"answer": "42"} done')
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(), local_transport=lambda _u: transport,
        signer=SIGNER)
    first = proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation", schema=SCHEMA)
    second = proxy.complete(CALL[0], "T3", *CALL[1:], purpose="ideation", schema=SCHEMA)
    assert first.parsed == second.parsed == {"answer": "42"}
    sent = client.chat.completions.calls
    # Rejected once, then the fallback; the second call goes straight to the fallback.
    assert ["response_format" in c for c in sent] == [True, False, False]
    assert "JSON Schema" in sent[1]["messages"][-1]["content"]
    assert transport.schema_supported is False


def test_the_local_client_never_carries_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    transport = LocalOpenAITransport("http://box:1234/v1")
    client = transport._cache.get()
    assert client.api_key != "sk-real-secret"
    assert str(client.base_url).rstrip("/") == "http://box:1234/v1"


# ---- wall-clock cap ---------------------------------------------------------------------


def test_the_time_cap_stops_at_a_clean_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(tm, "_now", lambda: now[0])
    meter = TokenMeter(None, time_cap_s=60)
    meter.check()
    now[0] = 59.0
    meter.check()
    now[0] = 61.0
    with pytest.raises(TimeCapExceeded, match="time cap reached"):
        meter.check()


def test_time_spent_on_uncharged_local_calls_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(tm, "_now", lambda: now[0])
    ledger = tmp_path / "tokens.json"
    meter = TokenMeter(None, ledger, time_cap_s=600)
    now[0] = 300.0
    meter.check()  # a local call: checked, never charged
    now[0] = 1000.0  # a new process starts its own clock
    assert TokenMeter(None, ledger, time_cap_s=600).elapsed_s == 300.0


def test_a_time_capped_run_exits_3_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    ticks = [0.0]

    def _clock() -> float:
        ticks[0] += 20.0
        return ticks[0]

    monkeypatch.setattr(tm, "_now", _clock)
    state = tmp_path / "state"
    assert cli.main(["--time-cap", "1", "--state-dir", str(state), "build a thing"]) == 3
    err = capsys.readouterr().err
    assert "time cap reached" in err and "higher --time-cap" in err
    (ledger,) = state.glob("calls/*/tokens.json")
    spent = json.loads(ledger.read_text())["elapsed_s"]
    assert spent >= 60
    # Resumed, not restarted: the rerun's clock carries on from the spent time.
    assert cli.main(["--time-cap", "100000", "--state-dir", str(state), "build a thing"]) == 0
    assert json.loads(ledger.read_text())["elapsed_s"] > spent


# ---- local server check -----------------------------------------------------------------


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_local_check_reports_the_served_models() -> None:
    urls: list[str] = []

    def _open(url: str, timeout: float) -> _Resp:
        urls.append(url)
        return _Resp({"data": [{"id": "qwen/qwen3.6-27b"}, {"id": "text-embedding"}]})

    status = check_local_server("http://box:1234/v1/", model="qwen/qwen3.6-27b", opener=_open)
    assert urls == ["http://box:1234/v1/models"]
    assert status.reachable and status.model_served is True
    assert status.models == ("qwen/qwen3.6-27b", "text-embedding")


def test_local_check_reports_an_unreachable_or_invalid_server() -> None:
    def _down(url: str, timeout: float) -> _Resp:
        raise OSError("connection refused")

    down = check_local_server(opener=_down)
    assert not down.reachable and "connection refused" in (down.error or "")
    bad = check_local_server("file:///etc/passwd", opener=_down)
    assert not bad.reachable and "http" in (bad.error or "")


def test_cli_local_check_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.proxy.local_transport import LocalServerStatus

    monkeypatch.setattr(cli, "check_local_server", lambda url, model=None: LocalServerStatus(
        url=url, reachable=True, models=("m",), model=model, model_served=model == "m"))
    assert cli.main(["--local-check", "--local-model", "m"]) == 0
    assert json.loads(capsys.readouterr().out)["models"] == ["m"]
    assert cli.main(["--local-check", "--local-model", "other"]) == 1
    assert cli.main(["--model-mode", "orbit", "build a thing"]) == 2
