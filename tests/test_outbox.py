"""Tests for the transactional outbox + shared-transaction checkpointer.

architecture.md §5 (DS2/DS3, DS-R9-F4): at-least-once relay with
intent-log → act → confirm-log, path-specific crash recovery, WORM audit
chain, and single-transaction atomicity between the LangGraph checkpoint and
the outbox.
"""

from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.outbox import (
    DispatchResult,
    GateBearingPath3Error,
    OutboxError,
    OutboxRelay,
    OutboxStatus,
    PayloadMismatchError,
    append_audit,
    enqueue,
    verify_audit_chain,
)
from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.state.seq import Seq


def make_action(
    key: str,
    *,
    kind: str = "notify",
    path: IdempotencyPath = IdempotencyPath.CLIENT_KEY,
    gate_bearing: bool = False,
) -> ExternalAction:
    return ExternalAction(
        idempotency_key=key,
        kind=kind,
        payload_uri=f"cas://payload/{key}",
        path=path,
        gate_bearing=gate_bearing,
        seq=Seq("run-1", 1, 0, "node-a", 0),
    )


@pytest.fixture()
def cp(tmp_path) -> FoundryCheckpointer:
    checkpointer = FoundryCheckpointer(tmp_path / "foundry.sqlite")
    yield checkpointer
    checkpointer.close()


def row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM outbox WHERE idempotency_key = ?", (key,)
    ).fetchone()


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def enqueue_in_txn(cp: FoundryCheckpointer, action: ExternalAction, payload: dict):
    with cp.superstep_txn() as conn:
        enqueue(conn, action, payload)


# ---------------------------------------------------------------------------
# at-least-once + idempotency (paths 1/2)
# ---------------------------------------------------------------------------


def test_crash_after_act_then_recover_confirms_exactly_once(cp):
    """Relay crashes AFTER act, BEFORE confirm-log; recover() re-queries by
    key (True = effect exists) -> confirmed; a second drain re-emits nothing."""
    acts: list[str] = []

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        acts.append(key)  # the external act lands ...
        raise ConnectionError("crash between act and confirm-log")

    enqueue_in_txn(cp, make_action("k1"), {"msg": "hello"})
    relay = OutboxRelay(cp.conn, dispatch, requery=lambda kind, key: True)

    assert relay.drain() == 0  # dispatch raised -> no confirm
    assert row(cp.conn, "k1")["status"] == OutboxStatus.INTENT
    assert acts == ["k1"]

    assert relay.recover() == 1
    assert row(cp.conn, "k1")["status"] == OutboxStatus.CONFIRMED

    relay.drain()  # second drain: nothing pending, no duplicate emission
    assert acts == ["k1"]


def test_recover_requery_false_requeues_and_reemits(cp):
    """requery False = act never applied -> back to pending, re-emitted once."""
    acts: list[str] = []
    fail_first = [True]

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        if fail_first[0]:
            fail_first[0] = False
            raise ConnectionError("crash BEFORE the act landed")
        acts.append(key)
        return DispatchResult()

    enqueue_in_txn(cp, make_action("k2", path=IdempotencyPath.EMBEDDED_MARKER), {"n": 2})
    relay = OutboxRelay(cp.conn, dispatch, requery=lambda kind, key: False)

    relay.drain()
    assert row(cp.conn, "k2")["status"] == OutboxStatus.INTENT
    relay.recover()
    assert row(cp.conn, "k2")["status"] == OutboxStatus.PENDING
    assert relay.drain() == 1
    assert row(cp.conn, "k2")["status"] == OutboxStatus.CONFIRMED
    assert acts == ["k2"]


def test_recover_unknown_state_fails_to_human(cp):
    """requery None (or absent) -> the node never assumes: failed_to_human."""
    enqueue_in_txn(cp, make_action("k3"), {"n": 3})
    relay = OutboxRelay(
        cp.conn,
        lambda kind, payload, key: (_ for _ in ()).throw(ConnectionError()),
        requery=lambda kind, key: None,
    )
    relay.drain()
    relay.recover()
    assert row(cp.conn, "k3")["status"] == OutboxStatus.FAILED_TO_HUMAN


# ---------------------------------------------------------------------------
# enqueue atomicity + idempotency
# ---------------------------------------------------------------------------


def test_rollback_of_enqueue_txn_leaves_no_outbox_or_audit_row(cp):
    with pytest.raises(RuntimeError, match="boom"), cp.superstep_txn() as conn:
        enqueue(conn, make_action("kx"), {"x": 1})
        assert count(conn, "outbox") == 1  # visible inside the txn
        raise RuntimeError("boom")
    assert count(cp.conn, "outbox") == 0
    assert count(cp.conn, "worm_audit") == 0


def test_enqueue_requires_open_transaction(cp):
    with pytest.raises(OutboxError, match="superstep transaction"):
        enqueue(cp.conn, make_action("kt"), {"x": 1})


def test_duplicate_enqueue_same_key_same_payload_is_noop(cp):
    a = make_action("dup")
    enqueue_in_txn(cp, a, {"v": 1})
    enqueue_in_txn(cp, a, {"v": 1})
    assert count(cp.conn, "outbox") == 1
    assert count(cp.conn, "worm_audit") == 1  # no second audit row either


def test_duplicate_enqueue_same_key_different_payload_raises(cp):
    enqueue_in_txn(cp, make_action("dup2"), {"v": 1})
    with pytest.raises(PayloadMismatchError):
        enqueue_in_txn(cp, make_action("dup2"), {"v": 2})
    assert count(cp.conn, "outbox") == 1


# ---------------------------------------------------------------------------
# path 3 (FOUNDRY_DEDUP)
# ---------------------------------------------------------------------------


def test_gate_bearing_path3_rejected_at_enqueue(cp):
    """DS-R9-F4: path 3 is never for anything a gate/release/deploy depends on."""
    bad = make_action("g1", path=IdempotencyPath.FOUNDRY_DEDUP, gate_bearing=True)
    with pytest.raises(GateBearingPath3Error), cp.superstep_txn() as conn:
        enqueue(conn, bad, {"deploy": "prod"})
    assert count(cp.conn, "outbox") == 0


def test_path3_crash_between_act_and_confirm_fails_to_human_never_reemits(cp):
    acts: list[str] = []

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        acts.append(key)
        raise ConnectionError("crash between act and confirm-log")

    enqueue_in_txn(
        cp, make_action("p3", path=IdempotencyPath.FOUNDRY_DEDUP), {"note": "hi"}
    )
    # Even with a requery that would say True, path 3 must NOT use it:
    relay = OutboxRelay(cp.conn, dispatch, requery=lambda kind, key: True)
    relay.drain()
    assert row(cp.conn, "p3")["status"] == OutboxStatus.INTENT
    relay.recover()
    assert row(cp.conn, "p3")["status"] == OutboxStatus.FAILED_TO_HUMAN
    relay.drain()
    relay.recover()
    assert acts == ["p3"]  # never re-emitted, never auto-deduped to confirmed
    assert row(cp.conn, "p3")["status"] == OutboxStatus.FAILED_TO_HUMAN


def test_path3_dedup_row_written_with_intent_and_consulted_on_redelivery(cp):
    acts: list[str] = []

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        acts.append(key)
        return DispatchResult()

    enqueue_in_txn(
        cp, make_action("p3b", path=IdempotencyPath.FOUNDRY_DEDUP), {"note": "x"}
    )
    relay = OutboxRelay(cp.conn, dispatch)
    assert relay.drain() == 1
    assert acts == ["p3b"]
    assert count(cp.conn, "outbox_dedup") == 1  # written in the intent txn

    # Simulate at-least-once redelivery (e.g. operator requeue): the dedup
    # row from the prior attempt is consulted and the emit is skipped.
    cp.conn.execute(
        "UPDATE outbox SET status = ? WHERE idempotency_key = ?",
        (OutboxStatus.PENDING, "p3b"),
    )
    assert relay.drain() == 1  # confirmed via dedup, without dispatching
    assert acts == ["p3b"]  # exactly one external emission
    assert row(cp.conn, "p3b")["status"] == OutboxStatus.CONFIRMED


# ---------------------------------------------------------------------------
# WORM audit chain
# ---------------------------------------------------------------------------


def test_audit_chain_verifies_and_detects_tampering(cp):
    for i in range(5):
        append_audit(cp.conn, {"event": "test", "i": i})
    assert count(cp.conn, "worm_audit") == 5
    assert verify_audit_chain(cp.conn) is True

    cp.conn.execute(  # tamper an interior row out-of-band
        "UPDATE worm_audit SET event = ? WHERE id = 3", ('{"event":"forged"}',)
    )
    assert verify_audit_chain(cp.conn) is False


def test_audit_chain_detects_deleted_interior_row(cp):
    for i in range(4):
        append_audit(cp.conn, {"i": i})
    cp.conn.execute("DELETE FROM worm_audit WHERE id = 2")
    assert verify_audit_chain(cp.conn) is False


# ---------------------------------------------------------------------------
# DS3: checkpoint + outbox in ONE transaction on ONE connection
# ---------------------------------------------------------------------------

CONFIG = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
METADATA = {"source": "loop", "step": 1, "parents": {}}


def test_superstep_txn_rolls_back_checkpoint_and_outbox_together(cp):
    """Dual-write divergence test: SqliteSaver.put() inside superstep_txn()
    together with an enqueue; an exception before commit must persist
    NEITHER the checkpoint NOR the outbox/audit rows."""
    checkpoint = empty_checkpoint()
    with pytest.raises(RuntimeError, match="mid-superstep crash"), cp.superstep_txn() as conn:
        cp.saver.put(CONFIG, checkpoint, METADATA, {})
        enqueue(conn, make_action("ds3"), {"effect": "push"})
        raise RuntimeError("mid-superstep crash")

    assert cp.saver.get_tuple(CONFIG) is None  # checkpoint did not persist
    assert count(cp.conn, "outbox") == 0  # neither did the outbox row
    assert count(cp.conn, "worm_audit") == 0


def test_superstep_txn_commits_checkpoint_and_outbox_together(cp):
    checkpoint = empty_checkpoint()
    with cp.superstep_txn() as conn:
        cp.saver.put(CONFIG, checkpoint, METADATA, {})
        enqueue(conn, make_action("ds3-ok"), {"effect": "push"})

    saved = cp.saver.get_tuple(CONFIG)
    assert saved is not None
    assert saved.checkpoint["id"] == checkpoint["id"]
    assert row(cp.conn, "ds3-ok")["status"] == OutboxStatus.PENDING
    assert verify_audit_chain(cp.conn) is True


def test_superstep_txn_does_not_nest(cp):
    with cp.superstep_txn(), pytest.raises(RuntimeError, match="nest"), cp.superstep_txn():
        pass  # pragma: no cover
