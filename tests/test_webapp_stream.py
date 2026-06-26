"""The Workshop's live-progress backing: a retained, broadcastable event log.

These cover the W1 fix — replacing the single per-project queue (which let two
streams steal each other's events) with an append-only log that many viewers tail
independently, that survives reconnect/restart via the durable store, and whose
checkpoint decisions are first-wins + idempotent so multiple devices can drive one
build without a stale click leaking into the next pause.
"""

from __future__ import annotations

import threading

import pytest

from webapp import server
from webapp.server import (
    Project,
    _await_decision,
    _close,
    _emit,
    _record_decision,
    _resume_cursor,
)
from webapp.store import BuildStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway BuildStore so _emit's durable append doesn't touch real state."""
    s = BuildStore(tmp_path / "foundry.db")
    monkeypatch.setattr(server, "_STORE", s)
    return s


def _project(**kw):
    return Project(id="p1", request="r", mode="assistant", supervised=True, floor=1, **kw)


# --- the durable event log --------------------------------------------------- #

def test_store_event_log_roundtrip_and_idempotent(tmp_path):
    s = BuildStore(tmp_path / "db.sqlite")
    s.append_event("p1", 0, {"type": "stage", "room": "product"})
    s.append_event("p1", 1, {"type": "gate", "name": "spec"})
    s.append_event("p2", 0, {"type": "stage"})           # a different build
    s.append_event("p1", 1, {"type": "DIFFERENT"})        # replay of seq 1 — ignored
    log = s.events_of("p1")
    assert [e["type"] for e in log] == ["stage", "gate"]  # order + idempotency held
    assert s.events_of("p2") == [{"type": "stage"}]


def test_emit_appends_to_log_persists_and_marks_end(store):
    p = _project()
    _emit(p, type="stage", room="product")
    _emit(p, type="gate", name="spec", status="pass")
    assert [e["type"] for e in p.log] == ["stage", "gate"]
    assert p.gates["spec"]["status"] == "pass"            # HUD pill captured
    assert not p.ended
    _close(p)
    assert p.ended and p.log[-1]["type"] == "_end"
    # The whole log is durable — a restart can replay it byte-for-byte.
    assert [e["type"] for e in store.events_of("p1")] == ["stage", "gate", "_end"]


# --- fan-out: many viewers, no event stealing -------------------------------- #

def test_fanout_every_cursor_sees_every_event(store):
    """The core W1 property: two independent tails over ONE log both see the FULL
    sequence (the old shared queue split events across consumers)."""
    p = _project()
    for i in range(6):
        _emit(p, type="narrate", text=f"e{i}")

    def drain_from(cursor: int) -> list[str]:
        seen = []
        while cursor < len(p.log):
            seen.append(p.log[cursor]["text"])
            cursor += 1
        return seen

    a, b = drain_from(0), drain_from(0)                   # two simultaneous viewers
    assert a == b == [f"e{i}" for i in range(6)]


def test_resume_cursor_from_last_event_id():
    assert _resume_cursor(None) == 0                      # fresh viewer → full replay
    assert _resume_cursor("") == 0
    assert _resume_cursor("2") == 3                       # reconnect → next event
    assert _resume_cursor("not-a-number") == 0           # malformed → safe replay


# --- checkpoint decisions: first-wins, idempotent, multi-device -------------- #

def test_first_decision_wins_later_clicks_are_noops(store):
    p = _project(pending_checkpoint=1)
    assert _record_decision(p, "approve", 1) == (True, False)    # first wins
    assert _record_decision(p, "reject", 1) == (False, False)    # 2nd device: no-op
    assert p.decision_for[1] == "approve"


def test_stale_checkpoint_click_is_ignored(store):
    p = _project(pending_checkpoint=2)
    # A click for the PREVIOUS pause (1) must not resolve the current one (2).
    assert _record_decision(p, "approve", 1) == (False, True)
    assert 2 not in p.decision_for
    # No pause awaiting at all → also stale.
    p.pending_checkpoint = None
    assert _record_decision(p, "approve", None) == (False, True)


def test_await_decision_blocks_then_returns_first(store):
    p = _project(pending_checkpoint=1)
    out: dict[str, str] = {}

    def waiter():
        out["decision"] = _await_decision(p, 1, timeout=5)

    t = threading.Thread(target=waiter)
    t.start()
    _record_decision(p, "reject", 1)                      # any device decides
    t.join(timeout=5)
    assert out["decision"] == "reject"


def test_await_decision_times_out_to_approve(store):
    p = _project(pending_checkpoint=1)
    # No decision arrives within the (tiny) timeout → fail-safe auto-approve.
    assert _await_decision(p, 1, timeout=0.05) == "approve"


# --- survive restart: replay history + honestly close interrupted builds ----- #

def test_load_persisted_replays_finished_build(store, monkeypatch):
    # A finished build whose log was persisted (…, _end) before a restart.
    for i, ev in enumerate([{"type": "stage"}, {"type": "release"}, {"type": "_end"}]):
        store.append_event("done1", i, ev)
    store.upsert({"id": "done1", "floor": 1, "release_seq": 1, "request": "r",
                  "mode": "assistant", "status": "done", "passed": True})
    monkeypatch.setattr(server, "_PROJECTS", {})
    server._load_persisted()
    p = server._PROJECTS["done1"]
    assert [e["type"] for e in p.log] == ["stage", "release", "_end"]
    assert p.ended                      # a late viewer replays then ends at once


def test_load_persisted_closes_interrupted_build(store, monkeypatch):
    # A build still "running" at restart: its thread is gone, so it must be closed
    # out honestly (history kept, terminal interrupted markers appended).
    for i, ev in enumerate([{"type": "stage"}, {"type": "gate", "name": "spec"}]):
        store.append_event("run1", i, ev)
    store.upsert({"id": "run1", "floor": 2, "release_seq": 1, "request": "r",
                  "mode": "assistant", "status": "running", "passed": False})
    monkeypatch.setattr(server, "_PROJECTS", {})
    server._load_persisted()
    p = server._PROJECTS["run1"]
    types = [e["type"] for e in p.log]
    assert types[:2] == ["stage", "gate"]               # original history preserved
    assert types[-1] == "_end" and "halt" in types       # closed out cleanly
    assert p.ended and p.status == "done"
    # Idempotent across a SECOND restart: it now ends in _end, so it isn't reopened.
    before = len(server._STORE.events_of("run1"))
    monkeypatch.setattr(server, "_PROJECTS", {})
    server._load_persisted()
    assert len(server._STORE.events_of("run1")) == before
