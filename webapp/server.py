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

import contextlib
import hmac
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from foundry.schemas.architecture import CANONICAL_AREAS
from webapp.events import events_for_payload, events_for_result, live_event_for_node
from webapp.repo import Repo, RepoUnavailable, read_repo
from webapp.store import BuildStore


def _load_env_file() -> bool:
    """Load `KEY=VALUE` lines from a local `.env` into the environment so the server
    picks up your config on its own — no wrapper script needed. Returns True iff a
    `.env` was found. Values may use `$HOME` / `~`.

    Precedence: a var ALREADY set in the real process environment wins (an explicit
    `export` / shell override is never clobbered). WITHIN the file, the LAST line for
    a key wins — so `echo "KEY=value" >> .env` reliably overrides an earlier line
    (e.g. a preset's placeholder)."""
    root = Path(__file__).resolve().parent.parent
    preexisting = set(os.environ)            # real-env vars win over the file
    for cand in (Path.cwd() / ".env", root / ".env"):
        if not cand.is_file():
            continue
        for raw in cand.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = os.path.expandvars(os.path.expanduser(val.strip().strip('"').strip("'")))
            if key and key not in preexisting:   # last file line for a key wins
                os.environ[key] = val
        return True
    return False


_ENV_FILE_FOUND = _load_env_file()   # before the config constants below read os.environ

_STATIC = Path(__file__).parent / "static"
_PACE = 0.5          # seconds between paced game events
_STAGE_PAUSE = 0.9   # extra beat when a new stage opens
_AUTO_APPROVE_DELAY = 1.4  # auto-run: dwell on each checkpoint before approving

_MODES = ("assistant", "gated", "autonomous")


def _int_env(name: str, default: int) -> int:
    """A small non-negative int from the environment (blank/garbage -> default)."""
    try:
        return max(0, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# Cap how many builds RUN at once. Each build is a real, resource-heavy run (model
# calls + local gate subprocesses); on a small host (a Pi) two heavy builds at once
# can exhaust RAM. Extra builds QUEUE — their worker thread blocks on this semaphore
# and starts as a slot frees. Tune per host: a Pi wants 1, an M-series Mac 3-4.
# WORKSHOP_MAX_CONCURRENT_BUILDS=0 disables the cap (unbounded, the prior behaviour).
_MAX_CONCURRENT_BUILDS = _int_env("WORKSHOP_MAX_CONCURRENT_BUILDS", 2)
_BUILD_GATE: threading.BoundedSemaphore | None = (
    threading.BoundedSemaphore(_MAX_CONCURRENT_BUILDS)
    if _MAX_CONCURRENT_BUILDS > 0 else None
)

# A server running for days accumulates one in-memory event log per build (every
# build ever, in _PROJECTS). The full history is ALSO durable in SQLite, so once a
# build has been FINISHED for a grace period its in-memory log is trimmed to a tail;
# a (rare) late viewer of a finished build replays the full story from the store.
_LOG_TAIL = _int_env("WORKSHOP_LOG_TAIL", 64)       # events kept in memory after trim
_LOG_GRACE_S = _int_env("WORKSHOP_LOG_GRACE_S", 90)  # wait after a build ends before trimming
_LOG_SWEEP_S = _int_env("WORKSHOP_LOG_SWEEP_S", 120)  # how often the sweeper runs (0 = off)

# Where each floor's build writes its output tree + doc bundle (gitignored).
# Override with WORKSHOP_OUTPUT_DIR (e.g. a mounted volume on a Pi).
_OUTPUT_ROOT = Path(os.environ.get("WORKSHOP_OUTPUT_DIR", ".run/web")).resolve()

# Durable factory state is ALWAYS ON for the Workshop (unlike the one-shot CLI,
# whose in-memory default is right for a hermetic smoke run). The Workshop is a
# long-lived, multi-device, restart-prone SERVER driving human-in-the-loop builds:
# making the checkpointer + trust ledgers + CAS + WORM audit file-backed lets a
# build SURVIVE a restart (and is the precondition for resuming one). Default lives
# beside the other persisted state (catalog/output); override or relocate with
# WORKSHOP_STATE_DIR / FOUNDRY_STATE_DIR.
_STATE_DIR = (
    os.environ.get("WORKSHOP_STATE_DIR")
    or os.environ.get("FOUNDRY_STATE_DIR")
    or str(_OUTPUT_ROOT / "state")
)

# Where the code lives: the durable CAS (content-addressed store) under _STATE_DIR
# is the SINGLE SOURCE OF TRUTH — a finished build's tree is materialized back out
# of it on demand (see webapp/repo.py), no output directory required. Writing the
# tree to disk during a build is now an OPT-IN export (WORKSHOP_WRITE_DISK=1), off
# by default: the Repo/OUTPUT browser reads straight from the database, and an
# explicit "Download .zip" exports on demand.
_WRITE_DISK = (os.environ.get("WORKSHOP_WRITE_DISK", "").strip().lower()
               in ("1", "true", "yes", "on"))

# State-changing routes (start a build → real model spend, drive a checkpoint) are
# guarded. A shared token (FOUNDRY_WORKSHOP_TOKEN) gates them when the server is
# exposed beyond localhost; an Origin/Host same-origin check blocks a malicious web
# page from POSTing to a localhost workshop (CSRF) even with no token set.
_AUTH_TOKEN = os.environ.get("FOUNDRY_WORKSHOP_TOKEN") or None


def _authorize_mutation(headers: Any, token: str | None) -> str | None:
    """Return a denial reason for a state-changing request, or None if allowed.

    1. CSRF: if an ``Origin`` header is present it MUST match the request ``Host``
       (a cross-site browser POST carries the attacker's Origin → rejected). A
       request with no Origin (curl, same-origin fetch) passes this check.
    2. Token: if a shared token is configured, a matching ``X-Workshop-Token``
       header (constant-time compared) is required. Unset ⇒ no token needed
       (localhost-only default)."""
    origin = headers.get("Origin")
    if origin:
        from urllib.parse import urlparse

        if urlparse(origin).netloc != (headers.get("Host") or ""):
            return "cross-origin request rejected (CSRF guard)"
    if token:
        provided = headers.get("X-Workshop-Token") or ""
        if not hmac.compare_digest(provided, token):
            return "missing or invalid workshop token"
    return None
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
    adapter: str = "stub"  # coding substrate (see foundry.cli.ADAPTERS)
    packs: list[str] = field(default_factory=list)
    spec: str | None = None  # literal spec text OR a dir of uploaded docs (run(spec=))
    max_slots: int = 4  # IC-pool CEILING (= cli.AUTO_SLOTS_CAP); the system sizes
    # actual parallelism to the lead's decomposition (min(tasks, this)).
    cost_cap_microusd: int | None = None  # global spend envelope (Wave D)
    max_fix_attempts: int = 5  # auto-fix retry budget per failing gate (run() default)
    release_seq: int = 1  # 1,2,3… within the floor; each iterate adds a release
    parent_id: str | None = None  # lineage: the release this one iterates on
    base_dir: str | None = None  # prior code seeded as the base tree (iterate)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # The retained, append-only event log + a Condition every viewer waits on.
    # A single shared queue would let two streams STEAL each other's events; a log
    # lets many devices each tail it at their own cursor, and a late/reconnecting
    # device replays from the start (or from its Last-Event-ID). `cond` guards
    # `log`, `ended`, and the checkpoint-decision fields below.
    log: list[dict[str, Any]] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)
    ended: bool = False
    ended_at: float | None = None    # monotonic time the build finished (for log sweeping)
    log_trimmed: bool = False        # in-memory log trimmed to a tail; full history in the store
    # Checkpoint decisions: first decision for a checkpoint wins (idempotent);
    # every device still watches the resolution play (it's just another log event).
    checkpoint_seq: int = 0          # monotonic id of the LATEST checkpoint reached
    pending_checkpoint: int | None = None  # the checkpoint awaiting a decision, if any
    decision_for: dict[int, str] = field(default_factory=dict)  # checkpoint id -> decision
    seen_uris: set[str] = field(default_factory=set)
    status: str = "running"
    passed: bool = False  # final verdict (for elevator/reload rehydration)
    awaiting: bool = False  # at a checkpoint right now (badge survives reload)
    journal: list[dict[str, Any]] = field(default_factory=list)  # latest agent journal ([11])
    gates: dict[str, Any] = field(default_factory=dict)  # name -> {status,attested,advisory}
    out_base: Path | None = None  # this floor's output dir (code/ + docs/)
    platform_opts: dict[str, Any] = field(default_factory=dict)  # platform build knobs (advanced)


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

# --- durable build catalog --------------------------------------------------- #
# Floors run in-memory (_PROJECTS), but the per-floor OUTPUT persists to disk
# (_OUTPUT_ROOT/floor-N) and each build's METADATA + repo reference is catalogued
# in a SQLite DB (webapp/store.py). So a server restart rehydrates the elevator /
# OUTPUT / HUD gate pills for finished floors, AND a new floor can be started FROM
# an existing finished build (its repo pulled from the catalog by out_base).
# A build in flight is an in-memory thread — it can't resume; on reload it's
# marked finished so the floor and its output stay browsable.
_STORE = BuildStore(_OUTPUT_ROOT / "foundry.db")
_PERSIST_ON = {"project", "gate", "files", "halt", "release", "done",
               "await_decision", "resolved"}


def _build_record(p: "Project") -> dict[str, Any]:
    """The catalog (DB) shape of a build."""
    return {
        "id": p.id, "floor": p.floor, "release_seq": p.release_seq,
        "parent_id": p.parent_id, "request": p.request, "mode": p.mode,
        "backend": p.backend, "adapter": p.adapter,
        "supervised": p.supervised, "slots": p.max_slots,
        "max_fix_attempts": p.max_fix_attempts, "status": p.status,
        "passed": p.passed, "gates": p.gates,
        "out_base": str(p.out_base) if p.out_base else None,
        "created_at": p.created_at,
    }


def _persist(p: "Project | None" = None) -> None:
    """Upsert build(s) into the catalog on a state change (best-effort, never raises)."""
    try:
        if p is not None:
            _STORE.upsert(_build_record(p))
            return
        with _LOCK:
            projs = list(_PROJECTS.values())
        for q in projs:
            _STORE.upsert(_build_record(q))
    except Exception:
        pass


def _load_persisted() -> None:
    """Rehydrate floors from the catalog on startup (best-effort).

    The persisted event log is loaded back into each project, so a device that
    connects after a restart replays the build's WHOLE story (not just a final
    snapshot). A floor that was mid-build at save time can't resume its computation
    (its thread is gone — that needs the durable checkpointer, FOUNDRY_STATE_DIR),
    so it's closed out honestly: an `interrupted` marker is appended and the stream
    ends once the replay catches up."""
    try:
        rows = _STORE.latest_per_floor()   # one row per project (its newest release)
    except Exception:
        return
    with _LOCK:
        for row in rows:
            try:
                ob = row.get("out_base")
                p = Project(
                    id=str(row["id"]), request=str(row.get("request", "")),
                    mode=str(row.get("mode", "assistant")),
                    supervised=bool(row.get("supervised", True)),
                    floor=int(row.get("floor", 0)),
                    release_seq=int(row.get("release_seq", 1) or 1),
                    backend=str(row.get("backend", "mock")),
                    adapter=str(row.get("adapter", "stub")),
                    max_slots=int(row.get("slots", 1) or 1),
                    max_fix_attempts=int(row.get("max_fix_attempts", 5) or 5),
                    parent_id=row.get("parent_id"),
                    out_base=Path(ob) if ob else None,
                    created_at=str(row.get("created_at") or ""),
                )
                try:
                    p.log = _STORE.events_of(p.id)   # the durable replay log
                except Exception:
                    p.log = []
                was_running = str(row.get("status", "")) == "running" and not any(
                    e.get("type") == "_end" for e in p.log
                )
                p.passed = bool(row.get("passed", False))
                p.awaiting = False
                p.gates = dict(row.get("gates") or {})
                _PROJECTS[p.id] = p
                if was_running and _has_durable_checkpoint(p.id):
                    # Durable state survived the restart → RESUME the build from its
                    # last checkpoint. Seed the dedup set so the resumed run doesn't
                    # re-emit the history already in the log.
                    try:
                        p.seen_uris = _STORE.seen_of(p.id)
                    except Exception:
                        p.seen_uris = set()
                    p.status = "running"
                    p.ended = False
                    threading.Thread(target=_gated_runner, args=(_run_factory, p),
                                     kwargs={"resume": True}, daemon=True).start()
                elif was_running:
                    # No recoverable checkpoint → close it out honestly (history kept,
                    # terminal markers appended on the same log so replays end cleanly).
                    p.status = "done"
                    _emit(p, type="narrate",
                          text="↻ Server restarted — this build was interrupted "
                               "(its history is preserved; it can't be resumed).")
                    _emit(p, type="halt", phase="interrupted",
                          reason="server restarted mid-build")
                    _emit(p, type="done", passed=False, phase="interrupted")
                    _close(p)
                else:
                    p.status = "done"
                    p.ended = True           # already terminal; a late connect ends at once
            except Exception:
                continue
        _FLOOR[0] = max(_FLOOR[0], _STORE.max_floor())


def _has_durable_checkpoint(thread_id: str) -> bool:
    """True iff a durable checkpoint exists for ``thread_id`` — i.e. the build can
    actually be RESUMED (vs. re-run from scratch). Probes the checkpoint DB read-only
    and fail-closed: any error ⇒ not resumable (we then close it out honestly)."""
    try:
        from foundry.durability.checkpointer import FoundryCheckpointer

        db = Path(_STATE_DIR) / "checkpoints.db"
        if not db.exists():
            return False
        ckpt = FoundryCheckpointer(str(db))
        try:
            tup = ckpt.saver.get_tuple({"configurable": {"thread_id": thread_id}})
            return tup is not None
        finally:
            ckpt.close()
    except Exception:
        return False


# --- CAS-backed repo reads (the code lives in the store, not a disk dir) ------ #
# A finished build's tree is read straight from the durable CAS + checkpoint (see
# webapp/repo.py). Reads are cached: a finished build's tree is immutable, so the
# materialization is done once per build id. A build still running has no terminal
# checkpoint yet — read_repo raises RepoUnavailable and we simply return None.
_REPO_CACHE: dict[str, Repo] = {}
_REPO_CACHE_LOCK = threading.Lock()


def _read_repo_cached(pid: str) -> Repo | None:
    """``read_repo(pid)`` from the durable store, cached. ``None`` when the build
    has no readable durable checkpoint (older build, or still in flight).

    A build STILL RUNNING is read fresh every time and never cached — its latest
    checkpoint is an intermediate superstep whose tree would be stale the moment the
    next node runs. Only a FINISHED build's tree is immutable, so only it is cached.
    """
    with _REPO_CACHE_LOCK:
        hit = _REPO_CACHE.get(pid)
    if hit is not None:
        return hit
    try:
        repo = read_repo(pid, _STATE_DIR)
    except RepoUnavailable:
        return None
    except Exception:
        return None
    proj = _PROJECTS.get(pid)
    finished = proj is None or proj.status == "done"
    if finished:
        with _REPO_CACHE_LOCK:
            _REPO_CACHE[pid] = repo
    return repo


def _invalidate_repo(pid: str) -> None:
    with _REPO_CACHE_LOCK:
        _REPO_CACHE.pop(pid, None)


def _sign_offs_payload(repo: "Repo | None") -> list[dict[str, Any]]:
    """The repo's recorded sign-offs as JSON rows (empty for an assistant build)."""
    return [
        {"point": s.point, "approver": s.approver, "role": s.role,
         "approved": s.approved, "provenance": s.provenance,
         "state_sha": s.state_sha, "evidence_hash": s.evidence_hash, "note": s.note}
        for s in (repo.sign_offs if repo else ())
    ]


def _evidence_files(pid: str) -> dict[str, bytes]:
    """The evidence sidecar for a build's export: gate verdicts + recorded
    sign-offs, as a machine-readable ``.foundry/evidence.json`` and a readable
    ``EVIDENCE.md``. Returns ``{}`` when there is nothing to attest yet.

    This is an EXPORT-time sidecar — built from the attested gate/sign-off records
    as-is and added ALONGSIDE the materialized tree, never mixed into it, so the
    bytes the gates ran against are unchanged."""
    repo = _read_repo_cached(pid)
    p = _PROJECTS.get(pid)
    checks = (
        [{"name": c.name, "status": c.status, "ok": c.ok,
          "advisory": c.advisory, "findings": list(c.findings)} for c in repo.checks]
        if repo else
        [{"name": n, "status": str(g.get("status", "")).split(".")[-1].lower(),
          "ok": str(g.get("status", "")).split(".")[-1].lower() == "pass",
          "advisory": bool(g.get("advisory")), "findings": []}
         for n, g in (p.gates.items() if p else [])]
    )
    sign_offs = _sign_offs_payload(repo)
    if not checks and not sign_offs:
        return {}
    doc = {
        "build_id": pid,
        "request": p.request if p else "",
        "mode": p.mode if p else "",
        "backend": p.backend if p else "",
        "passed": bool(p.passed) if p else None,
        "tree_sha": repo.tree_sha if repo else None,
        "gates": checks,
        "sign_offs": sign_offs,
    }
    md = _evidence_markdown(doc)
    return {
        ".foundry/evidence.json": (json.dumps(doc, indent=2) + "\n").encode("utf-8"),
        "EVIDENCE.md": md.encode("utf-8"),
    }


def _evidence_markdown(doc: dict[str, Any]) -> str:
    """A human-readable rendering of the evidence doc for ``EVIDENCE.md``."""
    lines = [
        "# Evidence pack",
        "",
        f"- **Build**: `{doc['build_id']}`",
        f"- **Request**: {doc.get('request') or '(none)'}",
        f"- **Mode**: {doc.get('mode') or '(unknown)'}",
        f"- **Backend**: {doc.get('backend') or '(unknown)'}",
    ]
    if doc.get("tree_sha"):
        lines.append(f"- **Tree sha**: `{doc['tree_sha']}`")
    if doc.get("passed") is not None:
        lines.append(f"- **Result**: {'PASSED' if doc['passed'] else 'not passed'}")
    lines += ["", "## Gate checks", ""]
    if doc["gates"]:
        for g in doc["gates"]:
            mark = "✓" if g["ok"] else "✗"
            adv = " _(advisory)_" if g.get("advisory") else ""
            lines.append(f"- {mark} **{g['name']}** — {g['status']}{adv}")
            for f in g.get("findings") or ():
                lines.append(f"    - {f}")
    else:
        lines.append("- _(no gate verdicts recorded)_")
    lines += ["", "## Sign-offs", ""]
    if doc["sign_offs"]:
        for s in doc["sign_offs"]:
            mark = "✓" if s["approved"] else "✗"
            who = " · ".join(x for x in (s.get("approver"), s.get("role")) if x) or "(unnamed)"
            lines.append(f"- {mark} **{s['point']}** — {who} _(provenance: {s.get('provenance') or 'n/a'})_")
            if s.get("evidence_hash"):
                lines.append(f"    - evidence hash: `{s['evidence_hash']}`")
            if s.get("note"):
                lines.append(f"    - note: {s['note']}")
    else:
        lines.append("- _(no human sign-offs — assistant mode, or none recorded)_")
    lines.append("")
    return "\n".join(lines)


def _lineage_ids(pid: str) -> list[str]:
    """The build's ancestry oldest→newest (root … pid) via ``parent_id`` in the
    catalog — so a release's full tree is the parent's tree + this release's diffs."""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = pid
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        try:
            rec = _STORE.get(cur)
        except Exception:
            rec = None
        cur = (rec or {}).get("parent_id")
    chain.reverse()
    return chain


def _overlay_tree(pid: str) -> dict[str, bytes] | None:
    """The FULL release tree for ``pid`` from the CAS: each ancestor's materialized
    files overlaid oldest→newest (this release's diffs win) — mirroring the on-disk
    'fill the unchanged files from the parent' merge. ``None`` if nothing readable."""
    blobs: dict[str, bytes] = {}
    found = False
    for anc in _lineage_ids(pid):
        repo = _read_repo_cached(anc)
        if repo is None:
            continue
        found = True
        for f in repo.files:
            b = repo.file_bytes(f.path)
            if b is not None:
                blobs[f.path] = b
    return blobs if found else None


def _materialize_base_dir(parent_id: str, dest: Path) -> bool:
    """Write the parent build's full CAS tree into ``dest`` (the base a release
    iterates on). Returns True if any file was written. Used in place of an on-disk
    parent ``code`` dir so iterate works with no output directory."""
    tree = _overlay_tree(parent_id)
    if not tree:
        return False
    for rel, data in tree.items():
        target = (dest / rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return True


def _emit(p: Project, **event: Any) -> None:
    # Capture gate verdicts as they stream so /api/projects can rehydrate the HUD
    # pills after a reload (a finished build keeps showing its signed gate row).
    if event.get("type") == "gate":
        name = str(event.get("name", ""))
        if name:
            p.gates[name] = {"status": event.get("status"),
                             "attested": event.get("attested", True),
                             "advisory": event.get("advisory", False)}
    with p.cond:
        seq = len(p.log)
        p.log.append(event)
        if event.get("type") == "_end":
            p.ended = True
            p.ended_at = time.monotonic()   # start the grace clock for log trimming
        p.cond.notify_all()       # wake EVERY connected stream (not just one)
    # Durably back the log so a reconnect/restart can replay the whole story.
    try:
        _STORE.append_event(p.id, seq, event)
    except Exception:
        pass
    if event.get("type") in _PERSIST_ON:
        _persist(p)   # catalog the build's metadata on each state change


def _close(p: Project) -> None:
    """Terminate the stream for every viewer (the build is over)."""
    _emit(p, type="_end")


def _trim_finished_logs() -> None:
    """Trim the in-memory event log of builds that finished a while ago.

    The full history stays durable in SQLite, so holding every finished build's
    whole log in RAM is pure waste on a long-lived server. We keep a short tail
    (so the catalog/HUD code that peeks at recent events still works) and mark the
    build so a late viewer replays from the store instead of the trimmed tail."""
    now = time.monotonic()
    for p in list(_PROJECTS.values()):
        if (not p.ended or p.log_trimmed or p.ended_at is None
                or now - p.ended_at < _LOG_GRACE_S):
            continue
        with p.cond:
            if len(p.log) > _LOG_TAIL:
                p.log = p.log[-_LOG_TAIL:]
            p.log_trimmed = True   # stream now replays this build's history from the store


def _log_sweeper() -> None:
    while True:
        time.sleep(_LOG_SWEEP_S)
        try:
            _trim_finished_logs()
        except Exception:
            pass


def _record_decision(p: Project, decision: str, checkpoint: int | None) -> tuple[bool, bool]:
    """Record a checkpoint decision, FIRST-wins. Returns ``(applied, stale)``.

    ``stale`` is True when the decision names no current pause (already resolved,
    or a different checkpoint than the one awaiting) — a no-op. ``applied`` is True
    only for the FIRST decision on the awaiting checkpoint; later/duplicate clicks
    from any device return ``(False, False)`` and simply watch the resolution play."""
    with p.cond:
        cidx = p.pending_checkpoint
        if checkpoint is not None and checkpoint != cidx:
            return (False, True)          # a stale click for an already-passed pause
        if cidx is None:
            return (False, True)          # nothing is awaiting a decision
        if cidx in p.decision_for:
            return (False, False)         # someone already decided this one
        p.decision_for[cidx] = decision
        p.cond.notify_all()
        return (True, False)


def _resume_cursor(last_event_id: str | None) -> int:
    """The log index a stream should resume at given an SSE ``Last-Event-ID``.

    A fresh viewer (no id) starts at 0 and replays the whole log; a reconnecting
    EventSource replays its last id, so we resume at the NEXT event."""
    if last_event_id and last_event_id.isdigit():
        return int(last_event_id) + 1
    return 0


def _await_decision(p: Project, cidx: int, timeout: float = 600.0) -> str:
    """Block the build until SOME device decides checkpoint ``cidx`` (first wins),
    or until ``timeout`` elapses (then auto-approve). Many devices can post; only
    the first is recorded — the rest watch the `resolved` event play, live."""
    deadline = time.monotonic() + timeout
    with p.cond:
        while cidx not in p.decision_for:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "approve"
            p.cond.wait(remaining)
        return p.decision_for[cidx]


def _emit_paced(p: Project, events: list[dict[str, Any]]) -> None:
    for ev in events:
        _emit(p, **ev)
        time.sleep(_STAGE_PAUSE if ev.get("type") == "stage" else _PACE)


def _gated_runner(runner, p: Project, **kwargs: Any) -> None:
    """Run a build under the global concurrency gate.

    When all build slots are busy, the project is marked ``queued`` and this worker
    thread blocks until a slot frees — so a small host never runs more heavy builds
    at once than ``WORKSHOP_MAX_CONCURRENT_BUILDS``. The slot is always released, even
    if the build raises, so a crash can't leak a permanently-held slot."""
    if _BUILD_GATE is None:
        runner(p, **kwargs)
        return
    if not _BUILD_GATE.acquire(blocking=False):
        # Every slot busy → tell the user it's queued, then wait for one to free.
        p.status = "queued"
        _persist(p)
        _emit(p, type="narrate",
              text="⏳ All build slots are busy — queued. Starting as soon as one frees…")
        _BUILD_GATE.acquire()
    try:
        runner(p, **kwargs)
    finally:
        _BUILD_GATE.release()


def _run_factory(p: Project, resume: bool = False) -> None:
    """Drive a REAL build; stream its telemetry as paced game events.

    ``resume=True`` re-enters a build that was interrupted by a server restart: the
    durable checkpointer continues the graph from its last superstep, and the
    persisted ``seen_uris`` (seeded by the caller) keep the resumed run from
    re-emitting the history it already streamed."""
    # Imported here so the server starts fast and a missing dep never blocks boot.
    from foundry.cli import run

    if resume:
        _emit(p, type="narrate",
              text="↻ Resuming after a restart — picking up from the last checkpoint…")
    else:
        _emit(p, type="project", id=p.id, request=p.request, mode=p.mode, floor=p.floor,
              backend=p.backend, slots=p.max_slots)
        _emit(p, type="narrate", text=f"📋 New project on Floor {p.floor}: {p.request!r}")
        _emit(p, type="narrate", text=f"🏭 Mode: {p.mode}. The crew reports for duty…")
    time.sleep(_PACE)

    def responder(payload: dict[str, Any]) -> str:
        # The REAL stage payload -> paced game events, then the CTO's call.
        _emit_paced(p, events_for_payload(payload, p.seen_uris))
        try:                          # persist the dedup set so a resume won't replay
            _STORE.add_seen(p.id, p.seen_uris)
        except Exception:
            pass
        # Stash the agents' journal so a lead can be "interviewed" ([11], advisory).
        jrnl = payload.get("agent_journal")
        if isinstance(jrnl, list):
            p.journal = jrnl
        boundary = str(payload.get("boundary", "stage"))
        if p.supervised:
            with p.cond:
                p.checkpoint_seq += 1
                cidx = p.checkpoint_seq
                p.pending_checkpoint = cidx
            p.awaiting = True
            # `checkpoint` lets any device's decision name WHICH pause it answers,
            # so a stale/duplicate click can't leak into the next checkpoint.
            _emit(p, type="await_decision", boundary=boundary, checkpoint=cidx,
                  journal=p.journal)
            decision = _await_decision(p, cidx)
        else:
            time.sleep(_AUTO_APPROVE_DELAY)
            decision = "approve"
        p.awaiting = False
        with p.cond:
            p.pending_checkpoint = None
        _emit(p, type="resolved", boundary=boundary, decision=decision)
        verb = {"approve": "approved", "needs_changes": "requested changes on",
                "reject": "REJECTED"}.get(decision, "REJECTED")
        _emit(p, type="narrate", text=f"🧑‍💼 CTO {verb} the {boundary} checkpoint.")
        return decision

    # LIVE per-node telemetry: as each real graph node executes, seat its agent at
    # the desk in real time (not the retroactive burst at the checkpoint). Emitted
    # immediately (unpaced) so the floor reflects the build actually grinding.
    def progress(node: str, scope: str) -> None:
        ev = live_event_for_node(node, scope)
        if ev is not None:
            _emit(p, **ev)

    # The code lives in the durable CAS — the build needs NO output directory. We
    # only write a disk tree when WORKSHOP_WRITE_DISK opts in (an export cache);
    # otherwise the Repo/OUTPUT browser materializes the tree straight from the store.
    code_dir = (p.out_base / "code") if (p.out_base and _WRITE_DISK) else None
    # Iterate base: prefer an on-disk parent code dir (set when disk export is on);
    # else materialize the parent's full tree from the CAS into a scratch dir, so a
    # release still builds ON TOP of the prior code with no output directory.
    base_stack = contextlib.ExitStack()
    base_dir = p.base_dir
    if not base_dir and p.parent_id:
        scratch = Path(base_stack.enter_context(
            tempfile.TemporaryDirectory(prefix="foundry-base-")))
        if _materialize_base_dir(p.parent_id, scratch):
            base_dir = str(scratch)
    try:
        try:
            result = run(
                p.request,
                progress_cb=progress,
                mode=p.mode,
                backend=p.backend,            # real backend per project (not always mock)
                adapter=p.adapter,            # coding substrate (stub/raw-react)
                packs=p.packs or None,        # optional domain packs (regulated/ml_data/frontend)
                spec=p.spec,                  # pasted spec text OR uploaded docs dir (additive)
                max_slots=p.max_slots,        # IC-pool parallelism (Wave C) + budget tree (Wave D)
                cost_cap_microusd=p.cost_cap_microusd,
                max_fix_attempts=p.max_fix_attempts,
                base_dir=base_dir,            # iterate: prior code seeded as the base tree
                thread_id=p.id,               # UNIQUE per project — no cross-floor collision
                state_dir=_STATE_DIR,         # durable: build survives + resumes past a restart
                checkpoints=True,
                checkpoint_responder=responder,
                output_dir=str(code_dir) if code_dir else None,
            )
        finally:
            base_stack.close()  # run() copies base_dir into its sandbox synchronously
    except Exception as exc:
        _emit(p, type="narrate", text=f"💥 Build error: {exc}")
        _emit(p, type="halt", phase="error", reason=str(exc))
        _emit(p, type="done", passed=False, phase="error")
        p.status = "done"
        _persist(p)
        _close(p)
        return

    # Disk-export mode only: the build wrote ONLY its changed/added files to
    # code_dir; fill in every unchanged file from the parent so the on-disk tree is
    # the prior code + the change. (The CAS Repo view does this overlay at read time.)
    if base_dir and code_dir:
        base = Path(base_dir)
        if base.is_dir():
            code_dir.mkdir(parents=True, exist_ok=True)
            for src in base.rglob("*"):
                if src.is_file():
                    dst = code_dir / src.relative_to(base)
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

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
    # Surface the repo (read from the store) for in-page browsing / export. The tree
    # is materialized from the CAS — no output directory required.
    _invalidate_repo(p.id)
    repo = _read_repo_cached(p.id)
    n_files = len(repo.files) if repo is not None else (
        sum(1 for f in p.out_base.rglob("*") if f.is_file()) if p.out_base else 0
    )
    if n_files:
        _emit(p, type="files", count=n_files)
        _emit(p, type="narrate",
              text=f"📂 {n_files} file(s) in the repo — open REPO to browse the store.")
    p.passed = bool(result.passed)
    p.status = "done"
    _persist(p)              # catalog the FINAL status/passed (after they're set)
    _close(p)


_STAGE_ORDER = ["defining requirements", "designing the system", "implementing",
                "testing & review", "security review", "verifying at the gate"]


def _node_stage(node: str) -> str | None:
    """Map a live graph node id to a readable pipeline STAGE for the platform log
    (None for infra/non-stage nodes we don't surface)."""
    n = (node or "").lower()
    if any(k in n for k in ("stakeholder", "product", "criteria", "program")):
        return "defining requirements"
    if "architect" in n:
        return "designing the system"
    if "engineer" in n and ("appsec" not in n):
        return "implementing"
    if any(k in n for k in ("qa", "sdet", "reviewer", "writer", "tester")):
        return "testing & review"
    if any(k in n for k in ("appsec", "audit", "supply", "security")):
        return "security review"
    if "gate" in n:
        return "verifying at the gate"
    return None


def _platform_opts(body: dict[str, Any]) -> dict[str, Any]:
    """Advanced platform knobs from the request body (all optional; sane defaults)."""
    def _int(key: str, default: int) -> int:
        try:
            return max(1, int(body.get(key, default)))
        except (TypeError, ValueError):
            return default
    return {
        "max_passes": _int("max_passes", 6),
        "critic": not bool(body.get("no_critic")),
        "max_parallel": _int("max_parallel", 4),
        "max_slots_per_service": _int("max_slots_per_service", 6),
    }


def _infers_platform(request: str) -> bool:
    """True when the request decomposes into MORE THAN ONE service — i.e. it reads as
    a multi-service app/platform (web + backend + data …) rather than a single
    component (a game, a tool, one service). Drives the auto platform/single choice."""
    try:
        from foundry.platform import plan_platform

        return len(plan_platform(request).services) >= 2
    except Exception:
        return False


def _run_platform(p: Project) -> None:
    """A PLATFORM build on one floor: compose many services into one monorepo via
    ``foundry.platform.build_platform`` and stream its progress to the quest log.
    The stitched monorepo is written to ``p.out_base`` so the repo browser / Floor-0
    hub serve it from disk (no CAS build of its own)."""
    from foundry.platform import build_platform
    from foundry.platform.planner import plan_platform

    # Map a service role to a discipline ROOM the floor can light up (the platform
    # floor has no engine stage stream of its own, so we drive it ourselves).
    role_room = {"web": "frontend", "backend": "backend",
                 "data": "data", "worker": "backend", "game": "frontend"}
    plan = plan_platform(p.request)
    # Synthetic org so the discipline rooms staff up (engineers per area) and the
    # static crew sits at their desks (idle => Zzz) — instead of waiting in the lounge.
    area_units: dict[str, list[str]] = {}
    order: list[str] = []
    for s in plan.services:
        a = role_room.get(s.role, "backend")
        if a not in area_units:
            area_units[a] = []
            order.append(a)
        area_units[a].append(s.name)
    areas = [{"area": a, "leads": [f"{a} lead"]} for a in order]
    domains = [{"owner": a, "area": a, "units": area_units[a]} for a in order]
    first_room = role_room.get(plan.services[0].role, "backend") if plan.services else "backend"

    # The floor's "extra workers" pool = the user's setting (p.max_slots), so the same
    # knob drives platform and single builds. Each discipline room still gets its one
    # baseline worker; these extras are shared out across the rooms by demand.
    _emit(p, type="project", request=p.request, mode=p.mode, backend=p.backend,
          slots=p.max_slots)
    _emit(p, type="org", areas=areas, domains=domains)
    _emit(p, type="active", room=first_room)   # seat everyone; first discipline works
    _emit(p, type="narrate", text=f"🏗  Platform build — {p.request}")

    role_desc = {"web": "the web frontend (SPA)",
                 "backend": "the back-end (REST API + auth + domain)",
                 "data": "the data layer (schema + DB)",
                 "worker": "the background worker", "game": "the game"}
    svc_role = {s.name: s.role for s in plan.services}
    state: dict[str, str] = {s.name: "queued" for s in plan.services}
    svc_stage: dict[str, str] = {}   # last pipeline stage per service (show every CHANGE)

    def push_prog() -> None:
        _emit(p, type="platprog", services=dict(state), total=len(state))

    push_prog()

    def set_state(svc: str, st: str) -> None:
        state[svc] = st
        push_prog()

    def cb(ev: dict[str, Any]) -> None:
        t = ev.get("type")
        svc = ev.get("service", "")
        pn = ev.get("pass", 1)
        if t == "plan":
            _emit(p, type="narrate", text="📋 Services: " + ", ".join(ev.get("services") or []))
        elif t == "svc_start":
            role = str(ev.get("role") or "")
            _emit(p, type="active", room=role_room.get(role, "backend"))
            set_state(svc, "building")
            _emit(p, type="narrate", text=f"▶ {svc} — building {role_desc.get(role, role)}")
        elif t == "svc_node":
            stage = _node_stage(str(ev.get("node") or ""))
            last = svc_stage.get(svc)
            if stage and stage != last:   # show every stage CHANGE (forward AND backward)
                back = (last in _STAGE_ORDER and stage in _STAGE_ORDER
                        and _STAGE_ORDER.index(stage) < _STAGE_ORDER.index(last))
                svc_stage[svc] = stage
                if back:   # regressed to an earlier stage — e.g. tests failed -> re-implement
                    _emit(p, type="narrate", text=f"   ↩ {svc} · back to {stage}")
                    _emit(p, type="rebuild", room=role_room.get(svc_role.get(svc, ""), "backend"))
                else:
                    _emit(p, type="narrate", text=f"   {svc} · {stage}")
        elif t == "pass" and pn > 1:
            svc_stage.pop(svc, None)   # a new pass re-runs the pipeline; let stages re-announce
            set_state(svc, "rebuilding")
            _emit(p, type="rebuild", room=role_room.get(svc_role.get(svc, ""), "backend"))
            _emit(p, type="narrate", text=f"   ↻ {svc} — rebuilding · pass {pn}")
        elif t == "pass_result":
            if ev.get("passed"):
                set_state(svc, "built")
                _emit(p, type="narrate", text=f"   ✓ {svc} — built, gates green (pass {pn})")
            else:
                set_state(svc, "held")
                _emit(p, type="narrate", text=f"   ✗ {svc} — held at the gate (pass {pn})")
        elif t == "critic":
            if ev.get("complete"):
                set_state(svc, "approved")
                _emit(p, type="narrate", text=f"   ✦ {svc} — approved: technically complete")
            else:
                gaps = ev.get("gaps") or []
                detail = "; ".join(gaps[:3]) if gaps else "more to do"
                _emit(p, type="narrate", text=f"   • {svc} — needs more: {detail} → rebuilding")
        elif t == "svc_done":
            set_state(svc, "done" if ev.get("passed") else "failed")
            mark = "✓" if ev.get("passed") else "✗"
            _emit(p, type="narrate", text=f"{mark} {svc} done · {ev.get('passes', 1)} pass(es)")
            for f in (ev.get("files") or [])[:8]:   # the specific pieces this service built
                _emit(p, type="narrate", text=f"     ⊞ {svc}/{f}")

    opts = p.platform_opts or {}
    staging = str(p.out_base.parent / f".staging-{p.id}") if p.out_base else None
    try:
        result = build_platform(
            p.request, out_dir=str(p.out_base), backend=p.backend, mode=p.mode,
            cost_cap_microusd=p.cost_cap_microusd, progress_cb=cb, work_dir=staging,
            max_passes=int(opts.get("max_passes", 6)),
            critic=bool(opts.get("critic", True)),
            max_parallel=int(opts.get("max_parallel", 4)),
            max_slots_per_service=int(opts.get("max_slots_per_service", 6)),
        )
    except Exception as exc:
        _emit(p, type="narrate", text=f"💥 Platform error: {exc}")
        _emit(p, type="done", passed=False, phase="error")
        p.status = "done"
        _persist(p)
        _close(p)
        return

    # One "check" per service for the repo Checks tab (disk-fallback reads p.gates).
    p.gates = {s.name: {"status": "PASS" if s.passed else "FAIL", "advisory": False}
               for s in result.services}
    n_files = sum(1 for f in p.out_base.rglob("*") if f.is_file()) if p.out_base else 0
    _emit(p, type="narrate",
          text=f"📦 Stitched monorepo — {len(result.services)} services, {n_files} files")
    if n_files:
        _emit(p, type="files", count=n_files)
    if result.passed:   # shipped — open the vault chest + celebrate, like a single build
        _emit(p, type="release", uri=f"monorepo · {len(result.services)} services")
    _emit(p, type="done", passed=result.passed, phase="done")
    p.passed = bool(result.passed)
    p.status = "done"
    _persist(p)
    _close(p)


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
        if path == "/api/builds":
            self._list_builds()
            return
        if path.startswith("/api/releases/"):
            self._list_releases(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/repo/"):
            self._repo_meta(path.rsplit("/", 1)[-1])
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
        from foundry.cli import ADAPTERS, BACKENDS, _tier_models

        default = os.environ.get("FOUNDRY_MODEL_BACKEND", "mock")
        if default not in BACKENDS:
            default = "mock"
        default_adapter = os.environ.get("FOUNDRY_CODING_ADAPTER", "stub")
        if default_adapter not in ADAPTERS:
            default_adapter = "stub"
        # The effective per-tier model ids each real backend would serve (curated
        # default + FOUNDRY_MODEL_T{1,2,3} overrides) so the UI can SHOW what runs
        # for whichever backend the user picks. Mock has no real models.
        models = {} if default == "mock" else _tier_models(default)
        models_by_backend = {
            b: _tier_models(b) for b in BACKENDS if b != "mock"
        }
        try:
            default_slots = max(1, int(os.environ.get("FOUNDRY_MAX_SLOTS", "1") or "1"))
        except ValueError:
            default_slots = 1
        self._json(200, {
            "default_backend": default, "backends": list(BACKENDS),
            "default_adapter": default_adapter, "adapters": list(ADAPTERS),
            "default_max_slots": default_slots, "models": models,
            "models_by_backend": models_by_backend,
        })

    # -- output browsing (the build's written code + docs) ----------------------
    def _out_base(self, pid: str) -> Path | None:
        p = _PROJECTS.get(pid)
        if p is None or p.out_base is None or not p.out_base.is_dir():
            return None
        return p.out_base

    def _list_files(self, pid: str) -> None:
        # The code lives in the store: list the materialized CAS tree first. Fall
        # back to a disk export only for an older build with no durable checkpoint.
        tree = _overlay_tree(pid)
        if tree is not None:
            files = [{"path": p, "size": len(b)} for p, b in sorted(tree.items())]
            self._json(200, {"files": files, "source": "store"})
            return
        base = self._out_base(pid)
        if base is None:
            self._json(404, {"error": "no output yet"})
            return
        files = [
            {"path": str(f.relative_to(base)), "size": f.stat().st_size}
            for f in sorted(base.rglob("*")) if f.is_file()
        ]
        self._json(200, {"files": files, "source": "disk"})

    def _repo_meta(self, pid: str) -> None:
        """The self-hosted-GitHub view of a build: tree + gate 'checks' + the
        FeatureDiff 'pull requests', all read from the durable store."""
        repo = _read_repo_cached(pid)
        tree = _overlay_tree(pid)
        if repo is None and tree is None:
            # No durable checkpoint — degrade to a disk listing + the cached HUD gates.
            base = self._out_base(pid)
            p = _PROJECTS.get(pid)
            if base is None:
                self._json(404, {"error": "no repo yet"})
                return
            files = [
                {"path": str(f.relative_to(base)), "size": f.stat().st_size}
                for f in sorted(base.rglob("*")) if f.is_file()
            ]
            checks = [
                {"name": n, "status": str(g.get("status", "")).split(".")[-1].lower(),
                 "ok": str(g.get("status", "")).split(".")[-1].lower() == "pass",
                 "advisory": bool(g.get("advisory")), "findings": []}
                for n, g in (p.gates.items() if p else [])
            ]
            self._json(200, {"source": "disk", "tree_sha": None, "files": files,
                             "checks": checks, "diffs": [], "sign_offs": []})
            return
        files = [{"path": p, "size": len(b)} for p, b in sorted((tree or {}).items())]
        checks = [
            {"name": c.name, "status": c.status, "ok": c.ok,
             "advisory": c.advisory, "findings": list(c.findings)}
            for c in (repo.checks if repo else ())
        ]
        diffs = [
            {"uri": d.uri, "summary": d.summary, "text": d.text}
            for d in (repo.diffs if repo else ())
        ]
        self._json(200, {"source": "store",
                         "tree_sha": repo.tree_sha if repo else None,
                         "files": files, "checks": checks, "diffs": diffs,
                         "sign_offs": _sign_offs_payload(repo)})

    def _resolve_within(self, base: Path, rel: str) -> Path | None:
        """Resolve ``rel`` under ``base``, fail-closed on traversal/symlink escape."""
        target = (base / rel).resolve()
        root = str(base.resolve())
        if target == base.resolve() or not str(target).startswith(root + os.sep):
            return None
        return target

    def _serve_output_file(self, pid: str) -> None:
        from urllib.parse import parse_qs, urlparse

        rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
        # Store first: the materialized tree's keys are already containment-checked
        # by materialize_workdir (D-R1-03), so an exact-key lookup needs no re-guard.
        tree = _overlay_tree(pid)
        if tree is not None:
            data = tree.get(rel)
            if data is None:
                self._json(404, {"error": "not found"})
                return
            self._send_text(data[:_MAX_VIEW_BYTES])
            return
        base = self._out_base(pid)
        if base is None:
            self._json(404, {"error": "no output"})
            return
        target = self._resolve_within(base, rel)
        if target is None or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send_text(target.read_bytes()[:_MAX_VIEW_BYTES])

    def _send_text(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _zip_output(self, pid: str) -> None:
        """Export the build's repo as a .zip — materialized straight from the store
        (the explicit 'export' now that the build writes no output dir by default)."""
        import io
        import zipfile

        tree = _overlay_tree(pid)
        name = pid
        if tree is None:
            base = self._out_base(pid)
            if base is None:
                self._json(404, {"error": "no output"})
                return
            tree = {
                str(f.relative_to(base)): f.read_bytes()
                for f in sorted(base.rglob("*")) if f.is_file()
            }
            name = base.name
        # Export-time sidecar: ship the attested gate verdicts + recorded sign-offs
        # alongside the tree (a sibling .foundry/evidence.json + EVIDENCE.md), never
        # mixed INTO the materialized tree the gates ran against.
        tree = {**tree, **_evidence_files(pid)}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, data in sorted(tree.items()):
                zf.writestr(rel, data)
        payload = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}.zip"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        denied = _authorize_mutation(self.headers, _AUTH_TOKEN)
        if denied is not None:
            self._json(403, {"error": denied})
            return
        if path == "/api/projects":
            self._new_project()
        elif path == "/api/reset":
            self._reset()
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
            projs = list(_PROJECTS.values())
        # One row per FLOOR (project) = its latest release, plus the release count.
        by_floor: dict[int, "Project"] = {}
        counts: dict[int, int] = {}
        for p in projs:
            counts[p.floor] = counts.get(p.floor, 0) + 1
            cur = by_floor.get(p.floor)
            if cur is None or p.release_seq > cur.release_seq:
                by_floor[p.floor] = p
        rows = [
            {"id": p.id, "floor": p.floor, "release": p.release_seq,
             "releases": counts[p.floor], "request": p.request, "mode": p.mode,
             "backend": p.backend, "adapter": p.adapter,
             "supervised": p.supervised, "slots": p.max_slots,
             "status": p.status, "passed": p.passed, "awaiting": p.awaiting,
             "gates": p.gates, "parent_id": p.parent_id}
            for p in sorted(by_floor.values(), key=lambda x: x.floor)
        ]
        self._json(200, {"projects": rows})

    def _list_builds(self) -> None:
        """Finished builds from the catalog — the 'iterate on…' picker reads this
        to let a new floor start from an existing build (lineage via parent_id)."""
        try:
            rows = _STORE.finished()
        except Exception:
            rows = []
        builds = [
            {"id": r["id"], "floor": r.get("floor"), "release": r.get("release_seq"),
             "request": r.get("request"), "passed": r.get("passed"),
             "parent_id": r.get("parent_id"), "created_at": r.get("created_at"),
             # The repo lives in the store: a finished build has one (a disk export
             # may also exist). Either makes it a valid base to iterate on.
             "has_repo": bool(r.get("status") == "done" or (
                 r.get("out_base") and (Path(r["out_base"]) / "code").is_dir()))}
            for r in rows
        ]
        self._json(200, {"builds": builds})

    def _list_releases(self, floor_s: str) -> None:
        """Every release of one floor (project), newest first — release history."""
        try:
            floor = int(floor_s)
        except ValueError:
            self._json(400, {"error": "bad floor"})
            return
        try:
            rows = _STORE.releases_of(floor)
        except Exception:
            rows = []
        rels = [
            {"id": r["id"], "release": r.get("release_seq"), "request": r.get("request"),
             "status": r.get("status"), "passed": r.get("passed"),
             "created_at": r.get("created_at")}
            for r in rows
        ]
        self._json(200, {"floor": floor, "releases": rels})

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
        # validate against the real backend + adapter sets
        from foundry.cli import ADAPTERS, BACKENDS, _default_adapter_for

        body = self._read_json()
        request = str(body.get("request", "")).strip() or "build a tiny URL-shortener service"
        # The Workshop offers only gated / autonomous (both approve in the floor
        # screen — no external Git merge). `assistant` stays a valid ENGINE/CLI mode
        # for back-compat, but a UI build defaults to gated and never silently runs
        # the advisory-PR posture.
        mode = str(body.get("mode", "gated"))
        if mode not in _MODES:
            mode = "gated"
        backend = str(body.get("backend", "mock"))
        if backend not in BACKENDS:
            backend = "mock"
        # The adapter is DERIVED from the backend when not explicitly provided (CLI
        # parity: mock→stub, any real backend→raw-react), so a real backend writes
        # real code without a second field. An explicit valid adapter still wins.
        adapter = str(body.get("adapter", "")).strip()
        if adapter not in ADAPTERS:
            adapter = _default_adapter_for(backend)
        raw_packs = body.get("packs", [])
        if isinstance(raw_packs, str):
            raw_packs = raw_packs.replace(",", " ").split()
        packs = [str(p).strip() for p in raw_packs if str(p).strip()]
        supervised = bool(body.get("supervised", True))
        spec_text = str(body.get("spec_text", "")).strip()
        spec_zip_b64 = body.get("spec_zip_b64")
        # Parallelism is system-decided (the Tech-Lead's decomposition sizes the IC
        # pool, capped by AUTO_SLOTS_CAP). The env/body value only OVERRIDES the
        # ceiling; absent ⇒ the automatic cap. The UI no longer asks for a count.
        from foundry.cli import AUTO_SLOTS_CAP

        try:
            default_slots = int(os.environ.get("FOUNDRY_MAX_SLOTS", "") or AUTO_SLOTS_CAP)
        except ValueError:
            default_slots = AUTO_SLOTS_CAP
        try:
            max_slots = int(body.get("max_slots", default_slots))
        except (TypeError, ValueError):
            max_slots = default_slots
        max_slots = max(1, max_slots)
        # auto-fix retry budget (run() default = 3); optional UI override.
        try:
            max_fix_attempts = int(body.get("max_fix_attempts", 5))
        except (TypeError, ValueError):
            max_fix_attempts = 5
        max_fix_attempts = max(0, max_fix_attempts)
        # spend cap: UI sends dollars (cost_cap_usd); fall back to the env micro-USD.
        cost_env = os.environ.get("FOUNDRY_COST_CAP_MICROUSD", "").strip()
        cost_cap_microusd = int(cost_env) if cost_env.isdigit() else None
        try:
            usd = float(body.get("cost_cap_usd") or 0)
            if usd > 0:
                cost_cap_microusd = int(round(usd * 1_000_000))
        except (TypeError, ValueError):
            pass
        # A floor is one PROJECT. A fresh build is a NEW project (new floor, release
        # 1). Iterating (parent_id) adds the NEXT RELEASE to the parent's floor — same
        # project, release_seq + 1 — never a new floor.
        parent_id = str(body.get("parent_id") or "").strip() or None
        parent = _STORE.get(parent_id) if parent_id else None
        if parent is None:
            parent_id = None
        if parent is not None:
            floor = int(parent.get("floor", 0))
            with _LOCK:
                release_seq = _STORE.max_release(floor) + 1
        else:
            with _LOCK:
                _FLOOR[0] += 1
                floor = _FLOOR[0]
            release_seq = 1
        pid = uuid.uuid4().hex[:12]
        out_base = _OUTPUT_ROOT / f"floor-{floor}" / f"r{release_seq}"
        # Spec input: pasted text (literal), else an uploaded docs-zip (a dir),
        # else the PARENT's design docs (iterate), else None (request alone).
        spec: str | None = spec_text or None
        if spec is None and isinstance(spec_zip_b64, str) and spec_zip_b64.strip():
            try:
                spec_dir = out_base / "spec-in"
                if _safe_unzip_spec(spec_zip_b64, spec_dir) > 0:
                    spec = str(spec_dir)
            except Exception:
                spec = None
        base_dir: str | None = None
        if parent and parent.get("out_base"):
            pbase = Path(parent["out_base"])
            pdocs, pcode = pbase / "docs", pbase / "code"
            if spec is None and pdocs.is_dir():
                spec = str(pdocs)            # ground the release in the prior design
            if pcode.is_dir():
                base_dir = str(pcode)        # seed the base tree with the prior code
        p = Project(
            id=pid, request=request, mode=mode, supervised=supervised, floor=floor,
            release_seq=release_seq, backend=backend, adapter=adapter, packs=packs, spec=spec,
            out_base=out_base, max_slots=max_slots, cost_cap_microusd=cost_cap_microusd,
            max_fix_attempts=max_fix_attempts, parent_id=parent_id, base_dir=base_dir,
        )
        with _LOCK:
            _PROJECTS[pid] = p
        # NOTE: do NOT catalog the build here. A floor is persisted only once the
        # build ACTUALLY starts — the runner's first `project` event (in _PERSIST_ON)
        # upserts it. So a created-but-never-run project leaves no floor to restore
        # on restart (the response below carries the floor/release in-memory anyway).
        # A PLATFORM build composes many services into one monorepo on this floor; a
        # normal build is one service through the factory. INFERRED from the request
        # (multi-service => platform), unless the caller pins it with `platform`.
        want = body.get("platform")
        is_platform = want if isinstance(want, bool) else _infers_platform(request)
        if is_platform:
            p.platform_opts = _platform_opts(body)
        runner = _run_platform if is_platform else _run_factory
        threading.Thread(target=_gated_runner, args=(runner, p), daemon=True).start()
        self._json(200, {"id": pid, "floor": floor, "release": release_seq})

    def _reset(self) -> None:
        """Clear ALL floors & repos: drop the in-memory floors, reset the floor
        counter, and wipe the durable catalog (builds + event logs + seen-uris) so a
        restart rehydrates nothing. Behind the mutation guard. The content-addressed
        store on disk is left as-is — it is unreferenced once the catalog is empty."""
        with _LOCK:
            _PROJECTS.clear()
            _FLOOR[0] = 0
        try:
            _STORE.clear_all()
        except Exception:
            pass
        with _REPO_CACHE_LOCK:
            _REPO_CACHE.clear()
        self._json(200, {"ok": True})

    def _decision(self, pid: str) -> None:
        body = self._read_json()
        decision = str(body.get("decision", "approve"))
        if decision not in ("approve", "reject", "needs_changes"):
            decision = "approve"
        p = _PROJECTS.get(pid)
        if p is None:
            self._json(404, {"error": "no such project"})
            return
        # First decision for the awaiting checkpoint wins; later/duplicate clicks
        # (from this or another device) are no-ops — but every device still sees
        # the `resolved` event play, because it rides the shared log.
        cp = body.get("checkpoint")
        try:
            checkpoint = int(cp) if cp is not None else None
        except (TypeError, ValueError):
            checkpoint = None
        applied, stale = _record_decision(p, decision, checkpoint)
        self._json(200, {"ok": not stale, "applied": applied, "stale": stale})

    def _stream(self, pid: str) -> None:
        p = _PROJECTS.get(pid)
        if p is None:
            self._json(404, {"error": "no such project"})
            return
        # Resume point: the browser's EventSource replays its Last-Event-ID on a
        # transient reconnect (so it tails from where it left off, no re-replay);
        # a brand-new viewer sends none and replays the whole log from 0.
        cursor = _resume_cursor(self.headers.get("Last-Event-ID"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # A finished build whose in-memory log was trimmed to save RAM: replay its
        # FULL history from the durable store (the trimmed tail isn't the whole story).
        if p.ended and p.log_trimmed:
            history = _STORE.events_of(pid)
            try:
                for idx in range(cursor, len(history)):
                    ev = history[idx]
                    if ev.get("type") == "_end":
                        self.wfile.write(f"id: {idx}\nevent: end\ndata: {{}}\n\n".encode())
                        self.wfile.flush()
                        return
                    self.wfile.write(f"id: {idx}\ndata: {json.dumps(ev)}\n\n".encode())
                self.wfile.write(b"event: end\ndata: {}\n\n")   # caught up; build is over
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        try:
            while True:
                with p.cond:
                    while len(p.log) <= cursor and not p.ended:
                        if not p.cond.wait(timeout=15):
                            break          # idle → fall through to a keep-alive
                    batch = p.log[cursor:]
                    ended = p.ended
                if not batch:
                    if ended:              # caught up AND the build is over
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    self.wfile.write(b": keep-alive\n\n")  # comment frame
                    self.wfile.flush()
                    continue
                for idx, ev in enumerate(batch, start=cursor):
                    if ev.get("type") == "_end":
                        self.wfile.write(f"id: {idx}\nevent: end\ndata: {{}}\n\n".encode())
                        self.wfile.flush()
                        return
                    self.wfile.write(
                        f"id: {idx}\ndata: {json.dumps(ev)}\n\n".encode()
                    )
                cursor += len(batch)
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
    if not _ENV_FILE_FOUND:
        print("\n  ⚠  No .env found — set one up so your backend + key are configured:")
        print("       cp foundry.mac.env.example .env    # Mac  (Linux: foundry.linux.env.example)")
        print("       …then edit .env (token/key) and start again.\n")
    Path(_STATE_DIR).mkdir(parents=True, exist_ok=True)   # durable factory state
    _load_persisted()   # rehydrate floors (+ resume any interrupted build) from disk
    if _LOG_SWEEP_S > 0:   # bound RAM on a long-lived server: trim finished logs
        threading.Thread(target=_log_sweeper, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"\n  🏭  Foundry Workshop running →  http://{shown}:{port}\n")
    print(f"      durable state: {_STATE_DIR}  (builds survive a restart)")
    if _PROJECTS:
        resumed = sum(1 for q in _PROJECTS.values() if q.status == "running")
        line = f"      restored {len(_PROJECTS)} floor(s) from the build catalog"
        print(line + (f" — resuming {resumed} interrupted build(s)\n" if resumed else "\n"))
    if host == "0.0.0.0":  # opt-in LAN exposure (e.g. a Pi) — warned below
        lan = _lan_ip()
        if lan:
            tok = f"/?token={_AUTH_TOKEN}" if _AUTH_TOKEN else ""
            print(f"      On your LAN (phone/tablet):  http://{lan}:{port}{tok}")
        print("      ⚠ bound to ALL interfaces — anyone on this network can reach it.")
        if _AUTH_TOKEN:
            print("      🔒 a FOUNDRY_WORKSHOP_TOKEN is required for builds (good).\n")
        else:
            print("      ⚠ no FOUNDRY_WORKSHOP_TOKEN set — set one to require auth for"
                  " state-changing requests on an exposed network.\n")
    else:
        # Localhost default: the Origin/CSRF guard still blocks a malicious page.
        print(f"      auth: {'token required' if _AUTH_TOKEN else 'localhost-only + CSRF guard'}")
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
