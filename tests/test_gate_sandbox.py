"""Namespace (``unshare``) sandbox for the test/build/smoke gates — argv, routing, fail-closed,
and a REAL end-to-end run (user namespaces are available in CI, so we exercise it)."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from foundry.gates import sandbox
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
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p evil")
    captured: dict[str, Any] = {}

    def fake_run(cmd: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        captured["cmd"], captured["env"] = list(cmd), env
        return (0, "1 passed", "")

    monkeypatch.setattr(sandbox, "run_grounding_command", fake_run)
    TestGate().execute(tmp_path)
    assert captured["cmd"][0] != "unshare"                  # NOT wrapped
    assert "-c" in captured["cmd"]                          # the isolating bootstrap
    env = captured["env"]                                   # ...but still env-allowlisted
    assert "GH_TOKEN" not in env and "PYTEST_ADDOPTS" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1" and env["PATH"] == os.environ["PATH"]
    assert env["FOUNDRY_GATE_WORKDIR"] == str(Path(tmp_path).resolve())


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
    rc, out, _err = sandbox.ground_untrusted(
        TestGate().cmd, tmp_path, extra_env=TestGate()._env(Path(tmp_path)), timeout_s=120.0
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


def _fence_cfg(argv: list[str]) -> dict[str, Any]:
    return json.loads(argv[argv.index(sandbox._FENCE) + 1])


def test_install_alone_gets_the_egress_relay_build_and_smoke_are_offline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls = _capture(monkeypatch)
    gate = BuildGate(cmd=("true",), install_cmd="npm ci")
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.PASS
    SmokeGate(("true",)).execute(tmp_path)
    TestGate(cmd=("true",), min_passed=0).execute(tmp_path)
    install, build, smoke, test = calls
    assert all(argv[0] == "unshare" and "--net" in argv for argv in calls)  # own netns
    assert all(sandbox._FENCE in argv for argv in calls)                  # all confined
    assert install[-3:] == ["/bin/sh", "-c", "npm ci"] and _fence_cfg(install)["relay"]
    assert build[-1] == "true" and not _fence_cfg(build)["relay"]
    assert not _fence_cfg(smoke)["relay"] and not _fence_cfg(test)["relay"]


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
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")
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
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")
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
def test_real_build_and_install_cannot_reach_host_loopback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A host loopback service (a DB, the docker API, a dev server) is unreachable from
    every step — the install step included: it has its own netns, not the host's."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        code = _CONNECT.format(port=srv.getsockname()[1])
        for gate in (BuildGate(cmd=_py(code)),
                     BuildGate(cmd=("true",), install_cmd=shlex.join(_py(code)))):
            obs = gate.execute(tmp_path)
            assert gate.decide(obs) is GateStatus.FAIL and "blocked" in obs.stderr
        inet = BuildGate(cmd=_py("socket = __import__('socket'); socket.create_connection("
                                 "('1.1.1.1', 53), timeout=3)"))
        assert inet.decide(inet.execute(tmp_path)) is GateStatus.FAIL


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


# ---- test/bdd confinement, install egress, sockets, credentials, opt-out ----------


def _host_dir(tag: str) -> Path:
    """A host path OUTSIDE /tmp (the sandbox shadows /tmp with a private tmpfs)."""
    return Path(__file__).parent / f".{tag}-{os.getpid()}-{os.urandom(3).hex()}"


@_REAL
def test_real_test_gate_is_confined(tmp_path: Path, monkeypatch: Any) -> None:
    """The pinned pytest harness runs confined like build/smoke: no host secrets in its
    env, no writes outside the workdir, loopback up for scenarios that boot a server."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    escaped = _host_dir("escaped")
    (tmp_path / "test_probe.py").write_text(
        "import os, socket, pytest\n\n"
        "def test_no_host_secrets():\n"
        "    assert 'GH_TOKEN' not in os.environ\n"
        "    assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
        "    assert os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'\n\n"
        "def test_cannot_write_outside_workdir():\n"
        "    with pytest.raises(OSError):\n"
        f"        open({str(escaped)!r}, 'w')\n"
        "    open('inside.txt', 'w').write('ok')\n\n"
        "def test_loopback_server():\n"
        "    s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
        "    socket.create_connection(s.getsockname(), timeout=3).close()\n",
        encoding="utf-8",
    )
    gate = TestGate()
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.stdout + obs.stderr
    assert "3 passed" in obs.stdout
    assert not escaped.exists() and (tmp_path / "inside.txt").read_text() == "ok"


@_REAL
def test_real_bdd_cmd_is_confined_but_writes_its_report(tmp_path: Path, monkeypatch: Any) -> None:
    """The Cucumber bdd command: the gate-owned report file (outside the workdir) is
    bound in writable at its own path; everything else outside the workdir is not."""
    from foundry.gates import cucumber

    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    seen: dict[str, str] = {}
    real_record = cucumber.record_report

    def record(path: Path) -> Any:
        seen["report"] = path.read_text()
        seen["siblings"] = ",".join(sorted(p.name for p in path.parent.iterdir()))
        return real_record(path)

    monkeypatch.setattr(cucumber, "record_report", record)
    escaped = _host_dir("escaped")
    bdd = (
        'test -z "$GH_TOKEN" && printf report > "$FOUNDRY_BDD_REPORT" && '
        'echo x > "$(dirname "$FOUNDRY_BDD_REPORT")/planted"; '
        f"echo x > {shlex.quote(str(escaped))}; "
        f"{shlex.quote(sys.executable)} -c 'import socket; s = socket.socket(); "
        's.bind(("127.0.0.1", 0)); s.listen(1); '
        "socket.create_connection(s.getsockname(), timeout=3)' && echo LOOPBACK-OK"
    )
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "T1.feature").write_text(
        "Feature: f\n  Scenario: AC-1\n    Given a thing\n", encoding="utf-8"
    )
    gate = TestGate(cmd=("true",), min_passed=0, bdd_cmd=bdd)
    obs = gate.execute(tmp_path)
    assert seen["report"] == "report", obs.stdout + obs.stderr     # report reached host
    assert "planted" not in seen["siblings"]                       # only that file is bound
    assert not escaped.exists()
    assert "LOOPBACK-OK" in obs.stdout, obs.stdout + obs.stderr


@_REAL
def test_real_binds_are_exactly_as_writable_as_asked(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    wd, chan = tmp_path / "wd", tmp_path / "chan"
    wd.mkdir()
    chan.mkdir()
    (chan / "ro.json").write_text("{}")
    code = (
        "import sys\n"
        f"open({str(chan / 'report')!r}, 'x').write('r')\n"
        "try:\n"
        f"    open({str(chan / 'ro.json')!r}, 'w')\n"
        "except OSError:\n    pass\n"
        "else:\n    sys.exit('ro bind was writable')\n"
    )
    rc, _out, err = sandbox.run_confined(
        _py(code), wd, binds=((chan, True), (chan / "ro.json", False))
    )
    assert rc == 0, err
    assert (chan / "report").read_text() == "r" and (chan / "ro.json").read_text() == "{}"


class _FakeUpstream:
    """A host-loopback stand-in for the HTTPS egress proxy: answers CONNECT for any
    target with 200 and then echoes, recording what it was asked for."""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c: socket.socket) -> None:
        with c:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.targets.append(head.split()[1].decode())
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while data := c.recv(4096):
                c.sendall(data)


_EGRESS_PROBE = r"""
import os, socket, sys, urllib.parse
proxy = urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
assert proxy.hostname == "127.0.0.1", proxy
assert "registry.npmjs.org" not in os.environ.get("NO_PROXY", "")
def tunnel(target):
    s = socket.create_connection((proxy.hostname, proxy.port), timeout=5)
    s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
    return s, s.recv(4096).split()[1]
s, status = tunnel("registry.example.com:443")
assert status == b"200", status
s.sendall(b"ping"); assert s.recv(4) == b"ping"
for bad in ("169.254.169.254:80", "[fd00:ec2::254]:80", "127.0.0.1:%s" % sys.argv[1],
            "localhost:22", "metadata.google.internal:80", "2130706433:80"):
    assert tunnel(bad)[1] == b"403", bad
def blocked(family, addr):
    try:
        socket.socket(family).connect(addr)
    except OSError:
        return True
    return False
assert blocked(socket.AF_INET, ("169.254.169.254", 80))
assert blocked(socket.AF_INET, ("127.0.0.1", int(sys.argv[1])))
assert blocked(socket.AF_UNIX, "\0" + sys.argv[2])
print("EGRESS-OK")
"""


@_REAL
def test_real_install_egress_is_proxy_only(tmp_path: Path, monkeypatch: Any) -> None:
    """Install reaches the network ONLY through the gate's CONNECT relay, chained to
    the host's HTTPS_PROXY: metadata, host loopback and abstract sockets are out."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    up = _FakeUpstream()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{up.port}")
    monkeypatch.setenv("NO_PROXY", "registry.npmjs.org")
    abstract = f"foundry-test-{os.getpid()}-{os.urandom(3).hex()}"
    with socket.socket() as svc, socket.socket(socket.AF_UNIX) as ab:
        svc.bind(("127.0.0.1", 0))
        svc.listen(4)
        ab.bind("\0" + abstract)
        ab.listen(4)
        probe = shlex.join((*_py(_EGRESS_PROBE), str(svc.getsockname()[1]), abstract))
        gate = BuildGate(cmd=("true",), install_cmd=probe)
        obs = gate.execute(tmp_path)
    up.srv.close()
    assert gate.decide(obs) is GateStatus.PASS, obs.stdout + obs.stderr
    assert "EGRESS-OK" in obs.stdout
    assert up.targets == ["registry.example.com:443"]          # nothing else got out


def test_egress_allowed_refuses_non_public_destinations() -> None:
    for ok in ("registry.npmjs.org", "pypi.org", "1.1.1.1", "2606:4700:4700::1111"):
        assert sandbox.egress_allowed(ok), ok
    for bad in ("169.254.169.254", "fd00:ec2::254", "[fd00:ec2::254]", "127.0.0.1", "::1",
                "10.0.0.1", "100.64.0.1", "::ffff:169.254.169.254", "localhost", "a.localhost",
                "metadata.google.internal", "2130706433", "0x7f.1", "0", "", "host name"):
        assert not sandbox.egress_allowed(bad), bad


@pytest.mark.slow
@_REAL
@pytest.mark.skipif(not os.environ.get("HTTPS_PROXY"), reason="no HTTPS egress proxy")
def test_real_install_reaches_the_registry_through_the_proxy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Live: the registry is reachable through the real host proxy (network needed)."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    gate = BuildGate(
        cmd=("true",),
        install_cmd="curl -sS -o /dev/null -w '%{http_code}' https://registry.npmjs.org/left-pad",
    )
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.stderr
    assert obs.stdout.strip() == "200"


@_REAL
def test_real_host_unix_sockets_and_run_are_masked(tmp_path: Path, monkeypatch: Any) -> None:
    """A docker.sock-like socket on a read-only host mount is still connect()able; the
    fence masks every host socket and /run."""
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    d = _host_dir("sock")
    d.mkdir()
    path = d / "docker.sock"
    try:
        with socket.socket(socket.AF_UNIX) as srv:
            srv.bind(str(path))
            srv.listen(4)
            code = (
                "import os, socket, sys\n"
                "s = socket.socket(socket.AF_UNIX)\n"
                "try:\n"
                f"    s.connect({str(path)!r})\n"
                "except OSError:\n    pass\n"
                "else:\n    sys.exit('socket reachable')\n"
                "assert not os.path.isdir('/run') or os.listdir('/run') == [], os.listdir('/run')\n"
            )
            for gate in (BuildGate(cmd=_py(code)),
                         BuildGate(cmd=("true",), install_cmd=shlex.join(_py(code)))):
                obs = gate.execute(tmp_path)
                assert gate.decide(obs) is GateStatus.PASS, obs.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


@_REAL
def test_real_symlinked_and_extra_credential_stores_are_masked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    home, store = _host_dir("home"), _host_dir("store")
    try:
        (store / "aws").mkdir(parents=True)
        (store / "aws" / "credentials").write_text("AKIA-secret")
        (store / "settings.xml").write_text("<password>m2-secret</password>")
        (home / ".m2").mkdir(parents=True)
        (home / ".aws").symlink_to(store / "aws")                          # symlinked dir
        (home / ".m2" / "settings.xml").symlink_to(store / "settings.xml")  # symlinked file
        (home / ".pypirc").write_text("password = pypi-secret")
        monkeypatch.setenv("HOME", str(home))
        probe = (
            "import os, pathlib\n"
            f"store = pathlib.Path({str(store)!r})\n"
            "h = pathlib.Path(os.environ['HOME'])\n"
            "def unreadable(p):\n"
            "    try:\n        return 'secret' not in p.read_text()\n"
            "    except OSError:\n        return True\n"
            "for p in (h / '.aws' / 'credentials', store / 'aws' / 'credentials',\n"
            "          h / '.m2' / 'settings.xml', store / 'settings.xml', h / '.pypirc'):\n"
            "    assert unreadable(p), p\n"
            "ccr = pathlib.Path('/root/.ccr')\n"
            "if ccr.is_dir():\n"
            "    assert not (ccr / 'README.md').exists()\n"
            "    if (ccr / 'ca-bundle.crt').exists():\n"
            "        assert (ccr / 'ca-bundle.crt').read_text().startswith('-----BEGIN')\n"
        )
        gate = BuildGate(cmd=("true",), install_cmd=shlex.join(_py(probe)))
        obs = gate.execute(tmp_path)
        assert gate.decide(obs) is GateStatus.PASS, obs.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(store, ignore_errors=True)


def test_inprocess_refuses_networked_install_unless_explicitly_allowed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("FOUNDRY_GATE_SANDBOX", "inprocess")
    monkeypatch.delenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp-secret")
    calls: list[dict[str, str]] = []

    def fake_run(argv: Any, workdir: Any, *, env: Any = None, timeout_s: float = 600.0) -> Any:
        calls.append(env)
        return (0, "", "")

    monkeypatch.setattr(sandbox, "run_grounding_command", fake_run)
    with pytest.raises(GroundingUnavailable, match="FOUNDRY_ALLOW_UNSANDBOXED_INSTALL"):
        BuildGate(cmd=("true",), install_cmd="npm ci").execute(tmp_path)
    assert calls == []                                       # nothing ran
    sandbox.ground_untrusted(("true",), tmp_path)            # offline steps still run...
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")
    BuildGate(cmd=("true",), install_cmd="npm ci").execute(tmp_path)
    assert len(calls) == 3
    assert all("GH_TOKEN" not in env and "PATH" in env for env in calls)  # ...allowlisted
