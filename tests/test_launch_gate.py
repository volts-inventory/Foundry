"""The launch gate: Foundry starts the app as a user would (an http app on $PORT,
a CLI with its scenario command or --help) and FAILs what the tests missed."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from foundry.gates.launch import LaunchGate, LaunchTarget, launch_target
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, StackDecision, Ticket
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq

_SEQ = Seq("run", 1, 1, "planner", 0)
_PY = sys.executable

# A threaded stdlib server sharing one sqlite connection across request threads:
# the 500 the in-process tests never hit (the notes/shop bench apps).
_SHARED_SQLITE = """\
import os, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = sqlite3.connect(":memory:")
DB.execute("create table notes (body text)")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            rows = DB.execute("select count(*) from notes").fetchone()
            code, body = 200, f"<p>{rows[0]} notes</p>"
        except Exception as exc:
            code, body = 500, repr(exc)
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


ThreadingHTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""

# The same server made safe: the one connection is shared, but only under a lock
# (check_same_thread=False alone still races: fetchone() can return None).
_LOCKED_SQLITE = _SHARED_SQLITE.replace(
    'sqlite3.connect(":memory:")',
    'sqlite3.connect(":memory:", check_same_thread=False)\nLOCK = __import__("threading").Lock()',
).replace(
    "            rows = DB.execute",
    "            with LOCK:\n                rows = DB.execute",
)

_FORM_APP = """\
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b'<form method="post" action="/notes"><input name="body"><button>Add</button></form>'


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, PAGE)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n).decode()
        self._send(500 if "{post}" in data else 303, b"saved")

    def log_message(self, *a):
        pass


ThreadingHTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""


def _http(tmp_path: Path, source: str, **kw: object) -> LaunchGate:
    (tmp_path / "server.py").write_text(source)
    target = LaunchTarget(kind="http", cmd=f"{_PY} server.py", required=True, **kw)  # type: ignore[arg-type]
    return LaunchGate(target, boot_s=10)


def test_a_threaded_server_sharing_one_sqlite_connection_fails(tmp_path: Path) -> None:
    gate = _http(tmp_path, _SHARED_SQLITE)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.findings
    assert any("GET / answered 500" in f and "thread" in f for f in obs.findings)
    assert obs.findings[0].startswith("launch FAILED")


def test_a_working_server_passes_on_every_probed_path(tmp_path: Path) -> None:
    good = _LOCKED_SQLITE
    gate = _http(tmp_path, good, probes=(("GET", "/"), ("GET", "/notes")))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.findings
    assert [r["path"] for r in obs.structured["launch"]["requests"]] == ["/", "/notes"]


def test_the_form_a_page_serves_is_posted(tmp_path: Path) -> None:
    ok = _http(tmp_path, _FORM_APP.replace("{post}", "never-sent"))
    assert ok.decide(ok.execute(tmp_path)) is GateStatus.PASS
    bad = _http(tmp_path, _FORM_APP.replace("{post}", "body=launch"))
    obs = bad.execute(tmp_path)
    assert bad.decide(obs) is GateStatus.FAIL
    assert any("POST /notes answered 500" in f for f in obs.findings)


# A notes app behind sign-in, as a generated Flask app shapes it: CSRF tokens bound
# to the session (rotated at login), signup -> login -> create, an edit form that
# POSTs `_method=PUT`, a logout form. BREAK picks the defect under test.
_NOTES_APP = """\
import os, re, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BREAK = "{brk}"
LOCK = threading.Lock()
USERS, SESSIONS, NOTES = {{}}, {{}}, []
BUSY = [False]
PW = '<input type="password" name="password">'


def csrf_field(tok):
    return f'<input type="hidden" name="csrf_token" value="{{tok}}">'


class H(BaseHTTPRequestHandler):
    def _cookies(self):
        raw = (self.headers.get("Cookie") or "").split(";")
        return dict(p.strip().split("=", 1) for p in raw if "=" in p)

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def _session(self):
        c = self._cookies()
        sid = c.get("sid")
        with LOCK:
            if sid not in SESSIONS:
                sid = secrets.token_hex(8)
                SESSIONS[sid] = {{"csrf": secrets.token_hex(8), "user": None}}
            return sid, SESSIONS[sid]

    def do_GET(self):
        sid, s = self._session()
        hdr = [("Set-Cookie", f"sid={{sid}}; Path=/; HttpOnly")]
        tok = csrf_field(s["csrf"])
        if self.path == "/signup":
            page = (
                '<form method="post" action="/signup">' + tok + PW +
                '<input type="email" name="email"><input type="password" name="confirm">'
                "<button>Sign up</button></form>"
            )
        elif self.path == "/login":
            page = (
                '<a href="/signup">Sign up</a><form method="post" action="/login">' + tok + PW +
                '<input name="email"><button>Log in</button></form>'
            )
        elif s["user"] is None:
            return self._send(302, b"", hdr + [("Location", "/login")])
        elif self.path == "/":
            if BREAK == "race":
                if BUSY[0]:
                    return self._send(500, "sqlite3.ProgrammingError: shared connection in use")
                BUSY[0] = True
                time.sleep(0.2)
                BUSY[0] = False
            links = "".join(f'<a href="/notes/{{i}}">{{n}}</a>' for i, n in enumerate(NOTES))
            page = (
                links + '<form method="post" action="/notes">' + tok + '<input name="title">'
                '<textarea name="body"></textarea><button>Add</button></form>'
                '<form method="post" action="/logout">' + tok + "<button>Log out</button></form>"
            )
        elif re.fullmatch(r"/notes/\\d+", self.path):
            page = (
                f'<form method="post" action="{{self.path}}">' + tok +
                '<input type="hidden" name="_method" value="PUT"><input name="title">'
                "<button>Save</button></form>"
            )
        else:
            return self._send(404, b"not found")
        self._send(200, page, hdr)

    def do_POST(self):
        sid, s = self._session()
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        form = {{k: v[0] for k, v in parse_qs(data).items()}}
        if form.get("csrf_token") != s["csrf"]:
            return self._send(400, b"The CSRF token is missing or invalid.")
        if self.path == "/signup":
            if form.get("password") != form.get("confirm") or "@" not in form.get("email", ""):
                return self._send(400, b"bad signup")
            USERS[form["email"]] = form["password"]
            return self._send(303, b"", [("Location", "/login")])
        if self.path == "/login":
            if USERS.get(form.get("email")) != form.get("password"):
                return self._send(401, b"bad login")
            s["user"], s["csrf"] = form["email"], secrets.token_hex(8)
            return self._send(303, b"", [("Location", "/")])
        if s["user"] is None:
            return self._send(302, b"", [("Location", "/login")])
        if self.path == "/notes":
            if BREAK == "create":
                return self._send(500, b"Traceback (most recent call last):\\nKeyError: 'tags'")
            NOTES.append(form.get("title", ""))
            return self._send(303, b"", [("Location", "/")])
        if re.fullmatch(r"/notes/\\d+", self.path):
            if BREAK == "method" or form.get("_method") != "PUT":
                return self._send(405, b"Method Not Allowed")
            return self._send(303, b"", [("Location", "/")])
        if self.path == "/logout":
            s["user"] = None
            return self._send(303, b"", [("Location", "/login")])
        self._send(404, b"not found")

    def log_message(self, *a):
        pass


ThreadingHTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""


def _notes(tmp_path: Path, brk: str = "") -> tuple[GateStatus, tuple[str, ...], dict[str, Any]]:
    gate = _http(tmp_path, _NOTES_APP.format(brk=brk))
    obs = gate.execute(tmp_path)
    return gate.decide(obs), obs.findings, obs.structured["launch"]


def _submitted(launch: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [(q["method"], q["path"], q["status"]) for q in launch["requests"] if q.get("form")]


def test_a_correct_app_is_driven_through_signup_login_and_create(tmp_path: Path) -> None:
    status, findings, launch = _notes(tmp_path)
    assert status is GateStatus.PASS, findings
    sent = _submitted(launch)
    paths = [p for _, p, _ in sent]
    assert paths.index("/signup") < paths.index("/login") < paths.index("/notes"), sent
    assert paths[-1] == "/logout", sent
    # Each submission's redirect landed on a page (the session and CSRF token held).
    assert all(code == 200 for _, _, code in sent), sent
    assert "/notes/0" in paths, "the edit form behind the created note was never submitted"
    assert launch["burst"]["sent"] == 12 and launch["burst"]["failures"] == []
    assert launch["sent"] <= 80


def test_a_form_post_that_500s_behind_sign_in_fails(tmp_path: Path) -> None:
    status, findings, _ = _notes(tmp_path, "create")
    assert status is GateStatus.FAIL
    bad = [f for f in findings if "POST /notes answered 500" in f]
    assert bad and "KeyError: 'tags'" in bad[0], findings


def test_a_rendered_form_whose_method_the_route_refuses_fails_405(tmp_path: Path) -> None:
    status, findings, _ = _notes(tmp_path, "method")
    assert status is GateStatus.FAIL
    bad = [f for f in findings if "405 Method Not Allowed" in f]
    assert bad and "POST /notes/0" in bad[0] and "_method=PUT" in bad[0], findings


def test_shared_state_that_breaks_under_concurrent_requests_fails(tmp_path: Path) -> None:
    status, findings, launch = _notes(tmp_path, "race")
    assert status is GateStatus.FAIL, findings
    assert all(q.get("status", 0) < 500 for q in launch["requests"]), "sequential probe raced"
    bad = [f for f in findings if "concurrent requests" in f]
    assert bad and all("answered 500" in f and "shared connection" in f for f in bad), findings


def test_a_server_that_exits_before_listening_fails(tmp_path: Path) -> None:
    gate = _http(tmp_path, "print('app ready')\n")
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("exited (code 0) before listening on $PORT" in f for f in obs.findings)


def test_a_cli_package_without_main_fails(tmp_path: Path) -> None:
    (tmp_path / "dice").mkdir()
    (tmp_path / "dice" / "__init__.py").write_text("def roll(): return 4\n")
    gate = LaunchGate(
        LaunchTarget(kind="cli", cmd=f"{_PY} -m dice", runs=(f"{_PY} -m dice --help",))
    )
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("cannot start" in f and "cannot be directly executed" in f for f in obs.findings)


def test_a_cli_traceback_fails_and_a_working_cli_passes(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text(
        "import sys\nif '--help' in sys.argv: print('usage')\nelse: 1/0\n"
    )
    good = LaunchGate(LaunchTarget(kind="cli", cmd=f"{_PY} cli.py", runs=(f"{_PY} cli.py --help",)))
    assert good.decide(good.execute(tmp_path)) is GateStatus.PASS
    bad = LaunchGate(LaunchTarget(kind="cli", cmd=f"{_PY} cli.py", runs=(f"{_PY} cli.py roll",)))
    obs = bad.execute(tmp_path)
    assert bad.decide(obs) is GateStatus.FAIL
    assert any("traceback" in f and "ZeroDivisionError" in f for f in obs.findings)


def test_an_app_with_no_entry_fails_and_a_library_passes(tmp_path: Path) -> None:
    must = LaunchGate(LaunchTarget(kind="cli", required=True))
    obs = must.execute(tmp_path)
    assert must.decide(obs) is GateStatus.FAIL
    assert obs.findings[0].startswith("no runnable entry point")
    library = LaunchGate(LaunchTarget())
    assert library.decide(library.execute(tmp_path)) is GateStatus.PASS
    missing = LaunchGate(
        LaunchTarget(kind="cli", cmd="python3 app.py", required=True, needs=("app.py",))
    )
    obs = missing.execute(tmp_path)
    assert missing.decide(obs) is GateStatus.FAIL and "expected app.py" in obs.findings[0]


# ---- the target ------------------------------------------------------------------


def _ticket(tid: str, files: tuple[str, ...], *, entrypoint: bool = False) -> Ticket:
    return Ticket(
        id=tid, title=tid, specialty=Specialty.BACKEND, files=files, entrypoint=entrypoint, seq=_SEQ
    )


def _criterion(cid: str, tid: str, when: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=when,
        given=("the app",),
        when=(when,),
        then=("it works",),
        ticket_id=tid,
        seq=_SEQ,
    )


def test_the_target_comes_from_launch_then_run_then_the_entrypoint_files() -> None:
    web = StackDecision(language="python", framework="flask", run="python3 smoke.py")
    tickets = [_ticket("T1", ("notes/db.py",)), _ticket("T2", ("app.py",), entrypoint=True)]
    crit = [_criterion("AC-1", "T2", 'I GET /notes and POST /notes with "x"')]
    t = launch_target(web, None, tickets=tickets, criteria=crit, python="py")
    assert (t.kind, t.cmd, t.required, t.needs) == ("http", "py app.py", True, ("app.py",))
    assert t.probes == (("GET", "/"), ("GET", "/notes")) and t.posts == ("/notes",)
    served = web.model_copy(update={"run": "flask run"})
    assert launch_target(served, None, tickets=tickets).cmd == "flask run"
    declared = served.model_copy(update={"launch": "python3 -m notes"})
    assert launch_target(declared, None, tickets=tickets).cmd == "python3 -m notes"


def test_a_cli_target_runs_the_entrypoint_scenarios_command() -> None:
    cli = StackDecision(language="python", kind="cli")
    tickets = [_ticket("T1", ("dice/__init__.py", "dice/__main__.py"), entrypoint=True)]
    crit = [_criterion("AC-1", "T1", "I run `dice roll 2d6`")]
    t = launch_target(cli, None, tickets=tickets, criteria=crit, python="py")
    assert (t.cmd, t.runs, t.required) == (
        "py -m dice",
        ("py -m dice roll 2d6", "py -m dice --help"),
        True,
    )
    bare = launch_target(cli, None, tickets=tickets, python="py")
    assert bare.runs == ("py -m dice --help",)
    nothing = launch_target(cli, None, tickets=[_ticket("T1", ("dice/core.py",), entrypoint=True)])
    assert (nothing.cmd, nothing.required) == ("", True)


def test_a_security_abuse_scenarios_command_is_not_launched() -> None:
    cli = StackDecision(language="python", kind="cli")
    tickets = [_ticket("T1", ("todo/__init__.py", "todo/__main__.py"), entrypoint=True)]
    abuse = _criterion("AC-2", "T1", "I run `todo add '; rm -rf /'`").model_copy(
        update={"security_abuse": True}
    )
    crit = [_criterion("AC-1", "T1", "I run `todo done 99`"), abuse]
    t = launch_target(cli, None, tickets=tickets, criteria=crit, python="py")
    assert t.runs == ("py -m todo done 99", "py -m todo --help")


def test_a_cli_user_error_is_not_a_launch_failure(tmp_path: Path) -> None:
    (tmp_path / "todo.py").write_text(
        "import sys\n"
        "if '--help' in sys.argv: print('usage')\n"
        "else:\n"
        "    print('Error: task 99: not found', file=sys.stderr)\n"
        "    print('todo.json: No such file or directory', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    runs = (f"{_PY} todo.py done 99", f"{_PY} todo.py --help")
    gate = LaunchGate(LaunchTarget(kind="cli", cmd=f"{_PY} todo.py", runs=runs))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.findings


def test_a_declared_cli_whose_help_exits_nonzero_fails(tmp_path: Path) -> None:
    # The bench CLI that caught SystemExit: usage printed, but exit 1.
    (tmp_path / "todo.py").write_text("import sys\nprint('usage: todo [add|done]')\nsys.exit(1)\n")
    cmd = f"{_PY} todo.py"
    runs = (f"{cmd} done 99", f"{cmd} --help")
    gate = LaunchGate(LaunchTarget(kind="cli", cmd=cmd, runs=runs, required=True))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    bad = [f for f in obs.findings if "must print usage and exit 0" in f]
    assert len(bad) == 1 and f"`{cmd} --help` exited 1" in bad[0], obs.findings


def test_a_cli_whose_program_is_missing_or_file_absent_fails(tmp_path: Path) -> None:
    gone = LaunchGate(
        LaunchTarget(kind="cli", cmd="no-such-prog-xyz", runs=("no-such-prog-xyz --help",))
    )
    obs = gone.execute(tmp_path)
    assert gone.decide(obs) is GateStatus.FAIL, obs.findings
    absent = LaunchGate(
        LaunchTarget(kind="cli", cmd=f"{_PY} nope.py", runs=(f"{_PY} nope.py --help",))
    )
    obs = absent.execute(tmp_path)
    assert absent.decide(obs) is GateStatus.FAIL
    assert any("can't open file" in f for f in obs.findings)


def test_a_library_or_an_undeclared_build_without_an_entry_has_nothing_to_launch() -> None:
    stack = StackDecision(language="python")
    assert launch_target(stack, None, deliverable="library").kind == ""
    undeclared = launch_target(stack, None, tickets=[_ticket("T1", ("lib/core.py",))])
    assert (undeclared.cmd, undeclared.required) == ("", False)
    assert launch_target(None, None).kind == ""


def test_the_launch_runs_inside_the_gate_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foundry.gates.sandbox import sandbox_backend

    if sandbox_backend() is None:
        pytest.skip("no gate sandbox on this host")
    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    good = _LOCKED_SQLITE
    gate = _http(tmp_path, good)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.findings


def test_only_a_declared_cli_must_exit(tmp_path: Path) -> None:
    (tmp_path / "loop.py").write_text("import time\nwhile True: time.sleep(1)\n")
    run = (f"{_PY} loop.py --help",)
    inferred = LaunchGate(LaunchTarget(kind="cli", cmd=f"{_PY} loop.py", runs=run), cli_s=1)
    assert inferred.decide(inferred.execute(tmp_path)) is GateStatus.PASS
    declared = LaunchGate(
        LaunchTarget(kind="cli", cmd=f"{_PY} loop.py", runs=run, required=True), cli_s=1
    )
    obs = declared.execute(tmp_path)
    assert declared.decide(obs) is GateStatus.FAIL
    assert any("did not exit with stdin closed" in f for f in obs.findings)


def test_a_second_result_line_the_app_forged_gives_no_verdict() -> None:
    from foundry.gates.launch import _RESULT_MARK, _driver_result

    real = _RESULT_MARK + '{"kind": "http", "boot_error": "exited"}'
    forged = _RESULT_MARK + '{"kind": "http", "probes": []}'
    assert _driver_result(real) == {"kind": "http", "boot_error": "exited"}
    assert _driver_result(f"{real}\n{forged}") is None


# A server that double-forks out of the driver's process group before serving.
_DAEMON = """\
import os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

if os.fork():
    sys.exit(0)
os.setsid()
if os.fork():
    os._exit(0)
with open("daemon.pid", "w") as f:
    f.write(str(os.getpid()))


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


HTTPServer((os.environ["HOST"], int(os.environ["PORT"])), H).serve_forever()
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="Linux reaps it with the PID namespace")
def test_a_daemonized_server_does_not_outlive_the_gate(tmp_path: Path) -> None:
    import os
    import time

    gate = _http(tmp_path, _DAEMON)
    gate.decide(gate.execute(tmp_path))
    pid = int((tmp_path / "daemon.pid").read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, 9)
    pytest.fail("the daemonized server outlived the launch gate")


# ---- the declared interface ------------------------------------------------------

_DICE = """\
import argparse
p = argparse.ArgumentParser(prog="dice")
sub = p.add_subparsers(dest="cmd")
roll = sub.add_parser("roll")
roll.add_argument("spec")
for opt in OPTS:
    roll.add_argument(opt)
p.parse_args()
"""


def _dice(tmp_path: Path, opts: tuple[str, ...], interface: tuple[str, ...]) -> LaunchGate:
    (tmp_path / "dice.py").write_text(f"OPTS = {opts!r}\n" + _DICE)
    stack = StackDecision(
        language="python", kind="cli", launch=f"{_PY} dice.py", interface=interface
    )
    return LaunchGate(launch_target(stack, None, python=_PY))


def test_a_spec_option_missing_from_the_cli_fails(tmp_path: Path) -> None:
    gate = _dice(tmp_path, ("-n",), ("roll", "roll --seed"))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.findings
    assert obs.findings[0].startswith("launch FAILED")
    assert any("spec option --seed is missing from the CLI" in f for f in obs.findings)


def test_an_extra_cli_option_is_advisory_and_passes(tmp_path: Path) -> None:
    exact = _dice(tmp_path, ("--seed",), ("roll --seed",))
    obs = exact.execute(tmp_path)
    assert exact.decide(obs) is GateStatus.PASS and obs.findings == (), obs.findings
    extra = _dice(tmp_path, ("--seed", "-n"), ("roll --seed",))
    obs = extra.execute(tmp_path)
    assert extra.decide(obs) is GateStatus.PASS, obs.findings
    assert any("advisory" in f and "-n" in f for f in obs.findings), obs.findings
    assert not any(f.startswith("launch FAILED") for f in obs.findings)


def test_a_spec_subcommand_missing_from_the_cli_fails(tmp_path: Path) -> None:
    gate = _dice(tmp_path, ("--seed",), ("stats",))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("spec subcommand `stats` is missing" in f for f in obs.findings)


def test_a_spec_route_the_server_lacks_fails(tmp_path: Path) -> None:
    good = _LOCKED_SQLITE
    ok = _http(tmp_path, good, interface=("GET /notes", "DELETE /notes/{id}"))
    assert ok.decide(ok.execute(tmp_path)) is GateStatus.PASS
    # The stdlib handler has no do_POST: it answers 501 Unsupported method.
    bad = _http(tmp_path, good, interface=("GET /notes", "POST /notes"))
    obs = bad.execute(tmp_path)
    assert bad.decide(obs) is GateStatus.FAIL, obs.findings
    assert any("spec route POST /notes is missing" in f for f in obs.findings)


def test_nothing_declared_leaves_the_launch_unchanged(tmp_path: Path) -> None:
    gate = _dice(tmp_path, ("-n",), ())
    assert gate.target.runs == (f"{_PY} dice.py --help",)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS and obs.findings == ()


# ---- hostile input: a pinned "never prints a traceback" the scenarios never test ----

# A dice CLI as the bench generated it: a recursive-descent parser over int(), so
# deep nesting is a RecursionError and a 5000-digit literal a ValueError traceback.
_EVAL_CLI = """\
import sys

def expr(s, i):
    if s[i] == "(":
        v, i = expr(s, i + 1)
        return v, i + 1
    j = i
    while j < len(s) and s[j].isdigit():
        j += 1
    return int(s[i:j]), j

args = sys.argv[1:]
if "--help" in args:
    print("usage: calc eval EXPR")
    sys.exit(0)
text = args[-1] if len(args) > 1 else sys.stdin.read().strip()
GUARD
print(expr(text, 0)[0] if text else 0)
"""
_GUARDED = """\
if len(text) > 200 or not text.replace("(", "").replace(")", "").isdigit():
    print("error: bad expression", file=sys.stderr)
    sys.exit(2)
"""


def _calc(tmp_path: Path, guard: str, crit: str) -> LaunchGate:
    (tmp_path / "calc.py").write_text(_EVAL_CLI.replace("GUARD", guard))
    stack = StackDecision(language="python", kind="cli", launch=f"{_PY} calc.py")
    return LaunchGate(launch_target(stack, None, criteria=[_criterion("AC-1", "T1", crit)]))


def test_the_hostile_probes_use_the_scenarios_program_and_argument() -> None:
    cli = StackDecision(language="python", kind="cli", interface=("roll", "--seed"))
    tickets = [_ticket("T1", ("dice/__init__.py", "dice/__main__.py"), entrypoint=True)]
    crit = [
        _criterion("AC-1", "T1", "I run `dice roll --seed 7 2d6 --sum`"),
        _criterion("AC-2", "T1", 'I run "echo 2d6 | dice roll"'),
    ]
    t = launch_target(cli, None, tickets=tickets, criteria=crit, python="py")
    assert t.fuzz == (
        ("argument", "py -m dice roll --seed 7", " --sum"),
        ("stdin", "py -m dice roll", ""),
    )
    # Only a subcommand and an option's value: no argument to probe.
    only = [_criterion("AC-1", "T1", "I run `dice roll --seed 7`")]
    assert launch_target(cli, None, tickets=tickets, criteria=only, python="py").fuzz == ()


def test_hostile_input_that_crashes_the_cli_fails_and_a_clean_error_passes(
    tmp_path: Path,
) -> None:
    bad = _calc(tmp_path, "", "I run `calc eval (1)`")
    assert bad.target.fuzz == (("argument", f"{_PY} calc.py eval", ""),)
    obs = bad.execute(tmp_path)
    assert bad.decide(obs) is GateStatus.FAIL, obs.findings
    crashed = [f for f in obs.findings if "crashed" in f]
    assert any("2000 nested brackets" in f and "RecursionError" in f for f in crashed), crashed
    assert any("5000-digit number" in f for f in crashed), crashed
    assert not any("empty string" in f for f in crashed)
    # The echoed input is cut down, not pasted 10,000 characters long.
    assert all(len(f) < 3000 for f in obs.findings)
    good = _calc(tmp_path, _GUARDED, "I run `calc eval (1)`")
    obs = good.execute(tmp_path)
    assert good.decide(obs) is GateStatus.PASS, obs.findings


def test_hostile_input_on_stdin_is_probed_too(tmp_path: Path) -> None:
    gate = _calc(tmp_path, "", "I run `echo '(1)' | calc`")
    assert gate.target.fuzz == (("stdin", f"{_PY} calc.py", ""),)
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert any("on stdin" in f and "RecursionError" in f for f in obs.findings), obs.findings


def test_a_crash_signal_on_hostile_input_fails(tmp_path: Path) -> None:
    (tmp_path / "seg.py").write_text(
        "import os, signal, sys\n"
        "if len(sys.argv[-1]) > 1000: os.kill(os.getpid(), signal.SIGSEGV)\n"
    )
    gate = LaunchGate(
        LaunchTarget(
            kind="cli",
            cmd=f"{_PY} seg.py",
            runs=(f"{_PY} seg.py a",),
            fuzz=(("argument", f"{_PY} seg.py", ""),),
        )
    )
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.findings
    assert any("crashed" in f and "10000-character" in f for f in obs.findings)


def test_an_oversized_form_field_that_500s_fails(tmp_path: Path) -> None:
    big = _FORM_APP.replace('500 if "{post}" in data', "500 if n > 100_000")
    obs = (gate := _http(tmp_path, big)).execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL, obs.findings
    assert any("1 MB value" in f and "field(s) body answered 500" in f for f in obs.findings)
    capped = _FORM_APP.replace('500 if "{post}" in data', "413 if n > 100_000")
    gate = _http(tmp_path, capped)
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.PASS
