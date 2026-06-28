"""Docker sandbox runner for the test gate — argv construction + gate routing.

No Docker daemon is needed: these prove the INVOCATION is correctly fenced and that
the gate routes to the sandbox only when opted in (the grounding call is faked)."""
from __future__ import annotations

from typing import Any

from foundry.gates import sandbox
from foundry.gates import test as testmod
from foundry.gates.sandbox import (
    SANDBOX_PYTEST,
    docker_run_argv,
    sandbox_enabled,
    sandbox_image,
)
from foundry.gates.test import TestGate
from foundry.schemas.gates import GateStatus


def test_docker_run_argv_is_fully_fenced(tmp_path: Any) -> None:
    argv = docker_run_argv(tmp_path, "img:1")
    assert argv[:3] == ["docker", "run", "--rm"]
    # no network, read-only rootfs + workdir, writable scratch, resource caps
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert f"{tmp_path.resolve()}:/work:ro" in argv          # workdir mounted READ-ONLY
    assert argv[argv.index("--tmpfs") + 1].startswith("/tmp")
    assert "--memory" in argv and "--pids-limit" in argv
    # the image precedes the inner pinned-pytest argv
    assert argv[argv.index("img:1") + 1:] == list(SANDBOX_PYTEST)


def test_sandbox_enabled_and_image_from_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    assert sandbox_enabled() is False
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "Docker")   # case-insensitive
    assert sandbox_enabled() is True
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX_IMAGE", raising=False)
    assert sandbox_image() == "foundry-test-sandbox"
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX_IMAGE", "reg/x@sha256:abc")
    assert sandbox_image() == "reg/x@sha256:abc"


def test_gate_routes_to_sandbox_when_enabled(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "docker")
    captured: dict[str, Any] = {}

    def fake_run(argv: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        captured["argv"] = list(argv)
        return (0, "1 passed in 0.01s", "")

    monkeypatch.setattr(sandbox, "run_grounding_command", fake_run)
    obs = TestGate().execute(tmp_path)
    assert captured["argv"][:2] == ["docker", "run"]         # went through the sandbox
    assert TestGate().decide(obs) is GateStatus.PASS          # parsed "1 passed" -> PASS


def test_gate_in_process_harness_when_sandbox_off(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    captured: dict[str, Any] = {}

    def fake_run(cmd: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        captured["cmd"] = list(cmd)
        return (0, "1 passed", "")

    monkeypatch.setattr(testmod, "run_grounding_command", fake_run)
    TestGate().execute(tmp_path)
    assert "docker" not in captured["cmd"][0]                 # NOT the container path
    assert "-c" in captured["cmd"]                            # the isolating bootstrap
