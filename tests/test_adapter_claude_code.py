"""ClaudeCodeAdapter — the agentic coding substrate (ROADMAP Phase 1a/1b).

Covers, without a real ``claude`` binary (injectable exec seam, same discipline
as the proxy's CliRunner):

* the authored tree slice is observed as a ``full-file-snapshot`` blob that
  ``gate_wiring._diff_files`` materializes (additions AND modifications);
* deletions / binary files are surfaced as triage, never silently dropped;
* turns/model/cost are observed from the JSON envelope; a hard-kill records a
  budget-breach triage instead of raising;
* the sandbox's TYPED credential grant: model-auth names pass, anything else
  fails closed at construction; ``home_passthrough`` copies (never binds) the
  cached ``~/.claude`` login session; the grant is auditable via
  ``credential_grant_record``;
* the Junior Engineer builds the sandbox FROM the adapter's declared grant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.execution.adapter_contract import AdapterUnavailableError, TaskPacket
from foundry.execution.adapters.claude_code import (
    ClaudeCodeAdapter,
    changed_files,
    walk_worktree,
)
from foundry.execution.sandbox import GRANTABLE_MODEL_AUTH, Sandbox, SandboxResult

# ---- helpers -----------------------------------------------------------------


def make_packet(worktree: Path, **overrides: Any) -> TaskPacket:
    base: dict[str, Any] = {
        "task": {"id": "t-1", "title": "t-1", "branch": "task/t-1", "criterion_ids": ["c1"]},
        "pinned_constraints": ("CONSTRAINT-A: use stdlib only.",),
        "worktree_path": str(worktree),
        "turn_budget": 5,
        "cost_budget_microusd": 1_000_000,
    }
    base.update(overrides)
    return TaskPacket(**base)


def envelope(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "result": "Built the module.\nTRIAGE: config default is a guess",
        "num_turns": 4,
        "total_cost_usd": 0.0,
        "modelUsage": {"claude-fable-5": {"inputTokens": 100, "outputTokens": 50}},
    }
    body.update(overrides)
    return json.dumps(body)


def fake_exec(writes: dict[str, str | None], stdout: str, *, timed_out: bool = False):
    """An exec seam that mutates the worktree like the agentic CLI would:
    path -> content writes a file, path -> None deletes it."""

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any) -> SandboxResult:
        for rel, content in writes.items():
            target = sandbox.worktree / rel
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        return SandboxResult(
            returncode=0, stdout=stdout, stderr="", timed_out=timed_out, wall_clock_s=1.0
        )

    return _exec


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "existing.py").write_text("OLD = 1\n")
    return src


# ---- authored tree slice -> full-file-snapshot blob ---------------------------


def test_run_records_added_and_modified_files_as_snapshot(source_dir: Path) -> None:
    adapter = ClaudeCodeAdapter(
        exec_fn=fake_exec(
            {"pkg/new.py": "NEW = 2\n", "existing.py": "OLD = 99\n"}, envelope()
        )
    )
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["kind"] == "full-file-snapshot"
    # BOTH the addition and the MODIFICATION ride in the slice — the load-bearing
    # difference from the additions-only unified-diff path (ROADMAP ceiling #2).
    assert body["files"] == {"pkg/new.py": "NEW = 2\n", "existing.py": "OLD = 99\n"}


def test_resume_id_adds_flag_and_session_id_is_captured(source_dir: Path) -> None:
    """B2 foundation: a resume_session_id adds `--resume <id>` to argv, and the
    envelope's session_id is captured on the result (so a future fix attempt could
    continue the session). Both are inert by default (unset)."""
    seen: list[Any] = []

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any) -> SandboxResult:
        seen.extend(argv)
        (sandbox.worktree / "a.py").write_text("A = 1\n")
        return SandboxResult(0, envelope(session_id="sess-new"), "", False, 1.0)

    adapter = ClaudeCodeAdapter(exec_fn=_exec, resume_session_id="sess-prev")
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert "--resume" in seen and "sess-prev" in seen  # continues the prior session
    assert result.session_id == "sess-new"              # captured for the next attempt


def test_auth_failure_with_no_files_raises_unavailable(source_dir: Path) -> None:
    """A "Not logged in" (or root-permission) is_error envelope that authored NOTHING is
    the substrate failing to START — raise AdapterUnavailableError loudly so the build
    fails in seconds with the real reason, instead of handing the gates an empty diff
    that masquerades as a code failure and burns the whole fix loop."""
    bad = envelope(is_error=True, result="Not logged in · Please run /login")
    adapter = ClaudeCodeAdapter(exec_fn=fake_exec({}, bad))  # authors nothing
    with Sandbox(source_dir) as sb, pytest.raises(AdapterUnavailableError, match="could not run"):
        adapter.run(make_packet(sb.worktree), sb)


def test_is_error_that_authored_code_is_not_treated_as_unavailable(source_dir: Path) -> None:
    """The unavailability guard is gated on AUTHORED-NOTHING: an is_error run that DID
    write code is a real attempt — even if its text happens to match a marker — so it is
    handed to the gates, never misread as unavailability."""
    bad = envelope(is_error=True, result="warning: not logged in to the deploy service")
    adapter = ClaudeCodeAdapter(exec_fn=fake_exec({"a.py": "A = 1\n"}, bad))
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)  # must NOT raise
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"a.py": "A = 1\n"}


def test_snapshot_slice_materializes_via_gate_wiring(source_dir: Path) -> None:
    # End-to-end contract: the blob the adapter emits is the exact body shape the
    # integration boundary consumes.
    from foundry.graph.gate_wiring import _maybe_snapshot_files

    adapter = ClaudeCodeAdapter(exec_fn=fake_exec({"a.py": "A = 1\n"}, envelope()))
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        text = sb.get_blob(result.diff_uri).decode("utf-8")
    assert _maybe_snapshot_files("cas://x", text) == {"a.py": "A = 1\n"}


def test_unchanged_files_stay_out_of_the_slice(source_dir: Path) -> None:
    adapter = ClaudeCodeAdapter(exec_fn=fake_exec({"b.py": "B = 1\n"}, envelope()))
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert "existing.py" not in body["files"]  # untouched base file: not authored


def test_deletion_and_binary_are_triaged_not_dropped(source_dir: Path) -> None:
    (source_dir / "gone.py").write_text("GONE = 1\n")

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any) -> SandboxResult:
        (sandbox.worktree / "gone.py").unlink()
        (sandbox.worktree / "img.png").write_bytes(b"\x89PNG\x00\xff")
        return SandboxResult(0, envelope(), "", False, 1.0)

    adapter = ClaudeCodeAdapter(exec_fn=_exec)
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {}
    assert body["deleted"] == ["gone.py"]  # integration unlinks it (Phase 2)
    notes = [t.explanation for t in result.triage]
    assert any("gone.py" in n and "deleted" in n for n in notes)
    assert any("img.png" in n and "binary" in n for n in notes)


# ---- envelope observation ------------------------------------------------------


def test_turns_model_and_triage_are_observed(source_dir: Path) -> None:
    adapter = ClaudeCodeAdapter(exec_fn=fake_exec({"a.py": "A = 1\n"}, envelope()))
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
    assert result.turns_used == 4
    assert result.adapter_self_reported_model == "claude-fable-5"
    notes = [t.explanation for t in result.triage]
    assert "config default is a guess" in notes  # TRIAGE: line, prefix stripped
    assert any("total_cost_usd=0.0" in n for n in notes)


def test_hard_kill_records_budget_breach_triage(source_dir: Path) -> None:
    # A wall-clock kill is a budget breach, never a verdict (sandbox contract):
    # the partially-authored tree is still recorded; the gates decide.
    adapter = ClaudeCodeAdapter(
        exec_fn=fake_exec({"partial.py": "P = 1\n"}, "", timed_out=True)
    )
    with Sandbox(source_dir) as sb:
        result = adapter.run(make_packet(sb.worktree), sb)
        body = json.loads(sb.get_blob(result.diff_uri))
    assert body["files"] == {"partial.py": "P = 1\n"}
    assert any("wall-clock" in t.explanation for t in result.triage)


def test_missing_binary_fails_unavailable(source_dir: Path) -> None:
    adapter = ClaudeCodeAdapter(claude_bin="definitely-not-a-real-binary-xyz")
    with Sandbox(source_dir) as sb, pytest.raises(AdapterUnavailableError):
        adapter.run(make_packet(sb.worktree), sb)


def test_argv_carries_turn_budget_and_skip_permissions(source_dir: Path) -> None:
    seen: dict[str, Any] = {}

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any) -> SandboxResult:
        seen["argv"] = list(argv)
        (sandbox.worktree / "a.py").write_text("A = 1\n")
        return SandboxResult(0, envelope(), "", False, 1.0)

    adapter = ClaudeCodeAdapter(exec_fn=_exec)
    with Sandbox(source_dir) as sb:
        adapter.run(make_packet(sb.worktree, turn_budget=7), sb)
    argv = seen["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert "CONSTRAINT-A: use stdlib only." in argv[2]  # pinned VERBATIM in prompt
    assert argv[argv.index("--max-turns") : argv.index("--max-turns") + 2] == ["--max-turns", "7"]
    assert "--dangerously-skip-permissions" in argv  # the SANDBOX is the boundary


# ---- typed credential grant (Phase 1b) ------------------------------------------


def test_model_auth_grant_is_admitted(source_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    with Sandbox(source_dir, credential_grant=("CLAUDE_CODE_OAUTH_TOKEN",)) as sb:
        env = sb.scrubbed_env()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-123"
        assert sb.credential_grant_record == ("CLAUDE_CODE_OAUTH_TOKEN",)


def test_ungranted_credentials_stay_scrubbed(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-456")
    with Sandbox(source_dir, credential_grant=("CLAUDE_CODE_OAUTH_TOKEN",)) as sb:
        env = sb.scrubbed_env()
        assert "ANTHROPIC_API_KEY" not in env  # grantable but NOT granted here


def test_target_system_credentials_are_never_grantable(source_dir: Path) -> None:
    # The typology line (ROADMAP 1b): AWS_*/deploy keys belong to the credentialed
    # phase; asking for them fails closed BEFORE any subprocess exists.
    for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DEPLOY_KEY", "MY_SECRET"):
        with pytest.raises(ValueError, match="not grantable"):
            Sandbox(source_dir, credential_grant=(name,))
    assert "AWS_SECRET_ACCESS_KEY" not in GRANTABLE_MODEL_AUTH


def test_default_sandbox_is_fully_scrubbed(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
    with Sandbox(source_dir) as sb:  # no grant: byte-identical to the old cage
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in sb.scrubbed_env()
        assert sb.credential_grant_record == ()


def test_home_passthrough_copies_claude_session(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "realhome"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text('{"session": "cached"}')
    (fake_home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))
    with Sandbox(source_dir, home_passthrough=True) as sb:
        sandbox_home = Path(sb.scrubbed_env()["HOME"])
        assert sandbox_home != fake_home  # a COPY into the redirected HOME, never a bind
        assert (sandbox_home / ".claude" / ".credentials.json").read_text() == (
            '{"session": "cached"}'
        )
        assert (sandbox_home / ".claude.json").is_file()
        assert "home:~/.claude" in sb.credential_grant_record


def test_home_passthrough_with_no_session_is_a_noop(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with Sandbox(source_dir, home_passthrough=True) as sb:
        assert sb.credential_grant_record == ()


# ---- Junior Engineer builds the sandbox FROM the adapter's declaration ----------


def test_junior_sandbox_honors_adapter_grant(source_dir: Path) -> None:
    from foundry.agents.implementation.junior_engineer import _sandbox_for

    class Rt:
        adapter = ClaudeCodeAdapter(exec_fn=fake_exec({}, envelope()))

    sb = _sandbox_for(Rt(), str(source_dir), None)
    try:
        assert sb._credential_grant == ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        assert sb._home_passthrough is True
    finally:
        sb.destroy()


def test_junior_sandbox_stays_scrubbed_for_nondeclaring_adapters(source_dir: Path) -> None:
    from foundry.agents.implementation.junior_engineer import _sandbox_for
    from foundry.execution.adapters.stub import StubAdapter

    class Rt:
        adapter = StubAdapter()

    sb = _sandbox_for(Rt(), str(source_dir), None)
    try:
        assert sb._credential_grant == ()
        assert sb._home_passthrough is False
    finally:
        sb.destroy()


# ---- pure helpers ----------------------------------------------------------------


def test_walk_worktree_excludes_scratch_dirs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("M = 1\n")
    assert set(walk_worktree(tmp_path)) == {"src/m.py"}


def test_changed_files_partitions_correctly() -> None:
    before = {"keep.py": b"K\n", "mod.py": b"OLD\n", "gone.py": b"G\n"}
    after = {"keep.py": b"K\n", "mod.py": b"NEW\n", "new.py": b"N\n", "bin.dat": b"\xff\xfe"}
    authored, deleted, skipped = changed_files(before, after)
    assert authored == {"mod.py": "NEW\n", "new.py": "N\n"}
    assert deleted == ["gone.py"]
    assert skipped == ["bin.dat"]


# ---- live activity: streaming what the agent does INSIDE one step -------------


def _assistant(*tools: tuple[str, dict[str, Any]]) -> str:
    """One stream-json `assistant` line carrying tool_use blocks."""
    blocks = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return json.dumps({"type": "assistant", "message": {"content": blocks}})


def test_activity_phrases_name_the_tool_and_its_target() -> None:
    from foundry.execution.adapters.claude_code import _stream_activities

    assert _stream_activities(_assistant(("Edit", {"file_path": "src/store.py"}))) == [
        "editing src/store.py"
    ]
    assert _stream_activities(_assistant(("Bash", {"command": "pytest -q"}))) == [
        "running pytest -q"
    ]
    # several tool calls in one message -> one phrase each, in order
    assert _stream_activities(_assistant(("Read", {"file_path": "a.md"}),
                                         ("Grep", {"pattern": "def main"}))) == [
        "reading a.md", "searching for def main"
    ]
    # an unknown/MCP tool is NAMED but its arguments are never echoed (unknown shape)
    assert _stream_activities(_assistant(("mcp__x__do", {"secret": "s3cr3t"}))) == [
        "using mcp__x__do"
    ]
    # non-assistant lines, junk and partial lines are silently ignored
    for line in ('{"type":"system","subtype":"init"}', "not json", "", "{",
                 json.dumps({"type": "result", "result": "done"})):
        assert _stream_activities(line) == []


def test_activity_never_leaks_file_contents_or_long_payloads() -> None:
    from foundry.execution.adapters.claude_code import _stream_activities

    secret = "API_KEY = 'sk-live-DO-NOT-LEAK'\n" * 40
    line = _assistant(("Write", {"file_path": "src/settings.py", "content": secret}))
    phrases = _stream_activities(line)
    assert phrases == ["writing src/settings.py"]
    assert "sk-live" not in phrases[0] and "API_KEY" not in phrases[0]
    # Edit's old/new strings are content too — never reported
    edit = _assistant(("Edit", {"file_path": "a.py", "old_string": secret,
                                "new_string": secret}))
    assert _stream_activities(edit) == ["editing a.py"]
    # and any allowlisted field is truncated to one short line
    long_cmd = _assistant(("Bash", {"command": "x " * 200}))
    out = _stream_activities(long_cmd)[0]
    assert len(out) < 90 and out.endswith("…")


def test_streaming_run_reports_activity_and_still_parses_the_envelope(tmp_path) -> None:
    """With `on_activity` the CLI is asked for stream-json, each tool call is reported
    as it arrives, and the final `result` object is read exactly like the plain json
    envelope — same summary, same turn count."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "seed.py").write_text("SEED = 1\n")
    stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        _assistant(("Read", {"file_path": "seed.py"})),
        _assistant(("Write", {"file_path": "impl.py", "content": "IMPL = 1\n"})),
        _assistant(("Bash", {"command": "pytest -q"})),
        envelope(),                     # the same result object as the json format
    ]) + "\n"

    seen_argv: list[Any] = []
    streamed: list[str] = []

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any, on_stdout_line=None):
        seen_argv.append(list(argv))
        (sandbox.worktree / "impl.py").write_text("IMPL = 1\n")
        for line in stdout.splitlines(keepends=True):
            if on_stdout_line is not None:
                on_stdout_line(line)
        return SandboxResult(returncode=0, stdout=stdout, stderr="", timed_out=False,
                             wall_clock_s=1.0)

    adapter = ClaudeCodeAdapter(exec_fn=_exec, on_activity=streamed.append)
    with Sandbox(src) as sandbox:
        result = adapter.run(make_packet(sandbox.worktree), sandbox)

    assert streamed == ["reading seed.py", "writing impl.py", "running pytest -q"]
    argv = seen_argv[0]
    assert "stream-json" in argv and "--verbose" in argv and "json" not in argv
    # the envelope still drives the outcome
    assert result.turns_used == 4
    assert any("config default is a guess" in t.explanation for t in result.triage)


def test_without_a_listener_nothing_changes(tmp_path) -> None:
    """No `on_activity` => the plain json format and the buffered exec seam, byte for
    byte what it was before streaming existed (a 3-arg exec_fn still works)."""
    src = tmp_path / "src"
    src.mkdir()
    seen_argv: list[Any] = []

    def _exec(sandbox: Sandbox, argv: Any, timeout_s: Any) -> SandboxResult:
        seen_argv.append(list(argv))
        (sandbox.worktree / "a.py").write_text("A = 1\n")
        return SandboxResult(returncode=0, stdout=envelope(), stderr="", timed_out=False,
                             wall_clock_s=1.0)

    adapter = ClaudeCodeAdapter(exec_fn=_exec)
    with Sandbox(src) as sandbox:
        result = adapter.run(make_packet(sandbox.worktree), sandbox)
    argv = seen_argv[0]
    assert "json" in argv and "stream-json" not in argv and "--verbose" not in argv
    assert result.turns_used == 4
