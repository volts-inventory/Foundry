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
    assert any("hello-from-tree" in str(m.get("content", "")) for m in seen[1])


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


def test_a_symlink_to_a_name_prefixed_sibling_is_contained(tmp_path: Path) -> None:
    from foundry.execution.adapters.proxy_agent import _contained

    root = tmp_path / "wt"
    sibling = tmp_path / "wt2"
    root.mkdir()
    sibling.mkdir()
    (root / "link").symlink_to(sibling)
    try:
        _contained(root, "link/evil.py")
    except ValueError:
        pass
    else:
        raise AssertionError("a path into /wt2 passed containment for /wt")
    assert _contained(root, "ok.py") == (root / "ok.py").resolve()


def test_declares_no_sandbox_grant() -> None:
    assert ProxyAgentAdapter.credential_grant == ()
    assert ProxyAgentAdapter.home_passthrough is False


def recording(actions: list[dict[str, Any]], seen: list[list[dict[str, Any]]]):
    it = iter(actions)

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        seen.append([dict(m) for m in messages])
        act = next(it)
        return json.dumps(act), act

    return _complete


def observations(msgs: list[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in msgs if str(m["content"]).startswith("OBSERVATION")]


def test_masking_elides_in_batches_with_stable_placeholders(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    n = 11
    for i in range(n):
        (src / f"f{i}.txt").write_text(f"body-{i}\n")
    seen: list[list[dict[str, Any]]] = []
    reads = [{"action": "read_file", "path": f"f{i}.txt"} for i in range(n)]
    adapter = ProxyAgentAdapter(recording([*reads, {"action": "done"}], seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=n + 1), sb)
    for turn, msgs in enumerate(seen):
        obs = observations(msgs)
        assert len(obs) == turn
        kept = [o for o in obs if "elided" not in o]
        # Newest observations stay verbatim: at least min(3, all) and at most 6.
        assert min(3, turn) <= len(kept) <= 6
        assert kept == obs[len(obs) - len(kept) :]
    # Prefix rewrites happen in batches (on the 7th and 11th observations), not on every turn.
    elided = [sum("elided" in o for o in observations(m)) for m in seen]
    assert elided == [0] * 7 + [4] * 4 + [8]
    last = observations(seen[-1])
    assert last[0] == (
        "OBSERVATION: [output of read_file f0.txt elided — re-run or re-read if needed]"
    )
    # Once elided, a placeholder is byte-identical on every later turn (prefix cache).
    assert observations(seen[7])[:4] == last[:4]
    # Assistant actions are never rewritten.
    assert [m["content"] for m in seen[-1] if m["role"] == "assistant"] == [
        json.dumps(a) for a in reads
    ]


def test_repeat_detector_warns_on_third_and_stops_on_fifth(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    act = {"action": "run", "cmd": "true"}
    adapter = ProxyAgentAdapter(recording([act] * 10, seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=10), sb)
    assert result.turns_used == 5
    obs = observations(seen[-1])
    assert "WARNING" not in obs[1]
    assert "WARNING" in obs[2] and "3 times" in obs[2]
    assert "no change to the worktree" in obs[2]
    notes = [t.explanation for t in result.triage]
    assert any("stopped early" in n and "repeated" in n for n in notes)
    assert not any("turn budget exhausted" in n for n in notes)


def test_repeat_count_resets_after_a_real_write(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    run = {"action": "run", "cmd": "true"}
    actions: list[dict[str, Any]] = []
    for i in range(4):
        actions += [run, {"action": "write_file", "path": "m.py", "content": f"X = {i}\n"}]
    adapter = ProxyAgentAdapter(scripted([*actions, run, {"action": "done", "summary": "ok"}]))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=12), sb)
    assert [t.explanation for t in result.triage] == ["ok"]


def test_repeat_count_resets_after_a_change_made_via_run(tmp_path: Path) -> None:
    # edit-via-sed -> test loops must not trip the detector: the tree changed.
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("X = 0\n")
    test = {"action": "run", "cmd": "python -c 'import m'"}
    actions: list[dict[str, Any]] = []
    for i in range(1, 6):
        actions += [test, {"action": "run", "cmd": f"echo 'X = {i}' > m.py"}]
    adapter = ProxyAgentAdapter(scripted([*actions, test, {"action": "done", "summary": "ok"}]))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=14), sb)
    assert [t.explanation for t in result.triage] == ["ok"]


def test_repeat_count_ignores_cache_churn(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    act = {"action": "run", "cmd": "mkdir -p __pycache__ && date +%s%N > __pycache__/x"}
    adapter = ProxyAgentAdapter(scripted([act] * 10))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=10), sb)
    assert result.turns_used == 5


def test_read_file_range_and_truncation_hint(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "small.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    (src / "big.txt").write_text("".join(f"{'x' * 99}\n" for _ in range(100)))
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "read_file", "path": "small.txt", "start_line": 3, "end_line": 4},
                {"action": "read_file", "path": "big.txt"},
                {"action": "done"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    ranged, big = observations(seen[-1])
    assert ranged == "OBSERVATION:\nsmall.txt (lines 3-4 of 10)\nline3\nline4\n"
    assert "truncated at line 40 of 100; read_file with start_line=41" in big


def test_search_finds_match_and_rejects_escape(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("def foo():\n    return 42\n")
    (src / "blob.bin").write_bytes(b"\0return 42")
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "search", "pattern": r"return \d+"},
                {"action": "search", "pattern": "x", "path": "../"},
                {"action": "done"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    found, escaped = observations(seen[-1])
    assert found == "OBSERVATION:\npkg/a.py:2: return 42"
    assert escaped.startswith("OBSERVATION:\nERROR:") and "escapes the worktree" in escaped


def test_search_skips_env_dirs_big_files_and_long_lines(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for d in (".venv", "node_modules", "build", "__pycache__"):
        (src / d).mkdir()
        (src / d / "x.py").write_text("needle\n")
    (src / "big.txt").write_text("needle\n" + "y" * 1_100_000)
    (src / "long.txt").write_text("needle" + "z" * 3_000 + "\nneedle short\n")
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording([{"action": "search", "pattern": "needle"}, {"action": "done"}], seen)
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    assert observations(seen[-1]) == ["OBSERVATION:\nlong.txt:2: needle short"]


def test_lint_on_write_never_crashes_and_ignores_warnings(tmp_path: Path, monkeypatch: Any) -> None:
    import warnings

    from foundry.execution.adapters import proxy_agent

    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "write_file", "path": "warn.py", "content": 'x = "\\d"\n'},
                {"action": "write_file", "path": "deep.py", "content": "x = 1\n"},
                {"action": "done"},
            ],
            seen,
        )
    )
    real_compile = compile
    calls = iter([real_compile, None])

    def fake_compile(*args: Any, **kwargs: Any) -> Any:
        fn = next(calls)
        if fn is None:
            raise RecursionError("maximum recursion depth exceeded during compilation")
        return fn(*args, **kwargs)

    monkeypatch.setattr(proxy_agent, "compile", fake_compile, raising=False)
    with Sandbox(src) as sb, warnings.catch_warnings():
        warnings.simplefilter("error")  # a leaked SyntaxWarning would fail the run
        adapter.run(make_packet(sb.worktree), sb)
    warn, deep = observations(seen[-1])
    assert "SYNTAX" not in warn
    assert "LIKELY SYNTAX ERROR" in deep and "RecursionError" in deep
    assert "compile check" in deep


def test_syntax_error_reported_after_writing_bad_python(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "write_file", "path": "bad.py", "content": "x = 1\ndef f(:\n"},
                {"action": "write_file", "path": "ok.py", "content": "x = 1\n"},
                {"action": "done"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    bad, ok = observations(seen[-1])
    assert "LIKELY SYNTAX ERROR line 2" in bad and "host Python" in bad
    assert "SYNTAX" not in ok
