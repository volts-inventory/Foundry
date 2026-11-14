"""The run-scoped progress sink (foundry.progress): every progress line reaches the
run's listener, from the graph's node threads and the work pool's workers too,
whatever FOUNDRY_PROGRESS says; a broken listener never breaks the run."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from foundry import cli, progress
from foundry.agents.planner import _run_parallel
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
from foundry.execution.work_pool import ExecutionTask, LocalWorkPool


def test_a_sink_gets_each_line_and_stderr_only_with_the_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FOUNDRY_PROGRESS", raising=False)
    lines: list[str] = []
    with progress.sink(lines.append):
        progress.log("start model call: planner")
        progress.finished("model call: planner", time.monotonic(), None, None)
    progress.log("after the run")
    assert [line.split("] ", 1)[1] for line in lines] == [
        "start model call: planner",
        "done  model call: planner in 0s",
    ]
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("FOUNDRY_PROGRESS", "1")
    with progress.sink(lines.append):
        progress.log("both")
    assert lines[-1] in capsys.readouterr().err


def test_a_failing_sink_never_breaks_the_caller() -> None:
    def broken(_line: str) -> None:
        raise RuntimeError("listener down")

    with progress.sink(broken):
        progress.log("still fine")


class _LoggingAdapter(StubAdapter):
    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        progress.log(f"session {packet.task['id']}")
        return super().run(packet, sandbox)


def test_a_work_pool_worker_thread_reaches_the_submitters_sink(tmp_path: Path) -> None:
    src = tmp_path / "source"
    src.mkdir()
    lines: list[str] = []
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(src),
        adapter_factory=lambda _name: _LoggingAdapter(),
        max_slots=1,
    )
    with pool, progress.sink(lines.append):
        ids = [
            pool.submit(
                ExecutionTask(
                    packet=TaskPacket(
                        task={"id": tid, "title": tid},
                        worktree_path=str(src),
                        turn_budget=1,
                        cost_budget_microusd=1_000_000,
                    ),
                    adapter="stub",
                )
            )
            for tid in ("t1", "t2")
        ]
        for tid in ids:
            pool.result(tid)
    assert sorted(line.split("] ", 1)[1] for line in lines) == ["session t1", "session t2"]


def test_the_planners_epic_pool_reaches_the_sink() -> None:
    lines: list[str] = []

    def epic(name: str) -> str:
        progress.log(f"epic {name}")
        return name

    with progress.sink(lines.append):
        assert _run_parallel(epic, ["a", "b", "c"]) == ["a", "b", "c"]
    assert sorted(line.split("] ", 1)[1] for line in lines) == ["epic a", "epic b", "epic c"]


def test_run_settings_override_the_env_and_log_cb_gets_the_runs_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOUNDRY_PROGRESS", raising=False)
    monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", "off")
    monkeypatch.setenv("FOUNDRY_SELF_CHECK", "1")
    monkeypatch.setenv("FOUNDRY_NATIVE_TOOLS", "0")
    seen: dict[str, Any] = {}
    real_proxy, real_adapter, real_graph = (
        cli.wiring._build_proxy,
        cli.wiring._resolve_coding_adapter,
        cli.runner.build_factory_graph,
    )

    def build_proxy(backend: str, **kw: Any) -> Any:
        seen["local_thinking"] = kw.get("local_thinking")
        proxy = real_proxy(backend, **kw)
        seen["parsed_thinking"] = proxy._local_thinking
        return proxy

    def resolve_adapter(adapter: Any, **kw: Any) -> Any:
        seen["native_tools"] = kw.get("native_tools")
        return real_adapter(adapter, **kw)

    def build_graph(*args: Any, **kw: Any) -> Any:
        seen["self_check_mode"] = kw.get("self_check_mode")
        return real_graph(*args, **kw)

    monkeypatch.setattr(cli.wiring, "_build_proxy", build_proxy)
    monkeypatch.setattr(cli.wiring, "_resolve_coding_adapter", resolve_adapter)
    monkeypatch.setattr(cli.runner, "build_factory_graph", build_graph)
    lines: list[str] = []
    result = cli.run(
        "build a tiny URL-shortener service",
        thread_id="progress-sink-run",
        log_cb=lines.append,
        local_thinking="agent",
        self_check="off",
        native_tools=True,
    )
    assert result.passed
    assert seen == {
        "local_thinking": "agent",
        "parsed_thinking": "agent",
        "native_tools": True,
        "self_check_mode": "off",
    }
    # The model calls run in the graph's node threads; their lines reach the run's sink.
    assert any("start model call:" in line for line in lines)
    assert any("done  model call:" in line for line in lines)


def test_a_done_line_shows_the_local_tokens_beside_the_capped_total() -> None:
    from foundry.proxy.token_meter import TokenMeter

    meter = TokenMeter(cap=None)
    meter.note_local({"input_tokens": 400_000, "output_tokens": 20_000})
    lines: list[str] = []
    with progress.sink(lines.append):
        progress.finished("model call: planner", time.monotonic(), None, meter)
    assert meter.used == 0
    assert lines[-1].endswith("(run 0.00M, local 0.42M)")
