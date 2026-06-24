"""Foundry Workshop — a localhost game frontend over the REAL factory.

Run:  uv run python -m webapp.server   then open  http://localhost:8765

A tiny stdlib HTTP + Server-Sent-Events server (no extra deps, never touches the
trust core). ``POST /api/projects`` starts a REAL ``foundry.cli.run`` in a thread;
the build pauses at its real stage boundaries and this server streams the real
artifacts / agent-journal / gate verdicts to the browser as paced game events.
The CTO (you) approves or rejects each checkpoint — driving the real run — and an
approve can NEVER release a failing build (the gates decide; this is just the view).
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from webapp.events import events_for_payload, events_for_result

_STATIC = Path(__file__).parent / "static"
_PACE = 0.5          # seconds between paced game events
_STAGE_PAUSE = 0.9   # extra beat when a new stage opens
_AUTO_APPROVE_DELAY = 1.4  # auto-run: dwell on each checkpoint before approving
_END = {"type": "_end"}

_MODES = ("assistant", "gated", "autonomous")


@dataclass
class Project:
    id: str
    request: str
    mode: str
    supervised: bool
    floor: int
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    decisions: queue.Queue[str] = field(default_factory=queue.Queue)
    seen_uris: set[str] = field(default_factory=set)
    status: str = "running"


_PROJECTS: dict[str, Project] = {}
_LOCK = threading.Lock()
_FLOOR = [0]


def _emit(p: Project, **event: Any) -> None:
    p.events.put(event)


def _emit_paced(p: Project, events: list[dict[str, Any]]) -> None:
    for ev in events:
        _emit(p, **ev)
        time.sleep(_STAGE_PAUSE if ev.get("type") == "stage" else _PACE)


def _run_factory(p: Project) -> None:
    """Drive a REAL build; stream its telemetry as paced game events."""
    # Imported here so the server starts fast and a missing dep never blocks boot.
    from foundry.cli import run

    _emit(p, type="project", id=p.id, request=p.request, mode=p.mode, floor=p.floor)
    _emit(p, type="narrate", text=f"📋 New project on Floor {p.floor}: {p.request!r}")
    _emit(p, type="narrate", text=f"🏭 Mode: {p.mode}. The crew reports for duty…")
    time.sleep(_PACE)

    def responder(payload: dict[str, Any]) -> str:
        # The REAL stage payload -> paced game events, then the CTO's call.
        _emit_paced(p, events_for_payload(payload, p.seen_uris))
        boundary = str(payload.get("boundary", "stage"))
        if p.supervised:
            _emit(p, type="await_decision", boundary=boundary)
            try:
                decision = p.decisions.get(timeout=600)
            except queue.Empty:
                decision = "approve"
        else:
            time.sleep(_AUTO_APPROVE_DELAY)
            decision = "approve"
        _emit(p, type="resolved", boundary=boundary, decision=decision)
        verb = "approved" if decision == "approve" else "REJECTED"
        _emit(p, type="narrate", text=f"🧑‍💼 CTO {verb} the {boundary} checkpoint.")
        return decision

    try:
        result = run(p.request, mode=p.mode, checkpoints=True, checkpoint_responder=responder)
    except Exception as exc:
        _emit(p, type="narrate", text=f"💥 Build error: {exc}")
        _emit(p, type="halt", phase="error", reason=str(exc))
        _emit(p, type="done", passed=False, phase="error")
        p.status = "done"
        p.events.put(_END)
        return

    result_dict = {
        "passed": result.passed,
        "phase": result.phase,
        "mode": result.mode,
        "artifact_uri": result.artifact_uri,
        "gate_statuses": {k: str(v) for k, v in result.gate_statuses.items()},
    }
    _emit_paced(p, events_for_result(result_dict))
    if result.passed:
        _emit(p, type="narrate", text="💰 Gates all green — the vault opens. Shipped!")
    else:
        _emit(p, type="narrate", text="🚫 Held at the gate (fail-closed). Nothing shipped.")
    p.status = "done"
    p.events.put(_END)


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a: Any) -> None:  # quiet console
        pass

    # -- helpers ----------------------------------------------------------------
    def _json(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        target = (_STATIC / rel).resolve()
        if not str(target).startswith(str(_STATIC.resolve())) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon",
        }.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/stream/"):
            self._stream(path.rsplit("/", 1)[-1])
            return
        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if not path.startswith("/api"):
            self._serve_static(path)  # e.g. /favicon.ico
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/projects":
            self._new_project()
        elif path.startswith("/api/decision/"):
            self._decision(path.rsplit("/", 1)[-1])
        else:
            self._json(404, {"error": "not found"})

    def _new_project(self) -> None:
        body = self._read_json()
        request = str(body.get("request", "")).strip() or "build a tiny URL-shortener service"
        mode = str(body.get("mode", "assistant"))
        if mode not in _MODES:
            mode = "assistant"
        supervised = bool(body.get("supervised", True))
        with _LOCK:
            _FLOOR[0] += 1
            floor = _FLOOR[0]
            pid = uuid.uuid4().hex[:12]
            p = Project(id=pid, request=request, mode=mode, supervised=supervised, floor=floor)
            _PROJECTS[pid] = p
        threading.Thread(target=_run_factory, args=(p,), daemon=True).start()
        self._json(200, {"id": pid, "floor": floor})

    def _decision(self, pid: str) -> None:
        body = self._read_json()
        decision = str(body.get("decision", "approve"))
        if decision not in ("approve", "reject", "needs_changes"):
            decision = "approve"
        p = _PROJECTS.get(pid)
        if p is None:
            self._json(404, {"error": "no such project"})
            return
        p.decisions.put(decision)
        self._json(200, {"ok": True})

    def _stream(self, pid: str) -> None:
        p = _PROJECTS.get(pid)
        if p is None:
            self._json(404, {"error": "no such project"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    ev = p.events.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")  # comment frame
                    self.wfile.flush()
                    continue
                if ev is _END or ev.get("type") == "_end":
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client navigated away


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  🏭  Foundry Workshop running →  http://{host}:{port}\n")
    print("      (Ctrl-C to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye 👋\n")
        srv.shutdown()


if __name__ == "__main__":
    serve()
