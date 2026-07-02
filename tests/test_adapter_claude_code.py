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
    notes = [t.explanation for t in result.triage]
    assert any("gone.py" in n and "NOT integrated" in n for n in notes)
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
    assert ["--max-turns", "7"] == argv[argv.index("--max-turns") : argv.index("--max-turns") + 2]
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
