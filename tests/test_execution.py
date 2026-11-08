"""Execution layer tests: sandbox cage, adapter contract, adapters.

No network required — the raw-react substrate drives an injected fake proxy seam.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import site
import sys
import time
import zipfile
from pathlib import Path

import pytest

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
)
from foundry.execution.adapters.raw_react import RawReactAdapter
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage

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
    # macOS tmp is /var -> /private/var: pwd prints the resolved path.
    assert Path(res.stdout.strip()).resolve() == sandbox.worktree.resolve()


def test_destroyed_sandbox_refuses_use(source_dir: Path):
    sb = Sandbox(source_dir)
    sb.destroy()
    sb.destroy()  # idempotent
    with pytest.raises(RuntimeError, match="destroyed"):
        sb.run(["true"])


def test_destroy_removes_a_read_only_tree(source_dir: Path, tmp_path: Path):
    # Go's module cache is written 0555/0444: a plain rmtree(ignore_errors) leaks it.
    sb = Sandbox(source_dir)
    root = sb._root
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "keep").write_text("x")
    cache = sb.worktree / "gopath" / "pkg" / "mod" / "example.com" / "m@v1"
    cache.mkdir(parents=True)
    (cache / "go.mod").write_text("module m\n")
    (cache / "link").symlink_to(outside)
    (cache / "go.mod").chmod(0o444)
    for d in (cache, *cache.parents):
        if d == sb.worktree:
            break
        d.chmod(0o555)
    sb.destroy()
    assert not root.exists()
    assert (outside / "keep").exists()
    assert outside.stat().st_mode & 0o777 == 0o755  # the symlink was not followed


def test_dir_chmod_never_follows_a_symlink(tmp_path: Path):
    # The path is opened O_NOFOLLOW: a directory swapped for a symlink after the
    # walk's islink check would otherwise be chmod-ed through it.
    from foundry.fs import _chmod_dir_nofollow

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (tmp_path / "link").symlink_to(outside)
    _chmod_dir_nofollow(str(tmp_path / "link"))
    assert outside.stat().st_mode & 0o777 == 0o755
    inside = tmp_path / "ro"
    inside.mkdir(mode=0o555)
    _chmod_dir_nofollow(str(inside))
    assert inside.stat().st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
# Sandbox: env scrubbing (dev-grade credential denial)
# ---------------------------------------------------------------------------


def test_env_scrubbed_no_credentials_leak(source_dir: Path, monkeypatch: pytest.MonkeyPatch):
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
# Sandbox: Python environment (installs never reach Foundry's own venv)
# ---------------------------------------------------------------------------


def _dist_files(name: str, version: str, body: str) -> dict[str, str]:
    info = f"{name}-{version}.dist-info"
    return {
        f"{name}/__init__.py": body,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }


def _record(files: dict[str, str], record_path: str) -> str:
    lines = []
    for path, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        lines.append(f"{path},sha256={digest.decode()},{len(data)}")
    return "\n".join([*lines, f"{record_path},,"]) + "\n"


def _wheel(dest: Path, name: str, version: str, body: str) -> Path:
    """A minimal pure-Python wheel, built offline (pip installs it with --no-index)."""
    files = _dist_files(name, version, body)
    record = f"{name}-{version}.dist-info/RECORD"
    files[record] = _record(files, record)
    whl = dest / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return whl


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_scrubbed_env_drops_foundry_venv_bin_and_points_at_the_sandbox_venv(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host_venv = tmp_path / "foundry-venv"
    monkeypatch.setattr(sys, "prefix", str(host_venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(host_venv / "bin"), "/usr/bin", "/bin"]))
    monkeypatch.setenv("VIRTUAL_ENV", str(host_venv))
    with Sandbox(source_dir) as sb:
        env = sb.scrubbed_env()
    entries = env["PATH"].split(os.pathsep)
    assert str(host_venv / "bin") not in entries
    assert entries[1:] == ["/usr/bin", "/bin"]
    assert env["VIRTUAL_ENV"] != str(host_venv)
    assert entries[0] == str(Path(env["VIRTUAL_ENV"]) / "bin")


def test_pip_install_lands_in_the_sandbox_never_in_foundrys_environment(
    source_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Stand-in for Foundry's site-packages holding probe_pkg 1.0: the sandbox reads
    # it, an install of 2.0 shadows it, and pip must leave the host copy untouched.
    host_site = tmp_path / "host-site"
    host_site.mkdir()
    host_files = _dist_files("probe_pkg", "1.0", "WHERE = 'host'\n")
    host_files["probe_pkg-1.0.dist-info/RECORD"] = _record(
        host_files, "probe_pkg-1.0.dist-info/RECORD"
    )
    for rel, text in host_files.items():
        (host_site / rel).parent.mkdir(parents=True, exist_ok=True)
        (host_site / rel).write_text(text)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(host_site)])
    before = _tree(host_site)

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    fresh = _wheel(wheels, "sandbox_only_pkg", "0.1", "WHERE = 'sandbox'\n")
    shadow = _wheel(wheels, "probe_pkg", "2.0", "WHERE = 'sandbox'\n")

    with Sandbox(source_dir) as sb:
        assert sb.run("python -c 'import probe_pkg; print(probe_pkg.WHERE)'").stdout == "host\n"
        res = sb.run(["pip", "install", "-q", "--no-index", "--no-deps", str(fresh), str(shadow)])
        assert res.ok, res.stderr
        probe = sb.run(
            "python -c 'import sys, probe_pkg, sandbox_only_pkg as s;"
            " print(probe_pkg.WHERE, s.WHERE, s.__file__.startswith(sys.prefix))'"
        )
        assert probe.stdout == "sandbox sandbox True\n", probe.stderr

    assert _tree(host_site) == before
    for root in {*site.getsitepackages(), sys.prefix}:
        assert not list(Path(root).rglob("sandbox_only_pkg*"))
    assert importlib.util.find_spec("sandbox_only_pkg") is None


def test_project_tools_run_inside_the_sandbox(source_dir: Path):
    (source_dir / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    with Sandbox(source_dir) as sb:
        tests = sb.run(["pytest", "-q", "-p", "no:cacheprovider", "test_smoke.py"])
        assert tests.ok and "1 passed" in tests.stdout, tests.stdout + tests.stderr
        tools = sb.run("python -c 'import pytest_bdd' && mypy --version")
        assert tools.ok, tools.stderr
        foundry = sb.run(["python", "-c", "import foundry"])
        assert not foundry.ok  # Foundry's own source stays off the sandbox's path


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


def test_prompt_surfaces_prior_failures_for_the_fix_loop(tmp_path: Path):
    # On a re-attempt, the SPECIFIC prior failure rides the packet -> the prompt, so
    # the engineer fixes that instead of re-rolling blind.
    failure = "E       assert 'world hello' == 'world  hello'"
    packet = make_packet(tmp_path, prior_failures=(failure,))
    prompt = RawReactAdapter(lambda p: "").build_prompt(packet)
    assert failure in prompt
    assert "PREVIOUS ATTEMPT FAILED" in prompt
    assert prompt.index("TASK:") < prompt.index(failure)  # after the task, as feedback
    # No prior failures (first attempt) -> no feedback section.
    assert "PREVIOUS ATTEMPT FAILED" not in RawReactAdapter(lambda p: "").build_prompt(
        make_packet(tmp_path)
    )


def test_prompt_injects_constraints_verbatim_and_first(tmp_path: Path):
    constraints = ("never use floats for money", "unicode: ⧺ §2b — décorrélation")
    packet = make_packet(tmp_path, pinned_constraints=constraints)
    prompt = RawReactAdapter(lambda p: "").build_prompt(packet)
    for c in constraints:
        assert c in prompt  # byte-for-byte, never paraphrased
    assert prompt.index(constraints[0]) < prompt.index(constraints[1])
    assert prompt.index(constraints[1]) < prompt.index("TASK:")
    assert prompt.index("TASK:") < prompt.index("span-A")


# ---------------------------------------------------------------------------
# RawReactAdapter (fake proxy seam — no network)
# ---------------------------------------------------------------------------

FAKE_RESPONSE = """Here you go.
```diff
--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-print('hello')
+print('hello, world')
```
```triage
lint: line too long in pkg/mod.py
test: missing negative case for empty input
```
"""


def test_raw_react_adapter_records_outputs(sandbox: Sandbox):
    prompts: list[str] = []

    def fake_complete(prompt: str) -> str:
        prompts.append(prompt)
        return FAKE_RESPONSE

    adapter = RawReactAdapter(fake_complete, self_reported_model="totally-real-model-9000")
    packet = make_packet(sandbox.worktree)
    result = adapter.run(packet, sandbox)

    assert len(prompts) == 1
    assert "never use floats for money" in prompts[0]

    # diff recorded as a content-addressed blob, applied to NOTHING
    diff = sandbox.get_blob(result.diff_uri).decode()
    assert "+print('hello, world')" in diff
    assert (sandbox.worktree / "hello.py").read_text() == "print('hello')\n"

    # triage notes are advisory GateTriage, never a verdict
    assert len(result.triage) == 2
    assert all(isinstance(t, GateTriage) for t in result.triage)
    assert result.triage[0].explanation == "lint: line too long in pkg/mod.py"
    assert result.triage[0].run_against == "task/T1"

    # snapshot digest is the worktree's content address
    assert result.snapshot_digest == sandbox.snapshot()
    assert result.turns_used == 1
    assert adapter.supports_turn_snapshots


def test_self_reported_model_carried_but_flagged_ignored(sandbox: Sandbox):
    adapter = RawReactAdapter(lambda p: FAKE_RESPONSE, self_reported_model="claude-fake")
    result = adapter.run(make_packet(sandbox.worktree), sandbox)
    # carried...
    assert result.adapter_self_reported_model == "claude-fake"
    # ...but the contract flags it as ignored: lineage is proxy-observed only
    desc = AdapterResult.model_fields["adapter_self_reported_model"].description
    assert desc is not None
    assert "IGNORED" in desc
    assert "proxy" in desc.lower()


def test_raw_react_unparseable_response_still_triaged(sandbox: Sandbox):
    adapter = RawReactAdapter(lambda p: "no fences at all")
    result = adapter.run(make_packet(sandbox.worktree), sandbox)
    assert sandbox.get_blob(result.diff_uri) == b""  # no diff recorded
    assert len(result.triage) == 1
    assert "unparseable" in result.triage[0].explanation


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
    assert res.stdout == "one\ntwo\nthree\n"  # identical to the buffered path
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
    assert elapsed < 5.0  # killed at the cap, not after 30s
    assert [s.strip() for s in seen] == ["started"]  # what it managed to say is kept


def test_streaming_callback_errors_cannot_break_the_run(sandbox: Sandbox):
    # Progress reporting is advisory — a listener that throws must not fail the command.
    def _boom(_line: str) -> None:
        raise RuntimeError("listener exploded")

    res = sandbox.run(["/bin/sh", "-c", "printf 'a\\nb\\n'"], on_stdout_line=_boom)
    assert res.ok and res.stdout == "a\nb\n"
