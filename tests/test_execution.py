"""Execution layer tests: sandbox cage, adapter contract.

No network required.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
)
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    (src / "hello.py").write_text("print('hello')\n")
    (src / "pkg").mkdir()
    (src / "pkg" / "mod.py").write_text("X = 1\n")
    return src


@pytest.fixture()
def sandbox(source_dir: Path):
    sb = Sandbox(source_dir)
    yield sb
    sb.destroy()


def make_packet(worktree: Path, **overrides) -> TaskPacket:
    base = {
        "task": {"id": "T1", "title": "demo", "instructions": "do the thing"},
        "pinned_constraints": ("never use floats for money",),
        "retrieved_spans": ("span-A",),
        "worktree_path": str(worktree),
        "turn_budget": 5,
        "cost_budget_microusd": 1_000_000,
        "proxy_endpoint": None,
    }
    base.update(overrides)
    return TaskPacket(**base)


# ---------------------------------------------------------------------------
# Sandbox: worktree isolation
# ---------------------------------------------------------------------------


def test_write_inside_worktree_ok_and_source_untouched(source_dir: Path, sandbox: Sandbox):
    res = sandbox.run("echo agent-was-here > new.txt && echo mutated >> hello.py")
    assert res.ok
    assert (sandbox.worktree / "new.txt").read_text().strip() == "agent-was-here"
    # source dir untouched: no new file, original content intact
    assert not (source_dir / "new.txt").exists()
    assert (source_dir / "hello.py").read_text() == "print('hello')\n"


def test_run_cwd_is_worktree(sandbox: Sandbox):
    res = sandbox.run(["pwd"])
    assert res.ok
    assert Path(res.stdout.strip()) == sandbox.worktree


def test_destroyed_sandbox_refuses_use(source_dir: Path):
    sb = Sandbox(source_dir)
    sb.destroy()
    sb.destroy()  # idempotent
    with pytest.raises(RuntimeError, match="destroyed"):
        sb.run(["true"])


# ---------------------------------------------------------------------------
# Sandbox: env scrubbing (dev-grade credential denial)
# ---------------------------------------------------------------------------


def test_env_scrubbed_no_credentials_leak(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-do-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-fake")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-fake")
    with Sandbox(source_dir) as sb:
        res = sb.run(["env"])
        assert res.ok
        for needle in (
            "ANTHROPIC_API_KEY",
            "sk-ant-fake-do-not-leak",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "MY_SERVICE_TOKEN",
        ):
            assert needle not in res.stdout
        assert "PATH=" in res.stdout  # allowlisted basics survive
        # HOME is redirected into the sandbox, not the real user home
        home_line = next(ln for ln in res.stdout.splitlines() if ln.startswith("HOME="))
        assert "foundry-sandbox-" in home_line


def test_extra_env_cannot_smuggle_credentials(sandbox: Sandbox):
    env = sandbox.scrubbed_env({"FOO": "ok", "SOME_API_KEY": "nope", "AWS_REGION": "nope"})
    assert env["FOO"] == "ok"
    assert "SOME_API_KEY" not in env
    assert "AWS_REGION" not in env


def test_proxy_endpoint_is_the_only_egress_told(source_dir: Path):
    with Sandbox(source_dir) as no_proxy:
        assert "FOUNDRY_PROXY_ENDPOINT" not in no_proxy.scrubbed_env()
    with Sandbox(source_dir, proxy_endpoint="http://127.0.0.1:9999") as with_proxy:
        assert with_proxy.scrubbed_env()["FOUNDRY_PROXY_ENDPOINT"] == "http://127.0.0.1:9999"


# ---------------------------------------------------------------------------
# Sandbox: resource caps
# ---------------------------------------------------------------------------


def test_wall_clock_breach_kills_process_group(sandbox: Sandbox):
    start = time.monotonic()
    res = sandbox.run(["sleep", "30"], timeout_s=1.0)
    elapsed = time.monotonic() - start
    assert res.timed_out
    assert not res.ok
    assert elapsed < 3.0  # killed at the ~1s cap, not after 30s


# ---------------------------------------------------------------------------
# Sandbox: turn snapshots (content-addressed; restore discards delta)
# ---------------------------------------------------------------------------


def test_snapshot_mutate_restore_discards_mutation(sandbox: Sandbox):
    digest = sandbox.snapshot()
    (sandbox.worktree / "hello.py").write_text("print('TAMPERED')\n")
    (sandbox.worktree / "junk.txt").write_text("uncommitted delta\n")
    assert sandbox.snapshot() != digest  # mutation is visible in content address
    restored = sandbox.restore(digest)
    assert restored == digest
    assert (sandbox.worktree / "hello.py").read_text() == "print('hello')\n"
    assert not (sandbox.worktree / "junk.txt").exists()
    assert sandbox.snapshot() == digest  # round-trips to the recorded state


def test_restore_unknown_snapshot_raises(sandbox: Sandbox):
    with pytest.raises(FileNotFoundError):
        sandbox.restore("deadbeef" * 8)


def test_identical_worktrees_identical_digests(source_dir: Path):
    with Sandbox(source_dir) as a, Sandbox(source_dir) as b:
        assert a.snapshot() == b.snapshot()  # content-addressed: mtimes/paths irrelevant


def test_blob_store_round_trip(sandbox: Sandbox):
    uri = sandbox.put_blob(b"diff bytes")
    assert uri.startswith("cas://sha256/")
    assert sandbox.get_blob(uri) == b"diff bytes"
    assert sandbox.put_blob(b"diff bytes") == uri  # content-addressed: stable handle


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


def test_task_packet_frozen(tmp_path: Path):
    packet = make_packet(tmp_path)
    with pytest.raises(ValidationError):
        packet.turn_budget = 99


def test_adapter_result_frozen():
    triage = GateTriage(
        name="adapter-report",
        run_against="task/T1",
        explanation="note",
        produced_by="claude-code",
        seq=Seq("run", 0, 0, "claude-code", 0),
    )
    result = AdapterResult(
        diff_uri="cas://sha256/" + "0" * 64, triage=(triage,), snapshot_digest="d"
    )
    with pytest.raises(ValidationError):
        result.diff_uri = "cas://sha256/evil"


def test_constraints_verbatim_round_trip(tmp_path: Path):
    constraints = (
        "never use floats for money",
        "  weird   spacing\tand\ttabs preserved exactly  ",
        "unicode: ⧺ §2b — décorrélation",
    )
    packet = make_packet(tmp_path, pinned_constraints=constraints)
    assert packet.pinned_constraints == constraints
    rehydrated = TaskPacket.model_validate(packet.model_dump(mode="json"))
    assert rehydrated.pinned_constraints == constraints
    assert rehydrated == packet


def test_self_reported_model_contract_flags_it_ignored():
    # The contract carries an adapter's self-reported model but marks it IGNORED:
    # lineage is proxy-observed only, never trusted from the coding substrate.
    desc = AdapterResult.model_fields["adapter_self_reported_model"].description
    assert desc is not None
    assert "IGNORED" in desc
    assert "proxy" in desc.lower()


# ---------------------------------------------------------------------------
# Sandbox: streaming stdout (live progress out of a long agentic run)
# ---------------------------------------------------------------------------


def test_streaming_yields_lines_as_they_arrive_and_still_returns_full_stdout(sandbox: Sandbox):
    seen: list[str] = []
    res = sandbox.run(
        ["/bin/sh", "-c", "printf 'one\\ntwo\\nthree\\n'; printf 'to-err\\n' >&2"],
        on_stdout_line=seen.append,
    )
    assert [s.strip() for s in seen] == ["one", "two", "three"]
    assert res.stdout == "one\ntwo\nthree\n"      # identical to the buffered path
    assert res.stderr.strip() == "to-err"
    assert res.ok and not res.timed_out


def test_streaming_still_enforces_the_wall_clock_cap(sandbox: Sandbox):
    # The cap must be a TIMER, not a read deadline: this child prints one line and then
    # goes silent forever, so a loop that only checked the clock between lines would
    # block past the budget.
    seen: list[str] = []
    start = time.monotonic()
    res = sandbox.run(
        ["/bin/sh", "-c", "printf 'started\\n'; sleep 30"],
        timeout_s=1.0,
        on_stdout_line=seen.append,
    )
    elapsed = time.monotonic() - start
    assert res.timed_out and not res.ok
    assert elapsed < 5.0                          # killed at the cap, not after 30s
    assert [s.strip() for s in seen] == ["started"]   # what it managed to say is kept


def test_streaming_callback_errors_cannot_break_the_run(sandbox: Sandbox):
    # Progress reporting is advisory — a listener that throws must not fail the command.
    def _boom(_line: str) -> None:
        raise RuntimeError("listener exploded")

    res = sandbox.run(["/bin/sh", "-c", "printf 'a\\nb\\n'"], on_stdout_line=_boom)
    assert res.ok and res.stdout == "a\nb\n"
