"""Foundry Workshop — a localhost game frontend over the REAL factory.

Run:  uv run python -m webapp.server   then open  http://localhost:8765

A tiny stdlib HTTP + Server-Sent-Events server (no extra deps, never touches the
trust core). ``POST /api/projects`` starts a REAL ``foundry.cli.run`` in a thread;
this server streams the real per-node telemetry (artifacts, agent journal, gate
verdicts) to the browser as game events. Builds run hands-off; the gates decide
what ships — this is just the view.
"""

from __future__ import annotations

import contextlib
import hmac
import inspect
import json
import os
import re
import sys
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from foundry.schemas.architecture import CANONICAL_AREAS
from webapp.events import (
    AGENT_BY_NODE,
    STAGE_FOR_NODE,
    event_for_task,
    events_for_node_output,
    events_for_result,
    journal_dicts,
    live_event_for_node,
)
from webapp.repo import Repo, RepoUnavailable, read_plan_context, read_repo
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
            # Inline comments: the shipped presets annotate values on the SAME line
            # (`FOUNDRY_MODEL_T1=claude-opus-4-8   # heaviest reasoning`). Without
            # stripping them, the comment becomes PART of the value — poisoning
            # model ids, tokens, and paths with garbage that fails every CLI call.
            val = re.split(r"\s#", val, maxsplit=1)[0]
            val = os.path.expandvars(os.path.expanduser(val.strip().strip('"').strip("'")))
            if not key or key in preexisting:    # last file line for a key wins
                continue
            # A blank `KEY=` placeholder (the presets ship CLAUDE_CODE_OAUTH_TOKEN=
            # empty) must NOT inject a present-but-empty env var: the claude CLI
            # subprocess sees the var EXISTS, tries token auth with an empty token,
            # and fails — while the same command works fine in the user's own shell.
            if not val:
                continue
            # TMPDIR pointing at a directory that doesn't exist (the Linux preset's
            # tmpfs before its fstab entry) breaks every subprocess spawn with an
            # opaque transport error — warn and skip instead.
            if key == "TMPDIR" and not os.path.isdir(val):
                print(f"[env] ignoring TMPDIR={val!r} from .env — directory does not exist")
                continue
            os.environ[key] = val
        return True
    return False


_ENV_FILE_FOUND = _load_env_file()   # before the config constants below read os.environ

_STATIC = Path(__file__).parent / "static"
_PACE = 0.5          # seconds between paced closing events
# Live activity from inside an agentic step (each edit / command the substrate runs).
# Bounded on BOTH axes: a floor is readable at ~1 line every 2s, and one step can't
# flood the log (or every connected device's stream) with hundreds of tool calls.
_ACT_MIN_GAP = 2.0          # seconds between reported activities
_ACT_MAX_PER_STEP = 40      # then one "still working" line and silence for that step

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

# State-changing routes (start a build → real model spend) are
# guarded. A shared token (FOUNDRY_WORKSHOP_TOKEN) gates them when the server is
# exposed beyond localhost; an Origin/Host same-origin check blocks a malicious web
# page from POSTing to a localhost workshop (CSRF) even with no token set.
_AUTH_TOKEN = os.environ.get("FOUNDRY_WORKSHOP_TOKEN") or None

# Session login (webapp/auth.py). ON unless FOUNDRY_AUTH=off: what is behind
# this door spends real money and executes generated code, so an open default
# was the wrong way round. The token above still works for scripts and now
# satisfies the session check too, so automation needs no cookie jar.
from webapp import auth as _auth


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
# Advisory authoring aid: it produces ONLY
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
    "reflecting the whole conversation so far. If a prior spec and a summary of "
    "what a previous build already produced are given, treat the spec as the "
    "target and focus the new round on the GAPS and the requested changes — do "
    "not re-propose what is already built. Reply with JSON only."
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


WORKER_SPECIALTIES = ("frontend", "backend", "data", "test")
DEFAULT_WORKERS = 2
MAX_WORKERS = 16
_TICKET_DIFFICULTIES = ("trivial", "standard", "complex")


def parse_workers(raw: Any = None) -> dict[str, int]:
    """Per-specialty worker limits from a request body: ints >= 1 (clamped to
    MAX_WORKERS), default 2 each; unknown keys and non-integers fall back."""
    src = raw if isinstance(raw, Mapping) else {}
    out: dict[str, int] = {}
    for spec in WORKER_SPECIALTIES:
        try:
            n = int(src.get(spec, DEFAULT_WORKERS))
        except (TypeError, ValueError):
            n = DEFAULT_WORKERS
        # The pool needs >= 1 permit per specialty; 0 would strand that specialty's tickets.
        out[spec] = max(1, min(MAX_WORKERS, n))
    return out


def parse_ticket_tiers(raw: Any) -> dict[str, str]:
    """difficulty -> tier overrides; entries outside trivial|standard|complex -> T1|T2|T3 drop."""
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(k): str(v).upper() for k, v in raw.items()
        if str(k) in _TICKET_DIFFICULTIES and str(v).upper() in ("T1", "T2", "T3")
    }


@dataclass
class Project:
    id: str
    request: str
    mode: str  # always "assistant" (the only release mode); kept for the catalog
    floor: int
    backend: str = "mock"
    adapter: str = "stub"  # coding substrate (see foundry.cli.ADAPTERS)
    spec: str | None = None  # literal spec text OR a dir of uploaded docs (run(spec=))
    spec_text: str = ""  # the literal spec (converse/paste), cataloged so an iterate inherits + extends it
    max_slots: int = 4  # IC-pool CEILING (= cli.AUTO_SLOTS_CAP); the system sizes
    # actual parallelism to the lead's decomposition (min(tasks, this)).
    cost_cap_microusd: int | None = None  # global spend envelope (Wave D)
    token_cap: int | None = None  # stop the build cleanly after this many tokens
    workers: dict[str, int] = field(default_factory=parse_workers)  # per-specialty limits
    ticket_tiers: dict[str, str] = field(default_factory=dict)  # difficulty -> tier overrides
    max_fix_attempts: int = 2  # auto-fix retry budget per failing gate (run() default)
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
    # `log` and `ended`.
    log: list[dict[str, Any]] = field(default_factory=list)
    cond: threading.Condition = field(default_factory=threading.Condition)
    ended: bool = False
    ended_at: float | None = None    # monotonic time the build finished (for log sweeping)
    log_trimmed: bool = False        # in-memory log trimmed to a tail; full history in the store
    # Auto-resume budget for TRANSIENT provider failures (ProxyUnavailable — a
    # pause-and-escalate, not a build failure): the build backs off and resumes
    # from its durable checkpoint instead of dying, so an overnight run survives a
    # usage-window exhaustion / provider outage. Decrements per outer retry.
    autoresume_left: int = 6
    seen_uris: set[str] = field(default_factory=set)
    # Steps running RIGHT NOW: "who/what" -> monotonic start. Fed by the live per-node
    # telemetry so the quiet-watchdog can name what a long silence is waiting ON,
    # instead of just reporting that nothing has landed. Runtime-only (not persisted).
    inflight: dict[str, tuple[str, float]] = field(default_factory=dict)
    # Live ACTIVITY attribution + throttle. The coding substrate reports what it is
    # doing (editing x, running y) but not which lane it belongs to, so activity is
    # attributed to the most recently started step — right in the common case, and at
    # worst it names the wrong lane while several run at once.
    act_room: str = ""
    act_agent: str = ""
    act_last: float = 0.0            # monotonic time of the last emitted activity
    act_count: int = 0               # emitted this step (capped; reset on each start)
    act_text: str = ""               # last phrase (collapses immediate repeats)
    status: str = "running"
    passed: bool = False  # final verdict (for elevator/reload rehydration)
    journal: list[dict[str, Any]] = field(default_factory=list)  # agent journal so far ([11])
    gates: dict[str, Any] = field(default_factory=dict)  # name -> {status,attested,advisory}
    out_base: Path | None = None  # this floor's output dir (code/)
    profile: str = "auto"  # verification profile / stack ("auto" = infer from request)


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


def _suggest_slots(request: str) -> tuple[int, list[str]]:
    """N = the distinct CANONICAL_AREAS the request likely touches (>=1).
    'backend' is the baseline area for any service build."""
    text = request.lower()
    hit = {"backend"}
    for area, kws in _AREA_KEYWORDS.items():
        if any(k in text for k in kws):
            hit.add(area)
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
_PERSIST_ON = {"project", "gate", "files", "halt", "release", "done"}


def _build_record(p: "Project") -> dict[str, Any]:
    """The catalog (DB) shape of a build."""
    return {
        "id": p.id, "floor": p.floor, "release_seq": p.release_seq,
        "parent_id": p.parent_id, "request": p.request, "mode": p.mode,
        "backend": p.backend, "adapter": p.adapter,
        "slots": p.max_slots,
        "max_fix_attempts": p.max_fix_attempts, "status": p.status,
        "passed": p.passed, "gates": p.gates,
        "out_base": str(p.out_base) if p.out_base else None,
        "created_at": p.created_at,
        "spec_text": p.spec_text,
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
                    mode="assistant",  # gated/autonomous rows from old builds resume as assistant
                    floor=int(row.get("floor", 0)),
                    release_seq=int(row.get("release_seq", 1) or 1),
                    backend=str(row.get("backend", "mock")),
                    adapter=str(row.get("adapter", "stub")),
                    max_slots=int(row.get("slots", 1) or 1),
                    max_fix_attempts=int(row.get("max_fix_attempts", 2) or 2),
                    parent_id=row.get("parent_id"),
                    out_base=Path(ob) if ob else None,
                    created_at=str(row.get("created_at") or ""),
                    spec_text=str(row.get("spec_text") or ""),
                )
                try:
                    p.log = _STORE.events_of(p.id)   # the durable replay log
                except Exception:
                    p.log = []
                was_running = str(row.get("status", "")) == "running" and not any(
                    e.get("type") == "_end" for e in p.log
                )
                p.passed = bool(row.get("passed", False))
                p.gates = dict(row.get("gates") or {})
                _PROJECTS[p.id] = p
                # A checkpoint written by the removed gated/autonomous graph names nodes
                # that no longer exist, so it can't be resumed on the assistant graph.
                # Neither can a floor paused at a removed supervision checkpoint: run()
                # would re-enter from START, re-plan without retiring the first plan's
                # tickets and build both — so it is closed out like a legacy floor.
                legacy_mode = str(row.get("mode") or "assistant") != "assistant"
                if (was_running and not legacy_mode and not _paused_at_retired_checkpoint(p.log)
                        and _has_durable_checkpoint(p.id)):
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


def _paused_at_retired_checkpoint(log: list[dict[str, Any]]) -> bool:
    """True iff the floor's log ends paused at a supervision checkpoint (an
    ``await_decision`` no ``resolved`` followed) — a pause the graph no longer has."""
    paused = False
    for e in log:
        if e.get("type") == "await_decision":
            paused = True
        elif e.get("type") == "resolved":
            paused = False
    return paused


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


def _build_summary(pid: str) -> str:
    """A concise 'what the last build actually produced' digest — gate outcomes,
    implemented features, and the file manifest — so an iterate knows what is
    already DONE (vs. what the accumulated spec still calls for). Empty string when
    there's nothing readable yet."""
    rec = _STORE.get(pid) or {}
    repo = _read_repo_cached(pid)
    lines: list[str] = []
    passed = rec.get("passed")
    if passed is not None:
        verdict = "passed the gates" if passed else "did NOT pass the gates"
        lines.append(f"Release r{rec.get('release_seq', '?')} {verdict}.")
    if repo is not None:
        if repo.checks:
            checks = ", ".join(f"{c.name} {'PASS' if c.ok else c.status.upper()}" for c in repo.checks)
            lines.append(f"Gate checks: {checks}.")
        feats = [d.summary for d in repo.diffs if d.summary and d.summary != "(feature diff)"]
        if feats:
            shown = "; ".join(feats[:12])
            lines.append("Implemented: " + shown + ("…" if len(feats) > 12 else "") + ".")
        if repo.files:
            paths = [f.path for f in repo.files]
            top = sorted({p.split("/")[0] for p in paths})
            lines.append(f"{len(paths)} file(s) across: {', '.join(top[:20])}.")
    if not lines:
        return ""
    return "## What the last build actually produced\n\n" + "\n".join(f"- {ln}" for ln in lines)


def _evidence_files(pid: str) -> dict[str, bytes]:
    """The evidence sidecar for a build's export: the gate verdicts, as a
    machine-readable ``.foundry/evidence.json`` and a readable ``EVIDENCE.md``.
    Returns ``{}`` when there is nothing to attest yet.

    This is an EXPORT-time sidecar — built from the attested gate records
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
    if not checks:
        return {}
    doc = {
        "build_id": pid,
        "request": p.request if p else "",
        "mode": p.mode if p else "",
        "backend": p.backend if p else "",
        "passed": bool(p.passed) if p else None,
        "tree_sha": repo.tree_sha if repo else None,
        "gates": checks,
    }
    md = _evidence_markdown(doc)
    return {
        ".foundry/evidence.json": (json.dumps(doc, indent=2) + "\n").encode("utf-8"),
        "EVIDENCE.md": md.encode("utf-8"),
    }


def _evidence_markdown(doc: dict[str, Any]) -> str:
    """A human-readable rendering of the evidence doc for ``EVIDENCE.md``."""
    lines = [
        "# Evidence",
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


def _child_spec(p: Project) -> str | None:
    """The spec a release runs with: its own (text or uploaded docs dir) plus, when
    it iterates on a parent, the parent's shipped plan (PRD, design, interface
    contracts, pinned constraints) so the planner extends that plan."""
    parent_plan = read_plan_context(p.parent_id, _STATE_DIR) if p.parent_id else ""
    if not parent_plan:
        return p.spec
    from foundry.cli import _load_spec

    own = _load_spec(p.spec)
    return f"{own}\n\n{parent_plan}" if own.strip() else parent_plan


def take_activity(p: Project, text: str, now: float | None = None) -> str | None:
    """The activity line to report for ``text``, or ``None`` to drop it.

    A long agentic pass fires tool calls faster than anyone can read, and every one
    reported costs an SSE frame to every connected device — so this is bounded on
    three axes: blank/duplicate lines, a minimum gap, and a per-step ceiling. Accepting
    a line advances the throttle state (reset on each step start).
    """
    line = " ".join(str(text or "").split())
    if not line:
        return None
    t = time.monotonic() if now is None else now
    if line == p.act_text or p.act_count >= _ACT_MAX_PER_STEP:
        return None                       # an immediate repeat, or this step said enough
    if p.act_last and (t - p.act_last) < _ACT_MIN_GAP:
        return None                       # too soon after the last one
    p.act_last, p.act_count, p.act_text = t, p.act_count + 1, line
    return line


def waiting_on(p: Project, now: float | None = None, cap: int = 3) -> str:
    """What the build is waiting ON, longest-running first — e.g.
    ``"Backend Engineer (7 min) · running the TEST gate (2 min)"``.

    Reads the live in-flight set fed by the per-node telemetry. Empty string when
    nothing is reported in flight (which is itself the interesting case: a real stall).
    """
    t = time.monotonic() if now is None else now
    items = sorted(p.inflight.values(), key=lambda it: it[1])
    parts = [f"{label} ({max(1, round((t - started) / 60))} min)"
             for label, started in items[:cap]]
    if len(items) > cap:
        parts.append(f"+{len(items) - cap} more")
    return " · ".join(parts)


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


def _resume_cursor(last_event_id: str | None) -> int:
    """The log index a stream should resume at given an SSE ``Last-Event-ID``.

    A fresh viewer (no id) starts at 0 and replays the whole log; a reconnecting
    EventSource replays its last id, so we resume at the NEXT event."""
    if last_event_id and last_event_id.isdigit():
        return int(last_event_id) + 1
    return 0


_STACK_SOP = (
    "You are a principal architect choosing the implementation stack for a new "
    "build. You are given the request, the spec, and the stacks whose toolchains "
    "are INSTALLED on this host (each with the exact gates it pins). Pick the "
    "best-fit stack: prefer the simplest one that satisfies the spec, respect any "
    "stack the request names explicitly, and never pick a stack the spec's "
    "requirements can't meet. Reply with JSON only."
)


def _choose_stack(p: Project) -> tuple[str, str] | None:
    """Architect-driven stack selection for profile="auto" (real backends only).

    Offers ONLY the stacks whose toolchains are installed on this host and asks
    the backend to choose; returns (profile_name, reason) or None (fail-soft →
    the keyword inference in foundry.gates.profiles decides, as before)."""
    if p.backend == "mock":
        return None
    try:
        from foundry.cli import _build_proxy
        from foundry.gates.profiles import PROFILES, installed_profiles

        avail = installed_profiles()
        if len(avail) <= 1:
            return None
        menu = "\n".join(f"- {n}: {PROFILES[n].contract_text}" for n in avail)
        schema = {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": avail},
                "reason": {"type": "string"},
            },
            "required": ["profile", "reason"],
            "additionalProperties": False,
        }
        result = _build_proxy(p.backend).complete(
            "principal_architect", "T2", _STACK_SOP,  # one structural pick; Sonnet is ample
            [{"role": "user",
              "content": (f"REQUEST:\n{p.request}\n\nSPEC:\n{(p.spec_text or '')[:2000]}"
                          f"\n\nINSTALLED STACKS (pick one):\n{menu}")}],
            purpose="ideation", schema=schema,
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else None
        if parsed is None:
            parsed = json.loads(result.text or "{}")
        name = str(parsed.get("profile") or "")
        if name in avail:
            return name, str(parsed.get("reason") or "")[:300]
    except Exception as exc:
        print(f"[stack] architect selection failed (keyword inference instead): {exc}")
    return None


_RL_HOOKED = [False]


def _hook_rate_limit_waits() -> None:
    """Surface long provider-limit waits (a Claude Max window, a Retry-After) in
    the UI: the proxy fires its advisory ``on_rate_limit_wait`` observer and we
    broadcast a ``limitwait`` event + quest-log line to EVERY running floor —
    the account limit throttles all of them, not one. Registered once, lazily
    (with the first build), so server boot never imports the proxy stack."""
    if _RL_HOOKED[0]:
        return
    _RL_HOOKED[0] = True
    try:
        from foundry.proxy import model_provider_proxy as _mpp
    except Exception:
        return

    def _on_wait(wait_s: float, resume_epoch: float, explicit: bool) -> None:
        if wait_s < 120:                     # short backoff blips stay invisible
            return
        hhmm = time.strftime("%H:%M", time.localtime(resume_epoch))
        with _LOCK:
            running = [q for q in _PROJECTS.values() if q.status == "running"]
        for q in running:
            _emit(q, type="limitwait", seconds=int(wait_s), until=int(resume_epoch))
            _emit(q, type="narrate",
                  text=f"⏸ Provider usage limit reached — the build is waiting it "
                       f"out and resumes ~{hhmm}. Nothing is lost.")

    _mpp.on_rate_limit_wait = _on_wait


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

    _hook_rate_limit_waits()

    if resume:
        _emit(p, type="narrate",
              text="↻ Resuming after a restart — picking up from the last checkpoint…")
    else:
        _emit(p, type="project", id=p.id, request=p.request, mode=p.mode, floor=p.floor,
              backend=p.backend, slots=p.max_slots, workers=p.workers)
        _emit(p, type="narrate", text=f"📋 New project on Floor {p.floor}: {p.request!r}")
        _emit(p, type="narrate", text=f"🏭 Mode: {p.mode}. The crew reports for duty…")
        if p.backend != "mock":
            # Real backends: the first graph node is one long model call, and node
            # telemetry only streams as nodes COMPLETE — warn that the quiet is normal.
            _emit(p, type="narrate",
                  text="🧠 Drafting has begun — real model calls can be quiet for "
                       "several minutes; rooms light up as each step lands.")
    # The build ALWAYS opens with product/intake work, but live node telemetry only
    # fires as nodes COMPLETE — seat the product team NOW so the floor shows the
    # work already grinding instead of an all-lounge office.
    _emit(p, type="active", room="plan", agent="", live=True)
    time.sleep(_PACE)

    # Stack = auto on a real backend: the Planner picks the stack from
    # the toolchains actually installed on this host; the chosen profile pins the
    # gates for THIS build and every iterate child.
    if (p.profile or "auto") == "auto":
        chosen = _choose_stack(p)
        if chosen:
            p.profile = chosen[0]
            _emit(p, type="active", room="plan", agent="Planner",
                  live=True)
            _emit(p, type="narrate",
                  text=f"🏗 Planner chose the stack: {chosen[0]}"
                       + (f" — {chosen[1]}" if chosen[1] else ""))

    # LIVE per-node telemetry: as each real graph node executes, seat its agent at
    # the desk in real time; as it finishes, surface what it produced (artifacts,
    # tickets, org chart, journal). Emitted immediately so the floor reflects the
    # build actually grinding.
    def progress(node: str, scope: str, *, phase: str = "finish",
                 result: object = None) -> None:
        if phase == "start" and scope == "factory" and node in STAGE_FOR_NODE:
            room, label = STAGE_FOR_NODE[node]
            _emit(p, type="stage", room=room, label=label)
        if phase == "finish" and isinstance(result, dict):
            _emit_node_output(result)
        ev = live_event_for_node(node, scope, phase, result)
        if ev is None:
            return
        # Track what's in flight so the quiet-watchdog can say WHAT a long silence is
        # waiting on. Keyed by NODE id (a finish event may carry no label at all, so
        # keying by the label would leak entries). Best-effort bookkeeping only.
        if phase == "start":
            label = str(ev.get("agent") or ev.get("step") or node.replace("_", " "))
            p.inflight[node] = (label, time.monotonic())
            # attribute any activity that arrives during this step, and give it a
            # fresh per-step budget
            p.act_room = str(ev.get("room") or "")
            p.act_agent = str(ev.get("agent") or "")
            p.act_count, p.act_text = 0, ""
        else:
            p.inflight.pop(node, None)
        _emit(p, **ev)

    def _emit_node_output(result: Mapping[str, Any]) -> None:
        for out in events_for_node_output(result, p.seen_uris, p.ticket_tiers):
            _emit(p, **out)
        with contextlib.suppress(Exception):   # persist the dedup set so a resume won't replay
            _STORE.add_seen(p.id, p.seen_uris)
        # The agents' journal, so a lead can be "interviewed" ([11], advisory). `agent`
        # = the floor sprite that owns the node, so the interview picker can match a
        # Planner/Reviewer click to entries written under old role ids.
        known = {(e.get("node_id"), e.get("summary")) for e in p.journal}
        for e in journal_dicts(result.get("agent_journal")):
            if (e.get("node_id"), e.get("summary")) not in known:
                known.add((e.get("node_id"), e.get("summary")))
                entry = {**e, "agent": AGENT_BY_NODE.get(str(e.get("node_id", "")), ("",))[0]}
                p.journal.append(entry)
                _emit(p, type="journal", entry=entry)

    def task_cb(ev: Mapping[str, Any]) -> None:
        """One ticket's worker started / finished / failed (the floor's per-ticket sprites)."""
        out = event_for_task(ev)
        if out is not None:
            _emit(p, **out)

    def activity(text: str) -> None:
        """One line of "what the agent is doing right now", from inside a step."""
        line = take_activity(p, text)
        if line is None:
            return
        _emit(p, type="act", room=p.act_room, agent=p.act_agent, text=line)
        if p.act_count == _ACT_MAX_PER_STEP:
            _emit(p, type="act", room=p.act_room, agent=p.act_agent,
                  text="… (still working; further tool calls not listed)")

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
    # Quiet-watchdog: on a REAL backend, if no event lands for a long stretch,
    # report WHO the build is waiting on. A single
    # agentic step is legitimately minutes long, so the first tick is a status line,
    # not an alarm — the "something may be wrong" advice waits until it really drags.
    # (Without this, a hung CLI — auth prompt, wall-clock cap + silent retries — looks
    # exactly like a long model call.)
    wd_stop = threading.Event()

    def _quiet_watchdog() -> None:
        quiet_min = 0
        last_len = len(p.log)
        while not wd_stop.wait(60):
            if p.ended:
                return
            if len(p.log) != last_len:
                last_len, quiet_min = len(p.log), 0
                continue
            quiet_min += 1
            if quiet_min in (5, 15, 30):
                busy = waiting_on(p)
                if busy:
                    text = (f"⏳ Still working — {busy}. Agentic steps report only when "
                            "they finish, so a long quiet stretch here is normal.")
                else:
                    text = (f"⏳ No step has started or finished in {quiet_min} min "
                            "and nothing is reported in flight.")
                if quiet_min >= 15:
                    text += (f" It has been {quiet_min} min — if this keeps up check "
                             "the server terminal, and that the backend CLI answers "
                             "(e.g. `claude -p \"hi\"`) on the host.")
                _emit(p, type="narrate", text=text)
                last_len = len(p.log)

    if p.backend != "mock":
        threading.Thread(target=_quiet_watchdog, daemon=True,
                         name=f"quiet-watchdog-{p.id}").start()
    # Pass the ticket-scheduler kwargs only when this foundry.cli.run knows them.
    accepted = inspect.signature(run).parameters
    sched: dict[str, Any] = {k: v for k, v in (("task_cb", task_cb), ("workers", p.workers),
                               ("ticket_tiers", p.ticket_tiers or None))
             if k in accepted and v is not None}
    try:
        try:
            result = run(
                p.request,
                **sched,
                progress_cb=progress,
                activity_cb=activity,         # what the agent is doing INSIDE a step
                mode=p.mode,
                backend=p.backend,            # real backend per project (not always mock)
                adapter=p.adapter,            # coding substrate (stub/raw-react)
                spec=_child_spec(p),          # spec text OR docs dir, + the parent's plan
                max_slots=p.max_slots,        # IC-pool parallelism (Wave C) + budget tree (Wave D)
                cost_cap_microusd=p.cost_cap_microusd,
                token_cap=p.token_cap,
                max_fix_attempts=p.max_fix_attempts,
                base_dir=base_dir,            # iterate: prior code seeded as the base tree
                thread_id=p.id,               # UNIQUE per project — no cross-floor collision
                state_dir=_STATE_DIR,         # durable: build survives + resumes past a restart
                output_dir=str(code_dir) if code_dir else None,
                profile=p.profile or "auto",  # stack picked in the UI ("auto" = infer)
            )
        finally:
            wd_stop.set()
            base_stack.close()  # run() copies base_dir into its sandbox synchronously
    except Exception as exc:
        # Print the full traceback to the SERVER terminal too — the quest-log line
        # is a one-line summary, and "no error in the terminal" must never mislead.
        print(f"[build] floor {p.floor} r{p.release_seq} failed: {exc}")
        traceback.print_exc()
        # A TRANSIENT provider failure (ProxyUnavailable — the proxy already rode
        # its bounded retry/rate-limit budget and is telling judgment nodes to
        # "pause-and-escalate") is NOT a build failure: the durable checkpoint is
        # intact, so back off and RESUME from it rather than losing an overnight
        # run. Bounded (autoresume_left) with growing backoff so a spent Max
        # window / provider outage is ridden out, not hammered.
        transient = "ProxyUnavailable" in type(exc).__name__ or "pause-and-escalate" in str(exc)
        if transient and p.autoresume_left > 0 and _has_durable_checkpoint(p.id):
            idx = 6 - p.autoresume_left
            p.autoresume_left -= 1
            wait = min(3600, 300 * (2**idx))     # 5,10,20,40,60,60 min
            resume_at = time.strftime("%H:%M", time.localtime(time.time() + wait))
            _emit(p, type="narrate",
                  text=f"⏸ Provider unavailable ({exc}). Auto-resuming from the last "
                       f"checkpoint ~{resume_at} — nothing is lost.")
            _emit(p, type="limitwait", seconds=int(wait), until=int(time.time() + wait))
            _persist(p)                          # status stays "running": a restart resumes too
            time.sleep(wait)
            with _LOCK:
                alive = _PROJECTS.get(p.id) is p  # a Clear-all during the wait removes it
            if not alive:
                return
            return _run_factory(p, resume=True)  # re-enter from the durable checkpoint
        if type(exc).__name__ == "TokenCapExceeded":
            _emit(p, type="narrate", text=f"⏹ Stopped: {exc}. Raise the token cap to go further.")
            _emit(p, type="halt", phase="token_cap", reason=str(exc))
            _emit(p, type="done", passed=False, phase="token_cap")
            p.status = "done"
            _persist(p)
            _close(p)
            return
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
    for ev in events_for_result(result_dict):
        _emit(p, **ev)
        time.sleep(_PACE)
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

    # -- sign-in ---------------------------------------------------------------

    def _signed_in(self) -> bool:
        """A valid session cookie, or the shared token a script would send.

        Accepting the token here keeps FOUNDRY_WORKSHOP_TOKEN a complete answer
        for curl and CI: they were never going to hold a cookie.
        """
        if not _auth.enabled():
            return True
        if _AUTH_TOKEN:
            provided = self.headers.get("X-Workshop-Token") or ""
            if hmac.compare_digest(provided, _AUTH_TOKEN):
                return True
        return _auth.valid_session(_auth.session_from_cookies(self.headers.get("Cookie")))

    def _client_addr(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _json_with_session(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", _auth.cookie_header(_auth.issue_session()))
        self.end_headers()
        self.wfile.write(body)

    def _login(self) -> None:
        client = self._client_addr()
        wait = _auth.throttled(client)
        if wait:
            # Same answer whatever the credentials were, so a blocked client
            # cannot use this to test a password.
            self._json(429, {"error": f"too many attempts — wait {wait}s"})
            return
        body = self._read_json()
        user = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if user != _auth.username() or not _auth.check_password(password):
            _auth.note_failure(client)
            # One message for both halves: naming which was wrong tells an
            # attacker which one to keep working on.
            self._json(401, {"error": "Wrong username or password."})
            return
        _auth.clear_failures(client)
        self._json_with_session(200, {"must_change": _auth.must_change()})

    def _change_password(self) -> None:
        body = self._read_json()
        if not _auth.check_password(str(body.get("current", ""))):
            _auth.note_failure(self._client_addr())
            self._json(401, {"error": "Current password is wrong."})
            return
        ok, problem = _auth.set_password(str(body.get("new", "")))
        if not ok:
            self._json(400, {"error": problem})
            return
        # The change bumped the token version, so this request's own cookie is
        # already stale. Hand back a fresh one rather than bouncing to login.
        self._json_with_session(200, {"ok": True})

    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        target = (_STATIC / rel).resolve()
        if not str(target).startswith(str(_STATIC.resolve())) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon",
            # A manifest served as octet-stream is silently ignored by the
            # browser, and the home-screen shortcut falls back to a screenshot.
            ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
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

        # Everything is behind the login. The sign-in page and the icons it
        # references are the exceptions — all static, all carrying no state.
        if not self._signed_in():
            if path in ("/", "/index.html", "/login.html"):
                self._serve_static("login.html")
                return
            if path in ("/icon.svg", "/icon-180.png", "/icon-192.png",
                        "/icon-512.png", "/manifest.webmanifest", "/favicon.ico"):
                self._serve_static(path)
                return
            self._json(401, {"error": "sign in"})
            return

        # Signed in, but still on the seeded password: the POST guard already
        # refuses every build, so send them somewhere that says why rather than
        # to a Workshop whose buttons all fail.
        if _auth.enabled() and _auth.must_change() and path in ("/", "/index.html"):
            self._serve_static("change-password.html")
            return

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
        if path.startswith("/api/spec/"):
            self._get_spec(path.rsplit("/", 1)[-1])
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
        if path.startswith("/api/gitzip/"):
            self._gitzip(path.rsplit("/", 1)[-1])
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
        try:
            from foundry.gates.profiles import PROFILES
            profiles = sorted(PROFILES)
        except Exception:
            profiles = []
        self._json(200, {
            "default_backend": default, "backends": list(BACKENDS),
            "default_adapter": default_adapter, "adapters": list(ADAPTERS),
            "profiles": profiles,
            "default_max_slots": default_slots, "models": models,
            "models_by_backend": models_by_backend,
        })

    # -- output browsing (the build's written code) ---------------------------------
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
                             "checks": checks, "diffs": []})
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
                         "files": files, "checks": checks, "diffs": diffs})

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
        # Export-time sidecar: ship the attested gate verdicts
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

    def _gitzip(self, pid: str) -> None:
        """Export the FLOOR LINEAGE as one git repository, zipped: each ancestor
        release becomes a tagged commit (r01..rNN) via the same deterministic
        export_lineage — the store stays the source of truth."""
        import io
        import tempfile
        import zipfile

        from foundry.lifecycle.git_export import (
            GitExportError, ReleaseExport, export_lineage,
        )

        chain = _lineage_ids(pid)
        if not chain:
            self._json(404, {"error": "no lineage"})
            return
        try:
            with tempfile.TemporaryDirectory(prefix="foundry-gitzip-") as td:
                releases = []
                for i, anc in enumerate(chain, start=1):
                    tree = _overlay_tree(anc)  # full repo AT that release
                    if not tree:
                        continue
                    tdir = Path(td) / f"r{i:02d}"
                    for rel, data in tree.items():
                        target = tdir / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                    rec = _STORE.get(anc) or {}
                    releases.append(ReleaseExport(
                        tag=f"r{i:02d}", tree_dir=tdir,
                        message=str(rec.get("request") or anc)[:200]))
                repo = Path(td) / "repo"
                export_lineage(repo, releases)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in sorted(repo.rglob("*")):
                        if f.is_file():
                            zf.write(f, f.relative_to(repo))
                payload = buf.getvalue()
        except GitExportError as exc:
            self._json(503, {"error": f"git export unavailable: {exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{pid}-git.zip"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        _p = self.path.split("?", 1)[0]
        if _p == "/api/login":
            self._login()
            return
        if _p == "/api/logout":
            self.send_response(204)
            self.send_header("Set-Cookie", _auth.cookie_header("", max_age=0))
            self.end_headers()
            return
        if not self._signed_in():
            self._json(401, {"error": "sign in"})
            return
        if _p == "/api/password":
            self._change_password()
            return
        # A seeded default password is not a password. Nothing that spends money
        # or runs generated code happens until it has been replaced.
        if _auth.enabled() and _auth.must_change() and _p != "/api/password":
            self._json(403, {"error": "change the default password first"})
            return
        return self._do_POST_inner()

    def _do_POST_inner(self) -> None:
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
             "slots": p.max_slots,
             "status": p.status, "passed": p.passed,
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

    def _get_spec(self, pid: str) -> None:
        """The saved spec + a 'what was built' digest for a build. On iterate the UI
        pre-fills the converse draft with the parent's ``spec`` (what it SHOULD be)
        and seeds the round-table with ``built`` (what was actually COMPLETED), so
        the leads reason about the remaining gap."""
        p = _PROJECTS.get(pid)
        spec = p.spec_text if p is not None else ""
        if not spec:
            rec = _STORE.get(pid)
            spec = str((rec or {}).get("spec_text") or "")
        self._json(200, {"id": pid, "spec": spec, "built": _build_summary(pid)})

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
        """Suggest an IC-pool size N from the request — count the distinct
        CANONICAL_AREAS the work is likely to touch (one IC per area). Advisory: the
        real area split is derived during the build (Wave A); this just seeds the UI."""
        body = self._read_json()
        request = str(body.get("request", ""))
        n, areas = _suggest_slots(request)
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
            mine = [e for e in p.journal
                    if role in (str(e.get("role", "")), str(e.get("agent", "")))] or p.journal
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
        # Iterate seed: on a NEW session, an inherited base spec (the parent's saved
        # spec) primes the round-table so it EXTENDS that spec instead of drafting
        # from scratch. Recorded as prior context + the current draft.
        base_spec = str(body.get("base_spec", "")).strip()
        base_built = str(body.get("base_built", "")).strip()
        if (base_spec or base_built) and not chat["history"] and not chat["spec"]:
            if base_spec:
                chat["spec"] = base_spec
                chat["history"].append(
                    {"role": "lead", "text": f"[Prior spec to extend]\n{base_spec}"}
                )
            if base_built:
                chat["history"].append(
                    {"role": "lead", "text": f"[What was already built — focus the "
                                             f"new work on the gaps]\n{base_built}"}
                )
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
        # Gated/autonomous modes and domain packs were removed. Refuse them instead of
        # quietly building with a weaker trust posture than the caller asked for.
        if str(body.get("mode") or "assistant") != "assistant" or body.get("packs"):
            self._json(400, {"error": "only assistant mode is supported; domain packs "
                                      "were removed"})
            return
        request = str(body.get("request", "")).strip() or "build a tiny URL-shortener service"
        backend = str(body.get("backend", "mock"))
        if backend not in BACKENDS:
            backend = "mock"
        # The adapter is DERIVED from the backend when not explicitly provided (CLI
        # parity: mock→stub, any real backend→raw-react), so a real backend writes
        # real code without a second field. An explicit valid adapter still wins.
        adapter = str(body.get("adapter", "")).strip()
        if adapter not in ADAPTERS:
            adapter = _default_adapter_for(backend)
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
            max_fix_attempts = int(body.get("max_fix_attempts", 2))
        except (TypeError, ValueError):
            max_fix_attempts = 2
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
        workers = parse_workers(body.get("workers"))
        ticket_tiers = parse_ticket_tiers(body.get("ticket_tiers"))
        tok_env = os.environ.get("FOUNDRY_TOKEN_CAP", "").strip()
        token_cap = int(tok_env) if tok_env.isdigit() else None
        try:
            tok = int(body.get("token_cap") or 0)
            if tok > 0:
                token_cap = tok
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
        # Spec text is SAVED per build and INHERITED on iterate: a new release with
        # no fresh spec reuses the parent's saved spec; with a fresh spec (the
        # converse draft is pre-filled from the parent, so it already carries the
        # prior spec PLUS this round's additions) that extended draft supersedes it.
        # Either way the release's spec_text is the cumulative spec, saved for the
        # NEXT iterate to build on.
        parent_spec_text = str(parent.get("spec_text") or "") if parent else ""
        effective_spec_text = spec_text or parent_spec_text
        # Spec input: the effective spec TEXT (literal), else an uploaded docs-zip (a
        # dir), else None (request alone).
        spec: str | None = effective_spec_text or None
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
            pcode = pbase / "code"
            if pcode.is_dir():
                base_dir = str(pcode)        # seed the base tree with the prior code
        p = Project(
            id=pid, request=request, mode="assistant", floor=floor,
            release_seq=release_seq, backend=backend, adapter=adapter, spec=spec,
            spec_text=effective_spec_text,
            out_base=out_base, max_slots=max_slots, cost_cap_microusd=cost_cap_microusd,
            token_cap=token_cap, workers=workers, ticket_tiers=ticket_tiers,
            max_fix_attempts=max_fix_attempts, parent_id=parent_id, base_dir=base_dir,
        )
        # Verification profile / stack: "auto" (default) infers from the request;
        # a named profile pins the gate commands (validated by run(), which
        # rejects unknown names fail-closed before any model spend).
        prof = str(body.get("profile") or "auto").strip().lower()
        p.profile = prof or "auto"
        with _LOCK:
            _PROJECTS[pid] = p
        # NOTE: do NOT catalog the build here. A floor is persisted only once the
        # build ACTUALLY starts — the runner's first `project` event (in _PERSIST_ON)
        # upserts it. So a created-but-never-run project leaves no floor to restore
        # on restart (the response below carries the floor/release in-memory anyway).
        threading.Thread(target=_gated_runner, args=(_run_factory, p), daemon=True).start()
        self._json(200, {"id": pid, "floor": floor, "release": release_seq})

    def _reset(self) -> None:
        """Clear ALL floors & repos: drop the in-memory floors, reset the floor
        counter, and wipe the durable catalog (builds + event logs + seen-uris) so a
        restart rehydrates nothing. Behind the mutation guard. The content-addressed
        store on disk is left as-is — it is unreferenced once the catalog is empty.

        REFUSES while a build is still active: an in-flight build holds its own
        Project reference and keeps writing to the catalog on each emit, so wiping
        mid-build would resurrect that build's row (and the build would become
        undecidable — gone from _PROJECTS but still running). Fail-closed: wait for
        builds to finish (or reload) before clearing."""
        with _LOCK:
            active = [
                p.id for p in _PROJECTS.values()
                if p.status in ("running", "queued") and not p.ended
            ]
            if active:
                self._json(409, {
                    "error": f"{len(active)} build(s) still running — let them "
                             "finish (or reload), then clear.",
                    "running": active,
                })
                return
            _PROJECTS.clear()
            _FLOOR[0] = 0
        try:
            _STORE.clear_all()
        except Exception:
            pass
        with _REPO_CACHE_LOCK:
            _REPO_CACHE.clear()
        self._json(200, {"ok": True})

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
                    # Named event (not a comment frame) so the CLIENT can see the
                    # heartbeat too and show a live/stalled indicator. No `id:` —
                    # it must not advance the Last-Event-ID resume cursor.
                    self.wfile.write(b"event: ka\ndata: {}\n\n")
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
    if _auth.enabled():
        _auth.ensure_seeded()
        if _auth.must_change():
            print(f"\n  🔑 Sign in as {_auth.username()!r} / {_auth.SEED_PASSWORD!r} — "
                  "you'll be made to change it before any build can start.\n")
    else:
        print("\n  ⚠  FOUNDRY_AUTH=off — the Workshop is open to anyone who can "
              "reach this port, and it spends money.\n")
    _load_persisted()   # rehydrate floors (+ resume any interrupted build) from disk
    if _LOG_SWEEP_S > 0:   # bound RAM on a long-lived server: trim finished logs
        threading.Thread(target=_log_sweeper, daemon=True).start()
    class _QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):  # noqa: N802 (stdlib API)
            # Phones/tabs drop SSE streams constantly (backgrounding, reload) —
            # stdlib prints a full traceback per dropped socket, which reads like
            # a server crash. Swallow disconnects; real errors still print.
            exc = sys.exc_info()[1]
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    srv = _QuietServer((host, port), Handler)
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
