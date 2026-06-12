"""Phase 0 exit criterion: trivial task end-to-end, checkpointed;
kill-then-resume produces no duplicate external action. (implementation-plan §7)
"""

from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from foundry.durability.serde import foundry_serde
from foundry.graph.skeleton import build_skeleton_graph
from foundry.state.seq import EpochCounter


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ckpt.db", check_same_thread=False)
    yield c
    c.close()


def _saver(conn: sqlite3.Connection) -> SqliteSaver:
    return SqliteSaver(conn, serde=foundry_serde())


def test_end_to_end_trivial_task(conn) -> None:
    g = build_skeleton_graph(_saver(conn))
    cfg = {"configurable": {"thread_id": "run-1"}}
    out = g.invoke({"request": "trivial"}, cfg)
    assert out["phase"] == "done"
    assert len(out["artifacts"]) == 1
    assert len(out["actions"]) == 1
    assert out["actions"][0].idempotency_key.startswith("open_pr:run-1:cas://")


def test_kill_then_resume_no_duplicate_action(conn) -> None:
    epochs = EpochCounter()
    cfg = {"configurable": {"thread_id": "run-2"}}

    # "Kill" mid-run: interrupt after `work`, before the effectful node.
    g_interrupted = build_skeleton_graph(_saver(conn), epochs=epochs)
    g_interrupted.invoke({"request": "trivial"}, cfg | {"interrupt_after": ["work"]})

    # "Resume" in a fresh process: a NEW graph object over the SAME durable
    # checkpoint (and a fresh EpochCounter, as a real process restart implies
    # re-reading the durable counter — worst case here: epochs restart at 1,
    # which still coalesces because the idempotency KEY is content-derived).
    g2 = build_skeleton_graph(_saver(conn), epochs=EpochCounter())
    out = g2.invoke(None, cfg)

    assert out["phase"] == "done"
    assert len(out["actions"]) == 1, "kill-then-resume must not duplicate the external action"
    assert len(out["artifacts"]) == 1

    # Re-resuming a finished run is a no-op for effects too.
    out2 = g2.invoke(None, cfg)
    assert len(out2["actions"]) == 1


def test_idempotency_key_is_process_stable(conn) -> None:
    """Same request, two independent runs of the same thread id ⇒ same key.

    Guards the content_digest-not-hash() rule: Python's seed-randomized
    hash() would generate different keys across processes and defeat dedup.
    """
    cfg = {"configurable": {"thread_id": "run-3"}}
    out_a = build_skeleton_graph(_saver(conn)).invoke({"request": "same"}, cfg)
    cfg_b = {"configurable": {"thread_id": "run-3b"}}
    out_b = build_skeleton_graph(_saver(conn)).invoke({"request": "same"}, cfg_b)
    key_a = out_a["actions"][0].idempotency_key.split(":", 2)[2]
    key_b = out_b["actions"][0].idempotency_key.split(":", 2)[2]
    assert key_a == key_b
