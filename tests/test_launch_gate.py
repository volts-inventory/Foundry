"""The launch gate: Foundry starts the app as a user would (an http app on $PORT,
a CLI with its scenario command or --help) and FAILs what the tests missed."""

from __future__ import annotations

import sys
from pathlib import Path

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
    good = _SHARED_SQLITE.replace(
        'sqlite3.connect(":memory:")', 'sqlite3.connect(":memory:", check_same_thread=False)'
    )
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
    assert (t.cmd, t.runs, t.required) == ("py -m dice", ("py -m dice roll 2d6",), True)
    bare = launch_target(cli, None, tickets=tickets, python="py")
    assert bare.runs == ("py -m dice --help",)
    nothing = launch_target(cli, None, tickets=[_ticket("T1", ("dice/core.py",), entrypoint=True)])
    assert (nothing.cmd, nothing.required) == ("", True)


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
    good = _SHARED_SQLITE.replace(
        'sqlite3.connect(":memory:")', 'sqlite3.connect(":memory:", check_same_thread=False)'
    )
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
