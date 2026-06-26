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
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from foundry.schemas.architecture import CANONICAL_AREAS
from webapp.events import events_for_payload, events_for_result

_STATIC = Path(__file__).parent / "static"
_PACE = 0.5          # seconds between paced game events
_STAGE_PAUSE = 0.9   # extra beat when a new stage opens
_AUTO_APPROVE_DELAY = 1.4  # auto-run: dwell on each checkpoint before approving
_END = {"type": "_end"}

_MODES = ("assistant", "gated", "autonomous")

# Where each floor's build writes its output tree + doc bundle (gitignored).
# Override with WORKSHOP_OUTPUT_DIR (e.g. a mounted volume on a Pi).
_OUTPUT_ROOT = Path(os.environ.get("WORKSHOP_OUTPUT_DIR", ".run/web")).resolve()
_MAX_VIEW_BYTES = 1_000_000  # cap a single file served to the in-page viewer
# Uploaded-spec-zip limits (defence-in-depth for an untrusted upload).
_MAX_ZIP_BYTES = 5_000_000
_MAX_ZIP_MEMBER_BYTES = 1_000_000
_MAX_ZIP_MEMBERS = 200
_SPEC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")


def _safe_unzip_spec(b64: str, dest: Path) -> int:
    """Decode + extract a base64 docs-zip into ``dest``, returning the file count.

    Defence-in-depth for an untrusted upload: caps total/member size + member
    count, keeps only text-doc suffixes, and fail-closes on zip-slip (a member
    that resolves outside ``dest`` is skipped, never written)."""
    import base64
    import io
    import zipfile

    raw = base64.b64decode(b64, validate=False)
    if len(raw) > _MAX_ZIP_BYTES:
        raise ValueError("uploaded zip exceeds size cap")
    dest.mkdir(parents=True, exist_ok=True)
    root = str(dest.resolve())
    written = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > _MAX_ZIP_MEMBER_BYTES:
                continue
            if Path(info.filename).suffix.lower() not in _SPEC_SUFFIXES:
                continue
            target = (dest / info.filename).resolve()
            if not str(target).startswith(root + os.sep):  # zip-slip guard
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            written += 1
            if written >= _MAX_ZIP_MEMBERS:
                break
    return written


# --- interactive spec chat with the leads (intake option 4) ----------------- #
# Advisory authoring aid (same posture as foundry.ideation): it produces ONLY
# text — a transcript + a running draft spec — and writes NO FactoryState / gate /
# verdict. "Lock spec & build" just feeds the draft back as run(spec=<text>); the
# spec gate + criteria machinery are unchanged (agents propose, gates decide).
_CHATS: dict[str, dict[str, Any]] = {}  # session id -> {"history":[...], "spec":str}

_LEADS_SOP = (
    "You are a product leadership round-table — the Program Manager, the Principal "
    "Architect, and 1-2 domain Tech-Leads — helping a CTO turn an idea into a "
    "buildable spec. Each turn, reply as 2-4 of these voices (set role to the "
    "title), each one or two concise, substantive sentences: ask the single most "
    "important clarifying question, surface a risk, or propose a concrete "
    "capability / acceptance criterion. Then output spec_markdown: the CURRENT best "
    "full draft spec (goals, constraints, interfaces, acceptance criteria) "
    "reflecting the whole conversation so far. Reply with JSON only."
)
_CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"role": {"type": "string"}, "text": {"type": "string"}},
                "required": ["role", "text"],
                "additionalProperties": False,
            },
        },
        "spec_markdown": {"type": "string"},
    },
    "required": ["messages", "spec_markdown"],
    "additionalProperties": False,
}


def _chat_proxy(backend: str) -> Any:
    """A ModelProviderProxy for the spec chat — mock returns canned/empty, the real
    backends actually converse. Uses the observation path (complete), no signer."""
    from foundry.proxy.cli_transports import build_backend
    from foundry.proxy.model_provider_proxy import ModelProviderProxy
    from foundry.proxy.test_doubles import MockTransport

    if backend == "mock":
        return ModelProviderProxy(MockTransport(default_text="{}"))
    return build_backend(backend)  # type: ignore[arg-type]


def _ideate_turn(backend: str, history: list[dict[str, str]]) -> dict[str, Any]:
    """One round-table turn over the chosen backend; returns {messages, spec_markdown}."""
    proxy = _chat_proxy(backend)
    messages = [
        {
            "role": "user" if h["role"] == "user" else "assistant",
            "content": h["text"] if h["role"] == "user" else f"[{h['role']}] {h['text']}",
        }
        for h in history
    ]
    result = proxy.complete(
        "ideation_chat", "T1", _LEADS_SOP, messages, purpose="ideation", schema=_CHAT_SCHEMA
    )
    return result.parsed or {}


@dataclass
class Project:
    id: str
    request: str
    mode: str
    supervised: bool
    floor: int
    backend: str = "mock"
    packs: list[str] = field(default_factory=list)
    spec: str | None = None  # literal spec text OR a dir of uploaded docs (run(spec=))
    max_slots: int = 1  # IC-pool parallelism (Wave C; >1 engages the budget tree, Wave D)
    cost_cap_microusd: int | None = None  # global spend envelope (env-only; Wave D)
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    decisions: queue.Queue[str] = field(default_factory=queue.Queue)
    seen_uris: set[str] = field(default_factory=set)
    status: str = "running"
    passed: bool = False  # final verdict (for elevator/reload rehydration)
    awaiting: bool = False  # at a checkpoint right now (badge survives reload)
    journal: list[dict[str, Any]] = field(default_factory=list)  # latest agent journal ([11])
    gates: dict[str, Any] = field(default_factory=dict)  # name -> {status,attested,advisory} (survives reload)
    out_base: Path | None = None  # this floor's output dir (code/ + docs/)


_PROJECTS: dict[str, Project] = {}

# Keyword -> area map for the "suggest N" hint (one IC per area the work touches).
# Advisory only: the REAL area split is derived during the build (Wave A).
_AREA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "frontend": ("ui", "page", "frontend", "react", "css", "button", "screen",
                 "dashboard", "web", "form", "style", "client"),
    "backend": ("api", "service", "server", "endpoint", "backend", "route",
                "handler", "queue", "worker", "webhook", "microservice"),
    "data": ("db", "database", "data", "sql", "schema", "store", "analytics",
             "ml", "model", "pipeline", "etl", "report", "warehouse"),
    "infra": ("deploy", "docker", "k8s", "kubernetes", "infra", "terraform",
              "cloud", "scale", "ops", "ci/cd", "observability"),
    "security": ("security", "encrypt", "compliance", "pii", "regulated", "audit",
                 "secret", "gdpr", "hipaa", "auth", "oauth"),
}
_PACK_AREA: dict[str, str] = {"frontend": "frontend", "regulated": "security", "ml_data": "data"}


def _suggest_slots(request: str, packs: list[Any]) -> tuple[int, list[str]]:
    """N = the distinct CANONICAL_AREAS the request + packs likely touch (>=1).
    'backend' is the baseline area for any service build."""
    text = request.lower()
    hit = {"backend"}
    for area, kws in _AREA_KEYWORDS.items():
        if any(k in text for k in kws):
            hit.add(area)
    for pk in packs:
        pack_area = _PACK_AREA.get(str(pk).strip().lower())
        if pack_area:
            hit.add(pack_area)
    ordered = [a for a in CANONICAL_AREAS if a in hit]
    return max(1, min(len(ordered), len(CANONICAL_AREAS))), ordered
_LOCK = threading.Lock()
_FLOOR = [0]


def _emit(p: Project, **event: Any) -> None:
    # Capture gate verdicts as they stream so /api/projects can rehydrate the HUD
    # pills after a reload (a finished build keeps showing its signed gate row).
    if event.get("type") == "gate":
        name = str(event.get("name", ""))
        if name:
            p.gates[name] = {"status": event.get("status"),
                             "attested": event.get("attested", True),
                             "advisory": event.get("advisory", False)}
    p.events.put(event)


def _emit_paced(p: Project, events: list[dict[str, Any]]) -> None:
    for ev in events:
        _emit(p, **ev)
        time.sleep(_STAGE_PAUSE if ev.get("type") == "stage" else _PACE)


def _run_factory(p: Project) -> None:
    """Drive a REAL build; stream its telemetry as paced game events."""
    # Imported here so the server starts fast and a missing dep never blocks boot.
    from foundry.cli import run

    _emit(p, type="project", id=p.id, request=p.request, mode=p.mode, floor=p.floor,
          backend=p.backend, slots=p.max_slots)
    _emit(p, type="narrate", text=f"📋 New project on Floor {p.floor}: {p.request!r}")
    _emit(p, type="narrate", text=f"🏭 Mode: {p.mode}. The crew reports for duty…")
    time.sleep(_PACE)

    def responder(payload: dict[str, Any]) -> str:
        # The REAL stage payload -> paced game events, then the CTO's call.
        _emit_paced(p, events_for_payload(payload, p.seen_uris))
        # Stash the agents' journal so a lead can be "interviewed" ([11], advisory).
        jrnl = payload.get("agent_journal")
        if isinstance(jrnl, list):
            p.journal = jrnl
        boundary = str(payload.get("boundary", "stage"))
        if p.supervised:
            p.awaiting = True
            _emit(p, type="await_decision", boundary=boundary, journal=p.journal)
            try:
                decision = p.decisions.get(timeout=600)
            except queue.Empty:
                decision = "approve"
        else:
            time.sleep(_AUTO_APPROVE_DELAY)
            decision = "approve"
        p.awaiting = False
        _emit(p, type="resolved", boundary=boundary, decision=decision)
        verb = "approved" if decision == "approve" else "REJECTED"
        _emit(p, type="narrate", text=f"🧑‍💼 CTO {verb} the {boundary} checkpoint.")
        return decision

    code_dir = (p.out_base / "code") if p.out_base else None
    docs_dir = (p.out_base / "docs") if p.out_base else None
    try:
        result = run(
            p.request,
            mode=p.mode,
            backend=p.backend,            # real backend per project (not always mock)
            packs=p.packs or None,        # optional domain packs (regulated/ml_data/frontend)
            spec=p.spec,                  # pasted spec text OR uploaded docs dir (additive)
            max_slots=p.max_slots,        # IC-pool parallelism (Wave C) + budget tree (Wave D)
            cost_cap_microusd=p.cost_cap_microusd,
            thread_id=p.id,               # UNIQUE per project — no cross-floor collision
            checkpoints=True,
            checkpoint_responder=responder,
            output_dir=str(code_dir) if code_dir else None,
            docs_dir=str(docs_dir) if docs_dir else None,
        )
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
    # Surface the written output (code + docs) for in-page browsing / download.
    n_files = sum(1 for f in p.out_base.rglob("*") if f.is_file()) if p.out_base else 0
    if n_files:
        _emit(p, type="files", count=n_files)
        _emit(p, type="narrate", text=f"📂 {n_files} file(s) written — open OUTPUT to browse.")
    p.passed = bool(result.passed)
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
        if path == "/api/config":
            self._config()
            return
        if path == "/api/projects":
            self._list_projects()
            return
        if path.startswith("/api/files/"):
            self._list_files(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/file/"):
            self._serve_output_file(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/zip/"):
            self._zip_output(path.rsplit("/", 1)[-1])
            return
        if not path.startswith("/api"):
            self._serve_static(path)  # e.g. /favicon.ico
            return
        self._json(404, {"error": "not found"})

    def _config(self) -> None:
        """UI config: the default backend (FOUNDRY_MODEL_BACKEND, else mock) so the
        modal preselects it — a deployment sets the env to default to claude-cli."""
        from foundry.cli import BACKENDS

        default = os.environ.get("FOUNDRY_MODEL_BACKEND", "mock")
        if default not in BACKENDS:
            default = "mock"
        try:
            default_slots = max(1, int(os.environ.get("FOUNDRY_MAX_SLOTS", "1") or "1"))
        except ValueError:
            default_slots = 1
        self._json(200, {
            "default_backend": default, "backends": list(BACKENDS),
            "default_max_slots": default_slots,
        })

    # -- output browsing (the build's written code + docs) ----------------------
    def _out_base(self, pid: str) -> Path | None:
        p = _PROJECTS.get(pid)
        if p is None or p.out_base is None or not p.out_base.is_dir():
            return None
        return p.out_base

    def _list_files(self, pid: str) -> None:
        base = self._out_base(pid)
        if base is None:
            self._json(404, {"error": "no output yet"})
            return
        files = [
            {"path": str(f.relative_to(base)), "size": f.stat().st_size}
            for f in sorted(base.rglob("*")) if f.is_file()
        ]
        self._json(200, {"files": files})

    def _resolve_within(self, base: Path, rel: str) -> Path | None:
        """Resolve ``rel`` under ``base``, fail-closed on traversal/symlink escape."""
        target = (base / rel).resolve()
        root = str(base.resolve())
        if target == base.resolve() or not str(target).startswith(root + os.sep):
            return None
        return target

    def _serve_output_file(self, pid: str) -> None:
        from urllib.parse import parse_qs, urlparse

        base = self._out_base(pid)
        if base is None:
            self._json(404, {"error": "no output"})
            return
        rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
        target = self._resolve_within(base, rel)
        if target is None or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        data = target.read_bytes()[:_MAX_VIEW_BYTES]  # capped for the in-page viewer
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _zip_output(self, pid: str) -> None:
        import io
        import zipfile

        base = self._out_base(pid)
        if base is None:
            self._json(404, {"error": "no output"})
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(base))
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{base.name}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/projects":
            self._new_project()
        elif path == "/api/ideate":
            self._ideate()
        elif path.startswith("/api/decision/"):
            self._decision(path.rsplit("/", 1)[-1])
        elif path == "/api/suggest-slots":
            self._suggest_slots()
        elif path.startswith("/api/interview/"):
            self._interview(path.rsplit("/", 1)[-1])
        else:
            self._json(404, {"error": "not found"})

    def _list_projects(self) -> None:
        """Every floor's live state — the browser rehydrates the elevator on reload
        (and reconnects each running floor's stream) so a refresh loses nothing."""
        with _LOCK:
            ordered = sorted(_PROJECTS.values(), key=lambda p: p.floor)
        rows = [
            {"id": p.id, "floor": p.floor, "request": p.request, "mode": p.mode,
             "backend": p.backend, "supervised": p.supervised, "slots": p.max_slots,
             "status": p.status, "passed": p.passed, "awaiting": p.awaiting,
             "gates": p.gates}
            for p in ordered
        ]
        self._json(200, {"projects": rows})

    def _suggest_slots(self) -> None:
        """Suggest an IC-pool size N from the request + packs — count the distinct
        CANONICAL_AREAS the work is likely to touch (one IC per area). Advisory: the
        real area split is derived during the build (Wave A); this just seeds the UI."""
        body = self._read_json()
        request = str(body.get("request", ""))
        packs = body.get("packs") or []
        n, areas = _suggest_slots(request, packs if isinstance(packs, list) else [])
        self._json(200, {"slots": n, "areas": areas})

    def _interview(self, pid: str) -> None:
        """Ask a lead a question given its journal ([11]) — advisory, writes nothing.
        Best-effort: a backend/proxy error degrades to a short string, never 500s."""
        p = _PROJECTS.get(pid)
        if p is None:
            self._json(404, {"error": "unknown project"})
            return
        body = self._read_json()
        role = str(body.get("role", "")).strip() or "agent"
        question = str(body.get("question", "")).strip() or "How is it going?"
        if p.backend == "mock":
            # The mock transport can't deliberate; answer from the real journal we have.
            mine = [e for e in p.journal if str(e.get("role", "")) == role] or p.journal
            bits = "; ".join(
                str(e.get("summary", "")) for e in mine[:3] if str(e.get("summary", ""))
            )
            concerns = [c for e in mine for c in (e.get("concerns") or [])]
            note = f" Concerns: {concerns[0]}." if concerns else ""
            self._json(200, {"role": role, "answer": (
                f"(mock — from {role}'s journal) {bits or 'Working as planned.'}{note} "
                "Pick a real backend to actually interview the leads.")})
            return
        try:
            from foundry.agents.interview import interview
            from foundry.cli import _build_proxy
            from foundry.schemas.journal import JournalEntry
            from foundry.state.seq import Seq

            entries = []
            for i, e in enumerate(p.journal):
                conf = e.get("confidence")
                node_id = str(e.get("node_id", "")) or f"agent-{i}"
                entries.append(JournalEntry(
                    node_id=node_id,
                    role=str(e.get("role", role)),
                    summary=str(e.get("summary", "")),
                    assumptions=tuple(str(a) for a in (e.get("assumptions") or [])),
                    concerns=tuple(str(c) for c in (e.get("concerns") or [])),
                    confidence=float(conf) if isinstance(conf, (int, float)) else None,
                    # interview() never reads seq; mint a valid synthetic one.
                    seq=Seq(run_id=p.id, emission_epoch=0, superstep_index=0,
                            node_id=node_id, local=i),
                ))
            answer = interview(
                _build_proxy(p.backend), role=role, question=question, entries=entries,
            )
        except Exception as exc:  # advisory — never let an interview wedge the UI
            answer = f"(interview unavailable: {type(exc).__name__})"
        self._json(200, {"role": role, "answer": answer})

    def _ideate(self) -> None:
        from foundry.cli import BACKENDS

        body = self._read_json()
        session = str(body.get("session", "")).strip() or uuid.uuid4().hex[:12]
        message = str(body.get("message", "")).strip()
        backend = str(body.get("backend", "mock"))
        if backend not in BACKENDS:
            backend = "mock"
        chat = _CHATS.setdefault(session, {"history": [], "spec": ""})
        if message:
            chat["history"].append({"role": "user", "text": message})
        try:
            reply = _ideate_turn(backend, chat["history"])
        except Exception as exc:  # advisory aid — never 500 the chat
            self._json(200, {"session": session, "messages":
                             [{"role": "system", "text": f"(ideation error: {exc})"}],
                             "spec": chat["spec"]})
            return
        msgs = [
            {"role": str(m.get("role", "lead")), "text": str(m.get("text", ""))}
            for m in (reply.get("messages") or []) if str(m.get("text", "")).strip()
        ]
        if not msgs:
            msgs = [{"role": "system", "text":
                     "(no reply — on the mock backend the leads can't really converse; "
                     "pick a real backend for an interactive spec session.)"}]
        spec = str(reply.get("spec_markdown") or chat["spec"])
        for m in msgs:
            chat["history"].append(m)
        chat["spec"] = spec
        chat["history"] = chat["history"][-40:]  # bound the session
        self._json(200, {"session": session, "messages": msgs, "spec": spec})

    def _new_project(self) -> None:
        from foundry.cli import BACKENDS  # validate against the real backend set

        body = self._read_json()
        request = str(body.get("request", "")).strip() or "build a tiny URL-shortener service"
        mode = str(body.get("mode", "assistant"))
        if mode not in _MODES:
            mode = "assistant"
        backend = str(body.get("backend", "mock"))
        if backend not in BACKENDS:
            backend = "mock"
        raw_packs = body.get("packs", [])
        if isinstance(raw_packs, str):
            raw_packs = raw_packs.replace(",", " ").split()
        packs = [str(p).strip() for p in raw_packs if str(p).strip()]
        supervised = bool(body.get("supervised", True))
        spec_text = str(body.get("spec_text", "")).strip()
        spec_zip_b64 = body.get("spec_zip_b64")
        try:
            default_slots = int(os.environ.get("FOUNDRY_MAX_SLOTS", "1") or "1")
        except ValueError:
            default_slots = 1
        try:
            max_slots = int(body.get("max_slots", default_slots))
        except (TypeError, ValueError):
            max_slots = default_slots
        max_slots = max(1, max_slots)
        cost_env = os.environ.get("FOUNDRY_COST_CAP_MICROUSD", "").strip()
        cost_cap_microusd = int(cost_env) if cost_env.isdigit() else None
        with _LOCK:
            _FLOOR[0] += 1
            floor = _FLOOR[0]
        pid = uuid.uuid4().hex[:12]
        out_base = _OUTPUT_ROOT / f"floor-{floor}"
        # Spec input: pasted text (literal), else an uploaded docs-zip (a dir),
        # else None (build from the request alone). A bad zip degrades to None.
        spec: str | None = spec_text or None
        if spec is None and isinstance(spec_zip_b64, str) and spec_zip_b64.strip():
            try:
                spec_dir = out_base / "spec-in"
                if _safe_unzip_spec(spec_zip_b64, spec_dir) > 0:
                    spec = str(spec_dir)
            except Exception:
                spec = None
        p = Project(
            id=pid, request=request, mode=mode, supervised=supervised, floor=floor,
            backend=backend, packs=packs, spec=spec, out_base=out_base,
            max_slots=max_slots, cost_cap_microusd=cost_cap_microusd,
        )
        with _LOCK:
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


def _lan_ip() -> str | None:
    """Best-effort LAN IP of this host (for the 0.0.0.0 banner). None if unknown."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # no packets sent — just selects the route's iface
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"\n  🏭  Foundry Workshop running →  http://{shown}:{port}\n")
    if host == "0.0.0.0":  # opt-in LAN exposure (e.g. a Pi) — warned below
        lan = _lan_ip()
        if lan:
            print(f"      On your LAN (phone/tablet):  http://{lan}:{port}")
        print("      ⚠ bound to ALL interfaces — anyone on this network can reach it"
              " (and start/steer builds). Use only on a trusted LAN.\n")
    print("      (Ctrl-C to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye 👋\n")
        srv.shutdown()


if __name__ == "__main__":
    # Host/port from env so a deployment (e.g. a Raspberry Pi on your LAN) can bind
    # 0.0.0.0 via WORKSHOP_HOST. Default stays localhost — exposure is opt-in.
    serve(
        host=os.environ.get("WORKSHOP_HOST", "127.0.0.1"),
        port=int(os.environ.get("WORKSHOP_PORT", "8765")),
    )
