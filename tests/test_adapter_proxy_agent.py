"""ProxyAgentAdapter (ROADMAP 1c): agentic inner loop, original trust story.

Every model call rides the injected complete_fn (a proxy closure in prod); the
adapter holds no credentials and declares NO sandbox grant. Covers: the action
cycle (write/run/read/done), turn-budget bound, path containment fail-closed,
and the full-file-snapshot output contract shared with claude-code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundry.execution.adapter_contract import TaskPacket
from foundry.execution.adapters.proxy_agent import ACTION_SCHEMA, ProxyAgentAdapter
from foundry.execution.sandbox import Sandbox


def make_packet(worktree: Path, turn_budget: int = 6) -> TaskPacket:
    return TaskPacket(
        task={"id": "t-1", "title": "t-1", "branch": "task/t-1", "criterion_ids": []},
        pinned_constraints=("CONSTRAINT: stdlib only.",),
        worktree_path=str(worktree),
        turn_budget=turn_budget,
        cost_budget_microusd=1_000_000,
    )


def scripted(actions: list[dict[str, Any]]):
    """A complete_fn yielding one scripted action per turn (schema path)."""
    it = iter(actions)

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        assert schema is ACTION_SCHEMA
        act = next(it)
        return json.dumps(act), act

    return _complete


def test_write_run_done_cycle(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    adapter = ProxyAgentAdapter(
        scripted(
            [
                {"action": "write_file", "path": "mod.py", "content": "X = 1\n"},
                {"action": "run", "cmd": "python -c 'import mod'"},
                {"action": "done", "summary": "built mod", "triage": ["no docs yet"]},
            ]
        )
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body == {"kind": "full-file-snapshot", "files": {"mod.py": "X = 1\n"}}
    assert result.turns_used == 3
    assert [t.explanation for t in result.triage] == ["no docs yet"]


def test_observations_feed_back(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.txt").write_text("hello-from-tree\n")
    seen: list[list[dict[str, Any]]] = []

    actions = iter(
        [
            {"action": "read_file", "path": "data.txt"},
            {"action": "done", "summary": "ok"},
        ]
    )

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        seen.append(list(messages))
        act = next(actions)
        return json.dumps(act), act

    adapter = ProxyAgentAdapter(_complete)
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    # Turn 2's message list carries turn 1's observation (the read content).
    assert any(
        "hello-from-tree" in str(m.get("content", "")) for m in seen[1]
    )


def test_turn_budget_bounds_the_loop(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        act = {"action": "run", "cmd": "true"}  # never says done
        return json.dumps(act), act

    adapter = ProxyAgentAdapter(_complete)
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=3), sb)
    assert result.turns_used == 3
    assert any("turn budget exhausted" in t.explanation for t in result.triage)


def test_path_escape_is_an_observation_error_not_a_write(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    adapter = ProxyAgentAdapter(
        scripted(
            [
                {"action": "write_file", "path": "../evil.py", "content": "x"},
                {"action": "done", "summary": "ok"},
            ]
        )
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {}  # nothing escaped, nothing written
    assert not (tmp_path / "evil.py").exists()


def test_declares_no_sandbox_grant() -> None:
    assert ProxyAgentAdapter.credential_grant == ()
    assert ProxyAgentAdapter.home_passthrough is False
