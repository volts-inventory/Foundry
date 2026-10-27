"""The cross-fork single-use nonce ledger (replay defense, gov §5)."""

from __future__ import annotations

import sqlite3

from foundry.state.global_records import GlobalRecords

# --- single-use nonce ledger (replay defense) -------------------------------- #

NONCE = "nonce-xyz"


def test_consume_nonce_semantics() -> None:
    records = GlobalRecords()
    assert records.consume_nonce(NONCE, "verdict-A") is True   # first consume
    assert records.consume_nonce(NONCE, "verdict-A") is True   # same identity: idempotent
    assert records.consume_nonce(NONCE, "verdict-B") is False  # different identity: replay


def test_consume_nonce_is_atomic_across_shared_connections(tmp_path) -> None:
    """The durable ledger is shared by concurrent builds (separate GlobalRecords
    instances on ONE db file). The atomic insert-if-absent must let exactly one
    consumer win a contested nonce — never both — so the replay defense holds even
    though each instance has its own threading lock."""
    db = str(tmp_path / "global_records.db")
    a = GlobalRecords(sqlite3.connect(db, check_same_thread=False))
    b = GlobalRecords(sqlite3.connect(db, check_same_thread=False))
    # Two builds race to consume the SAME nonce with DIFFERENT verdict identities.
    ra = a.consume_nonce(NONCE, "verdict-A")
    rb = b.consume_nonce(NONCE, "verdict-B")
    assert [ra, rb] == [True, False]                            # exactly one winner
    # The losing identity can never later "win" the consumed nonce.
    assert b.consume_nonce(NONCE, "verdict-B") is False
