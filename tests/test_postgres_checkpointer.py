"""Validation of the Postgres checkpointer SWAP POINT (architecture.md §5 / DS3).

These run only when ``FOUNDRY_TEST_PG_DSN`` points at a live Postgres (e.g.
``host=/var/run/postgresql port=5433 user=postgres dbname=foundry``); otherwise
they skip. They pin the two facts that de-risk the Postgres wiring:

1. ``PostgresSaver`` checkpoint put/get round-trips against a real PG.
2. ``PostgresSaver.put()`` respects the OUTER transaction (no self-commit), so the
   one-transaction-per-superstep discipline ports cleanly: a rolled-back outer txn
   discards the checkpoint, an committed one persists it — the atomic-or-nothing
   guarantee DS3 requires, with NO commit-deferral hack (unlike SqliteSaver).
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("FOUNDRY_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set FOUNDRY_TEST_PG_DSN for a live PG")


def _saver(conn):
    from langgraph.checkpoint.postgres import PostgresSaver

    s = PostgresSaver(conn)
    s.setup()
    return s


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}


def test_postgres_saver_round_trips() -> None:
    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint

    with psycopg.connect(DSN, autocommit=True) as conn:
        saver = _saver(conn)
        cfg = _cfg("rt-1")
        cp = empty_checkpoint()
        saver.put(cfg, cp, {"source": "test", "step": 1, "parents": {}}, {})
        got = saver.get_tuple(cfg)
        assert got is not None and got.checkpoint["id"] == cp["id"]


def test_put_respects_outer_transaction_atomicity() -> None:
    """The DS3 property: PostgresSaver.put joins the outer txn — a ROLLBACK discards
    the checkpoint (so a superstep that fails commits nothing), a COMMIT persists it.
    This is what lets the outbox/ledgers share one atomic superstep transaction."""
    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint

    # Writer with explicit transaction control (the superstep connection model).
    with psycopg.connect(DSN) as w:
        w.autocommit = False
        saver = _saver(w)
        w.commit()  # the setup() DDL

        cfg = _cfg("atomic-1")
        cp = empty_checkpoint()
        saver.put(cfg, cp, {"source": "test", "step": 1, "parents": {}}, {})
        w.rollback()  # superstep failed -> nothing should persist

        # A SEPARATE connection sees no checkpoint for the rolled-back thread.
        with psycopg.connect(DSN, autocommit=True) as r:
            assert _saver(r).get_tuple(cfg) is None

        # Now put + COMMIT -> it persists and is visible to other connections.
        cp2 = empty_checkpoint()
        saver.put(cfg, cp2, {"source": "test", "step": 2, "parents": {}}, {})
        w.commit()
        with psycopg.connect(DSN, autocommit=True) as r:
            got = _saver(r).get_tuple(cfg)
            assert got is not None and got.checkpoint["id"] == cp2["id"]
