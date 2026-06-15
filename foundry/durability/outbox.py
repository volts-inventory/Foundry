"""Transactional outbox + WORM audit chain. (architecture.md §5, DS2/DS3)

The outbox is the *only* durable-effect surface: every external effect is an
explicit outbox enqueue committed in the SAME transaction as the LangGraph
checkpoint (see :mod:`foundry.durability.checkpointer`). A separate
:class:`OutboxRelay` drains rows at-least-once under the protocol

    intent-log  →  act  →  confirm-log

and is the ONLY component in Foundry that touches external systems. Dispatch
and requery are caller-injected callables, so the relay is transport-agnostic.

Idempotency is per-:class:`~foundry.schemas.base.IdempotencyPath`:

1. ``CLIENT_KEY``      — external system stores the key; recovery re-queries.
2. ``EMBEDDED_MARKER`` — key embedded (branch/tag/PR marker); recovery
   re-derives existence via the same caller-injected ``requery``.
3. ``FOUNDRY_DEDUP``   — local dedup row written in the SAME txn as the
   intent mark and consulted before emitting. A crash between act and
   confirm-log on such a non-queryable effect resolves to **fail-to-human,
   never autonomous dedup** (DS-R9-F4); accordingly a ``gate_bearing`` action
   may never use path 3 (rejected at enqueue).

Efficiency: the relay drains in batches with a single ``SELECT`` over the
``status`` index — no per-row polling loop.

Dev backend is SQLite; everything here is plain SQL inside an externally
managed transaction, so the same logic ports to Postgres unchanged (the
intent/confirm marks become ``UPDATE ... WHERE status = ?`` rows exactly as
written, and the WORM chain is append-only in both).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from foundry.canonical import canonical_bytes, content_digest
from foundry.schemas.base import ExternalAction, IdempotencyPath

# Hex-digest "previous hash" of the first chain row.
_GENESIS = ""


class OutboxError(RuntimeError):
    """Base class for outbox protocol violations."""


class PayloadMismatchError(OutboxError):
    """Same idempotency_key re-enqueued with a different payload digest."""


class GateBearingPath3Error(OutboxError):
    """gate_bearing effect requested FOUNDRY_DEDUP — forbidden (DS-R9-F4)."""


class OutboxStatus:
    """Status enum for outbox rows (TEXT column; plain constants port to PG)."""

    PENDING = "pending"
    INTENT = "intent"
    CONFIRMED = "confirmed"
    FAILED_TO_HUMAN = "failed_to_human"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,  -- canonical JSON
    path            TEXT NOT NULL CHECK (path IN
                        ('client_key', 'embedded_marker', 'foundry_dedup')),
    gate_bearing    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending', 'intent', 'confirmed', 'failed_to_human')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status);

-- Path-3 local dedup rows; written in the SAME txn as the intent mark.
CREATE TABLE IF NOT EXISTS outbox_dedup (
    idempotency_key TEXT PRIMARY KEY
);

-- Append-only WORM hash chain. No UPDATE/DELETE is ever issued by this
-- module; verify_audit_chain() detects out-of-band tampering.
CREATE TABLE IF NOT EXISTS worm_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT NOT NULL,  -- canonical JSON
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
"""


def ensure_outbox_schema(conn: sqlite3.Connection) -> None:
    """Create the outbox / dedup / WORM tables (idempotent).

    NOTE: ``executescript`` implicitly COMMITs a pending transaction, so this
    must run at setup time, never inside a superstep transaction (the
    :class:`~foundry.durability.checkpointer.FoundryCheckpointer` factory
    calls it eagerly).
    """
    conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _chain_hash(prev_hash: str, event_text: str) -> str:
    """hash = sha256(prev_hash + canonical event bytes), hex."""
    return hashlib.sha256(
        prev_hash.encode("utf-8") + event_text.encode("utf-8")
    ).hexdigest()


def append_audit(conn: sqlite3.Connection, event: dict) -> str:
    """Append one event to the WORM hash chain; returns the new head hash.

    WORM discipline: this is the only writer and it only ever INSERTs.
    Callers running inside the superstep txn get the audit row committed
    atomically with the checkpoint/outbox rows.
    """
    event_text = canonical_bytes(event).decode("utf-8")
    row = conn.execute(
        "SELECT hash FROM worm_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = row[0] if row is not None else _GENESIS
    new_hash = _chain_hash(prev_hash, event_text)
    conn.execute(
        "INSERT INTO worm_audit (event, prev_hash, hash) VALUES (?, ?, ?)",
        (event_text, prev_hash, new_hash),
    )
    return new_hash


def verify_audit_chain(conn: sqlite3.Connection) -> bool:
    """Recompute the chain; False on any broken link or recomputed-hash
    mismatch (i.e. any UPDATE/DELETE tampering of event, prev_hash, hash,
    or a deleted/reordered interior row)."""
    prev = _GENESIS
    for event_text, prev_hash, hash_ in conn.execute(
        "SELECT event, prev_hash, hash FROM worm_audit ORDER BY id ASC"
    ):
        if prev_hash != prev:
            return False
        if _chain_hash(prev_hash, event_text) != hash_:
            return False
        prev = hash_
    return True


def enqueue(conn: sqlite3.Connection, action: ExternalAction, payload: dict) -> None:
    """Insert an outbox row + its audit row. MUST run inside the superstep txn.

    Idempotent on ``idempotency_key`` (INSERT-OR-IGNORE semantics): a
    re-enqueue with an identical payload digest is a no-op; a re-enqueue with
    a *different* payload under the same key raises
    :class:`PayloadMismatchError` (two different effects may never share a
    key).

    A ``gate_bearing`` action on path FOUNDRY_DEDUP raises
    :class:`GateBearingPath3Error` — path 3 is never for anything a
    gate/release/deploy depends on (DS-R9-F4).
    """
    if action.gate_bearing and action.path is IdempotencyPath.FOUNDRY_DEDUP:
        raise GateBearingPath3Error(
            f"gate-bearing action {action.idempotency_key!r} may not use "
            "FOUNDRY_DEDUP: a crash between act and confirm-log on a "
            "non-queryable effect resolves to fail-to-human (DS-R9-F4)"
        )
    if not conn.in_transaction:
        raise OutboxError(
            "enqueue() must be called inside the superstep transaction "
            "(FoundryCheckpointer.superstep_txn) so the outbox row commits "
            "atomically with the checkpoint (DS3)"
        )

    payload_text = canonical_bytes(payload).decode("utf-8")
    payload_digest = content_digest(payload)

    existing = conn.execute(
        "SELECT payload FROM outbox WHERE idempotency_key = ?",
        (action.idempotency_key,),
    ).fetchone()
    if existing is not None:
        if content_digest(json.loads(existing[0])) != payload_digest:
            raise PayloadMismatchError(
                f"idempotency_key {action.idempotency_key!r} already enqueued "
                "with a different payload digest"
            )
        return  # duplicate enqueue of the identical effect: no-op

    now = _now()
    conn.execute(
        "INSERT INTO outbox (idempotency_key, kind, payload, path,"
        " gate_bearing, status, attempts, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            action.idempotency_key,
            action.kind,
            payload_text,
            action.path.value,
            1 if action.gate_bearing else 0,
            OutboxStatus.PENDING,
            now,
            now,
        ),
    )
    append_audit(
        conn,
        {
            "event": "outbox_enqueued",
            "idempotency_key": action.idempotency_key,
            "kind": action.kind,
            "path": action.path.value,
            "gate_bearing": action.gate_bearing,
            "payload_digest": payload_digest,
            "payload_uri": action.payload_uri,
            "seq": list(action.seq),
        },
    )


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one dispatch attempt against the external system.

    ``ok=False`` means the act *definitively did not happen* (clean refusal),
    so the row safely returns to 'pending'. An ambiguous outcome must be
    signalled by RAISING from dispatch — that leaves the row in 'intent' for
    :meth:`OutboxRelay.recover` to resolve.
    """

    ok: bool = True
    info: str | None = None


Dispatch = Callable[[str, dict, str], DispatchResult]
Requery = Callable[[str, str], bool | None]


class OutboxRelay:
    """At-least-once drainer: intent-log → act → confirm-log.

    This relay is the ONLY component that touches external systems; every
    other part of Foundry reaches the outside world by enqueueing.

    ``dispatch(kind, payload, idempotency_key) -> DispatchResult`` performs
    the external act. ``requery(kind, idempotency_key) -> bool | None``
    re-establishes whether an effect exists (path 1: by stored client key;
    path 2: by re-deriving the embedded marker). Both are caller-injected;
    the relay is transport-agnostic.

    Each intent/confirm mark is its own transaction (``BEGIN IMMEDIATE`` on
    the shared connection), so the crash window between act and confirm-log
    is exactly the 'intent' status — :meth:`recover` resolves it per
    idempotency path, fail-closed: if state cannot be established, the row
    goes to 'failed_to_human' (never assumes).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        dispatch: Dispatch,
        requery: Requery | None = None,
        *,
        batch_size: int = 100,
    ) -> None:
        self.conn = conn
        self.dispatch = dispatch
        self.requery = requery
        self.batch_size = batch_size

    # -- internal -----------------------------------------------------------

    def _txn(self) -> _RelayTxn:
        return _RelayTxn(self.conn)

    def _mark(self, row_id: int, status: str, *, bump_attempts: bool = False) -> None:
        self.conn.execute(
            "UPDATE outbox SET status = ?, updated_at = ?,"
            " attempts = attempts + ? WHERE id = ?",
            (status, _now(), 1 if bump_attempts else 0, row_id),
        )

    def _audit(self, event: str, key: str, **extra: object) -> None:
        append_audit(
            self.conn, {"event": event, "idempotency_key": key, **extra}
        )

    # -- drain --------------------------------------------------------------

    def drain(self) -> int:
        """Process up to ``batch_size`` pending rows; returns #confirmed.

        One indexed SELECT fetches the whole batch (no per-row polling).
        A dispatch that raises leaves its row in 'intent' — exactly the
        state a hard crash would leave — and the loop moves on; recover()
        owns that window.
        """
        rows = self.conn.execute(
            "SELECT id, idempotency_key, kind, payload, path FROM outbox"
            " WHERE status = ? ORDER BY id ASC LIMIT ?",
            (OutboxStatus.PENDING, self.batch_size),
        ).fetchall()

        confirmed = 0
        for row_id, key, kind, payload_text, path in rows:
            already_emitted = False
            # intent-log (own txn); path-3 dedup row rides in the SAME txn.
            with self._txn():
                self._mark(row_id, OutboxStatus.INTENT, bump_attempts=True)
                if path == IdempotencyPath.FOUNDRY_DEDUP.value:
                    already_emitted = (
                        self.conn.execute(
                            "SELECT 1 FROM outbox_dedup WHERE idempotency_key = ?",
                            (key,),
                        ).fetchone()
                        is not None
                    )
                    if not already_emitted:
                        self.conn.execute(
                            "INSERT INTO outbox_dedup (idempotency_key) VALUES (?)",
                            (key,),
                        )
                self._audit("relay_intent", key, kind=kind)

            if already_emitted:
                # Prior attempt's dedup row: effectively-once — skip emit.
                with self._txn():
                    self._mark(row_id, OutboxStatus.CONFIRMED)
                    self._audit("relay_confirmed_via_dedup", key)
                confirmed += 1
                continue

            # act (outside any txn — this talks to the outside world)
            try:
                result = self.dispatch(kind, json.loads(payload_text), key)
            except Exception:
                # Ambiguous: act may or may not have landed. Leave 'intent';
                # recover() resolves per idempotency path.
                continue

            with self._txn():
                if result.ok:
                    self._mark(row_id, OutboxStatus.CONFIRMED)
                    self._audit("relay_confirmed", key)
                    confirmed += 1
                else:
                    # Definitive non-effect: safe to retry later.
                    self._mark(row_id, OutboxStatus.PENDING)
                    self._audit("relay_retry", key, info=result.info)
        return confirmed

    # -- crash recovery -----------------------------------------------------

    def recover(self) -> int:
        """Resolve rows stuck in 'intent' (crash between act and confirm-log).

        - CLIENT_KEY / EMBEDDED_MARKER: ``requery`` re-establishes state.
          True → the effect exists → confirm. False → not applied → back to
          pending (will re-emit). None / no requery → failed_to_human.
        - FOUNDRY_DEDUP: ALWAYS failed_to_human — a crash between act and
          confirm-log on a non-queryable effect resolves to fail-to-human,
          never autonomous dedup (DS-R9-F4). The row is never re-emitted.

        Returns the number of rows resolved (any terminal or pending mark).
        """
        rows = self.conn.execute(
            "SELECT id, idempotency_key, kind, path FROM outbox"
            " WHERE status = ? ORDER BY id ASC",
            (OutboxStatus.INTENT,),
        ).fetchall()

        resolved = 0
        for row_id, key, kind, path in rows:
            if path == IdempotencyPath.FOUNDRY_DEDUP.value:
                with self._txn():
                    self._mark(row_id, OutboxStatus.FAILED_TO_HUMAN)
                    self._audit(
                        "relay_failed_to_human",
                        key,
                        reason="crash between act and confirm-log on "
                        "non-queryable path-3 effect (DS-R9-F4)",
                    )
                resolved += 1
                continue

            exists: bool | None = None
            if self.requery is not None:
                try:
                    exists = self.requery(kind, key)
                except Exception:
                    exists = None  # cannot establish state → fail-closed

            with self._txn():
                if exists is True:
                    self._mark(row_id, OutboxStatus.CONFIRMED)
                    self._audit("relay_confirmed_via_requery", key)
                elif exists is False:
                    self._mark(row_id, OutboxStatus.PENDING)
                    self._audit("relay_requeued_via_requery", key)
                else:
                    self._mark(row_id, OutboxStatus.FAILED_TO_HUMAN)
                    self._audit(
                        "relay_failed_to_human",
                        key,
                        reason="state could not be established on resume",
                    )
            resolved += 1
        return resolved


class _RelayTxn:
    """BEGIN IMMEDIATE … COMMIT/ROLLBACK for the relay's own marks.

    The relay never runs inside a superstep transaction (it is a separate
    drainer process in prod), so each mark is its own short write txn.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            raise OutboxError(
                "relay marks must not run inside another transaction"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
