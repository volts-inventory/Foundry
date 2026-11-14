"""Review reproduction gate — class 2 (execution-deterministic).

A reviewer finding blocks in the proxy only on the model's own report of a command
whose observed result differs from the correct one (``_verdict_findings``). That
is the model's word: it never fails a run on its own. This gate makes it an
attested result: Foundry re-runs each such command against the final tree in the
gate sandbox (``sandbox.ground_untrusted``) and FAILs only when the run shows the
defect again (:func:`reproduces`). A finding that does not reproduce, or a run
that cannot tell (the command is missing, it hangs, the app never boots), stays
advisory.

For an http app the trusted driver (:data:`_DRIVER`, Foundry's code) starts the
app the way the launch gate does (its command, ``PORT``/``HOST`` on a free
loopback port), waits for it to listen, then runs the commands with any loopback
URL in them pointed at that port. A bare ``GET /path`` is sent by the driver
itself (no curl needed). Any other app runs the commands as they are.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .pydeps import PythonBuildEnv

__all__ = [
    "REPRODUCED_PREFIX",
    "REPRO_GATE",
    "ReproGate",
    "Reproduction",
    "reproduces",
    "tag_line",
]

REPRO_GATE = "repro"
REPRODUCED_PREFIX = "REPRODUCED on the final tree: "
_KEY = "repro"
_RESULT_MARK = "FOUNDRY-REPRO-RESULT "
_LAUNCH_ID_ENV = "FOUNDRY_LAUNCH_ID"
_OUTPUT_TAIL = 4000

_DRIVER = r"""
import http.client, json, os, re, signal, socket, subprocess, sys, threading, time

cfg = json.loads(sys.argv[1])
os.environ[cfg["launch_env"]] = cfg["launch_id"]
TAIL = cfg["tail"]
REQUEST = re.compile(r"^(GET|HEAD|DELETE|POST|PUT|PATCH)\s+(/\S*)$")
LOOPBACK = re.compile(r"\b(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?")


def spawn(cmd, env):
    proc = subprocess.Popen(
        ["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, start_new_session=True,
    )
    buf = []

    def drain():
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buf.append(chunk)
        except (OSError, ValueError):
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    return proc, buf, t


def kill(proc):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def text(buf, t):
    t.join(2)
    return b"".join(buf).decode("utf-8", "replace")[-TAIL:]


def request(port, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=cfg["cmd_s"])
    try:
        conn.request(method, path, body=b"" if method in ("POST", "PUT", "PATCH") else None)
        resp = conn.getresponse()
        body = resp.read(65536).decode("utf-8", "replace")
        return {"rc": 0, "output": f"HTTP {resp.status} {resp.reason}\n{body}"[-TAIL:]}
    except (OSError, http.client.HTTPException) as exc:
        return {"rc": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def run(cmd, env, port):
    m = REQUEST.match(cmd.strip())
    if m is not None and port is not None:
        return request(port, m.group(1), m.group(2))
    if port is not None:
        cmd = LOOPBACK.sub(lambda h: ("http://" if h.group(0).startswith("http") else "")
                           + f"127.0.0.1:{port}", cmd)
    proc, buf, t = spawn(cmd, env)
    try:
        rc = proc.wait(timeout=cfg["cmd_s"])
    except subprocess.TimeoutExpired:
        rc = None
    kill(proc)
    out = {"rc": rc, "output": text(buf, t)}
    if rc is None:
        out["error"] = f"did not exit within {cfg['cmd_s']}s"
    return out


def main():
    env = dict(os.environ)
    port = None
    app = None
    out = {"boot": "none"}
    if cfg["app"]:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env.update(PORT=str(port), HOST="127.0.0.1")
        app, abuf, at = spawn(cfg["app"], env)
        out["boot"] = "timeout"
        deadline = time.monotonic() + cfg["boot_s"]
        while time.monotonic() < deadline:
            if app.poll() is not None:
                out["boot"] = "exited"
                break
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                out["boot"] = "listening"
                break
            except OSError:
                time.sleep(0.2)
        env["BASE_URL"] = f"http://127.0.0.1:{port}"
    results = []
    for cmd in cfg["commands"]:
        if cfg["app"] and out["boot"] != "listening":
            results.append({"rc": None, "error": f"the app did not start ({out['boot']})"})
            continue
        results.append(run(cmd, env, port))
    out["results"] = results
    if app is not None:
        kill(app)
        out["app_output"] = text(abuf, at)
    return out


sys.stdout.write("\n" + cfg["mark"] + json.dumps(main()) + "\n")
sys.stdout.flush()
"""


@dataclass(frozen=True)
class Reproduction:
    """One backed review finding: its text, the command, what a correct app does
    and what the reviewer saw this one do."""

    finding: str
    command: str
    correct: str
    observed: str


# ---- the comparison (pure: zone B re-derives the verdict with it) ------------

# A crash in an observation: a correct app never answers with one. Mirrors the
# proxy's evidence rule, plus a non-zero exit and an http 5xx status line.
_CRASH = re.compile(
    r"traceback|(?-i:\b[A-Z]\w*(?:Error|Exception)\b)|\b(?:raised|threw|thrown)\b"
    r"|\bpanic:|segmentation fault|core dumped"
    r"|\bcrash(?:ed|es)?\b|\buncaught\b|\bunhandled\b|internal server error"
    r"|\b(?:http|status|code)\W{0,3}5\d\d\b|\b5\d\d (?:bad gateway|service unavailable)"
    r"|\bexit(?:ed)?(?: with)?(?: code| status)?\W{0,3}(?:[1-9]\d*|-\d+)\b"
    r"|killed by signal|\bsig(?:segv|abrt|kill)\b",
    re.IGNORECASE,
)
# The shell's "command not found" / "not executable": the reviewer's tool, not the app.
_UNRUNNABLE_RC = frozenset({126, 127})
_QUOTES = "'\"`"


def _norm(value: str) -> str:
    return " ".join(value.strip().strip(_QUOTES).split()).casefold()


def _contains(haystack: str, needle: str) -> bool:
    """``needle`` occurs in ``haystack`` as whole tokens (``0`` is not in ``200``)."""
    if not needle:
        return False
    lead = r"(?<!\w)" if needle[0].isalnum() or needle[0] == "_" else ""
    tail = r"(?!\w)" if needle[-1].isalnum() or needle[-1] == "_" else ""
    return re.search(lead + re.escape(needle) + tail, haystack) is not None


def _json(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return None


def _body(output: str) -> str:
    head, sep, rest = output.partition("\n")
    return rest if sep and head.startswith("HTTP ") else output


def reproduces(correct: str, observed: str, run: dict[str, Any], command: str = "") -> bool | None:
    """Did ``run`` (``{"rc": int | None, "output": str}``) of ``command`` show the
    defect again?

    ``None`` when it cannot tell: no exit (a hang, a request refused, the app never
    started), the shell could not run the command, or the command itself spells the
    observed result (``echo broken``: the claim, not the app, would be its output).
    Otherwise:

    * the reviewer saw a crash a correct result does not show: a crash again (a
      non-zero exit, a traceback, a 5xx) reproduces it;
    * else the run's output must hold the reviewer's observed result and not the
      correct one (whole tokens, case and whitespace folded; JSON compared as
      values when both sides parse). One the correct result itself contains (a
      correct ``notes`` inside an observed ``stub notes``) does not count against it.
    """
    rc = run.get("rc")
    if not isinstance(rc, int) or rc in _UNRUNNABLE_RC:
        return None
    if _contains(_norm(command), _norm(observed)):
        return None
    output = str(run.get("output", ""))
    if _CRASH.search(observed) and not _CRASH.search(correct):
        return rc != 0 or _CRASH.search(output) is not None
    got = _json(_body(output).strip())
    seen, right = _json(observed), _json(correct)
    if got is not None and seen is not None:
        return got == seen and got != right
    out, obs, cor = _norm(output), _norm(observed), _norm(correct)
    if not _contains(out, obs):
        return False
    return not (_contains(out, cor) and not _contains(obs, cor))


# ---- the gate ------------------------------------------------------------------

_PYTHON = re.compile(r"(^|[;&|(]\s*)python3?(?=\s)")


class ReproGate:
    """FAIL iff a reviewer's reproduction command shows the defect again on this
    tree (:func:`reproduces`); PASS when none does. ``tag`` names the review verdict
    whose findings are checked, recorded as the first finding."""

    name = REPRO_GATE
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        reproductions: Sequence[Reproduction],
        *,
        tag: str,
        app_cmd: str = "",
        python: str = sys.executable,
        seal_key: bytes | None = None,
        pyenv: PythonBuildEnv | None = None,
        boot_s: float = 30.0,
        cmd_s: float = 20.0,
        install_timeout_s: float = 600.0,
    ) -> None:
        self.reproductions = tuple(reproductions)
        self.tag = tag
        self.app_cmd = app_cmd
        self._python = pyenv.python if pyenv is not None else python
        self._pyenv = pyenv
        self._seal_key = seal_key
        self._boot_s = boot_s
        self._cmd_s = cmd_s
        self._install_timeout_s = install_timeout_s
        self._timeout_s = boot_s + (cmd_s + 12) * len(self.reproductions) + 30
        self.mutant_rotation_id = 0
        self.scanner_rule_version = "repro-v1"

    def _command(self, cmd: str) -> str:
        # The reviewer's `python` is the build's interpreter (its venv, its deps).
        return _PYTHON.sub(lambda m: m.group(1) + self._python, cmd)

    def execute(self, workdir: Path) -> ZoneAObservation:
        from .launch import _reap_escapees

        if self._pyenv is not None and not self._pyenv.ready:
            # This check runs on its own build env, after the final gates: their install
            # left the run's venv for this manifest in place (reused here, no pip run),
            # but ``ready`` is per build. Without it every Python app with dependencies
            # ERRORed here, and its backed review findings were never re-run.
            rc, out, err = self._pyenv.install(workdir, timeout_s=self._install_timeout_s)
            if not self._pyenv.ready:
                return observe(
                    self.name,
                    exit_codes=(rc,),
                    stdout=out,
                    stderr=err,
                    findings=(
                        f"review reproduction of {self.tag} could not install the "
                        f"app's Python dependencies (pip exited {rc})",
                    ),
                    seal_key=self._seal_key,
                )
        cfg = {
            "mark": _RESULT_MARK,
            "tail": _OUTPUT_TAIL,
            "app": self.app_cmd,
            "commands": [self._command(r.command) for r in self.reproductions],
            "boot_s": self._boot_s,
            "cmd_s": self._cmd_s,
            "launch_env": _LAUNCH_ID_ENV,
            "launch_id": (launch_id := secrets.token_hex(16)),
        }
        try:
            rc, out, err = ground_untrusted(
                (sys.executable, "-I", "-S", "-c", _DRIVER, json.dumps(cfg)),
                workdir,
                timeout_s=self._timeout_s,
                extra_env=self._pyenv.run_env() if self._pyenv is not None else None,
                binds=self._pyenv.binds if self._pyenv is not None else (),
            )
        except subprocess.TimeoutExpired:
            _reap_escapees(launch_id)
            return observe(
                self.name,
                structured={"timed_out_s": self._timeout_s},
                findings=(f"review reproduction of {self.tag} did not finish",),
                seal_key=self._seal_key,
            )
        _reap_escapees(launch_id)
        marked = [line for line in out.splitlines() if line.startswith(_RESULT_MARK)]
        result = json.loads(marked[0][len(_RESULT_MARK) :]) if len(marked) == 1 else None
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            return observe(
                self.name,
                exit_codes=(rc,),
                stdout=out,
                stderr=err,
                findings=(f"review reproduction driver exited {rc} with no result",),
                seal_key=self._seal_key,
            )
        runs = [
            {
                "finding": r.finding,
                "command": r.command,
                "correct": r.correct,
                "observed": r.observed,
                **{k: v for k, v in run.items() if k in ("rc", "output", "error")},
            }
            for r, run in zip(self.reproductions, result["results"], strict=False)
        ]
        structured = {"tag": self.tag, "boot": result.get("boot"), "runs": runs}
        return observe(
            self.name,
            exit_codes=(rc,),
            structured={_KEY: structured},
            findings=_findings(self.tag, runs),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        r = obs.structured.get(_KEY)
        if not isinstance(r, dict) or not isinstance(r.get("runs"), list):
            return GateStatus.ERROR
        for x in r["runs"]:
            fields = (str(x.get(k, "")) for k in ("correct", "observed", "command"))
            correct, observed, command = fields
            if reproduces(correct, observed, x, command):
                return GateStatus.FAIL
        return GateStatus.PASS


def tag_line(tag: str) -> str:
    return f"[review reproduction of {tag}]"


def _findings(tag: str, runs: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """The tag line, then one line per finding: a reproduced one in the reviewer's
    evidence form (so a later check can re-run it) with what this run printed."""
    out = [tag_line(tag)]
    for x in runs:
        seen = reproduces(str(x["correct"]), str(x["observed"]), x, str(x["command"]))
        evidence = f"[evidence: `{x['command']}` expected {x['correct']!r}, got {x['observed']!r}]"
        if seen:
            got = str(x.get("output", "")).strip()[-800:]
            out.append(f"{REPRODUCED_PREFIX}{x['finding']} {evidence}")
            out.append(f"  Foundry ran it and got (exit {x.get('rc')}): {got}")
        elif seen is None:
            why = x.get("error") or f"exit {x.get('rc')}"
            out.append(f"advisory, could not re-run ({why}): {x['finding']}")
        else:
            out.append(f"advisory, did not reproduce: {x['finding']}")
    return tuple(out)
