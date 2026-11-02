"""Global, cross-fork, transactionally-committed records. (arch §3/§5, gov §5)

These survive forking (a new `run_id`) and persist across runs:

- **nonce-consumption ledger** — judgment `verdict_nonce` is single-use;
  consumption is keyed on the verdict's CONTENT identity so re-folding an
  already-accepted write is idempotent (no consume-on-first-write side effect
  inside the reducer).
- **negative-verdict latch** — keyed on cross-fork-stable
  `(node_id, subject_artifact_uri, state_sha)`; monotonic; released only by a
  fresh server-issued nonce bound to a genuine re-review.
- **stable-FAIL latch** — deterministic gates, monotonic per digest; keyed on
  the digest (not `run_id`), so a fork cannot escape it (gov §5).
- **class-3 alpha look-ledger** — keyed on `(eval_set_identity, decision_id)`,
  NOT `state_sha`, so forking/retraining cannot re-draw the budget;
  `state_sha` is recorded per-look for audit only.

`_merge_gates` depends only on the read-only `GlobalRecordsView`; writes go
through the transactional store (the orchestrator/runner side). The reducer
stays pure, commutative, and replay-deterministic.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Protocol

LatchKey = tuple[str, str, str]  # (node_id, subject_artifact_uri, state_sha)


class GlobalRecordsView(Protocol):
    """Read-only committed snapshot — the ONLY global state reducers may see."""

    def nonce_consumed_by_other(self, nonce: str, content_identity: str) -> bool: ...

    def negative_verdict_latched(self, key: LatchKey) -> bool: ...

    def stable_fail_latched(self, digest: str, gate: str) -> bool: ...


class GlobalRecords:
    """Writable store. SQLite-backed (`:memory:` for tests, file for dev).

    Production swaps the connection for Postgres inside the superstep
    transaction (durability/); the schema and semantics are identical.
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nonce_ledger (
                  nonce TEXT PRIMARY KEY,
                  content_identity TEXT NOT NULL,
                  consumed_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS negative_latch (
                  node_id TEXT NOT NULL,
                  subject_artifact_uri TEXT NOT NULL,
                  state_sha TEXT NOT NULL,
                  released_by_nonce TEXT,
                  PRIMARY KEY (node_id, subject_artifact_uri, state_sha)
                );
                CREATE TABLE IF NOT EXISTS stable_fail_latch (
                  digest TEXT NOT NULL,
                  gate TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  PRIMARY KEY (digest, gate)
                );
                CREATE TABLE IF NOT EXISTS alpha_ledger (
                  eval_set_identity TEXT NOT NULL,
                  decision_id TEXT NOT NULL,
                  look_index INTEGER NOT NULL,
                  state_sha TEXT NOT NULL,            -- audit only, never the key
                  run_id TEXT NOT NULL,
                  PRIMARY KEY (eval_set_identity, decision_id, look_index)
                );
                """
            )

    # ---- read-only view (GlobalRecordsView) --------------------------------

    def nonce_consumed_by_other(self, nonce: str, content_identity: str) -> bool:
        """True iff `nonce` was consumed by a DIFFERENT content identity.

        Same identity -> idempotent re-fold of an already-accepted write.
        """
        row = self._conn.execute(
            "SELECT content_identity FROM nonce_ledger WHERE nonce = ?", (nonce,)
        ).fetchone()
        return row is not None and row[0] != content_identity

    def negative_verdict_latched(self, key: LatchKey) -> bool:
        row = self._conn.execute(
            "SELECT released_by_nonce FROM negative_latch"
            " WHERE node_id=? AND subject_artifact_uri=? AND state_sha=?",
            key,
        ).fetchone()
        return row is not None and row[0] is None

    def stable_fail_latched(self, digest: str, gate: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM stable_fail_latch WHERE digest=? AND gate=?", (digest, gate)
        ).fetchone()
        return row is not None

    # ---- transactional writes (orchestrator / trusted runner side) ---------

    def consume_nonce(self, nonce: str, content_identity: str) -> bool:
        """Consume a nonce for a verdict's content identity.

        Returns True if consumed (or already consumed by the SAME identity —
        idempotent); False if already consumed by a different identity (replay).

        Atomic insert-if-absent on the ``nonce`` PRIMARY KEY: a concurrent consumer
        (another build sharing the durable ledger) cannot pass a stale "absent" read
        and double-consume — exactly one ``INSERT OR IGNORE`` wins, and the loser
        re-reads the committed row. This closes the check-then-insert TOCTOU the old
        SELECT-then-INSERT form had across connections (mirrors `try_first_alpha_look`).
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO nonce_ledger (nonce, content_identity) VALUES (?, ?)",
                (nonce, content_identity),
            )
            if cur.rowcount == 1:
                return True                       # THIS call consumed the nonce
            row = self._conn.execute(             # already present — replay iff a
                "SELECT content_identity FROM nonce_ledger WHERE nonce=?", (nonce,)
            ).fetchone()                          # DIFFERENT identity holds it
            return row is not None and row[0] == content_identity

    def latch_negative_verdict(self, key: LatchKey) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO negative_latch (node_id, subject_artifact_uri, state_sha)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (node_id, subject_artifact_uri, state_sha)"
                " DO UPDATE SET released_by_nonce = NULL",
                key,
            )

    def release_negative_verdict(self, key: LatchKey, fresh_nonce: str) -> None:
        """Released ONLY by a fresh server-issued nonce bound to a re-review."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE negative_latch SET released_by_nonce=?"
                " WHERE node_id=? AND subject_artifact_uri=? AND state_sha=?",
                (fresh_nonce, *key),
            )

    def latch_stable_fail(self, digest: str, gate: str, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO stable_fail_latch (digest, gate, run_id) VALUES (?,?,?)",
                (digest, gate, run_id),
            )

    def record_alpha_look(
        self, eval_set_identity: str, decision_id: str, state_sha: str, run_id: str
    ) -> int:
        """Append a look; returns total looks spent. Forking does NOT reset alpha."""
        with self._lock, self._conn:
            (spent,) = self._conn.execute(
                "SELECT COUNT(*) FROM alpha_ledger WHERE eval_set_identity=? AND decision_id=?",
                (eval_set_identity, decision_id),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO alpha_ledger VALUES (?, ?, ?, ?, ?)",
                (eval_set_identity, decision_id, spent + 1, state_sha, run_id),
            )
            return spent + 1

    def try_first_alpha_look(
        self, eval_set_identity: str, decision_id: str, state_sha: str, run_id: str
    ) -> bool:
        """ATOMICALLY draw the FIRST alpha look for ``(eval_set_identity, decision_id)``.

        Inserts the look at ``look_index == 1`` IFF no look for the pair exists yet,
        in ONE lock acquisition. Returns True iff THIS call recorded the first look;
        False iff the pair was already drawn (a fork/retrain re-draw — caller must
        reject fail-closed). This is insert-if-absent: the
        ``INSERT ... WHERE NOT EXISTS`` plus the ``(eval_set_identity, decision_id,
        look_index)`` PK make the draw atomic, closing the check-then-record TOCTOU
        the pre-registration freeze would otherwise have between
        :meth:`alpha_looks_spent` and :meth:`record_alpha_look`.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO alpha_ledger (eval_set_identity, decision_id, look_index,"
                " state_sha, run_id)"
                " SELECT ?, ?, 1, ?, ? WHERE NOT EXISTS ("
                "   SELECT 1 FROM alpha_ledger WHERE eval_set_identity=? AND decision_id=?"
                " )",
                (
                    eval_set_identity,
                    decision_id,
                    state_sha,
                    run_id,
                    eval_set_identity,
                    decision_id,
                ),
            )
            return cur.rowcount == 1

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int:
        (spent,) = self._conn.execute(
            "SELECT COUNT(*) FROM alpha_ledger WHERE eval_set_identity=? AND decision_id=?",
            (eval_set_identity, decision_id),
        ).fetchone()
        return spent
