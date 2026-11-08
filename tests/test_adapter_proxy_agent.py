"""ProxyAgentAdapter (ROADMAP 1c): agentic inner loop, original trust story.

Every model call rides the injected complete_fn (a proxy closure in prod); the
adapter holds no credentials and declares NO sandbox grant. Covers: the action
cycle (write/run/read/done), turn-budget bound, path containment fail-closed,
and the full-file-snapshot output contract shared with claude-code.
"""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from foundry.execution.adapter_contract import TaskPacket
from foundry.execution.adapters.proxy_agent import ACTION_SCHEMA, TOOLS, ProxyAgentAdapter
from foundry.execution.sandbox import Sandbox
from foundry.proxy import ToolsUnsupported


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
    assert [t.explanation for t in result.triage] == ["built mod", "no docs yet"]


def test_each_action_reports_one_activity_line(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[str] = []

    def listener(line: str) -> None:
        seen.append(line)
        raise RuntimeError("a broken listener must not break the session")

    adapter = ProxyAgentAdapter(
        scripted(
            [
                {"action": "write_file", "path": "mod.py", "content": "X = 1\n"},
                {"action": "read_file", "path": "mod.py"},
                {"action": "run", "cmd": "python -c 'import mod'"},
                {"action": "search", "pattern": "X ="},
                {"action": "done", "summary": "built mod"},
            ]
        ),
        activity_cb=listener,
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert result.turns_used == 5
    assert seen == [
        "writing mod.py",
        "reading mod.py",
        "running python -c 'import mod'",
        "searching for X =",
        "done: built mod",
    ]


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


def _repair_packet(worktree: Path, turn_budget: int = 6) -> TaskPacket:
    return make_packet(worktree, turn_budget).model_copy(
        update={"prior_failures": ("[build gate FAILED]", "app.py:1: error: bad")}
    )


def test_a_repair_sessions_first_done_without_an_edit_is_refused(tmp_path: Path) -> None:
    # A local fixer named every fix in its done summary and changed no file: the
    # empty diff halted the wave as no progress.
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = (\n")
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "read_file", "path": "app.py"},
                {
                    "action": "done",
                    "summary": "Let me fix these one at a time",
                    "triage": ["Need to fix app.py"],
                },
                {"action": "write_file", "path": "app.py", "content": "x = 1\n"},
                {"action": "done", "summary": "fixed app.py"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        result = adapter.run(_repair_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"app.py": "x = 1\n"}
    assert result.turns_used == 4
    refusal = str(seen[2][-1]["content"])
    assert "ERROR: not done — no file in the worktree has changed yet" in refusal
    assert "package installed in this sandbox" in refusal
    assert refusal.endswith("Your triage names this unfinished work:\n- Need to fix app.py")


def test_a_repair_session_that_insists_on_done_ends(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    done = {"action": "done", "summary": "the failure is in the gate, not the code"}
    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(scripted([done, done])).run(_repair_packet(sb.worktree), sb)
    assert result.turns_used == 2
    assert [t.explanation for t in result.triage] == [done["summary"]]
    # On the last turn there is none left to edit in: done ends the session.
    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(scripted([done])).run(_repair_packet(sb.worktree, 1), sb)
    assert result.turns_used == 1


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
    seen: list[list[dict[str, Any]]] = []
    runs = [{"action": "run", "cmd": f"echo body-{i}"} for i in range(n)]
    adapter = ProxyAgentAdapter(recording([*runs, {"action": "done"}], seen))
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
        "OBSERVATION: [output of run 'echo body-0' elided — re-run or re-read if needed]"
    )
    # Once elided, a placeholder is byte-identical on every later turn (prefix cache).
    assert observations(seen[7])[:4] == last[:4]
    # Assistant actions are never rewritten.
    assert [m["content"] for m in seen[-1] if m["role"] == "assistant"] == [
        json.dumps(a) for a in runs
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
    (src / "big.txt").write_text("".join(f"{'x' * 99}\n" for _ in range(200)))
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
    assert "truncated at line 120 of 200" in big
    assert "get the rest in ONE call: read_file with start_line=121 —" in big


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


def test_a_write_without_a_path_says_the_path_is_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording([{"action": "write_file", "content": "x = 1\n"}, {"action": "done"}], seen)
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    (obs,) = observations(seen[-1])
    assert obs.startswith('OBSERVATION:\nERROR: no "path" given — nothing done')
    assert "escapes" not in obs


def test_a_session_run_that_misses_a_step_argument_gets_the_gates_why(tmp_path: Path) -> None:
    # A local fixer ran the scenarios itself 15 times and read only pytest's
    # "fixture not found", never the gate's explanation of it.
    src = tmp_path / "src"
    (src / "features").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "features" / "T1.feature").write_text(
        "Feature: T1\n  Scenario: A\n    Given a note\n    When it is read\n"
    )
    (src / "tests" / "test_t1.py").write_text(
        'scenarios("../features/T1.feature")\n\n'
        '@given("a note", target_fixture="note_a")\ndef _a():\n    return 1\n\n'
        '@given("a note", target_fixture="note_b")\ndef _b():\n    return 2\n'
    )
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "run", "cmd": "echo \"E  fixture 'note_a' not found\""},
                {"action": "run", "cmd": "echo fine"},
                {"action": "done"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    hit, plain = observations(seen[-1])
    assert '\nNOTE: step argument `note_a` is never set: `@given("a note")` is defined 2 ' in hit
    assert "NOTE:" not in plain


def test_a_failing_session_run_names_a_step_text_defined_twice(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "features").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "features" / "T1.feature").write_text(
        "Feature: T1\n  Scenario: A\n    Given a user\n    Then the user is returned\n"
    )
    (src / "tests" / "test_t1.py").write_text(
        'scenarios("../features/T1.feature")\n\n'
        '@then("the user is returned")\ndef _a(context):\n    assert context.user\n\n'
        '@then("the user is returned")\ndef _b(context):\n    assert context.login\n'
    )
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "run", "cmd": "echo 'FAILED tests/test_t1.py::test_a'"},
                {"action": "run", "cmd": "echo fine"},
                {"action": "done"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    hit, plain = observations(seen[-1])
    assert (
        '\nNOTE: `@then("the user is returned")` is defined 2 times in tests/test_t1.py '
        "(lines 3, 7)"
    ) in hit
    assert "NOTE:" not in plain


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
    assert deep.startswith("OBSERVATION:\nwrote deep.py") and "RecursionError" in deep
    assert "compile check" in deep


def write_session(
    tmp_path: Path, writes: list[dict[str, Any]], seed: dict[str, str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """Run ``writes`` then done; the observations and the worktree's files after."""
    src = tmp_path / "src"
    src.mkdir()
    for rel, text in (seed or {}).items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(recording([*writes, {"action": "done"}], seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=len(writes) + 1), sb)
        tree = {
            p.relative_to(sb.worktree).as_posix(): p.read_text()
            for p in sb.worktree.rglob("*")
            if p.is_file()
        }
    return observations(seen[-1]), tree


def test_rejected_python_write_keeps_old_content_and_drops_new_file(tmp_path: Path) -> None:
    obs, tree = write_session(
        tmp_path,
        [
            {"action": "write_file", "path": "old.py", "content": "x = 1\ndef f(:\n    pass\n"},
            {"action": "write_file", "path": "pkg/new.py", "content": "if True\n"},
        ],
        seed={"old.py": "X = 0\n"},
    )
    old, new = obs
    assert "ERROR: write rejected — old.py has a Python syntax error at line 2" in old
    assert "previous content is kept" in old and "    2 | def f(:" in old and "^" in old
    assert "pkg/new.py" in new and "it was not created" in new
    assert tree == {"old.py": "X = 0\n"}


def test_rejected_json_and_toml_writes(tmp_path: Path) -> None:
    obs, tree = write_session(
        tmp_path,
        [
            {"action": "write_file", "path": "a.json", "content": '{"a": 1,\n "b": }\n'},
            {"action": "write_file", "path": "pyproject.toml", "content": "[project\nname = 1\n"},
            {"action": "write_file", "path": "tsconfig.json", "content": "{ // jsonc\n}\n"},
            {"action": "write_file", "path": "ok.json", "content": '{"a": [1]}'},
        ],
    )
    bad_json, bad_toml, jsonc, ok = obs
    assert "invalid JSON at line 2, column 7" in bad_json and '    2 |  "b": }' in bad_json
    assert "invalid TOML at line 1" in bad_toml and "not created" in bad_toml
    assert "wrote tsconfig.json" in jsonc and "wrote ok.json" in ok
    assert set(tree) == {"tsconfig.json", "ok.json"}


def test_rejected_yaml_write_but_custom_tags_and_templates_pass(tmp_path: Path) -> None:
    obs, tree = write_session(
        tmp_path,
        [
            {"action": "write_file", "path": "a.yaml", "content": "a: [1, 2\nb: 3\n"},
            {"action": "write_file", "path": "cf.yml", "content": "Value: !Ref Bucket\n"},
            {"action": "write_file", "path": "t.yaml", "content": "a: {{ .Values.x }\n"},
        ],
    )
    assert "invalid YAML at line 2" in obs[0]
    assert set(tree) == {"cf.yml", "t.yaml"}


def test_rejected_feature_write(tmp_path: Path) -> None:
    good = "Feature: cart\n  Scenario: add\n    Given an empty cart\n"
    obs, tree = write_session(
        tmp_path,
        [
            {
                "action": "write_file",
                "path": "cart.feature",
                "content": (
                    "Feature: cart\n  Scenario: add\n    Given x\n"
                    "  Examples:\n  | a |\n  stray text\n"
                ),
            },
            {"action": "write_file", "path": "cart.feature", "content": good},
        ],
    )
    assert "malformed Gherkin at line 6" in obs[0] and "stray text" in obs[0]
    assert obs[1].startswith("OBSERVATION:\nwrote cart.feature")
    assert tree == {"cart.feature": good}


@pytest.mark.skipif(
    not (shutil.which("gofmt") and shutil.which("node")), reason="needs gofmt and node"
)
def test_rejected_go_and_module_js_writes(tmp_path: Path) -> None:
    obs, tree = write_session(
        tmp_path,
        [
            {"action": "write_file", "path": "main.go", "content": "package main\nfunc main() {\n"},
            {"action": "write_file", "path": "a.mjs", "content": "export const a = ;\n"},
            {"action": "write_file", "path": "ok.mjs", "content": 'import x from "y";\n'},
            {"action": "write_file", "path": "app.js", "content": "const a = <div/>;\n"},
        ],
    )
    assert "Go syntax error at line 2, column 15" in obs[0]
    assert "JavaScript syntax error at line 1" in obs[1] and "SyntaxError" in obs[1]
    assert set(tree) == {"ok.mjs", "app.js"}


def test_accepted_write_is_unchanged_and_gets_mypy_advice(tmp_path: Path) -> None:
    obs, tree = write_session(
        tmp_path,
        [
            {"action": "write_file", "path": "ok.py", "content": "x = 1\n"},
            {
                "action": "write_file",
                "path": "typed.py",
                "content": "def f(x: int) -> str:\n    return x\n",
            },
        ],
    )
    assert obs[0] == "OBSERVATION:\nwrote ok.py (6 chars)"
    assert "wrote typed.py" in obs[1] and "ADVISORY (mypy" in obs[1]
    assert "typed.py:2: error: Incompatible return value type" in obs[1]
    assert tree["typed.py"] == "def f(x: int) -> str:\n    return x\n"
    assert not any(".mypy_cache" in p for p in tree)


def runs(n: int) -> list[dict[str, Any]]:
    return [{"action": "run", "cmd": f"echo filler-{i}"} for i in range(n)]


def test_unchanged_file_reads_survive_masking(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("".join(f"a{i}\n" for i in range(1, 7)))
    (src / "b.txt").write_text("".join(f"b{i}\n" for i in range(1, 7)))
    seen: list[list[dict[str, Any]]] = []
    actions = [
        {"action": "read_file", "path": "a.txt", "start_line": 1, "end_line": 2},
        {"action": "read_file", "path": "a.txt", "start_line": 3, "end_line": 4},
        {"action": "read_file", "path": "b.txt", "start_line": 1, "end_line": 2},
        {"action": "read_file", "path": "b.txt"},
        *runs(8),
        {"action": "done"},
    ]
    adapter = ProxyAgentAdapter(recording(actions, seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=len(actions)), sb)
    obs = observations(seen[-1])
    # Disjoint partial reads of an unchanged file both stay; a read covered by a
    # newer one of the same file is superseded.
    assert obs[0].endswith("a1\na2\n") and obs[1].endswith("a3\na4\n")
    assert obs[2] == (
        "OBSERVATION: [output of read_file b.txt lines 1-2 elided — re-run or re-read if needed]"
    )
    assert obs[3].endswith("b1\nb2\nb3\nb4\nb5\nb6\n")
    assert all("elided" in o for o in obs[4:7])  # the run outputs still batch-elide
    assert not any("elided" in o for o in obs[7:])


def test_read_is_elided_after_the_file_is_written(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("X = 0\n")
    seen: list[list[dict[str, Any]]] = []
    actions = [
        {"action": "read_file", "path": "a.py"},
        {"action": "write_file", "path": "a.py", "content": "X = 1  # changed\n"},
        *runs(6),
        {"action": "done"},
    ]
    adapter = ProxyAgentAdapter(recording(actions, seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=len(actions)), sb)
    assert "X = 0" in observations(seen[2])[0]  # stale read waits for the next batch pass
    assert observations(seen[-1])[0] == (
        "OBSERVATION: [output of read_file a.py elided — re-run or re-read if needed]"
    )


def big_files(src: Path, n: int) -> None:
    for i in range(n):  # ~12k chars each: just under the read cap
        (src / f"f{i}.txt").write_text("".join(f"{i}{'y' * 98}\n" for _ in range(119)))


def rewrite_passes(seen: list[list[dict[str, Any]]]) -> int:
    return sum(
        any(after[i] != m for i, m in enumerate(before))
        for before, after in itertools.pairwise(seen)
    )


def test_pinned_reads_are_capped_oldest_first_in_few_passes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    big_files(src, 8)
    seen: list[list[dict[str, Any]]] = []
    actions = [{"action": "read_file", "path": f"f{i}.txt"} for i in range(8)]
    adapter = ProxyAgentAdapter(recording([*actions, {"action": "done"}], seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=9), sb)
    kept = ["elided" not in o for o in observations(seen[-1])]
    assert kept == [False] * 4 + [True] * 4
    assert rewrite_passes(seen) <= 2


def test_paging_through_a_file_is_not_thrash(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 81)))
    seen: list[list[dict[str, Any]]] = []
    pages = [
        {"action": "read_file", "path": "big.py", "start_line": i, "end_line": i + 9}
        for i in range(1, 81, 10)
    ]
    adapter = ProxyAgentAdapter(recording([*pages, {"action": "done", "summary": "ok"}], seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=9), sb)
    assert [t.explanation for t in result.triage] == ["ok"]
    assert not any("NOTE" in o for o in observations(seen[-1]))


def test_paging_with_shared_boundary_lines_is_not_thrash(tmp_path: Path) -> None:
    # A local fixer paged a 614-line module as 1-100, 100-200, ...: each read shares
    # one line with the one before, and it was stopped before its first edit.
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 61)))
    seen: list[list[dict[str, Any]]] = []
    pages = [
        {"action": "read_file", "path": "big.py", "start_line": i, "end_line": i + 10}
        for i in (1, 10, 20, 30, 40, 50)
    ]
    adapter = ProxyAgentAdapter(recording([*pages, {"action": "done", "summary": "ok"}], seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=7), sb)
    assert [t.explanation for t in result.triage] == ["ok"]
    assert not any("NOTE" in o for o in observations(seen[-1]))


def test_rereading_an_elided_read_is_not_thrash(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("A = 1\n")
    big_files(src, 6)
    seen: list[list[dict[str, Any]]] = []
    a = {"action": "read_file", "path": "a.py"}
    fill = [{"action": "read_file", "path": f"f{i}.txt"} for i in range(6)]
    # 6 reads of a.py in all, but the first is elided by the pin cap before the second.
    rereads = [
        {**a, "start_line": 1, "end_line": n} for n in range(1, 6)
    ]  # dodge the repeat detector
    actions = [a, *fill, *rereads, {"action": "done", "summary": "ok"}]
    adapter = ProxyAgentAdapter(recording(actions, seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=len(actions)), sb)
    assert [t.explanation for t in result.triage] == ["ok"]
    obs = observations(seen[-1])
    assert "elided" in obs[0]
    assert not any("NOTE" in o for o in obs[7:10])
    assert "read a.py 4 times" in obs[10]  # the re-read of the elided copy did not count


def test_read_thrash_nudges_on_fourth_and_stops_on_sixth(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "test_app.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 31)))
    seen: list[list[dict[str, Any]]] = []
    reads = [
        {"action": "read_file", "path": "test_app.py", "start_line": i, "end_line": i + 5}
        for i in range(1, 11)
    ]
    adapter = ProxyAgentAdapter(recording(reads, seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=10), sb)
    assert result.turns_used == 6
    obs = observations(seen[-1])
    assert not any("NOTE" in o for o in obs[:3])
    assert "read test_app.py 4 times without changing it" in obs[3]
    assert "make the edit or finish" in obs[3] and "5 times" in obs[4]
    notes = [t.explanation for t in result.triage]
    assert any("stopped early: read test_app.py 6 times" in n for n in notes)
    assert not any("turn budget exhausted" in n for n in notes)


def test_read_thrash_count_resets_after_a_write(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("X = 0\n")
    read = [
        {"action": "read_file", "path": "m.py", "start_line": 1, "end_line": i} for i in (1, 2, 3)
    ]
    write = {"action": "write_file", "path": "m.py", "content": "X = 10\n"}
    seen: list[list[dict[str, Any]]] = []
    actions = [*read, write, *read, {"action": "done", "summary": "ok"}]
    adapter = ProxyAgentAdapter(recording(actions, seen))
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree, turn_budget=len(actions)), sb)
    assert [t.explanation for t in result.triage] == ["ok"]
    assert not any("NOTE" in o for o in observations(seen[-1]))


def test_masked_prefix_is_stable_across_turns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("X = 0\n")
    (src / "b.py").write_text("Y = 0\n")
    seen: list[list[dict[str, Any]]] = []
    actions: list[dict[str, Any]] = [{"action": "read_file", "path": "b.py"}]
    for i in range(6):
        actions += [
            {"action": "read_file", "path": "a.py"},
            {"action": "write_file", "path": "a.py", "content": f"X = {i + 1}\n"},
            {"action": "run", "cmd": f"echo step-{i}"},
        ]
    adapter = ProxyAgentAdapter(recording([*actions, {"action": "done"}], seen))
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=len(actions) + 1), sb)
    rewrites = 0
    for before, after in itertools.pairwise(seen):
        changed = [i for i, m in enumerate(before) if after[i] != m]
        # The only rewrite is verbatim -> placeholder; a placeholder never changes.
        assert all("elided" in after[i]["content"] for i in changed)
        assert not any("elided" in before[i]["content"] for i in changed)
        rewrites += bool(changed)
    assert 0 < rewrites <= len(actions) // 4
    assert "Y = 0" in observations(seen[-1])[0]  # the unchanged file is never masked


# ---- native tool calls (Phase 5, opt-in) -------------------------------------------


def tool_turns(turns: list[Any], seen: list[list[dict[str, Any]]]):
    """A tool_complete_fn: each scripted turn is a list of (name, input) calls, or
    a str for a plain-text reply with no tool call."""
    it = iter(turns)

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        assert tools is TOOLS
        seen.append([dict(m) for m in messages])
        turn = next(it)
        if isinstance(turn, str):
            return turn, ()
        n = len(seen)
        return "", [
            {"id": f"t{n}-{i}", "name": name, "input": args} for i, (name, args) in enumerate(turn)
        ]

    return _tools


def no_json(messages: Any, schema: Any) -> tuple[str, Any]:
    raise AssertionError("the JSON path must not run when native tools work")


def results_of(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = messages[-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    return list(last["content"])


def test_native_tool_calls_in_one_turn_run_in_order_as_tool_results(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                [
                    ("write_file", {"path": "a.txt", "content": "first\n"}),
                    ("read_file", {"path": "a.txt"}),
                    ("write_file", {"path": "a.txt", "content": "second\n"}),
                    ("run", {"cmd": "cat a.txt"}),
                ],
                [("done", {"summary": "built", "triage": ["no docs"]})],
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert result.turns_used == 2  # one turn per model call, not per tool call
    assert body["files"] == {"a.txt": "second\n"}
    assert "Work by calling the provided tools" in seen[0][0]["content"]
    assistant = seen[1][-2]
    assert [b["id"] for b in assistant["content"]] == ["t1-0", "t1-1", "t1-2", "t1-3"]
    res = results_of(seen[1])
    assert [r["tool_use_id"] for r in res] == ["t1-0", "t1-1", "t1-2", "t1-3"]
    assert res[0]["content"].startswith("wrote a.txt")
    assert "first" in res[1]["content"]
    assert res[3]["content"].startswith("exit 0") and "second" in res[3]["content"]
    assert [t.explanation for t in result.triage] == ["built", "no docs"]


def test_native_containment_and_lint_apply_per_tool_call(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                [
                    ("write_file", {"path": "../evil.py", "content": "x"}),
                    ("write_file", {"path": "bad.py", "content": "x = 1\ndef f(:\n"}),
                    ("read_file", {"path": "/etc/passwd"}),
                    ("search", {"pattern": "x", "path": ".."}),
                ],
                [("done", {"summary": "ok"})],
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    escaped, bad, abs_read, escaped_search = (r["content"] for r in results_of(seen[1]))
    assert escaped.startswith("ERROR:") and "escapes the worktree" in escaped
    assert "write rejected — bad.py has a Python syntax error at line 2" in bad
    assert "escapes the worktree" in abs_read and "escapes the worktree" in escaped_search
    assert not (tmp_path / "evil.py").exists()
    assert body["files"] == {}


def test_native_tool_calls_per_turn_are_capped(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                [("write_file", {"path": f"f{i}.txt", "content": "x"}) for i in range(10)],
                [("done", {"summary": "ok"})],
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert set(body["files"]) == {f"f{i}.txt" for i in range(8)}
    res = results_of(seen[1])
    assert len(res) == 10  # every call is answered, the excess with an error
    assert all(r["content"].startswith("ERROR: not run") for r in res[8:])
    assert result.turns_used == 2


def test_native_plain_text_reply_is_a_nudge_turn(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                "I think I should write a file.",
                [("done", {"summary": "ok"})],
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert result.turns_used == 2
    assert seen[1][-2] == {"role": "assistant", "content": "I think I should write a file."}
    assert "Call a tool" in seen[1][-1]["content"]


def test_native_repeat_detector_counts_each_tool_call(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                [("run", {"cmd": "true"})] * 5,
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert result.turns_used == 1
    assert any("identical action repeated 5 times" in t.explanation for t in result.triage)


def test_native_masking_elides_old_tool_results(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    turns: list[Any] = [
        [("run", {"cmd": f"echo a-{i}"}), ("run", {"cmd": f"echo b-{i}"})] for i in range(5)
    ]
    adapter = ProxyAgentAdapter(
        no_json, tool_complete_fn=tool_turns([*turns, [("done", {"summary": "ok"})]], seen)
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=8), sb)
    contents = [
        r["content"]
        for m in seen[-1]
        if isinstance(m["content"], list)
        for r in m["content"]
        if r.get("type") == "tool_result"
    ]
    assert len(contents) == 10
    assert contents[0] == "[output of run 'echo a-0' elided — re-run or re-read if needed]"
    assert all("elided" not in c for c in contents[-3:])


def test_native_tools_unsupported_falls_back_to_json_for_the_session(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    tool_calls = 0

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        nonlocal tool_calls
        tool_calls += 1
        raise ToolsUnsupported("no tool channel")

    prompts: list[str] = []
    actions = iter(
        [
            {"action": "write_file", "path": "m.py", "content": "X = 1\n"},
            {"action": "done", "summary": "ok"},
        ]
        * 2
    )

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        prompts.append(messages[0]["content"])
        act = next(actions)
        return json.dumps(act), act

    adapter = ProxyAgentAdapter(_complete, tool_complete_fn=_tools)
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"m.py": "X = 1\n"}
    assert result.turns_used == 2  # the rejected call is not a turn
    assert "EXACTLY ONE action as JSON" in prompts[0]
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree), sb)
    assert tool_calls == 1  # remembered: the next ticket goes straight to JSON


def tier_packet(worktree: Path, tier: str) -> TaskPacket:
    return TaskPacket(
        task={
            "id": f"t-{tier}",
            "title": "t",
            "branch": "task/t",
            "criterion_ids": [],
            "tier": tier,
        },
        worktree_path=str(worktree),
        turn_budget=6,
        cost_budget_microusd=1_000_000,
    )


def json_done(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
    act = {"action": "done", "summary": "json"}
    return json.dumps(act), act


def test_native_fallback_is_per_tier(tmp_path: Path) -> None:
    from foundry.execution.adapter_contract import current_packet_tier

    src = tmp_path / "src"
    src.mkdir()
    tool_tiers: list[str | None] = []

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        tier = current_packet_tier()
        tool_tiers.append(tier)
        if tier == "T1":  # e.g. claude-cli: no tool channel
            raise ToolsUnsupported("no tool channel")
        return "", [{"id": "c", "name": "done", "input": {"summary": "native"}}]

    adapter = ProxyAgentAdapter(json_done, tool_complete_fn=_tools)
    summaries = []
    for tier in ("T1", "T2", "T1", "T2"):
        with Sandbox(src) as sb:
            result = adapter.run(tier_packet(sb.worktree, tier), sb)
        summaries.append(result.triage[0].explanation)
    assert summaries == ["json", "native", "json", "native"]
    assert tool_tiers == ["T1", "T2", "T2"]  # T1 remembered; T2 never disabled


def test_a_concurrent_fallback_leaves_a_running_native_session_alone(tmp_path: Path) -> None:
    import threading

    from foundry.execution.adapter_contract import current_packet_tier

    src = tmp_path / "src"
    src.mkdir()
    b_started, a_fell_back = threading.Event(), threading.Event()
    b_calls = 0

    def _tools(messages: Any, tools: Any) -> tuple[str, Any]:
        nonlocal b_calls
        if current_packet_tier() == "T1":
            assert b_started.wait(10)
            raise ToolsUnsupported("no tool channel")
        b_calls += 1
        if b_calls == 1:
            b_started.set()
            return "", [
                {"id": "w", "name": "write_file", "input": {"path": "b.txt", "content": "b"}}
            ]
        assert a_fell_back.wait(10)  # A's fallback lands mid-session for B
        return "", [{"id": "d", "name": "done", "input": {"summary": "native"}}]

    def _json(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        a_fell_back.set()
        return json_done(messages, schema)

    adapter = ProxyAgentAdapter(_json, tool_complete_fn=_tools)
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def session(tier: str) -> None:
        try:
            with Sandbox(src) as sb:
                result = adapter.run(tier_packet(sb.worktree, tier), sb)
                results[tier] = (result, json.loads(sb.get_blob(result.diff_uri)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=session, args=(t,)) for t in ("T1", "T2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert results["T1"][0].triage[0].explanation == "json"
    b_result, b_body = results["T2"]
    assert b_result.triage[0].explanation == "native" and b_result.turns_used == 2
    assert b_body["files"] == {"b.txt": "b"}


def test_a_bdd_run_gets_a_report_path_and_a_note_when_it_writes_none(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    actions = iter(
        [
            {"action": "run", "cmd": 'echo cucumber > "$FOUNDRY_BDD_REPORT"'},
            {"action": "run", "cmd": "echo cucumber"},
            {"action": "done", "summary": "ok"},
        ]
    )

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        seen.append(list(messages))
        act = next(actions)
        return json.dumps(act), act

    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(_complete).run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {}  # the report lives outside the worktree
    wrote, empty = str(seen[1][-1]["content"]), str(seen[2][-1]["content"])
    assert "wrote no report" not in wrote
    assert "wrote no report to $FOUNDRY_BDD_REPORT" in empty


def test_the_empty_bdd_report_note_skips_pytest_bdd_commands() -> None:
    from foundry.execution.adapters.proxy_agent import _BDD_CMD

    assert not _BDD_CMD.search("pip install pytest-bdd")
    assert not _BDD_CMD.search("pytest -k bdd")
    assert _BDD_CMD.search("npx cucumber-js") and _BDD_CMD.search("go test ./bdd/...")


def test_a_write_outside_the_tickets_files_is_refused_in_the_loop(tmp_path: Path) -> None:
    # The local notes run: the contracts ticket wrote contract.py for contracts.py, saw
    # "wrote", and spent its turns on a file the scope trim then discarded.
    src = tmp_path / "src"
    src.mkdir()
    seen: list[str] = []
    actions = iter(
        [
            {"action": "write_file", "path": "contract.py", "content": "X = 1\n"},
            {"action": "delete_file", "path": "features/a.feature"},
            {"action": "write_file", "path": "./contracts.py", "content": "X = 1\n"},
            {"action": "write_file", "path": "lib/db.py", "content": "Y = 2\n"},
            {"action": "done", "summary": "ok"},
        ]
    )

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        seen.append(str(messages[-1]["content"]))
        act = next(actions)
        return json.dumps(act), act

    packet = make_packet(tmp_path).model_copy(
        update={
            "task": {
                "id": "t-1",
                "title": "t-1",
                "branch": "task/t-1",
                "criterion_ids": [],
                "files": ["contracts.py", "lib/"],
            }
        }
    )
    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(_complete).run(packet, sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"contracts.py": "X = 1\n", "lib/db.py": "Y = 2\n"}
    assert seen[1].startswith("OBSERVATION:\nERROR: not written — contract.py is outside")
    assert "Write only: contracts.py, lib/." in seen[1]
    assert "ERROR: not written — features/a.feature" in seen[2]
    assert "wrote ./contracts.py" in seen[3]


def test_a_packet_without_files_writes_anywhere(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    adapter = ProxyAgentAdapter(
        scripted(
            [
                {"action": "write_file", "path": "anything.py", "content": "Z = 3\n"},
                {"action": "done", "summary": "ok"},
            ]
        )
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"anything.py": "Z = 3\n"}


def test_the_opening_lists_the_worktree_files(tmp_path: Path) -> None:
    # The local shop fixer spent turns on `find`, a regex search for files and two
    # reads of an app.py that never existed.
    src = tmp_path / "src"
    (src / "services").mkdir(parents=True)
    (src / "services" / "cart.py").write_text("X = 1\n")
    (src / "contracts.py").write_text("Y = 2\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "c.pyc").write_bytes(b"\0")
    prompts: list[str] = []

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        prompts.append(str(messages[0]["content"]))
        return '{"action": "done", "summary": "ok"}', {"action": "done", "summary": "ok"}

    with Sandbox(src) as sb:
        ProxyAgentAdapter(_complete).run(make_packet(sb.worktree), sb)
    assert "FILES IN THE WORKTREE (2):\ncontracts.py\nservices/cart.py\n" in prompts[0]
    assert "c.pyc" not in prompts[0]


def test_a_long_session_is_told_its_last_turns(tmp_path: Path) -> None:
    # The local shop fixer rewrote four modules, ran out of turns before it re-ran
    # mypy, and left 7 attr-defined errors in callers it never updated.
    src = tmp_path / "src"
    src.mkdir()
    seen: list[str] = []
    n = 0

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        nonlocal n
        n += 1
        seen.append(str(messages[-1]["content"]))
        act = {"action": "run", "cmd": f"echo {n}"}  # distinct: no repeat stop
        return json.dumps(act), act

    with Sandbox(src) as sb:
        result = ProxyAgentAdapter(_complete).run(make_packet(sb.worktree, turn_budget=12), sb)
    assert result.turns_used == 12
    # seen[i] is the observation of turn i (turn i+1's prompt); turn 7 leaves 5.
    assert not any("TURN BUDGET" in s for s in seen[1:7])
    assert "TURN BUDGET: 5 turns left. Stop exploring" in seen[7]
    assert "TURN BUDGET: 2 turns left" in seen[10]
    assert "TURN BUDGET: 1 turn left. Use it to declare done" in seen[11]


def test_a_short_session_gets_no_countdown(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[str] = []
    n = 0

    def _complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
        nonlocal n
        n += 1
        seen.append(str(messages[-1]["content"]))
        act = {"action": "run", "cmd": f"echo {n}"}
        return json.dumps(act), act

    with Sandbox(src) as sb:
        ProxyAgentAdapter(_complete).run(make_packet(sb.worktree, turn_budget=6), sb)
    assert not any("TURN BUDGET" in s for s in seen)


def test_native_tool_results_carry_the_countdown(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    turns = [[("run", {"cmd": f"echo {i}"}), ("run", {"cmd": f"echo x{i}"})] for i in range(11)]
    adapter = ProxyAgentAdapter(
        no_json, tool_complete_fn=tool_turns([*turns, [("done", {"summary": "ok"})]], seen)
    )
    with Sandbox(src) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=12), sb)
    last = results_of(seen[11])
    assert "TURN BUDGET: 1 turn left" in last[-1]["content"]
    assert "TURN BUDGET" not in last[0]["content"]


# A stand-in pip, so the session's install needs no network.
_FAKE_PIP = "pip() { echo Successfully installed bcrypt-4.1.0; }; pip install bcrypt"
_BCRYPT_NOTE = (
    "`bcrypt` is installed only in this sandbox; the gates install only what "
    "requirements.txt / pyproject [project].dependencies declares, so add it there "
    "(or a types-* stub for typing)"
)


def test_an_undeclared_sandbox_install_is_noted_and_refuses_done_once(tmp_path: Path) -> None:
    # A local engineer pip-installed bcrypt, so its own runs passed; it never
    # declared it, and every gate module importing it failed to collect.
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.txt").write_text("flask\n")
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "run", "cmd": _FAKE_PIP},
                {
                    "action": "run",
                    "cmd": "pip() { echo ok; }; pip install -r requirements.txt -e .",
                },
                {"action": "done", "summary": "built"},
                {"action": "write_file", "path": "requirements.txt", "content": "flask\nbcrypt\n"},
                {"action": "done", "summary": "declared bcrypt"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert result.turns_used == 5
    assert f"NOTE: {_BCRYPT_NOTE}" in observations(seen[1])[0]
    assert "NOTE:" not in observations(seen[2])[1]
    refusal = observations(seen[3])[2]
    assert refusal.startswith(f"OBSERVATION:\nERROR: not done —\n- {_BCRYPT_NOTE}")


def test_a_declared_or_insisted_install_ends_the_session(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "requirements.txt").write_text("bcrypt==4.1.0\n")
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "run", "cmd": _FAKE_PIP},
                {"action": "done", "summary": "built"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        assert adapter.run(make_packet(sb.worktree), sb).turns_used == 2
    assert "NOTE:" not in observations(seen[1])[0]
    (src / "requirements.txt").unlink()
    adapter = ProxyAgentAdapter(
        recording(
            [
                {"action": "run", "cmd": _FAKE_PIP},
                {"action": "done", "summary": "built"},
                {"action": "done", "summary": "it is not imported"},
            ],
            seen,
        )
    )
    with Sandbox(src) as sb:
        assert adapter.run(make_packet(sb.worktree), sb).turns_used == 3


def test_native_sessions_get_the_install_note_and_done_refusal(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    seen: list[list[dict[str, Any]]] = []
    adapter = ProxyAgentAdapter(
        no_json,
        tool_complete_fn=tool_turns(
            [
                [("run", {"cmd": _FAKE_PIP})],
                [("done", {"summary": "built"})],
                [("done", {"summary": "built"})],
            ],
            seen,
        ),
    )
    with Sandbox(src) as sb:
        assert adapter.run(make_packet(sb.worktree), sb).turns_used == 3
    assert f"NOTE: {_BCRYPT_NOTE}" in results_of(seen[1])[0]["content"]
    assert results_of(seen[2])[0]["content"].startswith("ERROR: not done —")
