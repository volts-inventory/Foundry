"""Per-run token cap: shared by the proxy and the coding substrate, checked before
each call so a run stops at a clean boundary, and never mistaken for a task failure."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.cli import main
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.execution.sandbox import Sandbox
from foundry.execution.work_pool import ExecutionTask, LocalWorkPool
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.proxy.token_meter import TokenCapExceeded, TokenMeter, weighted_tokens


def test_cache_reads_count_at_a_tenth() -> None:
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 1000}
    assert weighted_tokens(usage) == 10 + 5 + 100 + 100


def test_proxy_stops_before_the_call_that_would_exceed_the_cap() -> None:
    transport = MockTransport(default_text="ok", usage={"input_tokens": 60, "output_tokens": 0})
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(b"k" * 16, "kid"))
    proxy.token_meter = TokenMeter(100)
    call = ("node", "T2", "SOP", [{"role": "user", "content": "x"}])

    proxy.complete(*call, purpose="ideation")
    proxy.complete(*call, purpose="ideation")  # 60 < 100: allowed, ends at 120
    with pytest.raises(TokenCapExceeded):
        proxy.complete(*call, purpose="ideation")
    assert len(proxy.manifest) == 2


def test_coding_session_is_not_started_once_the_cap_is_spent(tmp_path: Path) -> None:
    ran: list[object] = []
    adapter = ClaudeCodeAdapter(exec_fn=lambda *a, **k: ran.append(a))  # type: ignore[arg-type,return-value]
    meter = TokenMeter(10)
    meter.charge({"input_tokens": 10})
    adapter.token_meter = meter
    with Sandbox(tmp_path) as sb, pytest.raises(TokenCapExceeded):
        adapter.run(TaskPacket(task={"id": "t"}, worktree_path=str(sb.worktree),
                               turn_budget=1, cost_budget_microusd=1), sb)
    assert ran == []


class _CappedAdapter:
    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        raise TokenCapExceeded("token cap reached")


def test_pool_surfaces_the_cap_instead_of_a_task_failure(tmp_path: Path) -> None:
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(tmp_path),
        adapter_factory=lambda _name: _CappedAdapter(),  # type: ignore[arg-type,return-value]
        max_slots=1,
    )
    packet = TaskPacket(task={"id": "t"}, worktree_path=str(tmp_path), turn_budget=1,
                        cost_budget_microusd=1)
    tid = pool.submit(ExecutionTask(packet=packet, adapter="x", lineage="default"))
    with pytest.raises(TokenCapExceeded):
        pool.result(tid)
    pool.shutdown()


def test_cli_exits_3_with_a_resume_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--token-cap", "1", "--state-dir", str(tmp_path), "build a thing"])
    assert code == 3
    err = capsys.readouterr().err
    assert "token cap reached" in err and "rerun the same command" in err
    # The hint holds: the same command without the cap continues to a finished run.
    assert main(["--state-dir", str(tmp_path), "build a thing"]) == 0


def test_rerun_resumes_the_capped_thread_instead_of_replanning(tmp_path: Path) -> None:
    """LangGraph starts a NEW run whenever it is given input; a rerun of a stopped
    thread must continue it, not re-plan and build a second set of tickets."""
    from foundry.cli import run

    with pytest.raises(TokenCapExceeded):
        run("build a thing", state_dir=str(tmp_path), token_cap=1)
    nodes: list[str] = []
    result = run("build a thing", state_dir=str(tmp_path),
                 progress_cb=lambda node, scope, **_k: nodes.append(node))
    assert result.passed
    assert nodes, "the resumed run should still stream its remaining nodes"
    assert "planner" not in nodes and "triage" not in nodes
