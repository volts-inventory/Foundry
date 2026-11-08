"""What a floor's build cost, why it stopped, and how to run what it shipped.

Server-side helpers for the Workshop's insight panels, kept out of server.py:

* request bodies: a size cap and a real error for bad JSON;
* the live meter: tokens used / cap and elapsed time, read off the run's token
  ledger (``<state_dir>/calls/<run>/tokens.json``) while it builds;
* the build queue: FIFO positions for floors waiting on a build slot;
* the halt report: the cause in plain words per phase, the failing gates with their
  findings, the fixer's notes and attempts, and the one-click next step;
* the run card: stack, install / run / test commands, models, tokens and tickets,
  from the BUILD-MANIFEST (falling back to the durable state's stack decision).

Nothing here drives a build; it reads what the factory already recorded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

# --- request bodies ------------------------------------------------------------ #


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# The one cap on every POST body, refused on its Content-Length before it is read.
# A full-size spec (settings.SPEC_MAX_BYTES, 200 KB) fits with room for JSON escaping.
MAX_BODY_BYTES = _int_env("WORKSHOP_MAX_BODY_BYTES", 1024 * 1024)


class BodyError(ValueError):
    """A request body the server refuses: ``status`` is the HTTP code to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def read_json_body(headers: Any, rfile: Any, limit: int | None = None) -> dict[str, Any]:
    """The request's JSON object; ``{}`` for an empty body. Raises :class:`BodyError`
    (413 over ``limit`` — the body is left unread; 400 for anything malformed)."""
    cap = MAX_BODY_BYTES if limit is None else limit
    raw = (headers.get("Content-Length") or "").strip()
    if not raw:
        return {}
    if not raw.isdigit():
        raise BodyError(400, "invalid Content-Length header")
    n = int(raw)
    if n == 0:
        return {}
    if n > cap:
        raise BodyError(413, f"request body too large: {n:,} bytes (the limit is {cap:,})")
    data = rfile.read(n)
    try:
        obj = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise BodyError(400, "request body is not valid UTF-8") from None
    except json.JSONDecodeError as exc:
        raise BodyError(
            400, f"invalid JSON in request body: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from None
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise BodyError(400, "request body must be a JSON object")
    return obj


# --- the live meter ------------------------------------------------------------ #


def ledger_path(state_dir: str | Path, thread_id: str) -> Path:
    """Where ``foundry.cli.run`` keeps the run's token ledger (see its ``run_dir``)."""
    run_key = hashlib.sha256(thread_id.encode()).hexdigest()[:16]
    return Path(state_dir) / "calls" / run_key / "tokens.json"


def read_ledger(state_dir: str | Path, thread_id: str) -> tuple[int, float]:
    """(tokens used, elapsed seconds) the ledger holds; zeros when there is none yet."""
    try:
        saved = json.loads(ledger_path(state_dir, thread_id).read_text())
        return int(saved.get("used", 0)), float(saved.get("elapsed_s", 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0.0


def meter_fields(
    used: int, cap: int | None, elapsed_s: float, time_cap_s: float | None = None
) -> dict[str, Any]:
    """The meter as the browser reads it. ``at`` (epoch seconds) lets a client tick
    the clock forward from an event it replays later."""
    return {
        "used": int(used),
        "cap": cap,
        "elapsed_s": round(float(elapsed_s), 1),
        "time_cap_s": time_cap_s,
        "at": round(time.time(), 1),
    }


class MeterWatch:
    """Polls a running build's token ledger and emits a ``meter`` event when the
    spend moves (at most one per ``min_gap`` seconds). Elapsed time is counted here
    too: the ledger only saves it when a call is charged."""

    def __init__(
        self,
        state_dir: str | Path,
        thread_id: str,
        emit: Callable[[dict[str, Any]], None],
        caps: Callable[[], tuple[int | None, float | None]],
        interval: float = 2.0,
        min_gap: float = 5.0,
    ) -> None:
        self.state_dir, self.thread_id = state_dir, thread_id
        self.emit, self.caps = emit, caps
        self.interval, self.min_gap = interval, min_gap
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        used, elapsed = read_ledger(state_dir, thread_id)
        self._base, self._t0 = elapsed, time.monotonic()
        self._last_used = used

    def snapshot(self) -> dict[str, Any]:
        used, ledger_elapsed = read_ledger(self.state_dir, self.thread_id)
        elapsed = max(ledger_elapsed, self._base + time.monotonic() - self._t0)
        cap, time_cap = self.caps()
        return meter_fields(used, cap, elapsed, time_cap)

    def start(self) -> None:
        self.emit({"type": "meter", **self.snapshot()})
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"meter-{self.thread_id}"
        )
        self._thread.start()

    def _loop(self) -> None:
        last_emit = time.monotonic()
        while not self._stop.wait(self.interval):
            snap = self.snapshot()
            if snap["used"] == self._last_used or time.monotonic() - last_emit < self.min_gap:
                continue
            if self._stop.is_set():
                return
            self._last_used, last_emit = snap["used"], time.monotonic()
            self.emit({"type": "meter", **snap})

    def stop(self) -> dict[str, Any]:
        """Stop polling (waiting out an emit in flight) and return the final reading."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        return self.snapshot()


_WATCHES: dict[str, MeterWatch] = {}
_WATCH_LOCK = threading.Lock()


def start_meter(pid: str, watch: MeterWatch) -> None:
    with _WATCH_LOCK:
        old = _WATCHES.pop(pid, None)
        _WATCHES[pid] = watch
    if old is not None:
        old.stop()
    watch.start()


def finish_meter(
    pid: str, state_dir: str | Path, cap: int | None, time_cap_s: float | None = None
) -> dict[str, Any]:
    """The build's final meter reading; stops its watcher if one is running, so no
    meter event can land after the floor's ``done``."""
    with _WATCH_LOCK:
        watch = _WATCHES.pop(pid, None)
    if watch is not None:
        return watch.stop()
    used, elapsed = read_ledger(state_dir, pid)
    return meter_fields(used, cap, elapsed, time_cap_s)


def last_meter(log: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The newest meter reading in a floor's event log (a ``meter`` event, or the
    reading a ``done`` carries)."""
    found: dict[str, Any] | None = None
    for e in log:
        if e.get("type") == "meter":
            found = {k: v for k, v in e.items() if k != "type"}
        elif e.get("type") == "done" and isinstance(e.get("meter"), Mapping):
            found = dict(e["meter"])
    return found


# --- the build queue ----------------------------------------------------------- #


class BuildQueue:
    """Floors waiting for a build slot, first come first served."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._told: dict[str, int] = {}
        self._lock = threading.Lock()

    def join(self, pid: str) -> int:
        with self._lock:
            if pid not in self._ids:
                self._ids.append(pid)
            return self._ids.index(pid) + 1

    def leave(self, pid: str) -> None:
        with self._lock:
            if pid in self._ids:
                self._ids.remove(pid)
            self._told.pop(pid, None)

    def head(self) -> str | None:
        with self._lock:
            return self._ids[0] if self._ids else None

    def position(self, pid: str) -> int:
        """1-based place in line; 0 when the floor isn't waiting."""
        with self._lock:
            return self._ids.index(pid) + 1 if pid in self._ids else 0

    def changed(self) -> list[tuple[str, int]]:
        """(pid, position) for every waiting floor whose position moved since it was
        last told — what to announce after someone joins or leaves the line."""
        with self._lock:
            out = [(pid, i + 1) for i, pid in enumerate(self._ids) if self._told.get(pid) != i + 1]
            for pid, pos in out:
                self._told[pid] = pos
            return out


QUEUE = BuildQueue()


# --- reading the durable state --------------------------------------------------- #


def durable_state(state_dir: str | Path, thread_id: str) -> Mapping[str, Any] | None:
    """The build's latest checkpointed state (read-only), or ``None``."""
    try:
        from foundry.durability.checkpointer import FoundryCheckpointer

        db = Path(state_dir) / "checkpoints.db"
        if not db.exists():
            return None
        ckpt = FoundryCheckpointer(str(db))
        try:
            tup = ckpt.saver.get_tuple({"configurable": {"thread_id": thread_id}})
        finally:
            ckpt.close()
        if tup is None:
            return None
        values = tup.checkpoint.get("channel_values") or {}
        return values if isinstance(values, Mapping) else None
    except Exception:
        return None


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _status(s: Any) -> str:
    return str(getattr(s, "value", s)).split(".")[-1].lower()


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split()) if n <= 240 else str(text or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_MAX_FINDINGS = 6
_FINDING_CHARS = 600
_NOTE_CHARS = 400
_MAX_NOTES = 6
MAX_FIX_ATTEMPTS = 20

# Triage notes that explain what the fixer and the ticket workers ran into.
_FIX_NOTES = frozenset(
    {
        "ticket-failed",
        "token-cap",
        "ticket-skipped",
        "fix-scope",
        "write-scope",
        "wave-overlay",
        "contract-change",
    }
)


def gate_rows(
    state: Mapping[str, Any] | None, fallback: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The newest verdict per gate with its findings (truncated), from the durable
    state; the floor's streamed gate pills when there is no state to read."""
    newest: dict[str, Any] = {}
    for g in (state or {}).get("gates") or ():
        name = str(_attr(g, "name", ""))
        if name and (name not in newest or _attr(g, "seq") > _attr(newest[name], "seq")):
            newest[name] = g
    if not newest:
        return [
            {
                "name": n,
                "status": _status(v.get("status")),
                "advisory": bool(v.get("advisory")),
                "findings": [],
                "more": 0,
            }
            for n, v in fallback.items()
        ]
    rows = []
    for name, g in newest.items():
        findings = [str(f) for f in (_attr(g, "findings") or ()) if str(f).strip()]
        rows.append(
            {
                "name": name,
                "status": _status(_attr(g, "status")),
                "advisory": bool(_attr(g, "advisory", False)),
                "findings": [_clip(f, _FINDING_CHARS) for f in findings[:_MAX_FINDINGS]],
                "more": max(0, len(findings) - _MAX_FINDINGS),
            }
        )
    return rows


def fix_notes(state: Mapping[str, Any] | None, failing: Iterable[str]) -> list[dict[str, str]]:
    """The newest triage notes about the failing gates and the fix work, oldest first."""
    names = set(failing) | _FIX_NOTES
    notes = [
        t
        for t in (state or {}).get("gate_triage") or ()
        if str(_attr(t, "name", "")) in names or "fix" in str(_attr(t, "produced_by", ""))
    ]
    notes.sort(key=lambda t: _attr(t, "seq"))
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for t in notes:  # the same note repeated by each pass reads once, at its newest spot
        row = {
            "name": str(_attr(t, "name", "")),
            "by": str(_attr(t, "produced_by", "")),
            "text": _clip(_attr(t, "explanation", ""), _NOTE_CHARS),
        }
        key = (row["name"], row["text"])
        rows.pop(key, None)
        rows[key] = row
    return list(rows.values())[-_MAX_NOTES:]


# --- the halt report ----------------------------------------------------------- #


def _nice_cap(n: int) -> int:
    step = 10_000 if n >= 10_000 else 1_000
    return -(-n // step) * step


def _nice_minutes(m: float) -> int:
    return max(15, -(-int(m + 0.999) // 15) * 15)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _minutes(s: float) -> str:
    m, sec = divmod(int(s), 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {sec:02d}s"


def _first_line(reason: str) -> str:
    for line in str(reason or "").splitlines():
        line = line.strip()
        if line and not line.startswith("Traceback"):
            return _clip(line, 240)
    return "an unexpected error"


def halt_report(
    *,
    phase: str,
    reason: str,
    token_cap: int | None,
    max_fix_attempts: int,
    resumable: bool,
    state: Mapping[str, Any] | None,
    gates: Mapping[str, Mapping[str, Any]],
    meter: Mapping[str, Any] | None,
    draft_files: int,
    time_cap_s: float | None = None,
) -> dict[str, Any]:
    """Why a floor ended without shipping, in plain words, and what to do next.

    ``actions`` are what the panel's buttons do: ``resume`` posts ``body`` to the
    floor's resume route, ``retry`` opens the Retry-with form, ``files`` opens the
    draft. A raw error message is kept in ``detail``, never in the headline."""
    rows = gate_rows(state, gates)
    failing = [r for r in rows if r["status"] != "pass" and not r["advisory"]]
    failing_names = [r["name"] for r in failing]
    budget = (state or {}).get("budget")
    fixes = _attr(budget, "fix_attempts_total")
    used = int((meter or {}).get("used") or 0)
    elapsed = float((meter or {}).get("elapsed_s") or 0.0)
    actions: list[dict[str, Any]] = []
    detail = ""

    def resume(label: str, body: Mapping[str, Any] | None = None, primary: bool = True) -> None:
        actions.append(
            {"kind": "resume", "label": label, "body": dict(body or {}), "primary": primary}
        )

    if phase in ("stopped", "paused"):
        verb = "paused" if phase == "paused" else "stopped"
        title = "Paused" if phase == "paused" else "Stopped"
        cause = f"You {verb} this build. It halted at a clean checkpoint; nothing is lost."
        nxt = "Resume picks up exactly where it left off."
        resume("Resume")
    elif phase == "token_cap":
        title = "Out of tokens"
        cap = token_cap or used
        cause = (
            f"It used {used:,} of its {cap:,}-token cap and stopped at a clean checkpoint."
            if cap
            else "It reached its token cap and stopped at a clean checkpoint."
        )
        new_cap = _nice_cap(max(cap * 2, used + cap, 10_000))
        nxt = "Raise the cap and resume — the tokens already spent carry over."
        resume(f"Raise the token cap to {new_cap:,} and resume", {"token_cap": new_cap})
        resume("Resume with no token cap", {"token_cap": 0}, primary=False)
    elif phase == "time_cap":
        title = "Out of time"
        cause = (
            f"It ran for {_minutes(elapsed)} and reached its time cap, so it stopped "
            "at a clean checkpoint."
            if elapsed
            else "It reached its time cap and stopped at a clean checkpoint."
        )
        nxt = "The time spent carries over, so raise the cap past it and resume."
        detail = str(reason or "")
        cap_min = (time_cap_s or elapsed) / 60
        new_min = _nice_minutes(max(cap_min * 2, elapsed / 60 + max(cap_min, 15)))
        resume(f"Raise the time cap to {new_min} minutes and resume", {"time_cap_minutes": new_min})
        resume("Resume with no time cap", {"time_cap_minutes": 0}, primary=False)
    elif phase == "error":
        title = "Build error"
        cause = f"The build stopped on an error: {_first_line(reason)}"
        nxt = (
            "Resume from the last checkpoint; if the same error comes back, the "
            "details below say where it came from."
        )
        detail = str(reason or "")
        resume("Resume from the last checkpoint")
    elif phase == "interrupted":
        title = "Interrupted"
        cause = "The server restarted while this build was running."
        nxt = (
            "Resume continues from its last checkpoint."
            if resumable
            else "It never reached a checkpoint, so it can't be resumed — start it again "
            "as a new build."
        )
        resume("Resume")
    else:
        title = "Held at the gate"
        tried = (
            f" after {_plural(int(fixes), 'fix attempt')}"
            if isinstance(fixes, int) and not isinstance(fixes, bool) and fixes
            else ""
        )
        if failing_names:
            what = ", ".join(n.upper() for n in failing_names)
            cause = (
                f"The {what} gate{'s' if len(failing_names) > 1 else ''} did not pass"
                f"{tried}, so nothing shipped."
            )
        else:
            cause = f"A gate did not pass{tried}, so nothing shipped."
        more = min(MAX_FIX_ATTEMPTS, max_fix_attempts + 3)
        if more > max_fix_attempts:
            nxt = (
                "Give the fixer more attempts: it resumes at the failing check with the "
                "findings below."
            )
            resume(f"Resume with more fix attempts ({more})", {"max_fix_attempts": more})
        else:
            nxt = (
                "The fix budget is already at its maximum — read the findings below, "
                "then retry or rephrase the request."
            )
            resume("Resume", {})
    if not resumable:
        actions = []
        if phase not in ("interrupted",):
            nxt = "This build can't be resumed (it predates resume or has no checkpoint)."
    else:
        actions.append({"kind": "retry", "label": "Retry with…", "primary": False})
    if draft_files:
        actions.append(
            {
                "kind": "files",
                "primary": False,
                "label": f"Open the draft ({_plural(draft_files, 'file')})",
            }
        )
    return {
        "phase": phase,
        "title": title,
        "cause": cause,
        "next": nxt,
        "detail": detail,
        "gates": failing,
        "passed_gates": [r["name"] for r in rows if r["status"] == "pass"],
        "fixes": {"attempts": fixes if isinstance(fixes, int) else None, "limit": max_fix_attempts},
        "notes": fix_notes(state, failing_names),
        "meter": dict(meter) if meter else None,
        "draft_files": draft_files,
        "resumable": resumable,
        "actions": actions,
    }


# --- the run card -------------------------------------------------------------- #


def find_manifest(dirs: Iterable[Path | None], thread_id: str) -> dict[str, Any] | None:
    """The BUILD-MANIFEST.json written for ``thread_id`` in the first of ``dirs`` that
    has one. A manifest from another run (a reused floor directory) is ignored."""
    for d in dirs:
        if d is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            m = json.loads((d / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("thread_id") == thread_id:
                return m
    return None


def tickets_from_log(log: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The floor's tickets with their final status, from its event log."""
    rows: dict[str, dict[str, str]] = {}
    for e in log:
        t = e.get("type")
        if t == "ticket":
            tid = str(e.get("id", ""))
            if e.get("retired"):
                rows.pop(tid, None)
                continue
            row = rows.setdefault(tid, {"id": tid, "title": "", "status": "planned"})
            row["title"] = str(e.get("title") or row["title"])
        elif t == "task":
            tid = str(e.get("ticket_id", ""))
            row = rows.setdefault(
                tid, {"id": tid, "title": str(e.get("title", "")), "status": "planned"}
            )
            row["status"] = {"start": "in progress", "done": "done", "failed": "failed"}.get(
                str(e.get("phase")), row["status"]
            )
    return [r for r in rows.values() if r["id"]]


_STEPS = (("install", "Install"), ("build", "Build"), ("run", "Run"), ("test", "Test"))


def run_card(
    *,
    manifest: Mapping[str, Any] | None,
    stack: Any,
    tickets: list[dict[str, str]],
    meter: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """How to run what shipped, and what it took to build it."""
    st: Mapping[str, Any] = (manifest or {}).get("stack") or {}
    if not st and stack is not None:
        st = {
            k: str(v)
            for k, v in (
                stack.model_dump() if hasattr(stack, "model_dump") else dict(stack)
            ).items()
            if v
        }
    steps = [{"label": label, "cmd": str(st[key])} for key, label in _STEPS if st.get(key)]
    usage: Mapping[str, Any] = (manifest or {}).get("model_usage") or {}
    tokens_in = int(usage.get("total_tokens_in", 0)) + int(usage.get("agent_tokens_in", 0))
    tokens_out = int(usage.get("total_tokens_out", 0)) + int(usage.get("agent_tokens_out", 0))
    return {
        "source": "manifest" if manifest else ("state" if st else "none"),
        "stack": " · ".join(
            str(st[k]) for k in ("language", "framework", "package_manager") if st.get(k)
        ),
        "steps": steps,
        "models": {str(k): list(v) for k, v in (usage.get("models_by_tier") or {}).items()},
        "served_models": list(usage.get("served_models") or ()),
        "calls": int(usage.get("model_calls", 0)),
        "sessions": int(usage.get("agent_sessions", 0)),
        "tokens": {
            "in": tokens_in,
            "out": tokens_out,
            "cache_read": int(usage.get("total_cache_read_tokens", 0))
            + int(usage.get("agent_cache_read_tokens", 0)),
            "cache_write": int(usage.get("total_cache_creation_tokens", 0))
            + int(usage.get("agent_cache_creation_tokens", 0)),
        },
        "cost_usd": round(int((manifest or {}).get("estimated_cost_microusd") or 0) / 1e6, 4),
        "tickets": tickets,
        "gates": [
            {"name": str(g.get("name")), "status": str(g.get("status"))}
            for g in (manifest or {}).get("gates") or ()
        ],
        "meter": dict(meter) if meter else None,
        "generated_at": str((manifest or {}).get("generated_at") or ""),
    }
