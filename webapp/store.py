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
  supervised       INTEGER,
  slots            INTEGER,
  max_fix_attempts INTEGER,
  status           TEXT,
  passed           INTEGER,
  gates            TEXT,   -- json: {name: {status, attested, advisory}}
  out_base         TEXT,
  created_at       TEXT
);
"""

_COLS = (
    "id", "floor", "release_seq", "parent_id", "request", "mode", "backend",
    "supervised", "slots", "max_fix_attempts", "status", "passed", "gates",
    "out_base", "created_at",
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
            self._conn.commit()

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
