"""Durable build catalog — a tiny SQLite registry of every floor's build.

The per-floor OUTPUT already persists to disk (``_OUTPUT_ROOT/floor-N``); this
catalogs each build's METADATA + a reference to that repo, so the Workshop can:

* rehydrate the elevator / OUTPUT / HUD gate pills after a server restart, and
* let a NEW floor be started *from an existing finished build* (lineage via
  ``parent_id``) — the parent's repo is pulled straight from here.

Dependency-free (stdlib ``sqlite3``, WAL) to match the project's dev-grade
single-host durability (cf. ``foundry/durability/checkpointer.py``). The code
itself stays on disk under ``out_base``; this table is the index that points at
it (``pull the referenced repo from the database`` = look up the row, read its
``out_base``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
  id               TEXT PRIMARY KEY,
  floor            INTEGER,   -- the PROJECT (stable across releases)
  release_seq      INTEGER,   -- 1,2,3… within the floor (each iterate = +1)
  parent_id        TEXT,
  request          TEXT,
  mode             TEXT,
  backend          TEXT,
  adapter          TEXT,   -- coding substrate (stub/raw-react)
  supervised       INTEGER,
  slots            INTEGER,
  max_fix_attempts INTEGER,
  status           TEXT,
  passed           INTEGER,
  gates            TEXT,   -- json: {name: {status, attested, advisory}}
  out_base         TEXT,
  created_at       TEXT
);
-- The append-only game-event log per build (the SSE stream's durable backing).
-- Persisting it lets ANY device replay a build's whole story after a reconnect or
-- a server restart, and lets many viewers each tail the same log independently.
CREATE TABLE IF NOT EXISTS events (
  project_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,   -- 0,1,2… the event's index within the build
  data        TEXT NOT NULL,      -- json of the event dict
  PRIMARY KEY (project_id, seq)
);
-- The set of artifact handles a build has already surfaced. Persisted so a build
-- RESUMED after a restart doesn't re-emit its whole history (the resumed run's
-- cumulative inspection payload re-lists every prior artifact; this dedups them).
CREATE TABLE IF NOT EXISTS seen_uris (
  project_id  TEXT NOT NULL,
  uri         TEXT NOT NULL,
  PRIMARY KEY (project_id, uri)
);
"""

_COLS = (
    "id", "floor", "release_seq", "parent_id", "request", "mode", "backend",
    "adapter", "supervised", "slots", "max_fix_attempts", "status", "passed",
    "gates", "out_base", "created_at",
)


class BuildStore:
    """A single SQLite file cataloguing builds. Thread-safe (one lock, WAL)."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (CREATE TABLE IF NOT
        EXISTS never alters an existing table). Idempotent; runs under the lock."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(builds)")}
        if "adapter" not in have:
            self._conn.execute("ALTER TABLE builds ADD COLUMN adapter TEXT")

    def upsert(self, rec: dict[str, Any]) -> None:
        """Insert or replace one build row. ``gates`` is JSON-encoded."""
        row = {k: rec.get(k) for k in _COLS}
        row["gates"] = json.dumps(rec.get("gates") or {})
        row["passed"] = 1 if rec.get("passed") else 0
        row["supervised"] = 1 if rec.get("supervised") else 0
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO builds ({','.join(_COLS)}) "
                f"VALUES ({','.join('?' for _ in _COLS)})",
                [row[k] for k in _COLS],
            )
            self._conn.commit()

    def _decode(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        try:
            d["gates"] = json.loads(d.get("gates") or "{}")
        except (TypeError, ValueError):
            d["gates"] = {}
        d["passed"] = bool(d.get("passed"))
        d["supervised"] = bool(d.get("supervised"))
        return d

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM builds ORDER BY floor ASC"
            ).fetchall()
        return [self._decode(r) for r in rows]

    def latest_per_floor(self) -> list[dict[str, Any]]:
        """The newest release of every floor (one row per project) — for rehydrate."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM builds b WHERE release_seq = "
                "(SELECT MAX(release_seq) FROM builds WHERE floor = b.floor) "
                "ORDER BY floor ASC"
            ).fetchall()
        return [self._decode(r) for r in rows]

    def finished(self) -> list[dict[str, Any]]:
        """The newest FINISHED release of every floor (for the 'iterate on…' picker)
        — you iterate on a PROJECT, taking its latest passed release as the base."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM builds b WHERE status = 'done' AND release_seq = "
                "(SELECT MAX(release_seq) FROM builds WHERE floor = b.floor AND status = 'done') "
                "ORDER BY floor DESC"
            ).fetchall()
        return [self._decode(r) for r in rows]

    def releases_of(self, floor: int) -> list[dict[str, Any]]:
        """All releases of one floor, newest first (for the release history view)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM builds WHERE floor = ? ORDER BY release_seq DESC", (floor,)
            ).fetchall()
        return [self._decode(r) for r in rows]

    def get(self, build_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM builds WHERE id = ?", (build_id,)
            ).fetchone()
        return self._decode(r) if r else None

    def max_floor(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT MAX(floor) AS m FROM builds").fetchone()
        return int(r["m"]) if r and r["m"] is not None else 0

    def max_release(self, floor: int) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT MAX(release_seq) AS m FROM builds WHERE floor = ?", (floor,)
            ).fetchone()
        return int(r["m"]) if r and r["m"] is not None else 0

    # ---- per-build event log (the durable SSE backing) ----------------------
    def append_event(self, project_id: str, seq: int, data: dict[str, Any]) -> None:
        """Append one game event at index ``seq``. Idempotent on (project_id, seq)
        — a replayed/duplicate emit (e.g. a resume) coalesces instead of forking."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO events (project_id, seq, data) VALUES (?, ?, ?)",
                (project_id, seq, json.dumps(data)),
            )
            self._conn.commit()

    def events_of(self, project_id: str) -> list[dict[str, Any]]:
        """Every event for a build, in order — the full replay log."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM events WHERE project_id = ? ORDER BY seq ASC",
                (project_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except (TypeError, ValueError):
                continue
        return out

    def add_seen(self, project_id: str, uris: set[str]) -> None:
        """Record the artifact handles a build has surfaced (idempotent)."""
        if not uris:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_uris (project_id, uri) VALUES (?, ?)",
                [(project_id, u) for u in uris],
            )
            self._conn.commit()

    def seen_of(self, project_id: str) -> set[str]:
        """The handles a build already surfaced — seeded into a resumed run so it
        doesn't re-emit them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT uri FROM seen_uris WHERE project_id = ?", (project_id,)
            ).fetchall()
        return {r["uri"] for r in rows}
