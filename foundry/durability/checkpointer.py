"""FoundryCheckpointer — one connection, one transaction per superstep. (DS3)

architecture.md §5: ONE transaction commits, per superstep, the LangGraph
checkpoint, the budget-ledger update, the outbox rows for every external
effect + audit event, and the adapter turn-snapshot handle. The checkpoint
and the ledgers can never diverge.

Dev backend (this module): a single ``sqlite3`` connection (WAL,
``check_same_thread=False``) shared between
``langgraph.checkpoint.sqlite.SqliteSaver`` and the outbox tables, so a
checkpoint ``put()`` and an outbox ``enqueue()`` inside
:meth:`FoundryCheckpointer.superstep_txn` commit (or roll back) atomically.

Prod backend: ``PostgresSaver`` constructed on the same shared
connection/transaction with the identical discipline — the saver, the outbox
INSERTs, and the ledger UPDATEs all join one server-side transaction; nothing
in :mod:`foundry.durability.outbox` is SQLite-specific.

SqliteSaver API friction (langgraph-checkpoint-sqlite 3.1.0) and how it is
resolved here:

* ``SqliteSaver.put()`` ends with ``self.conn.commit()`` (via its internal
  ``cursor()`` context manager). Under a naive outer ``BEGIN IMMEDIATE`` that
  would prematurely commit the superstep transaction — checkpoint durable,
  outbox rows not yet written: exactly the dual-write divergence DS3 forbids.
  Resolution: the shared connection is a :class:`_SuperstepConnection`
  (``sqlite3.Connection`` subclass) whose ``commit()`` is a no-op while a
  superstep transaction is open; the one real COMMIT is issued by
  ``superstep_txn()`` on successful exit. Outside a superstep, ``commit()``
  behaves normally, so direct saver use (e.g. by LangGraph itself between
  supersteps) still works.
* ``SqliteSaver.setup()`` uses ``executescript``, which implicitly COMMITs
  any pending transaction. Resolution: ``setup()`` (and the outbox DDL) run
  eagerly in the factory, never inside a superstep transaction.
* The connection is opened with ``isolation_level=None`` (explicit
  transaction control): no implicit ``BEGIN`` ever races our
  ``BEGIN IMMEDIATE``, and ``superstep_txn()`` owns the transaction
  boundaries outright.

Atomicity achieved: every statement executed on ``.conn`` between entering
``superstep_txn()`` and its exit — saver ``put``/``put_writes``, outbox
``enqueue`` + WORM audit appends, ledger updates, the turn-snapshot handle —
commits in ONE SQLite transaction, or none of it does.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from foundry.durability.outbox import ensure_outbox_schema

__all__ = ["FoundryCheckpointer", "make_checkpointer"]


class _SuperstepConnection(sqlite3.Connection):
    """Connection whose ``commit()`` defers to the superstep boundary.

    While ``_in_superstep`` is set, ``commit()`` is suppressed so that
    components which "helpfully" self-commit (SqliteSaver) cannot split the
    superstep transaction. ``rollback()`` is never suppressed.
    """

    _in_superstep: bool = False

    def commit(self) -> None:
        if self._in_superstep:
            return
        super().commit()


class FoundryCheckpointer:
    """Factory owning the ONE shared connection for checkpoint + outbox.

    Exposes ``.saver`` (the ``SqliteSaver``), ``.conn`` (the shared
    connection — pass it to ``enqueue()``/``append_audit()``), and
    :meth:`superstep_txn`.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.conn: _SuperstepConnection = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # explicit txn control; no implicit BEGIN
            factory=_SuperstepConnection,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.saver = SqliteSaver(self.conn)
        # DDL (executescript ⇒ implicit COMMIT) must run OUTSIDE supersteps:
        self.saver.setup()
        ensure_outbox_schema(self.conn)

    @contextmanager
    def superstep_txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE … COMMIT; ROLLBACK on exception. Not reentrant.

        Everything written on ``.conn`` inside this block — checkpoint,
        ledgers, outbox rows, audit events, turn-snapshot handle — commits
        atomically or not at all (DS3).
        """
        if self.conn._in_superstep or self.conn.in_transaction:
            raise RuntimeError("superstep_txn does not nest")
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn._in_superstep = True
        try:
            yield self.conn
        except BaseException:
            self.conn._in_superstep = False
            self.conn.rollback()
            raise
        else:
            self.conn._in_superstep = False
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def make_checkpointer(db_path: str | Path = ":memory:") -> FoundryCheckpointer:
    """Construct the checkpointer for the backend named by ``FOUNDRY_CHECKPOINTER``.

    The deployment-portability seam (TASK [14]): the dev / docker-compose default
    is ``sqlite`` (this module's :class:`FoundryCheckpointer`, the ONLY backend
    validated in this environment). ``postgres`` is the production EKS/RDS target.

    SWAP POINT — Postgres (HA): validated, scoped, fail-closed until wired. The
    one-transaction-per-superstep discipline (architecture.md §5 / DS3) ports cleanly
    to Postgres — and the hardest unknown is now RESOLVED (see
    ``tests/test_postgres_checkpointer.py``, which runs against a live PG when
    ``FOUNDRY_TEST_PG_DSN`` is set):

    * ``langgraph.checkpoint.postgres.PostgresSaver.put()`` does NOT self-commit (it
      uses a pipeline and respects the outer transaction) — so unlike ``SqliteSaver``
      it needs NO ``commit()``-deferral hack: opening the psycopg connection with
      ``autocommit=False`` and committing at the ``superstep_txn`` boundary gives the
      identical atomic discipline, more simply.
    * Validated end-to-end: a ``PostgresSaver`` checkpoint put/get round-trips on a
      live PG16, and a rolled-back outer transaction discards the checkpoint.

    REMAINING (the scoped, separately-reviewed change): the outbox + WORM-audit DDL
    and statements in :mod:`foundry.durability.outbox` are SQLite-dialect (``?``
    params, ``AUTOINCREMENT``); they must share the saver's psycopg connection to
    preserve DS3, so they need a dialect shim (``%s`` params, ``GENERATED … AS
    IDENTITY``; the ``ON CONFLICT`` upserts already port). Until that lands, this
    factory FAILS CLOSED on ``postgres`` rather than ship a path that would split the
    superstep transaction across two stores. See ``docs/deploy-compose.md``.
    """
    backend = os.environ.get("FOUNDRY_CHECKPOINTER", "sqlite").strip().lower()
    if backend == "sqlite":
        return FoundryCheckpointer(db_path)
    if backend == "postgres":
        raise NotImplementedError(
            "FOUNDRY_CHECKPOINTER=postgres: the PostgresSaver checkpoint path is "
            "VALIDATED (tests/test_postgres_checkpointer.py) and its atomicity model "
            "confirmed clean (PostgresSaver.put respects the outer txn). The remaining "
            "scoped step is the dialect-portable outbox/WORM SQL on the shared psycopg "
            "connection (DS3). Fail-closed until that lands. See docs/deploy-compose.md."
        )
    raise ValueError(
        f"unknown FOUNDRY_CHECKPOINTER {backend!r} (expected 'sqlite' or 'postgres')"
    )
