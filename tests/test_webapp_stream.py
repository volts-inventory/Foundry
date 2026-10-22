"""The Workshop's live-progress backing: a retained, broadcastable event log.

These cover the W1 fix — replacing the single per-project queue (which let two
streams steal each other's events) with an append-only log that many viewers tail
independently, that survives reconnect/restart via the durable store, and whose
checkpoint decisions are first-wins + idempotent so multiple devices can drive one
build without a stale click leaking into the next pause.
"""

from __future__ import annotations

import os
import threading
import time

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
    """A throwaway BuildStore + an empty durable-state dir, so _emit's persistence
    and the resume probe stay hermetic (no real state, no accidental resume)."""
    s = BuildStore(tmp_path / "foundry.db")
    monkeypatch.setattr(server, "_STORE", s)
    monkeypatch.setattr(server, "_STATE_DIR", str(tmp_path / "state"))
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


def test_finished_log_trimmed_but_store_keeps_full_history(store, monkeypatch):
    # A long-lived server trims finished builds' in-memory logs (RAM), but the FULL
    # history stays durable so a late viewer still replays the whole story.
    monkeypatch.setattr(server, "_LOG_TAIL", 2)
    monkeypatch.setattr(server, "_LOG_GRACE_S", 0)     # eligible as soon as it ends
    running = _project()
    done = Project(id="p2", request="r", mode="assistant", supervised=True, floor=2)
    monkeypatch.setattr(server, "_PROJECTS", {"p1": running, "p2": done})
    for i in range(6):
        _emit(running, type="narrate", text=f"e{i}")     # still running
        _emit(done, type="narrate", text=f"d{i}")
    _close(done)                                          # appends _end, stamps ended_at

    server._trim_finished_logs()

    assert not running.log_trimmed and len(running.log) == 6   # in-flight: untouched
    assert done.log_trimmed and len(done.log) == 2             # finished: trimmed to tail
    full = store.events_of("p2")                               # full story still durable
    assert len(full) == 7 and full[-1]["type"] == "_end"


def test_load_env_file_last_line_wins_and_real_env_wins(tmp_path, monkeypatch):
    # A `.env` where a key appears twice (a preset placeholder, then an
    # `echo "KEY=v" >> .env` append) → the LAST line wins. A var already set in the
    # real process environment is never clobbered by the file.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=\n"             # preset placeholder (empty)
        "CLAUDE_CODE_OAUTH_TOKEN=real-token\n"   # appended later → should win
        "FOUNDRY_MODEL_BACKEND=claude-cli\n"
        "WORKSHOP_PORT=9999\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL_BACKEND", raising=False)
    monkeypatch.setenv("WORKSHOP_PORT", "8080")   # real env must win over the file
    assert server._load_env_file() is True
    try:
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "real-token"   # last line wins
        assert os.environ["FOUNDRY_MODEL_BACKEND"] == "claude-cli"
        assert os.environ["WORKSHOP_PORT"] == "8080"                   # real env preserved
    finally:
        # _load_env_file() writes straight into os.environ. monkeypatch.delenv(
        # raising=False) on an already-absent var records nothing to restore, so
        # without this these would LEAK into later tests — a stray
        # FOUNDRY_MODEL_BACKEND=claude-cli makes a subsequent main() spin up a REAL
        # `claude -p` that hangs on a box with the binary installed.
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("FOUNDRY_MODEL_BACKEND", None)


def test_durable_state_dir_defaults_on_under_output_root():
    # The Workshop turns durable state ON by default (CLI leaves it off) so a build
    # survives a restart. The constant resolves to a real, non-empty path.
    assert server._STATE_DIR and server._STATE_DIR != ""


def test_seen_uris_roundtrip(tmp_path):
    s = BuildStore(tmp_path / "db.sqlite")
    s.add_seen("p1", {"cas://a", "cas://b"})
    s.add_seen("p1", {"cas://b", "cas://c"})        # overlap is idempotent
    assert s.seen_of("p1") == {"cas://a", "cas://b", "cas://c"}
    assert s.seen_of("other") == set()


def test_has_durable_checkpoint_false_without_db(store, monkeypatch):
    # No checkpoints.db under the (empty, tmp) state dir → not resumable.
    assert server._has_durable_checkpoint("anything") is False


def test_interrupted_build_resumes_to_completion(store, monkeypatch, tmp_path):
    """End-to-end: a durable build interrupted mid-run is RESUMED by _load_persisted
    and driven to completion (not re-run from scratch, not silently dropped)."""
    from foundry.cli import run

    state_dir = str(tmp_path / "state")
    monkeypatch.setattr(server, "_STATE_DIR", state_dir)
    monkeypatch.setattr(server, "_OUTPUT_ROOT", tmp_path / "out")
    tid = "rz1"

    # Simulate a crash mid-build: a checkpointed run with NO responder pauses at the
    # first boundary and leaves a durable checkpoint behind.
    run("build a tiny url shortener", thread_id=tid, state_dir=state_dir,
        checkpoints=True, mode="assistant")
    assert server._has_durable_checkpoint(tid) is True       # the probe sees it

    # Catalog it as it would have been mid-flight: status 'running', a little history,
    # unsupervised so the resumed run auto-approves to the end without a human.
    store.append_event(tid, 0, {"type": "stage", "room": "product"})
    store.upsert({"id": tid, "floor": 1, "release_seq": 1, "request": "r",
                  "mode": "assistant", "supervised": False, "status": "running",
                  "passed": False})

    monkeypatch.setattr(server, "_PROJECTS", {})
    server._load_persisted()                                  # → spawns the resume thread
    p = server._PROJECTS[tid]
    assert p.status == "running"                              # resuming, not closed

    deadline = time.monotonic() + 60
    while not p.ended and time.monotonic() < deadline:
        time.sleep(0.2)
    assert p.ended, "resumed build did not finish in time"
    types = [e["type"] for e in p.log]
    assert "done" in types and types[-1] == "_end"           # ran to completion
    assert p.status == "done"


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


# --- what a long silence is waiting on --------------------------------------- #

def test_waiting_on_names_the_longest_running_steps_first():
    # The quiet-watchdog used to report only that nothing had landed. It now names the
    # in-flight steps (fed by the live per-node telemetry) so a five-minute silence
    # says WHO it is waiting on — a long agentic step reads as normal, and a genuine
    # stall (nothing in flight at all) is distinguishable from it.
    p = _project()
    now = 1000.0
    p.inflight = {
        "backend_engineer": ("Backend Engineer", now - 7 * 60),
        "build_test_gate": ("running the BUILD / TEST gates", now - 2 * 60),
    }
    line = server.waiting_on(p, now=now)
    assert line == "Backend Engineer (7 min) · running the BUILD / TEST gates (2 min)"

    # Capped, with the overflow counted rather than dropped silently.
    p.inflight = {f"n{i}": (f"step {i}", now - (10 - i) * 60) for i in range(5)}
    capped = server.waiting_on(p, now=now, cap=3)
    assert capped.startswith("step 0 (10 min)") and capped.endswith("+2 more")

    # Nothing in flight -> empty, which is what makes the watchdog escalate its wording.
    p.inflight = {}
    assert server.waiting_on(p, now=now) == ""


def test_inflight_is_tracked_by_node_id_not_label():
    # A finish event can carry no label at all (a gate's outcome is reported by its own
    # `gate` event), so the in-flight set has to be keyed by node id — keying by label
    # would leak an entry per gate run and the watchdog would name steps long finished.
    from webapp.events import live_event_for_node

    start = live_event_for_node("build_test_gate", "factory", "start")
    fin = live_event_for_node("build_test_gate", "factory", "finish")
    assert start is not None and fin is not None
    assert "step" in start and "step" not in fin        # the asymmetry that caused it


# --- live activity from inside a step (throttling) ---------------------------- #

def test_activity_is_throttled_deduped_and_capped_per_step():
    # Every reported line is an SSE frame to every connected device, and an agentic pass
    # can fire tool calls many times a second — so the stream is bounded, not raw.
    p = _project()
    t = 500.0
    assert server.take_activity(p, "editing src/store.py", now=t) == "editing src/store.py"
    # an immediate repeat says nothing new
    assert server.take_activity(p, "editing src/store.py", now=t + 99) is None
    # blank / whitespace-only is never a line
    assert server.take_activity(p, "   ", now=t + 99) is None
    # too soon after the last accepted line
    assert server.take_activity(p, "running pytest", now=t + 0.5) is None
    # far enough apart -> reported, and whitespace is collapsed to one line
    assert server.take_activity(p, "running\n  pytest -q", now=t + 99) == "running pytest -q"

    # per-step ceiling: after the cap it goes quiet until the next step resets it
    p2 = _project()
    accepted = 0
    for i in range(server._ACT_MAX_PER_STEP + 25):
        if server.take_activity(p2, f"editing file{i}.py", now=1000.0 + i * 10) is not None:
            accepted += 1
    assert accepted == server._ACT_MAX_PER_STEP
    p2.act_count, p2.act_text, p2.act_last = 0, "", 0.0        # what a step start does
    assert server.take_activity(p2, "editing again.py", now=9000.0) is not None


def test_parse_workers_defaults_clamps_and_ignores_junk():
    assert server.parse_workers(None) == {"frontend": 2, "backend": 2, "data": 2, "test": 2}
    got = server.parse_workers({"frontend": "5", "backend": -3, "data": "x", "test": 99,
                                "bogus": 7})
    assert got == {"frontend": 5, "backend": 1, "data": 2, "test": 16}
    assert server.parse_workers({"frontend": 0})["frontend"] == 1  # 0 would strand tickets
    assert server.parse_ticket_tiers({"trivial": "t3", "standard": "T9", "x": "T1"}) == {
        "trivial": "T3"}


def test_run_gets_ticket_kwargs_and_task_events_stream(store, monkeypatch, tmp_path):
    import foundry.cli

    seen: dict = {}

    def run(request, *, task_cb, workers, ticket_tiers, progress_cb=None, **kw):
        seen.update(workers=workers, ticket_tiers=ticket_tiers)
        task_cb({"phase": "start", "ticket_id": "T-001", "title": "x",
                 "specialty": "data", "tier": "T2"})
        raise RuntimeError("stop here")       # ends the build through the error path

    monkeypatch.setattr(foundry.cli, "run", run)
    monkeypatch.setattr(server, "_OUTPUT_ROOT", tmp_path / "out")
    p = _project(workers=server.parse_workers({"data": 5}), ticket_tiers={"trivial": "T3"})
    server._run_factory(p)
    assert seen["workers"]["data"] == 5 and seen["ticket_tiers"] == {"trivial": "T3"}
    assert [e for e in p.log if e["type"] == "task"] == [
        {"type": "task", "phase": "start", "ticket_id": "T-001", "title": "x",
         "specialty": "data", "tier": "T2"}]
    proj = next(e for e in p.log if e["type"] == "project")
    assert proj["workers"]["data"] == 5


def test_run_without_ticket_support_gets_no_ticket_kwargs(store, monkeypatch, tmp_path):
    import foundry.cli

    seen: dict = {}

    def run(request, *, progress_cb=None, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(foundry.cli, "run", run)
    monkeypatch.setattr(server, "_OUTPUT_ROOT", tmp_path / "out")
    server._run_factory(_project())
    assert "workers" not in seen and "task_cb" not in seen and "ticket_tiers" not in seen
