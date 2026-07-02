"""The Workshop's build catalog (webapp.store.BuildStore).

Covers the 'clear all floors & repos' reset: clear_all wipes every catalogued
build, its event log, and its seen-uri set so a restart rehydrates nothing.
"""

from __future__ import annotations

from pathlib import Path

from webapp.store import BuildStore


def _rec(build_id: str, floor: int, *, spec_text: str = "") -> dict:
    return {
        "id": build_id, "floor": floor, "release_seq": 1, "parent_id": None,
        "request": "build a thing", "mode": "assistant", "backend": "mock",
        "adapter": "stub", "supervised": True, "slots": 1, "max_fix_attempts": 5,
        "status": "done", "passed": True, "gates": {"build": {"status": "PASS"}},
        "out_base": None, "created_at": "2026-06-30", "spec_text": spec_text,
    }


def test_spec_text_round_trips(tmp_path: Path) -> None:
    # The saved spec is what an iterate inherits + extends, so it must persist.
    store = BuildStore(tmp_path / "foundry.db")
    spec = "# Spec\n\nGoals: a moon-colony farming loop.\nAcceptance: day/night cycle."
    store.upsert(_rec("r1", 1, spec_text=spec))
    assert store.get("r1")["spec_text"] == spec
    assert store.latest_per_floor()[0]["spec_text"] == spec


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
