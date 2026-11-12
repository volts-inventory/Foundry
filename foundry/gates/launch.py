"""Launch gate — class 2 (execution-deterministic).

Foundry, not a model-written script, starts the app the way a user would and
checks that it works. It runs in the same confined sandbox as smoke
(``sandbox.ground_untrusted``: loopback only, writes fenced to the workdir).
Inside the sandbox a trusted driver (:data:`_DRIVER`, Foundry's code) starts the
app's command in its own process group and drives it:

* **http** — ``PORT``/``HOST`` name a free loopback port. The driver waits for the
  port to listen, GETs ``/`` and the entrypoint scenarios' paths, and POSTs the
  first form a page serves (else a scenario's POST path). Then it kills the whole
  group. FAIL when the app exits early or never listens, a request is refused, a
  response is a 5xx, or the app dies under the requests.
* **cli** — the driver runs the entrypoint scenarios' commands (else
  ``<cmd> --help``). FAIL on a traceback, a missing-entry/import error, or a
  command that never exits with stdin closed.

An app that must launch (a declared or inferred cli/http kind) but has no
runnable entry FAILs; a library, or a build with nothing to launch, gets an
attested "not pinned" PASS. The core is language-neutral: the profile/stack
supplies the command, and only the fallback entry derivation knows file names.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, timeout_findings
from .sandbox import ground_untrusted

if TYPE_CHECKING:
    from foundry.schemas.implementation import StackDecision, Ticket
    from foundry.schemas.product import AcceptanceCriterion

    from .profiles import VerificationProfile
    from .pydeps import PythonBuildEnv

__all__ = ["LaunchGate", "LaunchTarget", "entry_run", "launch_target"]

KINDS = ("cli", "http")
LAUNCH_KEY = "launch"
_RESULT_MARK = "FOUNDRY-LAUNCH-RESULT "
_MAX_GETS = 6
_MAX_RUNS = 3

# The trusted driver: argv[1] is its JSON config; its last stdout line is the result.
_LAUNCH_ID_ENV = "FOUNDRY_LAUNCH_ID"

_DRIVER = r"""
import http.client, json, os, re, signal, socket, subprocess, sys, threading, time
LAUNCH_ID_ENV = "FOUNDRY_LAUNCH_ID"
from urllib.parse import urlencode, urljoin, urlsplit

cfg = json.loads(sys.argv[1])
# Every process the app starts inherits this, so the host can find a daemonized
# one that left the process group (macOS has no PID namespace to reap it).
os.environ[LAUNCH_ID_ENV] = cfg["launch_id"]
MARK = cfg["mark"]
TAIL = 4000


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
    try:  # a child that outlived the leader still holds the group
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def text(buf, t):
    t.join(2)
    return b"".join(buf).decode("utf-8", "replace")[-TAIL:]


def run_cli():
    runs = []
    for cmd in cfg["runs"]:
        proc, buf, t = spawn(cmd, dict(os.environ))
        try:
            rc = proc.wait(timeout=cfg["cli_s"])
        except subprocess.TimeoutExpired:
            rc = None
        kill(proc)
        runs.append({"cmd": cmd, "rc": rc, "output": text(buf, t)})
    return {"runs": runs}


class Forms:
    def __init__(self):
        self.cookies = {}

    def headers(self, extra=None):
        h = {"Connection": "close"}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        h.update(extra or {})
        return h

    def keep(self, resp):
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]


FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
ATTR = re.compile(r'([\w-]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')
FIELD = re.compile(r"<(input|textarea|select)\b([^>]*)>", re.I)
FILL = {"email": "launch@example.com", "number": "1", "password": "Launch-check-1",
        "url": "http://example.com", "date": "2024-01-01"}


def attrs(raw):
    return {k.lower(): v.strip("\"'") for k, v in ATTR.findall(raw)}


def first_post_form(page, body):
    for head, inner in FORM.findall(body):
        a = attrs(head)
        if a.get("method", "get").lower() != "post":
            continue
        fields = {}
        for tag, raw in FIELD.findall(inner):
            f = attrs(raw)
            name, kind = f.get("name"), f.get("type", "text").lower()
            if not name or kind in ("submit", "button", "file", "reset", "image"):
                continue
            if kind in ("hidden", "checkbox", "radio"):
                fields.setdefault(name, f.get("value", "on"))
            else:
                fields[name] = FILL.get(kind, "launch check")
        action = urlsplit(urljoin(page, a.get("action") or page))
        return (action.path or "/") + (f"?{action.query}" if action.query else ""), fields
    return None


def request(port, jar, method, path, form=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=cfg["request_s"])
    body = urlencode(form).encode() if form is not None else None
    extra = {"Content-Type": "application/x-www-form-urlencoded"} if form is not None else {}
    try:
        conn.request(method, path, body=body, headers=jar.headers(extra))
        resp = conn.getresponse()
        jar.keep(resp)
        data = resp.read(65536).decode("utf-8", "replace")
        ctype = resp.getheader("Content-Type", "") or ""
        return {"method": method, "path": path, "status": resp.status, "body": data,
                "html": "html" in ctype.lower()}
    except Exception as exc:
        return {"method": method, "path": path, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def run_http():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1")
    proc, buf, t = spawn(cfg["cmd"], env)
    out = {"port": port, "boot": "timeout", "requests": []}
    deadline = time.monotonic() + cfg["boot_s"]
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out["boot"] = "exited"
            out["rc"] = proc.returncode
            break
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            out["boot"] = "listening"
            break
        except OSError:
            time.sleep(0.2)
    if out["boot"] == "listening":
        jar = Forms()
        form = None
        for method, path in cfg["probes"]:
            r = request(port, jar, method, path)
            out["requests"].append(r)
            if form is None and r.get("html") and r.get("status", 500) < 400:
                form = first_post_form(path, r.get("body", ""))
        if form is not None:
            out["requests"].append(request(port, jar, "POST", form[0], form[1]))
        elif cfg["posts"]:
            out["requests"].append(request(port, jar, "POST", cfg["posts"][0], {}))
        for r in out["requests"]:
            r["body"] = r.get("body", "")[:600]
            r.pop("html", None)
        out["alive"] = proc.poll() is None
        if not out["alive"]:
            out["rc"] = proc.returncode
    kill(proc)
    out["output"] = text(buf, t)
    return out


result = run_cli() if cfg["kind"] == "cli" else run_http()
result["kind"] = cfg["kind"]
sys.stdout.write("\n" + MARK + json.dumps(result) + "\n")
sys.stdout.flush()
"""


@dataclass(frozen=True)
class LaunchTarget:
    """What the launch gate starts and how: ``kind`` ("cli" | "http" | ""), the
    shell line ``cmd`` ("" = no runnable entry), whether the app must launch
    (``required``: a declared or inferred app kind), the http ``probes``
    ((method, path), GET only) and ``posts`` (scenario POST paths), the cli
    ``runs``, and the files the entry ``needs`` (derived from planned files)."""

    kind: str = ""
    cmd: str = ""
    required: bool = False
    probes: tuple[tuple[str, str], ...] = (("GET", "/"),)
    posts: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    source: str = ""  # where the command came from, for the findings


# ---- the target: stack fields, else the run command, else the entry's files ----

_SMOKE_LIKE = re.compile(r"\b(smoke|tests?|pytest|check|verify)\b|&&|;|\|\|")


def _server_run(stack: StackDecision) -> bool:
    from foundry.agents.planner.checks import _APP_ENTRY, _SERVER_COMMAND, _WEB_FRAMEWORK

    run = " ".join(stack.run.split())
    return bool(
        _SERVER_COMMAND.match(run)
        or (_APP_ENTRY.match(run) and _WEB_FRAMEWORK.search(stack.framework))
    )


def _web_stack(stack: StackDecision) -> bool:
    from foundry.agents.planner.checks import _WEB_FRAMEWORK

    return bool(_WEB_FRAMEWORK.search(stack.framework)) or _server_run(stack)


def entry_run(stack: StackDecision, kind: str) -> str:
    """``stack.run`` when it starts the app itself (a server entry for http, a plain
    CLI invocation for cli), not a smoke script; else ""."""
    if not stack.run.strip():
        return ""
    if kind == "http":
        return stack.run if _server_run(stack) else ""
    return stack.run if kind == "cli" and not _SMOKE_LIKE.search(stack.run) else ""


def _kind(stack: StackDecision | None, deliverable: str) -> tuple[str, bool]:
    """(kind, required): a declared kind or an inferred web stack / declared
    deliverable must launch; anything else is a CLI if it has an entry at all."""
    if stack is not None and stack.kind in KINDS:
        return stack.kind, True
    if deliverable == "library":
        return "", False
    if stack is not None and _web_stack(stack):
        return "http", True
    if deliverable == "service":
        return "http", True
    if deliverable == "cli":
        return "cli", True
    return "cli", False


_PY_SCRIPTS = ("__main__.py", "main.py", "app.py", "server.py", "cli.py", "run.py", "wsgi.py")
_JS_SCRIPTS = tuple(
    f"{n}.{x}" for n in ("server", "index", "app", "main", "cli") for x in ("js", "mjs")
)


def _default_entry(language: str, files: Sequence[str], python: str) -> tuple[str, str]:
    """(command, the file it needs) for the entry the planned ``files`` hold, or
    ("", "") when none is recognizable."""
    paths = sorted({f.removeprefix("./") for f in files}, key=lambda p: (p.count("/"), p))
    if language == "python":
        for name in _PY_SCRIPTS:
            for p in paths:
                pp = PurePosixPath(p)
                if pp.name != name or "test" in pp.parts[0]:
                    continue
                if name == "__main__.py" and len(pp.parts) > 1:
                    return f"{shlex.quote(python)} -m {'.'.join(pp.parts[:-1])}", p
                if name != "__main__.py":
                    return f"{shlex.quote(python)} {shlex.quote(p)}", p
    elif language in ("javascript", "typescript"):
        for name in _JS_SCRIPTS:
            for p in paths:
                if PurePosixPath(p).name == name and p.count("/") <= 1:
                    return f"node {shlex.quote(p)}", p
    elif language == "go":
        for p in paths:
            pp = PurePosixPath(p)
            if pp.name == "main.go" and (len(pp.parts) == 1 or pp.parts[0] == "cmd"):
                return ("go run ." if len(pp.parts) == 1 else f"go run ./{pp.parent}"), p
    return "", ""


_HTTP_STEP = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s\"'`),]*)")
_QUOTED_PATH = re.compile(r"[\"'`](/[\w\-./?=&%]*)[\"'`]")
_QUOTED = re.compile(r"`([^`]+)`|\"([^\"]+)\"")
_INTERPRETERS = frozenset(
    {"python", "python3", "node", "go", "npx", "npm", "cargo", "java", "dotnet"}
)


def _probes(lines: Iterable[str]) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    gets: list[str] = ["/"]
    posts: list[str] = []
    for line in lines:
        for method, path in _HTTP_STEP.findall(line):
            if re.search(r"[<{]", path):
                continue
            (gets if method == "GET" else posts if method == "POST" else []).append(path)
        gets += [p for p in _QUOTED_PATH.findall(line) if not re.search(r"[<{]", p)]
    gets = list(dict.fromkeys(gets))[:_MAX_GETS]
    return tuple(("GET", p) for p in gets), tuple(dict.fromkeys(posts))


def _cli_runs(cmd: str, lines: Iterable[str]) -> tuple[str, ...]:
    """The scenarios' quoted commands that invoke this app: one starting with the
    launch command, or with its program name (``dice roll`` for ``python -m dice``,
    run as ``python -m dice roll``)."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return ()
    if not argv:
        return ()
    names = {PurePosixPath(argv[-1]).stem, PurePosixPath(argv[0]).name} - _INTERPRETERS - {""}
    out: list[str] = []
    for line in lines:
        for a, b in _QUOTED.findall(line):
            text = " ".join((a or b).split())
            if text.startswith(" ".join(cmd.split())):
                out.append(text)
                continue
            try:
                words = shlex.split(text)
            except ValueError:
                continue
            if words and words[0] in names:
                out.append(" ".join([cmd, *map(shlex.quote, words[1:])]))
    return tuple(dict.fromkeys(out))[:_MAX_RUNS]


def launch_target(
    stack: StackDecision | None,
    profile: VerificationProfile | None,
    *,
    tickets: Sequence[Ticket] = (),
    criteria: Iterable[AcceptanceCriterion] = (),
    deliverable: str = "",
    python: str = sys.executable,
) -> LaunchTarget:
    """The app to launch for this run. A pinned profile (or no stack) uses the
    profile's ``launch_cmd``/``launch_kind``. A planner stack: ``stack.launch``,
    else ``stack.run`` when it is the app's own entry, else the language's default
    entry among the entrypoint ticket's files."""
    if stack is None or stack.profile:
        kind = profile.launch_kind if profile is not None else ""
        cmd = (profile.launch_cmd or "") if profile is not None else ""
        required = kind in KINDS
        language = ""
    else:
        kind, required = _kind(stack, deliverable)
        cmd = stack.launch.strip() or entry_run(stack, kind or "cli")
        language = stack.language
    if not kind:
        return LaunchTarget()
    entries = [t for t in tickets if t.entrypoint]
    owners = {t.id for t in entries}
    lines = [
        s
        for c in criteria
        if not owners or c.ticket_id in owners
        for s in (*c.given, *c.when, *c.then)
    ]
    source = "the stack's launch command" if stack is not None and stack.launch.strip() else ""
    needs: tuple[str, ...] = ()
    if cmd and not source:
        source = (
            "the stack's run command" if stack is not None and cmd == stack.run else "the profile"
        )
    if not cmd and language:
        files = [f for t in (entries or tickets) for f in t.files]
        cmd, need = _default_entry(language, files, python)
        needs = (need,) if need else ()
        source = f"the entrypoint's {need}" if need else ""
    if kind == "http":
        probes, posts = _probes(lines)
        return LaunchTarget(kind, cmd, required, probes, posts, needs=needs, source=source)
    # A security_abuse scenario's command is meant to be refused: no launch verdict.
    runs = _cli_runs(
        cmd,
        (
            s
            for c in criteria
            if (not owners or c.ticket_id in owners) and not c.security_abuse
            for s in c.when
        ),
    )
    return LaunchTarget(
        kind,
        cmd,
        required,
        runs=tuple(dict.fromkeys((*runs, f"{cmd} --help"))) if cmd else (),
        needs=needs,
        source=source,
    )


# ---- the gate ---------------------------------------------------------------

_TRACEBACK = re.compile(
    r"Traceback \(most recent call last\)|^panic: |^\S*Error: .*\n\s+at |Uncaught \w*Error",
    re.M,
)
# Only a failure to start the program: a CLI's own error for a bad argument or a
# missing record (`Error: task 99: not found`, exit 1) is the scenario's answer.
_ENTRY_ERROR = re.compile(
    r"No module named|ModuleNotFoundError|ImportError|cannot import name|"
    r"can't find '__main__' module|is a package and cannot be directly executed|"
    r"can't open file|Cannot find module|ERR_MODULE_NOT_FOUND|SyntaxError|no Go files|"
    r"cannot find package|package \S+ is not a main package|undefined: "
)


def _program_missing(cmd: str, text: str) -> bool:
    """The shell could not find the launch command's program itself."""
    try:
        prog = shlex.split(cmd)[0]
    except (ValueError, IndexError):
        return False
    p = re.escape(prog)
    return bool(re.search(rf"{p}: (?:command )?not found|command not found: {p}\b", text))


_NO_ENTRY = (
    "no runnable entry point: the app has no command a user can launch — add a "
    "__main__/main/app entry the entrypoint ticket owns (an http app serves on "
    "$PORT/$HOST) and declare it as the stack's launch command"
)


def _tail(text: str, lines: int = 20, chars: int = 1500) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:] or "(no output)"


def _http_findings(r: dict[str, Any], cmd: str) -> list[str]:
    out: list[str] = []
    output = _tail(str(r.get("output", "")))
    boot = r.get("boot")
    if boot == "exited":
        out.append(
            f"launch: `{cmd}` exited (code {r.get('rc')}) before listening on $PORT "
            f"({r.get('port')}): a web app must start its server and keep serving when "
            f"launched — read the port from the PORT env var and the host from HOST. "
            f"Its output:\n{output}"
        )
        return out
    if boot != "listening":
        out.append(
            f"launch: `{cmd}` never listened on $PORT ({r.get('port')}) within the boot "
            "timeout: bind the server to int(os.environ['PORT']) (the PORT env var) on "
            f"HOST, not a fixed port. Its output:\n{output}"
        )
        return out
    for q in r.get("requests") or ():
        what = f"{q.get('method')} {q.get('path')}"
        if "error" in q:
            out.append(f"launch: {what} failed ({q['error']}) against the running app")
        elif int(q.get("status", 0)) >= 500:
            out.append(
                f"launch: {what} answered {q['status']} under the real server: a server "
                f"error the tests did not catch. Response: {str(q.get('body', ''))[:400]}"
            )
    if r.get("alive") is False:
        out.append(f"launch: the app died while serving requests (code {r.get('rc')})")
    if out:
        out.append(f"launch: `{cmd}` output:\n{output}")
    return out


def _cli_findings(r: dict[str, Any], cmd: str) -> list[str]:
    out: list[str] = []
    for run in r.get("runs") or ():
        cmd, rc, text = run.get("cmd"), run.get("rc"), str(run.get("output", ""))
        if rc is None and r.get("required", True):
            out.append(
                f"launch: `{cmd}` did not exit with stdin closed: a CLI must finish (or "
                f"exit on end of input) when run. Its output:\n{_tail(text)}"
            )
        elif rc is None:
            continue  # an inferred CLI that keeps running (a game loop) is no verdict
        elif _TRACEBACK.search(text):
            out.append(f"launch: `{cmd}` crashed (exit {rc}) with a traceback:\n{_tail(text)}")
        elif rc != 0 and (_ENTRY_ERROR.search(text) or _program_missing(cmd, text)):
            out.append(
                f"launch: `{cmd}` cannot start (exit {rc}): the entry point or an import "
                f"is broken — the user's launch command must work as written:\n{_tail(text)}"
            )
    return out


class LaunchGate:
    """PASS iff Foundry can start the app as a user would and it works (see module
    doc); the verdict is decided from the driver's observed result."""

    name = "launch"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        target: LaunchTarget,
        *,
        seal_key: bytes | None = None,
        pyenv: PythonBuildEnv | None = None,
        boot_s: float = 30.0,
        request_s: float = 10.0,
        cli_s: float = 30.0,
    ) -> None:
        self.target = target
        self._pyenv = pyenv
        self._seal_key = seal_key
        self._cfg = {"boot_s": boot_s, "request_s": request_s, "cli_s": cli_s}
        runs = len(target.runs) if target.kind == "cli" else 1
        # Each CLI run may also spend its kill (3s + 5s) and output join (2s).
        self._timeout_s = boot_s + request_s * (len(target.probes) + 2) + (cli_s + 10) * runs + 30
        self.mutant_rotation_id = 0
        self.scanner_rule_version = "launch-v1"

    def _verdict(self, structured: dict[str, Any], findings: Sequence[str]) -> ZoneAObservation:
        return observe(
            self.name,
            structured={LAUNCH_KEY: structured},
            findings=tuple(findings),
            seal_key=self._seal_key,
        )

    def execute(self, workdir: Path) -> ZoneAObservation:
        t = self.target
        if not t.kind:
            return self._verdict(
                {"pinned": False}, ("launch not pinned: nothing to launch (a library)",)
            )
        missing = [p for p in t.needs if not (workdir / p).is_file()]
        if not t.cmd or missing:
            why = _NO_ENTRY + (f" (expected {', '.join(missing)})" if missing else "")
            return self._verdict(
                {"pinned": False, "required": t.required, "kind": t.kind},
                (why if t.required else f"launch not pinned: {why}",),
            )
        cfg = {
            **self._cfg,
            "mark": _RESULT_MARK,
            "kind": t.kind,
            "cmd": t.cmd,
            "probes": [list(p) for p in t.probes],
            "posts": list(t.posts),
            "runs": list(t.runs),
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
        except subprocess.TimeoutExpired as exc:
            _reap_escapees(launch_id)
            return observe(
                self.name,
                structured={"cmd": t.cmd, "timed_out_s": self._timeout_s},
                findings=(
                    f"launch: the launch check of `{t.cmd}` did not finish",
                    *timeout_findings(exc, workdir),
                ),
                seal_key=self._seal_key,
            )
        _reap_escapees(launch_id)
        result = _driver_result(out)
        if result is None:
            return observe(
                self.name,
                exit_codes=(rc,),
                stdout=out,
                stderr=err,
                structured={"cmd": t.cmd},
                findings=(f"launch driver exited {rc} with no result: {_tail(err or out)}",),
                seal_key=self._seal_key,
            )
        result["required"] = t.required
        findings = (
            _http_findings(result, t.cmd) if t.kind == "http" else _cli_findings(result, t.cmd)
        )
        if findings:
            source = f" ({t.source})" if t.source else ""
            findings.insert(
                0, f"launch FAILED: Foundry started `{t.cmd}`{source} as a {t.kind} app"
            )
        result.update(cmd=t.cmd, pinned=True)
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            structured={LAUNCH_KEY: result},
            findings=tuple(findings),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        r = obs.structured.get(LAUNCH_KEY)
        if not isinstance(r, dict):
            return GateStatus.ERROR
        if r.get("pinned") is False:
            return GateStatus.FAIL if r.get("required") else GateStatus.PASS
        if r.get("kind") == "http":
            bad = _http_findings(r, str(r.get("cmd", "")))
        elif r.get("kind") == "cli":
            bad = _cli_findings(r, str(r.get("cmd", "")))
        else:
            return GateStatus.ERROR
        return GateStatus.FAIL if bad else GateStatus.PASS


def _driver_result(stdout: str) -> dict[str, Any] | None:
    """The driver's one result line. More than one means the app wrote a forged one
    (it can add lines to the driver's stdout but never remove the driver's own): no
    verdict, so never a PASS it didn't earn."""
    marked = [line for line in stdout.splitlines() if line.startswith(_RESULT_MARK)]
    if len(marked) != 1:
        return None
    try:
        got = json.loads(marked[0][len(_RESULT_MARK) :])
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _reap_escapees(launch_id: str) -> None:
    """SIGKILL any process still carrying ``launch_id`` in its environment: an app
    that daemonized (setsid) out of the driver's process group outlives its kill on
    macOS, which has no PID namespace. Linux reaps them with the namespace."""
    if sys.platform != "darwin":
        return
    try:
        listing = subprocess.run(
            ("ps", "-Eww", "-x", "-o", "pid=,command="),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    needle = f"{_LAUNCH_ID_ENV}={launch_id}"
    for line in listing.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if needle in rest and pid.isdigit() and int(pid) != os.getpid():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid), signal.SIGKILL)
