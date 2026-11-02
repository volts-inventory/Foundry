"""The Workshop's build catalog (webapp.store.BuildStore).

Covers the 'clear all floors & repos' reset: clear_all wipes every catalogued
build, its event log, and its seen-uri set so a restart rehydrates nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from webapp.store import BuildStore


def _rec(build_id: str, floor: int) -> dict:
    return {
        "id": build_id, "floor": floor, "request": "build a thing", "mode": "assistant",
        "backend": "mock", "adapter": "stub", "slots": 1, "status": "done",
        "passed": True, "gates": {"build": {"status": "PASS"}},
        "out_base": None, "created_at": "2026-06-30",
    }


def test_rows_from_the_iterate_era_still_load(tmp_path: Path) -> None:
    # A DB written before iterate/spec upload were removed carries extra columns.
    path = tmp_path / "foundry.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE builds (id TEXT PRIMARY KEY, floor INTEGER, release_seq INTEGER, "
        "parent_id TEXT, request TEXT, mode TEXT, backend TEXT, adapter TEXT, "
        "supervised INTEGER, slots INTEGER, max_fix_attempts INTEGER, status TEXT, "
        "passed INTEGER, gates TEXT, out_base TEXT, created_at TEXT, spec_text TEXT)"
    )
    conn.execute(
        "INSERT INTO builds VALUES ('old', 1, 2, 'par', 'req', 'assistant', 'mock', 'stub', "
        "0, 1, 3, 'done', 1, '{}', NULL, '2026-01-01', 'spec')"
    )
    conn.commit()
    conn.close()
    store = BuildStore(path)
    store.upsert(_rec("new", 2))
    assert [r["id"] for r in store.latest_per_floor()] == ["old", "new"]
    assert [r["id"] for r in store.finished()] == ["new", "old"]
    assert store.get("old")["request"] == "req"


def test_clear_all_wipes_builds_events_and_seen(tmp_path: Path) -> None:
    store = BuildStore(tmp_path / "foundry.db")
    store.upsert(_rec("a", 1))
    store.upsert(_rec("b", 2))
    store.append_event("a", 0, {"type": "project", "id": "a"})
    store.add_seen("a", {"cas://x", "cas://y"})

    # Catalogued before the reset.
    assert {r["id"] for r in store.latest_per_floor()} == {"a", "b"}
    assert store.events_of("a") and store.seen_of("a")
    assert store.max_floor() == 2

    store.clear_all()

    # Nothing survives — a restart would rehydrate no floors.
    assert store.latest_per_floor() == []
    assert store.events_of("a") == []
    assert store.seen_of("a") == set()
    assert store.max_floor() == 0
    assert store.latest_per_floor() == []
