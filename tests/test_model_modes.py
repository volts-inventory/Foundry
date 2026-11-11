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
from foundry.execution.adapters.proxy_agent import ACTION_SCHEMA, TOOLS, ProxyAgentAdapter
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
from foundry.proxy import ModelProviderProxy, ProviderResponse, ProxyUnavailable
from foundry.proxy import token_meter as tm
from foundry.proxy.local_transport import (
    DEFAULT_LOCAL_MODEL,
    LocalOpenAITransport,
    check_local_server,
)
from foundry.proxy.model_provider_proxy import (
    _LOCAL_THINKING_MAX_TOKENS,
    TIER_MODELS,
    _with_parse_correction,
    local_sampling_for,
    parse_local_sampling,
)
from foundry.proxy.test_doubles import MockTransport
from foundry.proxy.token_meter import TimeCapExceeded, TokenMeter
from foundry.schemas.implementation import EFFORT_LEVELS, Difficulty, fix_effort, ticket_effort

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
            return ProviderResponse(
                served_model=request["model"],
                request_id="r",
                text=self.text,
                usage=dict(USAGE),
                stop_reason="stop",
            )
        finally:
            with self._lock:
                self.in_flight -= 1


def _hybrid(cloud: Any, local: Any) -> ModelProviderProxy:
    levels = preset_levels("hybrid", cloud_backend="mock", cloud_models=TIER_MODELS)
    return build_level_proxy(
        levels, cloud_transport=lambda _b: cloud, local_transport=lambda _u: local, signer=SIGNER
    )


# ---- presets + level tables ---------------------------------------------------


def test_presets_put_the_levels_where_the_plan_says() -> None:
    kw: dict[str, Any] = {"cloud_backend": "claude-cli", "cloud_models": TIER_MODELS}
    assert all(not lvl.local for lvl in preset_levels("cloud", **kw).values())
    hybrid = preset_levels("hybrid", **kw)
    assert hybrid["T1"] == Level("claude-cli", TIER_MODELS["T1"])
    assert hybrid["T2"].local and hybrid["T3"].local
    assert hybrid["T2"].model == DEFAULT_LOCAL_MODEL
    assert hybrid["T2"].base_url == "http://localhost:11434/v1"
    assert all(lvl.local for lvl in preset_levels("local", **kw).values())
    assert DEFAULT_FIX_ATTEMPTS == {"cloud": 3, "hybrid": 3, "local": 3}


def test_no_mode_and_cloud_mode_keep_todays_routing() -> None:
    kw: dict[str, Any] = {
        "levels": None,
        "backend": "claude-cli",
        "cloud_models": TIER_MODELS,
        "cloud_backends": cli.BACKENDS,
    }
    assert resolve_levels(model_mode=None, **kw) is None
    assert resolve_levels(model_mode="cloud", **kw) is None
    with pytest.raises(ValueError, match="unknown model mode"):
        resolve_levels(model_mode="orbit", **kw)


def test_an_explicit_table_overrides_and_omitted_tiers_run_on_the_backend() -> None:
    table = resolve_levels(
        model_mode="hybrid",
        backend="claude-cli",
        cloud_models=TIER_MODELS,
        cloud_backends=cli.BACKENDS,
        levels={
            "T3": {
                "backend": "local",
                "model": "qwen/qwen3.6-27b",
                "base_url": "http://box:1234/v1",
            }
        },
    )
    assert table is not None
    assert table["T3"] == Level("local", "qwen/qwen3.6-27b", "http://box:1234/v1")
    assert table["T1"] == Level("claude-cli", TIER_MODELS["T1"])
    assert table["T2"] == Level("claude-cli", TIER_MODELS["T2"])


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"T4": {"backend": "local", "model": "m"}}, "unknown tier"),
        ({"T1": {"backend": "bogus", "model": "m"}}, "unknown backend"),
        ({"T1": {"backend": "local", "model": "m; rm -rf /"}}, "invalid model id"),
        ({"T1": {"backend": "local", "model": "m", "base_url": "file:///etc/passwd"}}, "http"),
    ],
)
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
    levels = {
        t: Level("local", "acme-coder-7b", "http://localhost:1234/v1") for t in ("T1", "T2", "T3")
    }
    proxy = build_level_proxy(
        levels,
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: local,
        signer=SIGNER,
    )
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

    threads = [threading.Thread(target=call, args=(t,)) for t in ["T2", "T3"] * 4 + ["T1"] * 4]
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

    adapter = TieredAdapter(
        cloud=Session(), local=Session(), local_tiers={"T2", "T3"}, local_slot=proxy.local_slot
    )
    packet = TaskPacket(
        task={"id": "t", "tier": "T2"},
        worktree_path=str(tmp_path),
        turn_budget=1,
        cost_budget_microusd=1,
    )
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
    adapter = TieredAdapter(
        cloud=cloud, local=local, local_tiers={"T2", "T3"}, local_slot=threading.RLock
    )
    for tier in ("T1", "T2", "T3", None):
        task: dict[str, Any] = {"id": "t"} | ({"tier": tier} if tier else {})
        adapter.run(
            TaskPacket(
                task=task, worktree_path=str(tmp_path), turn_budget=1, cost_budget_microusd=1
            ),
            None,
        )  # type: ignore[arg-type]
    assert cloud.ran == ["T1", None]  # no tier (the fixer) runs on T1
    assert local.ran == ["T2", "T3"]


def test_a_local_sessions_sandbox_gets_no_cloud_credential(tmp_path: Path) -> None:
    from foundry.agents.implementation.junior_engineer import _sandbox_for

    adapter = TieredAdapter(
        cloud=ClaudeCodeAdapter(),
        local=ProxyAgentAdapter(complete_fn=lambda m, s: ("", None)),
        local_tiers={"T2", "T3"},
        local_slot=threading.RLock,
    )
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


def _agent_turn(schema: Any) -> bool:
    """A proxy-agent turn's action schema (with or without the `check` action)."""
    action = schema.get("properties", {}).get("action") if isinstance(schema, dict) else None
    return schema == ACTION_SCHEMA or (
        isinstance(action, dict) and "check" in action.get("enum", ())
    )


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
        if _agent_turn(schema):
            return ProviderResponse(
                served_model=request["model"],
                request_id="r",
                text=json.dumps({"action": "done", "summary": "ok"}),
                usage=dict(USAGE),
                stop_reason="stop",
            )
        return self.mock.send(request)


def test_a_hybrid_run_routes_tickets_and_calls_by_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeLocalServer()

    def _local(url: str) -> FakeLocalServer:
        server.urls.append(url)
        return server

    monkeypatch.setattr(cli.wiring, "_local_transport", _local)
    seen: dict[str, Any] = {}
    real_graph = cli.runner.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen["max_fix_attempts"] = kw["max_fix_attempts"]
        seen["adapter"] = rt.adapter
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli.runner, "build_factory_graph", _graph)
    result = cli.run(
        "build a thing",
        model_mode="hybrid",
        local_url="http://box:1234/v1",
        local_model="qwen/qwen3.6-27b",
        ticket_tiers={"standard": "T1"},
    )

    assert result.passed, result.summary()
    assert result.model_mode == "hybrid"
    assert result.levels is not None and result.levels["T2"]["backend"] == "local"
    assert seen["max_fix_attempts"] == 3
    assert server.urls == ["http://box:1234/v1"]
    assert {r["model"] for r in server.requests} == {"qwen/qwen3.6-27b"}

    # The trivial ticket (T3, local) ran the proxy-agent loop on the local server;
    # the standard ticket (pinned to T1, cloud) ran the cloud substrate (stub).
    def prompt(r: dict[str, Any]) -> str:
        return str(r["messages"][0]["content"])

    action_calls = [
        r
        for r in server.requests
        if _agent_turn(r.get("output_config", {}).get("format", {}).get("schema"))
    ]
    assert action_calls
    assert all("TEST / QA ENGINEER" in prompt(r) for r in action_calls)
    assert not any("BACKEND ENGINEER" in prompt(r) for r in server.requests)
    # The planner is T1 (cloud); the reviewer is T2 (local).
    assert not any("You are the Planner" in json.dumps(r) for r in server.requests)
    assert any(
        r["messages"][0]["content"].startswith("Review the artifact") for r in server.requests
    )
    adapter = seen["adapter"]
    assert isinstance(adapter, TieredAdapter)
    assert adapter.cloud.name == "stub" and adapter.local.name == "proxy-agent"


def test_a_run_without_a_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_local(url: str) -> Any:
        raise AssertionError("no local server without a model mode")

    monkeypatch.setattr(cli.wiring, "_local_transport", _no_local)
    seen: dict[str, Any] = {}
    real_graph = cli.runner.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen["max_fix_attempts"] = kw["max_fix_attempts"]
        seen["adapter"] = rt.adapter
        seen["proxy"] = rt.proxy
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli.runner, "build_factory_graph", _graph)
    result = cli.run("build a thing")
    assert result.passed and result.levels is None and result.model_mode is None
    assert seen["max_fix_attempts"] == 3
    assert seen["adapter"].name == "stub"
    assert seen["proxy"].local_tiers == frozenset()
    assert "models:" not in result.summary()


def test_local_mode_defaults_to_three_fix_attempts_and_one_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeLocalServer()
    monkeypatch.setattr(cli.wiring, "_local_transport", lambda url: server)
    seen: dict[str, Any] = {}
    real_graph = cli.runner.build_factory_graph

    def _graph(rt: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real_graph(rt, **kw)

    monkeypatch.setattr(cli.runner, "build_factory_graph", _graph)
    # The fake local agent writes nothing, so the run reaches the gates and halts
    # there; what matters is that every call went to the local server.
    result = cli.run("build a thing", model_mode="local")
    assert result.phase == "halted" and result.model_mode == "local"
    assert seen["max_fix_attempts"] == 3
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
            model=params["model"],
            id="cmpl-1",
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )


def _local_client(reject_schema: bool, text: str) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(reject_schema, text)))


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def test_local_server_gets_a_json_schema_request_when_it_supports_one() -> None:
    client = _local_client(False, '{"answer": "42"}')
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )
    result = proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation", schema=SCHEMA)
    assert result.parsed == {"answer": "42"}
    sent = client.chat.completions.calls
    assert len(sent) == 1
    assert sent[0]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert sent[0]["reasoning_effort"] == "xhigh"  # a one-shot call thinks hardest
    assert transport.schema_supported is True


def test_a_server_rejecting_reasoning_effort_is_retried_without_it() -> None:
    client = _local_client(False, '{"answer": "42"}')
    completions = client.chat.completions
    real_create = completions.create

    def create(**params: Any) -> Any:
        if "reasoning_effort" in params:
            completions.calls.append(params)
            raise RuntimeError("Error code: 400 - reasoning_effort must be low, medium or high")
        return real_create(**params)

    completions.create = create
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )
    first = proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation", schema=SCHEMA)
    second = proxy.complete(CALL[0], "T3", *CALL[1:], purpose="ideation", schema=SCHEMA)
    assert first.parsed == second.parsed == {"answer": "42"}
    assert ["reasoning_effort" in c for c in completions.calls] == [True, False, False]
    assert transport.reasoning_supported is False
    assert transport.schema_supported is True  # not mistaken for a schema rejection


def test_local_schema_rejection_falls_back_to_prompt_and_parse() -> None:
    client = _local_client(True, '<think>{maybe}</think>Sure: {"answer": "42"} done')
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )
    first = proxy.complete(CALL[0], "T2", *CALL[1:], purpose="ideation", schema=SCHEMA)
    second = proxy.complete(CALL[0], "T3", *CALL[1:], purpose="ideation", schema=SCHEMA)
    assert first.parsed == second.parsed == {"answer": "42"}
    sent = client.chat.completions.calls
    # Rejected once, then the fallback; the second call goes straight to the fallback.
    assert ["response_format" in c for c in sent] == [True, False, False]
    assert "JSON Schema" in sent[1]["messages"][-1]["content"]
    assert transport.schema_supported is False


# ---- local sampling profiles ---------------------------------------------------


QWEN38 = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}
QWEN3 = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (DEFAULT_LOCAL_MODEL, QWEN38),
        ("qwen/qwen3.8-27b", QWEN38),
        ("qwen3.6:27b-coding-mxfp8", QWEN3),
        ("hf.co/unsloth/Qwen3-32B-GGUF:Q4_K_M", QWEN3),
        (
            "qwen3-coder:30b",
            {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05},
        ),
        ("acme-coder-7b", None),
    ],
)
def test_local_sampling_profile_is_picked_by_model_glob(
    model: str, expected: dict[str, float] | None
) -> None:
    assert local_sampling_for(model) == expected


def test_local_sampling_env_parses_off_and_overrides() -> None:
    assert parse_local_sampling("") is None
    assert parse_local_sampling(" OFF ") == "off"
    assert parse_local_sampling("temperature=0.5, top_k=40") == {"temperature": 0.5, "top_k": 40}
    assert local_sampling_for(DEFAULT_LOCAL_MODEL, "off") is None
    assert local_sampling_for("acme-coder-7b", {"top_p": 0.9}) == {"top_p": 0.9}
    for bad in ("temperature", "seed=1", "top_p=high"):
        with pytest.raises(ValueError, match="FOUNDRY_LOCAL_SAMPLING"):
            parse_local_sampling(bad)


def test_a_bad_local_sampling_env_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_LOCAL_SAMPLING", "temp=1")
    with pytest.raises(ValueError, match="FOUNDRY_LOCAL_SAMPLING"):
        _hybrid(RecordingTransport(), RecordingTransport())


def test_local_tiers_sample_while_cloud_gates_stay_greedy() -> None:
    cloud, local = RecordingTransport(), RecordingTransport()
    proxy = _hybrid(cloud, local)
    single = RecordingTransport()
    ModelProviderProxy(single, signer=SIGNER).complete(CALL[0], "T2", *CALL[1:], purpose="gate")
    proxy.complete(CALL[0], "T1", *CALL[1:], purpose="gate")
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="gate")
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="judgment")
    proxy.complete(CALL[0], "T3", *CALL[1:], purpose="ideation")
    assert single.requests[0]["temperature"] == 0
    assert "local_sampling" not in single.requests[0]
    assert "local_sampling" not in cloud.requests[0]
    assert len(local.requests) == 3
    for req in local.requests:
        assert "temperature" not in req
        assert req["local_sampling"]["temperature"] == 1.0
        assert isinstance(req["local_sampling"]["seed"], int)
    assert proxy.manifest[1]["sampling"] == local.requests[0]["local_sampling"]
    assert "greedy" in proxy.manifest[1]["sampling_note"]


def test_local_sampling_off_restores_greedy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_LOCAL_SAMPLING", "off")
    local = RecordingTransport()
    _hybrid(RecordingTransport(), local).complete(CALL[0], "T2", *CALL[1:], purpose="gate")
    assert local.requests[0]["temperature"] == 0
    assert "local_sampling" not in local.requests[0]


def test_the_local_seed_is_fixed_per_request_and_moves_with_the_prompt() -> None:
    local = RecordingTransport()
    proxy = _hybrid(RecordingTransport(), local)
    other = [{"role": "user", "content": "y"}]
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="gate")
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="gate")
    proxy.complete(CALL[0], "T2", CALL[1], other, purpose="gate")
    seeds = [r["local_sampling"]["seed"] for r in local.requests]
    assert seeds[0] == seeds[1] != seeds[2]
    miss = ProviderResponse(
        served_model="m", request_id="r", text="{", usage={}, stop_reason="length"
    )
    retry = _with_parse_correction(local.requests[0], miss, 1)
    assert retry["local_sampling"]["seed"] != seeds[0]
    assert {k: v for k, v in retry["local_sampling"].items() if k != "seed"} == {
        k: v for k, v in local.requests[0]["local_sampling"].items() if k != "seed"
    }


def test_a_thinking_reply_cut_off_at_the_cap_is_retried_a_level_lower() -> None:
    request = {
        "messages": [{"role": "user", "content": "plan"}],
        "local_thinking": True,
        "local_effort": "xhigh",
    }
    cut = ProviderResponse(
        served_model="m", request_id="r", text="", usage={}, stop_reason="length"
    )
    retry = _with_parse_correction(request, cut, 1)
    assert retry["local_thinking"] is True and retry["local_effort"] == "medium"
    assert _with_parse_correction(retry, cut, 2)["local_thinking"] is False


def test_any_parse_miss_retry_carries_the_replys_tail_and_steps_effort_down() -> None:
    local = RecordingTransport(text="Here is my review. " + "x" * 5000 + " TAIL-END")
    proxy = _local_proxy(local)
    with pytest.raises(ProxyUnavailable, match="no parseable structured reply"):
        proxy.complete(
            "reviewer",
            "T2",
            "SOP",
            [{"role": "user", "content": "r"}],
            purpose="judgment",
            schema=SCHEMA,
            effort="xhigh",
        )
    _, second, third = local.requests
    assert [(r["local_thinking"], r.get("local_effort")) for r in local.requests] == [
        (True, "xhigh"),
        (True, "medium"),
        (False, "medium"),
    ]
    note = second["messages"][-1]["content"]
    assert note.startswith("r\n\n[Retry 1:") and "'stop'" in note
    assert "no parseable JSON object" in note and "ONLY that JSON object" in note
    assert "TAIL-END" in note and "Here is my review" not in note
    assert len(note) < 2600
    assert third["messages"][-1]["content"].startswith("r\n\n[Retry 2:")
    seeds = {r["local_sampling"]["seed"] for r in local.requests}
    assert len(seeds) == 3


def test_the_local_effort_overrides_the_transports_xhigh() -> None:
    client = _local_client(False, '{"answer": "42"}')
    transport = LocalOpenAITransport(client=client)
    transport.send(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "local_thinking": True,
            "local_effort": "medium",
        }
    )
    assert client.chat.completions.calls[0]["reasoning_effort"] == "medium"


def test_the_local_request_carries_the_sampling_params() -> None:
    client = _local_client(False, '{"answer": "42"}')
    transport = LocalOpenAITransport(client=client)
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )
    proxy.complete(CALL[0], "T2", *CALL[1:], purpose="judgment", schema=SCHEMA)
    sent = client.chat.completions.calls[0]
    # Ollama's /v1 maps these OpenAI fields onto its options ...
    assert sent["temperature"] == 1.0
    assert sent["top_p"] == 0.95
    assert sent["presence_penalty"] == 0.0
    assert isinstance(sent["seed"], int)
    # ... and drops the rest, so they ride as extra body fields for other servers.
    assert sent["extra_body"] == {"top_k": 20, "min_p": 0.0, "repeat_penalty": 1.0}
    assert "local_thinking" not in sent and "local_thinking" not in sent["extra_body"]
    assert "local_sampling" not in sent
    assert sent["response_format"]["type"] == "json_schema"


def test_the_local_client_never_carries_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    transport = LocalOpenAITransport("http://box:1234/v1")
    client = transport._cache.get()
    assert client.api_key != "sk-real-secret"
    assert str(client.base_url).rstrip("/") == "http://box:1234/v1"


# ---- local thinking, roles and truncation ----------------------------------------


def _local_proxy(transport: Any, model: str = DEFAULT_LOCAL_MODEL) -> ModelProviderProxy:
    levels = preset_levels(
        "local", cloud_backend="mock", cloud_models=TIER_MODELS, local_model=model
    )
    return build_level_proxy(
        levels,
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )


def _thinking_by_node(proxy: ModelProviderProxy, local: RecordingTransport) -> dict[str, bool]:
    for node in ("planner", "junior_engineer"):
        proxy.complete(node, "T2", "SOP", [{"role": "user", "content": node}], purpose="ideation")
    proxy.complete("reviewer", "T2", "SOP", [{"role": "user", "content": "r"}], purpose="judgment")
    nodes = ("planner", "junior_engineer", "reviewer")
    return {n: r["local_thinking"] for n, r in zip(nodes, local.requests[-3:], strict=True)}


@pytest.mark.parametrize(
    ("setting", "model", "expected"),
    [
        (None, DEFAULT_LOCAL_MODEL, (True, True, True)),
        (None, "qwen3.6:27b-coding-mxfp8", (True, True, True)),
        (None, "qwen3-coder:30b", (False, False, False)),
        (None, "acme-7b", (False, False, False)),
        ("off", DEFAULT_LOCAL_MODEL, (False, False, False)),
        ("agent", DEFAULT_LOCAL_MODEL, (False, True, False)),
        ("on", "acme-7b", (True, True, True)),
    ],
)
def test_local_thinking_is_routed_per_call_kind(
    monkeypatch: pytest.MonkeyPatch,
    setting: str | None,
    model: str,
    expected: tuple[bool, bool, bool],
) -> None:
    if setting is not None:
        monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", setting)
    local = RecordingTransport()
    proxy = _local_proxy(local, model)
    got = _thinking_by_node(proxy, local)
    assert (got["planner"], got["junior_engineer"], got["reviewer"]) == expected
    for req, thinks in zip(local.requests, expected, strict=True):
        # Thinking spends the answer's budget, so it lifts the cap (judgment too).
        assert req["max_tokens"] == (
            (98304 if req is local.requests[0] else 40960)
            if thinks
            else 8192
            if req is local.requests[2]
            else 16000
        )
    assert proxy.manifest[1]["thinking"] is expected[1]
    # The plan thinks at xhigh; agent turns (one of many) and advisory verdicts at medium.
    efforts = [r.get("local_effort") for r in local.requests]
    assert efforts == [
        ("xhigh" if expected[0] else None),
        ("medium" if expected[1] else None),
        ("medium" if expected[2] else None),
    ]


def test_a_call_can_ask_for_medium_effort_and_its_cap() -> None:
    local = RecordingTransport()
    proxy = _local_proxy(local)
    msgs = [{"role": "user", "content": "repair"}]
    proxy.complete("planner", "T2", "SOP", msgs, purpose="ideation", effort="medium")
    proxy.complete("junior_engineer", "T2", "SOP", msgs, purpose="ideation", effort="xhigh")
    assert [(r["local_effort"], r["max_tokens"]) for r in local.requests] == [
        ("medium", 40960),
        ("xhigh", 98304),
    ]


def test_a_judgment_passes_its_effort_through() -> None:
    from foundry.state.seq import Seq

    local = RecordingTransport(text=json.dumps({"status": "approve", "findings": []}))
    proxy = _local_proxy(local)
    seq = Seq("r", 1, 1, "reviewer", 0)
    msgs = [{"role": "user", "content": "re-review"}]
    proxy.judge("reviewer", "cas://d", "tree", "n", "SOP", msgs, seq=seq, tier="T2")
    proxy.judge(
        "reviewer", "cas://d", "tree", "n2", "SOP", msgs, seq=seq, tier="T2", effort="xhigh"
    )
    assert [(r["local_effort"], r["max_tokens"]) for r in local.requests] == [
        ("medium", 40960),
        ("xhigh", 98304),
    ]


def test_every_thinking_level_has_an_output_budget() -> None:
    assert set(_LOCAL_THINKING_MAX_TOKENS) == set(EFFORT_LEVELS)
    local = RecordingTransport()
    proxy = _local_proxy(local)
    msgs = [{"role": "user", "content": "rename"}]
    proxy.complete("junior_engineer", "T2", "SOP", msgs, purpose="ideation", effort="low")
    assert (local.requests[0]["local_effort"], local.requests[0]["max_tokens"]) == ("low", 40960)


def test_a_low_effort_reply_cut_off_at_the_cap_is_retried_without_thinking() -> None:
    request = {
        "messages": [{"role": "user", "content": "rename"}],
        "local_thinking": True,
        "local_effort": "low",
    }
    cut = ProviderResponse(
        served_model="m", request_id="r", text="", usage={}, stop_reason="length"
    )
    assert _with_parse_correction(request, cut, 1)["local_thinking"] is False


def test_local_calls_are_counted_by_thinking_level() -> None:
    from foundry import telemetry

    local = RecordingTransport()
    proxy = _local_proxy(local)
    msgs = [{"role": "user", "content": "x"}]
    with telemetry.counting() as counts:
        proxy.complete("junior_engineer", "T2", "SOP", msgs, purpose="ideation", effort="low")
        proxy.complete("junior_engineer", "T2", "SOP", msgs, purpose="ideation")
        proxy.complete("planner", "T2", "SOP", msgs, purpose="ideation")
        proxy.complete("reviewer", "T2", "SOP", msgs, purpose="judgment")
    assert counts.snapshot() == {"effort.low": 1, "effort.medium": 2, "effort.xhigh": 1}


def test_each_local_attempt_is_counted_at_the_level_it_was_sent() -> None:
    from foundry import telemetry

    class CutOnce(RecordingTransport):
        def send(self, request: dict[str, Any]) -> ProviderResponse:
            self.requests.append(request)
            first = len(self.requests) == 1
            return ProviderResponse(
                served_model=request["model"],
                request_id="r",
                text='{"a": ' if first else '{"a": 1}',
                usage=dict(USAGE),
                stop_reason="length" if first else "stop",
            )

    local = CutOnce()
    proxy = _local_proxy(local)
    proxy._retry.sleeper = lambda _s: None
    msgs = [{"role": "user", "content": "x"}]
    with telemetry.counting() as counts:
        proxy.complete("planner", "T2", "SOP", msgs, purpose="ideation", schema={"type": "object"})
    assert [r["local_effort"] for r in local.requests] == ["xhigh", "medium"]
    assert counts.snapshot() == {"effort.xhigh": 1, "effort.medium": 1}


def test_a_packets_effort_reaches_the_proxy_closures_but_not_the_prompt() -> None:
    from foundry.execution.adapter_contract import (
        assemble_prompt,
        current_packet_effort,
        packet_tier_scope,
    )

    packet = TaskPacket(
        task={"id": "T-1", "tier": "T2"},
        effort="low",
        worktree_path=".",
        turn_budget=1,
        cost_budget_microusd=0,
    )
    with packet_tier_scope(packet):
        assert current_packet_effort() == "low"
    assert current_packet_effort() is None
    assert "effort" not in assemble_prompt(packet)


def test_ticket_and_fixer_thinking_levels() -> None:
    assert [ticket_effort(d) for d in Difficulty] == ["low", "medium", "medium"]
    assert [ticket_effort(d, retry=True) for d in Difficulty] == ["medium", "xhigh", "xhigh"]
    assert [fix_effort(n) for n in (0, 1, 2, 5)] == ["medium", "medium", "xhigh", "xhigh"]


def test_cloud_tiers_never_carry_the_thinking_flag_and_a_bad_setting_fails_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud, local = RecordingTransport(), RecordingTransport()
    _hybrid(cloud, local).complete("junior_engineer", "T1", *CALL[1:], purpose="ideation")
    assert "local_thinking" not in cloud.requests[0]
    monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", "sometimes")
    with pytest.raises(ValueError, match="FOUNDRY_LOCAL_THINKING"):
        _hybrid(cloud, local)


def test_the_runs_local_thinking_wins_over_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", "on")
    local = RecordingTransport()
    levels = preset_levels(
        "local", cloud_backend="mock", cloud_models=TIER_MODELS, local_model=DEFAULT_LOCAL_MODEL
    )
    proxy = build_level_proxy(
        levels,
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: local,
        signer=SIGNER,
        local_thinking="agent",
    )
    got = _thinking_by_node(proxy, local)
    assert (got["planner"], got["junior_engineer"], got["reviewer"]) == (False, True, False)


class _ThinkingCompletions:
    """Replies with JSON content plus reasoning (in the `reasoning` field, or
    inline as Qwen's `<think>` block when ``inline``)."""

    def __init__(self, inline: bool = False) -> None:
        self.inline = inline
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        n = len(self.calls)
        thought = f'turn {n}: maybe {{"answer": "wrong"}}'
        answer = json.dumps({"answer": str(n)})
        message = (
            SimpleNamespace(content=f"<think>{thought}</think>{answer}")
            if self.inline
            else SimpleNamespace(content=answer, reasoning=thought)
        )
        return SimpleNamespace(
            model=params["model"],
            id=f"cmpl-{n}",
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )


@pytest.mark.parametrize("inline", [False, True])
def test_reasoning_never_reaches_the_answer_and_is_replayed_next_turn(inline: bool) -> None:
    completions = _ThinkingCompletions(inline)
    transport = LocalOpenAITransport(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    proxy = _local_proxy(transport)
    history: list[dict[str, Any]] = [{"role": "user", "content": "task"}]
    first = proxy.complete(
        "junior_engineer", "T2", "SOP", history, purpose="ideation", schema=SCHEMA
    )
    assert first.parsed == {"answer": "1"} and "think" not in first.text
    history += [
        {"role": "assistant", "content": first.text},
        {"role": "user", "content": "OBSERVATION: ok"},
    ]
    proxy.complete("junior_engineer", "T2", "SOP", history, purpose="ideation", schema=SCHEMA)
    sent = completions.calls[1]["messages"]
    assert sent[2] == {
        "role": "assistant",
        "content": first.text,
        "reasoning": 'turn 1: maybe {"answer": "wrong"}',
    }
    assert "reasoning" not in sent[1] and "reasoning" not in sent[3]


def test_reasoning_is_not_replayed_when_thinking_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", "off")
    completions = _ThinkingCompletions()
    transport = LocalOpenAITransport(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    proxy = _local_proxy(transport)
    first = proxy.complete("junior_engineer", "T2", *CALL[1:], purpose="ideation")
    history = [
        *CALL[2],
        {"role": "assistant", "content": first.text},
        {"role": "user", "content": "go on"},
    ]
    proxy.complete("junior_engineer", "T2", "SOP", history, purpose="ideation")
    assert completions.calls[0]["reasoning_effort"] == "none"
    assert all("reasoning" not in m for m in completions.calls[1]["messages"])


def test_the_developer_role_goes_to_a_local_server_as_system() -> None:
    client = _local_client(False, "ok")
    LocalOpenAITransport(client=client).send(
        {
            "model": "m",
            "system": "SOP",
            "messages": [
                {"role": "developer", "content": "Use Go."},
                {"role": "user", "content": "hi"},
            ],
        }
    )
    sent = client.chat.completions.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user"]
    assert sent[1]["content"] == "Use Go."


class _SeenTransport(RecordingTransport):
    """A local server that reports seeing ``seen`` prompt tokens."""

    def __init__(self, seen: int) -> None:
        super().__init__()
        self.seen = seen

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            served_model=request["model"],
            request_id="r",
            text="ok",
            usage={"input_tokens": self.seen, "output_tokens": 1},
            stop_reason="stop",
        )


@pytest.mark.parametrize(("seen", "warned"), [(9000, False), (2000, True)])
def test_a_local_prompt_the_server_cut_short_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    seen: int,
    warned: bool,
) -> None:
    monkeypatch.setenv("FOUNDRY_PROGRESS", "1")
    proxy = _local_proxy(_SeenTransport(seen))
    big = [{"role": "user", "content": "word " * 8000}]  # ~10K tokens by chars/4
    proxy.complete("junior_engineer", "T2", "SOP", big, purpose="ideation")
    record = proxy.manifest[0]
    err = capsys.readouterr().err
    if warned:
        assert record["context_truncated"].startswith("context truncated: sent ~")
        assert record["context_truncated"].endswith(", model saw 2000")
        assert "WARNING" in err and "context truncated" in err
    else:
        assert "context_truncated" not in record and "context truncated" not in err


def test_cloud_calls_are_never_checked_for_truncation() -> None:
    proxy = _hybrid(_SeenTransport(10), RecordingTransport())
    proxy.complete(
        "node", "T1", "SOP", [{"role": "user", "content": "word " * 8000}], purpose="ideation"
    )
    assert "context_truncated" not in proxy.manifest[0]


# ---- native tool calls on the local server (Phase 5) ----------------------------------


class _ToolCompletions:
    """Answers a `tools` call with two tool calls (one with broken arguments) and
    any other call with the next scripted JSON action."""

    def __init__(self, reject_tools: bool, actions: list[dict[str, Any]] | None = None) -> None:
        self.reject_tools = reject_tools
        self.actions = list(actions or [])
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        if "tools" in params:
            if self.reject_tools:
                raise RuntimeError("Error code: 400 - library/m does not support tools")
            calls = [
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'),
                ),
                SimpleNamespace(id=None, function=SimpleNamespace(name="run", arguments="{bad")),
            ]
            message = SimpleNamespace(content="", tool_calls=calls)
        else:
            message = SimpleNamespace(content=json.dumps(self.actions.pop(0)))
        return SimpleNamespace(
            model=params["model"],
            id="cmpl-t",
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )


def _tool_proxy(completions: _ToolCompletions) -> tuple[ModelProviderProxy, LocalOpenAITransport]:
    transport = LocalOpenAITransport(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: transport,
        signer=SIGNER,
    )
    return proxy, transport


TOOL_HISTORY = [
    {"role": "user", "content": "task"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "call_0", "name": "search", "input": {"pattern": "x"}},
        ],
    },
    {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_0", "content": "a.py:1: x"}],
    },
]


def test_local_server_gets_native_tools_and_returns_tool_calls() -> None:
    completions = _ToolCompletions(reject_tools=False)
    proxy, transport = _tool_proxy(completions)
    result = proxy.complete("node", "T2", "SOP", TOOL_HISTORY, purpose="ideation", tools=TOOLS)
    sent = completions.calls[0]
    assert sent["reasoning_effort"] == "medium" and "response_format" not in sent
    assert sent["tools"][0] == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": TOOLS[0]["description"],
            "parameters": TOOLS[0]["input_schema"],
        },
    }
    assert sent["messages"] == [
        {"role": "system", "content": "SOP"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "looking",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"pattern": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "a.py:1: x"},
    ]
    first, second = result.tool_calls
    assert first == {"id": "call_1", "name": "read_file", "input": {"path": "a.py"}}
    assert second["id"] == "tool_call_1" and "not a JSON object" in second["error"]
    assert result.manifest_record["tool_calls"] == 2
    assert transport.tools_support(sent["model"]) is True


def test_local_tools_rejection_falls_back_to_json_actions(tmp_path: Path) -> None:
    from foundry.execution.sandbox import Sandbox
    from foundry.proxy import ToolsUnsupported

    completions = _ToolCompletions(
        reject_tools=True,
        actions=[
            {"action": "write_file", "path": "m.py", "content": "X = 1\n"},
            {"action": "done", "summary": "ok"},
        ],
    )
    proxy, transport = _tool_proxy(completions)

    def _json(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        r = proxy.complete("node", "T2", "SOP", list(messages), purpose="ideation", schema=schema)
        return r.text, r.parsed

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        r = proxy.complete("node", "T2", "SOP", list(messages), purpose="ideation", tools=tools)
        return r.text, r.tool_calls

    src = tmp_path / "src"
    src.mkdir()
    adapter = ProxyAgentAdapter(_json, tool_complete_fn=_tools)
    with Sandbox(src) as sb:
        result = adapter.run(
            TaskPacket(
                task={"id": "t"},
                worktree_path=str(sb.worktree),
                turn_budget=6,
                cost_budget_microusd=1,
            ),
            sb,
        )
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"m.py": "X = 1\n"}
    assert ["tools" in c for c in completions.calls] == [True, False, False]
    assert transport.tools_support(completions.calls[0]["model"]) is False
    # Remembered for the session: the proxy refuses before reaching the server.
    with pytest.raises(ToolsUnsupported):
        proxy.complete("node", "T3", "SOP", TOOL_HISTORY, purpose="ideation", tools=TOOLS)
    assert len(completions.calls) == 3


@pytest.mark.parametrize("order", [("good", "bad"), ("bad", "good")])
def test_local_tool_support_is_tracked_per_model(order: tuple[str, str]) -> None:
    from foundry.proxy import ToolsUnsupported

    completions = _ToolCompletions(reject_tools=False)
    accept = completions.create

    def create(**params: Any) -> Any:
        if "tools" in params and params["model"] == "bad":
            completions.calls.append(params)
            raise RuntimeError("Error code: 400 - library/bad does not support tools")
        return accept(**params)

    completions.create = create  # type: ignore[method-assign]
    transport = LocalOpenAITransport(
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )
    tools = [{"name": "run", "description": "d", "input_schema": {"type": "object"}}]
    for model in (*order, *order):
        request = {"model": model, "messages": [{"role": "user", "content": "x"}], "tools": tools}
        if model == "bad":
            # Rejected as unsupported every time, never a plain error, never
            # disabling the other model's tools.
            with pytest.raises(ToolsUnsupported):
                transport.send(request)
        else:
            assert transport.send(request).tool_calls
    assert transport.tools_support("good") is True
    assert transport.tools_support("bad") is False


def test_a_backend_without_a_tool_channel_is_never_sent_tools() -> None:
    from foundry.proxy import ToolsUnsupported

    local = RecordingTransport()  # like claude-cli: no tools_support probe
    proxy = build_level_proxy(
        preset_levels("local", cloud_backend="mock", cloud_models=TIER_MODELS),
        cloud_transport=lambda _b: RecordingTransport(),
        local_transport=lambda _u: local,
        signer=SIGNER,
    )
    with pytest.raises(ToolsUnsupported):
        proxy.complete("node", "T2", "SOP", TOOL_HISTORY, purpose="ideation", tools=TOOLS)
    assert local.requests == []


def test_anthropic_tool_use_blocks_come_back_as_tool_calls() -> None:
    from foundry.proxy import AnthropicTransport

    sent: list[dict[str, Any]] = []

    def create(**request: Any) -> Any:
        sent.append(request)
        return SimpleNamespace(
            model=request["model"],
            _request_id="req-1",
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="text", text="ok"),
                SimpleNamespace(type="tool_use", id="toolu_1", name="run", input={"cmd": "ls"}),
            ],
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    proxy = ModelProviderProxy(transport=AnthropicTransport(client=client), signer=SIGNER)
    result = proxy.complete("node", "T1", "SOP", TOOL_HISTORY, purpose="ideation", tools=TOOLS)
    assert result.tool_calls == ({"id": "toolu_1", "name": "run", "input": {"cmd": "ls"}},)
    assert sent[0]["tools"] == list(TOOLS) and sent[0]["messages"] == TOOL_HISTORY
    assert "thinking" not in sent[0]  # a tool turn can't replay thinking blocks


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.proxy.local_transport import LocalServerStatus

    monkeypatch.setattr(
        cli.command,
        "check_local_server",
        lambda url, model=None: LocalServerStatus(
            url=url, reachable=True, models=("m",), model=model, model_served=model == "m"
        ),
    )
    assert cli.main(["--local-check", "--local-model", "m"]) == 0
    assert json.loads(capsys.readouterr().out)["models"] == ["m"]
    assert cli.main(["--local-check", "--local-model", "other"]) == 1
    assert cli.main(["--model-mode", "orbit", "build a thing"]) == 2


def test_concurrent_remember_keeps_the_reasoning_memory_bounded() -> None:
    from foundry.proxy import local_transport as lt

    transport = LocalOpenAITransport(client=_local_client(False, "{}"))

    def work(n: int) -> None:
        for i in range(lt._REASONING_MEMORY):
            transport.remember(
                ProviderResponse(
                    served_model="m",
                    request_id="r",
                    text=f"t{n}-{i}",
                    usage={},
                    stop_reason="stop",
                    reasoning="why",
                )
            )

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(transport._reasoning) == lt._REASONING_MEMORY
