"""Namespace (``unshare``) sandbox for the test gate — argv, routing, fail-closed,
and a REAL end-to-end run (user namespaces are available in CI, so we exercise it)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from foundry.gates import sandbox
from foundry.gates import test as testmod
from foundry.gates.runner import GroundingUnavailable
from foundry.gates.sandbox import (
    UNSHARE_FLAGS,
    sandbox_enabled,
    unshare_available,
    wrap_argv,
)
from foundry.gates.test import TestGate
from foundry.schemas.gates import GateStatus


def test_sandbox_is_the_default_and_opt_out_disables_it(monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    assert sandbox_enabled() is True                       # default: always sandboxed
    for off in ("inprocess", "in-process", "none", "off"):
        monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", off)
        assert sandbox_enabled() is False                  # explicit opt-out only


def test_wrap_argv_uses_unprivileged_namespaces() -> None:
    argv = wrap_argv(("python", "-c", "pass"))
    assert argv[0] == "unshare"
    # the load-bearing isolation flags: unprivileged userns + NO network + ns isolation
    for flag in ("--user", "--map-root-user", "--net", "--mount", "--pid"):
        assert flag in argv
    assert argv[-3:] == ["python", "-c", "pass"]           # the wrapped cmd is last
    assert tuple(argv[1:1 + len(UNSHARE_FLAGS)]) == UNSHARE_FLAGS


def test_run_in_sandbox_fails_closed_when_unavailable(tmp_path: Any, monkeypatch: Any) -> None:
    # No usable user namespaces -> fail-closed ERROR, never an unsandboxed run.
    monkeypatch.setattr(sandbox, "unshare_available", lambda: False)
    with pytest.raises(GroundingUnavailable):
        sandbox.run_in_sandbox(("true",), tmp_path)


def test_gate_routes_through_sandbox_when_enabled(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)   # default = sandbox
    monkeypatch.setattr(sandbox, "unshare_available", lambda: True)
    captured: dict[str, Any] = {}

    def fake_run(argv: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        captured["argv"] = list(argv)
        return (0, "1 passed in 0.01s", "")

    monkeypatch.setattr(sandbox, "run_grounding_command", fake_run)
    obs = TestGate().execute(tmp_path)
    assert captured["argv"][0] == "unshare"                 # wrapped in the namespace
    assert TestGate().decide(obs) is GateStatus.PASS


def test_gate_in_process_when_opted_out(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    captured: dict[str, Any] = {}

    def fake_run(cmd: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        captured["cmd"] = list(cmd)
        return (0, "1 passed", "")

    monkeypatch.setattr(testmod, "run_grounding_command", fake_run)
    TestGate().execute(tmp_path)
    assert captured["cmd"][0] != "unshare"                  # NOT wrapped
    assert "-c" in captured["cmd"]                          # the isolating bootstrap


@pytest.mark.skipif(not unshare_available(), reason="no unprivileged user namespaces")
def test_namespace_sandbox_really_runs_and_blocks_network(tmp_path: Any, monkeypatch: Any) -> None:
    """End-to-end: a generated impl under ``foundry/`` (the collision case) PASSES
    inside the real namespace sandbox, and the network is OFF (no exfiltration)."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)   # real sandbox
    (tmp_path / "foundry").mkdir()
    (tmp_path / "foundry" / "text_processing.py").write_text(
        "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_rw.py").write_text(
        "import socket\n"
        "from foundry.text_processing import reverse_words\n\n\n"
        "def test_rw():\n    assert reverse_words('a b c') == 'c b a'\n\n\n"
        "def test_no_network():\n"
        "    import pytest\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('1.1.1.1', 53), timeout=2)\n",
        encoding="utf-8",
    )
    rc, out, _err = sandbox.run_in_sandbox(
        TestGate().cmd, tmp_path, env=TestGate()._env(Path(tmp_path)), timeout_s=120.0
    )
    assert rc == 0, out          # both tests passed: collision resolved + network blocked
    assert "2 passed" in out
