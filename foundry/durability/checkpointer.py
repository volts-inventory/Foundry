"""FoundryCheckpointer — one connection, one transaction per superstep. (DS3)

A single ``sqlite3`` connection (WAL, ``check_same_thread=False``) backs
``langgraph.checkpoint.sqlite.SqliteSaver``; :meth:`FoundryCheckpointer.superstep_txn`
commits everything written on ``.conn`` in one transaction, or none of it.

SqliteSaver API friction (langgraph-checkpoint-sqlite 3.1.0) and how it is
resolved here:

* ``SqliteSaver.put()`` ends with ``self.conn.commit()`` (via its internal
  ``cursor()`` context manager), which would prematurely commit an outer
  ``BEGIN IMMEDIATE``. The shared connection is a :class:`_SuperstepConnection`
  whose ``commit()`` is a no-op while a superstep transaction is open; the one
  real COMMIT is issued by ``superstep_txn()`` on successful exit.
* ``SqliteSaver.setup()`` uses ``executescript``, which implicitly COMMITs any
  pending transaction, so it runs eagerly in the factory, never inside a
  superstep transaction.
* The connection is opened with ``isolation_level=None`` (explicit transaction
  control): no implicit ``BEGIN`` ever races our ``BEGIN IMMEDIATE``.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

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
    """Factory owning the ONE shared connection for the checkpoint.

    Exposes ``.saver`` (the ``SqliteSaver``), ``.conn`` (the shared
    connection), and :meth:`superstep_txn`.
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

    @contextmanager
    def superstep_txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE … COMMIT; ROLLBACK on exception. Not reentrant.

        Everything written on ``.conn`` inside this block — checkpoint,
        ledgers, turn-snapshot handle — commits
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
    """Construct the sqlite checkpointer (the single-host LAN backend).

    sqlite (this module's :class:`FoundryCheckpointer`) is durable on a mounted
    volume and survives a restart — the validated single-host backend. The
    ``FOUNDRY_CHECKPOINTER`` env is accepted for back-compat but only ``sqlite``
    is supported."""
    backend = os.environ.get("FOUNDRY_CHECKPOINTER", "sqlite").strip().lower()
    if backend in ("", "sqlite"):
        return FoundryCheckpointer(db_path)
    raise ValueError(
        f"unknown FOUNDRY_CHECKPOINTER {backend!r} (only 'sqlite' is supported)"
    )
