"""Namespace (``unshare``) sandbox for the test/build/smoke gates — argv, routing, fail-closed,
and a REAL end-to-end run (user namespaces are available in CI, so we exercise it)."""
from __future__ import annotations

import os
import shlex
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from foundry.gates import sandbox
from foundry.gates import test as testmod
from foundry.gates.build import BuildGate
from foundry.gates.runner import GroundingUnavailable
from foundry.gates.sandbox import (
    UNSHARE_FLAGS,
    sandbox_enabled,
    unshare_available,
    wrap_argv,
)
from foundry.gates.smoke import SmokeGate
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


# ---- build / smoke / install: the confined sandbox ---------------------------------

_REAL = pytest.mark.skipif(not unshare_available(), reason="no unprivileged user namespaces")


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _capture(monkeypatch: Any) -> list[list[str]]:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "unshare_available", lambda: True)
    calls: list[list[str]] = []

    def fake_run(argv: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        calls.append(list(argv))
        return (0, "", "")

    monkeypatch.setattr(sandbox, "run_grounding_command", fake_run)
    return calls


def test_install_alone_gets_network_build_and_smoke_are_offline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = _capture(monkeypatch)
    gate = BuildGate(cmd=("true",), install_cmd="npm ci")
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.PASS
    SmokeGate(("true",)).execute(tmp_path)
    install, build, smoke = calls
    assert all(argv[0] == "unshare" for argv in calls)
    assert "--net" not in install and install[-3:] == ["/bin/sh", "-c", "npm ci"]
    assert "--net" in build and build[-1] == "true"
    assert "--net" in smoke


def test_build_and_smoke_fail_closed_when_sandbox_unavailable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "unshare_available", lambda: False)
    for gate in (BuildGate(), BuildGate(install_cmd="true"), SmokeGate(("true",))):
        with pytest.raises(GroundingUnavailable):
            gate.execute(tmp_path)


def test_fence_setup_failure_is_grounding_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(sandbox, "unshare_available", lambda: True)
    monkeypatch.setattr(
        sandbox,
        "run_grounding_command",
        lambda *a, **k: (sandbox._FENCE_SETUP_RC, "", f"{sandbox._FENCE_SETUP_MARKER}: x"),
    )
    with pytest.raises(GroundingUnavailable):
        sandbox.run_confined(("true",), tmp_path)


def test_build_and_smoke_opt_out_mirrors_testgate(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sandbox, "run_grounding_command",
        lambda argv, *a, **k: calls.append(list(argv)) or (0, "", ""),
    )
    BuildGate(cmd=("true",), install_cmd="echo hi").execute(tmp_path)
    SmokeGate(("true",)).execute(tmp_path)
    assert all(argv[0] != "unshare" for argv in calls) and len(calls) == 3


def test_install_failure_is_build_fail_with_findings(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    gate = BuildGate(cmd=("/bin/sh", "-c", "touch built"), install_cmd="echo nope >&2; exit 4")
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.exit_codes == (4,)
    assert obs.findings[0].startswith("install command exited 4") and "nope" in obs.findings
    assert not (tmp_path / "built").exists()          # build never ran


def test_build_decide_shapes() -> None:
    from foundry.gates.runner import observe

    plain, inst = BuildGate(), BuildGate(install_cmd="true")
    assert plain.decide(observe("build", exit_codes=(0,))) is GateStatus.PASS
    assert plain.decide(observe("build", exit_codes=(0, 0))) is GateStatus.ERROR
    assert inst.decide(observe("build", exit_codes=(0, 0))) is GateStatus.PASS
    assert inst.decide(observe("build", exit_codes=(0, 2))) is GateStatus.FAIL
    assert inst.decide(observe("build", exit_codes=(0,))) is GateStatus.ERROR
    assert inst.decide(observe("build", exit_codes=())) is GateStatus.ERROR


_CONNECT = (
    "import socket,sys\n"
    "try:\n    socket.create_connection(('127.0.0.1', {port}), timeout=3).close()\n"
    "except OSError as e:\n    sys.exit('blocked: %s' % e)\n"
)


@_REAL
def test_real_build_is_offline_but_install_reaches_the_network(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A host-side listener stands in for the internet: reachable only from the
    install step (host netns), never from the build (its own empty netns)."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        code = _CONNECT.format(port=srv.getsockname()[1])
        build = BuildGate(cmd=_py(code))
        obs = build.execute(tmp_path)
        assert build.decide(obs) is GateStatus.FAIL and "blocked" in obs.stderr
        inet = BuildGate(cmd=_py("socket = __import__('socket'); socket.create_connection("
                                 "('1.1.1.1', 53), timeout=3)"))
        assert inet.decide(inet.execute(tmp_path)) is GateStatus.FAIL
        both = BuildGate(cmd=("true",), install_cmd=shlex.join(_py(code)))
        obs = both.execute(tmp_path)
        assert both.decide(obs) is GateStatus.PASS, obs.stderr


@_REAL
def test_real_writes_are_fenced_to_the_workdir(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    wd = tmp_path / "wd"
    wd.mkdir()
    host_tmp = tmp_path / "escaped.txt"                       # host /tmp: shadowed
    host_fs = Path(__file__).parent / f".escaped-{os.getpid()}"  # elsewhere: read-only
    for target, blocked in ((host_tmp, False), (host_fs, True)):
        write = f"open({str(target)!r}, 'w').write('x')"
        for gate in (
            BuildGate(cmd=_py(write)),
            BuildGate(cmd=("true",), install_cmd=shlex.join(_py(write))),
            SmokeGate(_py(write)),
        ):
            obs = gate.execute(wd)
            if blocked:
                assert gate.decide(obs) is GateStatus.FAIL and "Read-only" in obs.stderr
        assert not target.exists()
    # ...while the workdir itself, /tmp and installed deps persist across steps.
    gate = BuildGate(
        cmd=_py("import os; assert open('node_modules/dep').read() == 'ok'; "
                "open(os.path.join(os.environ['TMPDIR'], 't'), 'w').write('t')"),
        install_cmd="mkdir -p node_modules && printf ok > node_modules/dep",
    )
    obs = gate.execute(wd)
    assert gate.decide(obs) is GateStatus.PASS, obs.stderr
    assert (wd / "node_modules" / "dep").read_text() == "ok"


@_REAL
def test_real_smoke_is_sandboxed_with_working_loopback(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    boot = (
        "import socket\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
        "socket.create_connection(s.getsockname(), timeout=3).close()\n"
        "import os; assert os.getpid() < 10  # own PID namespace\n"
    )
    gate = SmokeGate(_py(boot))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.stderr


@_REAL
def test_real_fence_cannot_be_undone_from_inside(tmp_path: Path, monkeypatch: Any) -> None:
    """The command is root in its userns; the nested namespace must lock the fence."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    target = Path(__file__).parent / f".escaped-{os.getpid()}"
    script = (
        f"mount -o remount,bind,rw / ; umount -l /tmp ; umount -l {shlex.quote(str(tmp_path))} ; "
        f"echo x > {shlex.quote(str(target))}"
    )
    gate = BuildGate(cmd=("/bin/sh", "-c", script))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.stderr
    assert not target.exists()


@_REAL
def test_real_secrets_are_not_visible(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    home = Path(__file__).parent / f".home-{os.getpid()}"  # outside /tmp (else shadowed)
    try:
        (home / ".aws").mkdir(parents=True)
        (home / ".aws" / "credentials").write_text("AKIA-secret")
        (home / ".netrc").write_text("machine x password hunter2")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("GH_TOKEN", "ghp-secret")
        probe = (
            "import os, pathlib\n"
            "h = pathlib.Path(os.environ['HOME'])\n"
            "assert 'GH_TOKEN' not in os.environ\n"
            "assert not os.access(h / '.aws' / 'credentials', os.R_OK)\n"
            "assert (h / '.netrc').read_text() == ''\n"
        )
        gate = BuildGate(cmd=("true",), install_cmd=shlex.join(_py(probe)))
        obs = gate.execute(tmp_path)
        assert gate.decide(obs) is GateStatus.PASS, obs.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)
