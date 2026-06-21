"""Store HA / outage-pause + poisoned-checkpoint recovery. (operations.md §3)

A Phase-3 EXIT criterion: a stateful-store outage (Postgres / object store /
WORM) must **PAUSE** in-flight runs (status ``paused: store_outage``), never
**CORRUPT** them, and they must **RECOVER** when the store returns; a poisoned /
corrupt checkpoint must be **DETECTED** and recovered by rolling back to the last
clean checkpoint — and a poisoned checkpoint is NEVER trusted/loaded as state
(fail-closed, governance §1b).

Two seams, both built on the EXISTING durability surface (REUSED, never
rebuilt):

* **Outage → pause-not-corrupt.** The one-txn/superstep discipline
  (:class:`foundry.durability.checkpointer.FoundryCheckpointer.superstep_txn`,
  LEARNINGS B3) already makes the commit ATOMIC: the checkpoint, the ledgers,
  the outbox rows + WORM audit either ALL commit or NONE do. So "don't corrupt
  on outage" is enforced by construction — :func:`with_store_outage_handling`
  runs the superstep body inside that transaction and, on a store-unavailability
  signal, lets it ROLL BACK (nothing persists) and transitions the run to
  :class:`~foundry.schemas.operations.RunStatus.PAUSED` with
  :class:`~foundry.schemas.operations.PauseReason.STORE_OUTAGE` — a legitimate,
  $0/hr, NON-alerted wait (operations.md §2). It NEVER writes a partial
  checkpoint. :func:`resume_after_outage` resumes from the last COMMITTED
  checkpoint and reconciles the relay (the relay is at-least-once + idempotent,
  so reconciliation never double-fires an external action; LEARNINGS — outbox
  ``recover``).

* **Poisoned-checkpoint recovery.** :func:`check_checkpoint_health` inspects a
  checkpoint and reports :class:`~foundry.schemas.operations.CheckpointHealth`:
  ``clean=False`` (poisoned) when (a) the body fails the allowlist serde /
  degrades to a non-frozen ``dict`` (crash-on-resume), (b) a content-addressed
  handle it references DANGLES (object-store blob lost), or (c) the WORM audit
  chain link is broken (tamper / corruption). :func:`recover_to_last_clean`
  rolls FORWARD-TO-PRIOR-GOOD: it loads the ``last_clean_checkpoint_uri`` (after
  re-verifying THAT one is clean) and returns its body — the poisoned checkpoint
  is quarantined and never returned as trusted state.

Determinism: no wall-clock is read internally — the caller injects ``now_ms``
and the per-record :class:`~foundry.state.seq.Seq` (AR2), so the same inputs
yield byte-identical records.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from foundry.durability.artifact_store import UnknownArtifactError
from foundry.durability.outbox import verify_audit_chain
from foundry.schemas.base import FrozenModel
from foundry.schemas.operations import (
    CheckpointHealth,
    PauseReason,
    RunStatus,
    RunStatusRecord,
)

if TYPE_CHECKING:
    from foundry.durability.checkpointer import FoundryCheckpointer
    from foundry.state.seq import Seq

__all__ = [
    "CheckpointBodyResolver",
    "PoisonedCheckpointError",
    "StoreUnavailable",
    "check_checkpoint_health",
    "on_store_unavailable",
    "recover_to_last_clean",
    "resume_after_outage",
    "with_store_outage_handling",
]


# A resolver that loads a checkpoint BODY (a frozen pydantic record) from its
# ``cas://`` uri — in prod the ``ArtifactStore.get_body`` view; in tests an
# in-memory map. It FAILS CLOSED (raises) on an unknown uri / non-frozen body,
# EXACTLY like ``ArtifactStore.get_body`` (the serde-allowlist trust posture).
CheckpointBodyResolver = Callable[[str], FrozenModel]

# A resolver that asserts a content-addressed HANDLE still resolves to a body
# (raises ``UnknownArtifactError`` / ``KeyError`` if the blob/body dangles) —
# in prod ``ArtifactStore.get_blob`` / ``get_body``; the integrity sweep uses it
# to find dangling handles WITHOUT loading the whole body into state.
HandleProbe = Callable[[str], object]


class StoreUnavailable(RuntimeError):
    """Signal that a stateful store (Postgres / object store / WORM) is down.

    Raise this from a superstep body (or detect it on the connection) to trip
    the outage-pause seam: the superstep transaction rolls back (nothing
    persists) and the run transitions to ``paused: store_outage`` — never a
    partial / corrupting write.
    """


class PoisonedCheckpointError(RuntimeError):
    """A checkpoint was poisoned and there is no clean checkpoint to fork to.

    Raised by :func:`recover_to_last_clean` rather than returning a body, so a
    caller can NEVER accidentally resume on poisoned state (fail-closed). The
    poisoned checkpoint is quarantined for operator override (operations.md §3).
    """


# ===========================================================================
# Outage → pause-not-corrupt
# ===========================================================================


def _store_is_unavailable(exc: BaseException) -> bool:
    """A store-unavailability signal vs an ordinary error.

    Either an explicit :class:`StoreUnavailable`, or the
    ``sqlite3.OperationalError`` the dev backend raises when the database file
    is locked / unreachable (the Postgres connection-error analogue). Any other
    exception is a genuine run error — NOT an outage — and is re-raised by the
    seam so it is not silently masked as a pause.
    """
    return isinstance(exc, (StoreUnavailable, sqlite3.OperationalError))


def on_store_unavailable(
    *,
    run_id: str,
    current_phase: str,
    now_ms: int,
    seq: Seq,
    awaiting: str = "store recovery",
    state_footprint_bytes: int = 0,
) -> RunStatusRecord:
    """Build the ``paused: store_outage`` status record for an in-flight run.

    The transition a run makes on a store-unavailability signal: PAUSED with
    :class:`~foundry.schemas.operations.PauseReason.STORE_OUTAGE`, a
    machine-readable ``$0/hr`` cost rate (a paused run burns no budget,
    operations.md §2), and an ``awaiting`` the operator surface renders. This is
    a PURE function (clock + seq injected) so it is deterministic. It does NOT
    touch the store — the whole point is that a paused run writes nothing while
    the store is down (the last committed checkpoint, written atomically by a
    PRIOR superstep, is the resume point).
    """
    return RunStatusRecord(
        run_id=run_id,
        status=RunStatus.PAUSED,
        current_phase=current_phase,
        pause_reason=PauseReason.STORE_OUTAGE,
        awaiting=awaiting,
        last_progress_ts_ms=now_ms,
        cost_rate_microusd_per_hr=0,  # paused ⇒ $0/hr (machine-readable, §2)
        state_footprint_bytes=state_footprint_bytes,
        seq=seq,
    )


@contextmanager
def with_store_outage_handling(
    checkpointer: FoundryCheckpointer,
    *,
    run_id: str,
    current_phase: str,
    now_ms: int,
    seq: Seq,
    on_pause: Callable[[RunStatusRecord], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Run a superstep body fail-closed against a store outage.

    Yields the shared connection inside
    :meth:`FoundryCheckpointer.superstep_txn` (LEARNINGS B3): the checkpoint +
    ledgers + outbox rows + WORM audit commit ATOMICALLY at block exit, or — on
    ANY exception — roll back, so NO partial / corrupt state ever persists.

    If the exception is a store-unavailability signal (:class:`StoreUnavailable`
    or the backend connection error), the transaction has already rolled back
    (nothing persisted), and this seam SWALLOWS the signal and invokes
    ``on_pause`` with a ``paused: store_outage``
    :class:`~foundry.schemas.operations.RunStatusRecord` instead of propagating
    — the run pauses, it does not fail or corrupt. Any OTHER exception is a
    genuine run error and is re-raised after rollback (never masked as a pause).

    Usage::

        with with_store_outage_handling(ckpt, run_id=..., current_phase=...,
                                        now_ms=..., seq=..., on_pause=record) as conn:
            saver.put(...)            # checkpoint
            enqueue(conn, action, payload)  # outbox + WORM, same txn
    """
    try:
        with checkpointer.superstep_txn() as conn:
            yield conn
    except BaseException as exc:
        if _store_is_unavailable(exc):
            # superstep_txn already rolled back: nothing persisted. Pause, not
            # corrupt. The status record is the ONLY observable effect, and it
            # is written OUTSIDE the (now closed) store txn by the caller's
            # on_pause sink (e.g. an operator-surface channel), never a partial
            # checkpoint.
            record = on_store_unavailable(
                run_id=run_id,
                current_phase=current_phase,
                now_ms=now_ms,
                seq=seq,
            )
            if on_pause is not None:
                on_pause(record)
            return
        raise


def resume_after_outage(
    checkpointer: FoundryCheckpointer,
    *,
    reconcile_relay: Callable[[], int] | None = None,
) -> bool:
    """Resume a paused-by-outage run once the store is back.

    Reconciliation, NOT replay: the run resumes from the last COMMITTED
    checkpoint (LangGraph re-folds committed writes on resume — no work since the
    outage was ever persisted, so there is nothing to undo). The only extra step
    is draining the crash window the outage may have left in the outbox: a
    halted relay is safe (at-least-once + idempotent), so ``reconcile_relay``
    (typically :meth:`OutboxRelay.recover`) resolves any ``intent``-stuck row
    per its idempotency path WITHOUT double-firing an external action (LEARNINGS
    L7 / outbox ``recover``). Returns True once the store probe succeeds.

    Fail-closed: if the store is STILL down, the probing transaction raises and
    we return False — the run stays paused (we never declare recovery against a
    store we could not reach).
    """
    try:
        # Probe the store with a trivial read inside a real superstep txn — if
        # it is still down this raises (connection / OperationalError) and we
        # report not-yet-recovered rather than falsely resuming.
        with checkpointer.superstep_txn() as conn:
            conn.execute("SELECT 1")
    except BaseException as exc:
        if _store_is_unavailable(exc):
            return False
        raise
    if reconcile_relay is not None:
        reconcile_relay()
    return True


# ===========================================================================
# Poisoned-checkpoint detection + recovery
# ===========================================================================


def check_checkpoint_health(
    *,
    run_id: str,
    checkpoint_uri: str,
    seq: Seq,
    resolve_body: CheckpointBodyResolver,
    handle_uris: Iterable[str] = (),
    probe_handle: HandleProbe | None = None,
    conn: sqlite3.Connection | None = None,
    last_clean_checkpoint_uri: str | None = None,
    hibernated: bool = False,
) -> CheckpointHealth:
    """Detect whether a checkpoint is clean or POISONED (operations.md §3).

    Poisoned iff ANY of:

    * **Crash-on-resume** — ``resolve_body(checkpoint_uri)`` raises (unknown uri)
      or the stored payload does not round-trip back to a
      :class:`~foundry.schemas.base.FrozenModel` (a non-allowlisted type degrades
      to a ``dict`` under the serde; ``ArtifactStore.get_body`` already
      fail-closes this, LEARNINGS I5).
    * **Dangling handle** — any uri in ``handle_uris`` does not resolve via
      ``probe_handle`` (the periodic integrity sweep; object-store blob lost).
      The dangling uris are recorded on ``dangling_handle_uris``.
    * **Broken WORM chain** — ``verify_audit_chain(conn)`` is False (the audit
      hash chain that backs the checkpoint's effects is tampered / corrupt).

    A poisoned checkpoint is ``clean=False`` + ``quarantined=True`` and carries
    the ``last_clean_checkpoint_uri`` fork-to-prior-good pointer. For a
    ``hibernated`` worktree a dangling object is a HARD alert BEFORE wake — we
    surface it (``quarantined``) while the run is still paused-recoverable rather
    than at wake-time (operations.md §3). PURE wrt the clock (seq injected).
    """
    dangling: list[str] = []
    crash_on_resume = False

    # (a) Crash-on-resume: the checkpoint body itself must load through the
    #     allowlist serde to a frozen model. We never TRUST a poisoned body — we
    #     only attempt the load to classify it, and discard the result.
    try:
        body = resolve_body(checkpoint_uri)
    except Exception:
        crash_on_resume = True
    else:
        if not isinstance(body, FrozenModel):
            crash_on_resume = True

    # (b) Dangling content-addressed handles (integrity sweep).
    if probe_handle is not None:
        for uri in handle_uris:
            try:
                probe_handle(uri)
            except (UnknownArtifactError, KeyError):
                dangling.append(uri)
            except Exception:
                dangling.append(uri)

    # (c) WORM chain integrity (the audit chain backing the checkpoint).
    chain_broken = conn is not None and not verify_audit_chain(conn)

    clean = not (crash_on_resume or dangling or chain_broken)
    return CheckpointHealth(
        run_id=run_id,
        checkpoint_uri=checkpoint_uri,
        clean=clean,
        quarantined=not clean,
        last_clean_checkpoint_uri=last_clean_checkpoint_uri,
        dangling_handle_uris=tuple(dangling),
        hibernated=hibernated,
        seq=seq,
    )


def recover_to_last_clean(
    health: CheckpointHealth,
    *,
    resolve_body: CheckpointBodyResolver,
) -> FrozenModel:
    """Fork-to-prior-good: load the last CLEAN checkpoint, never the poisoned one.

    Given a poisoned :class:`~foundry.schemas.operations.CheckpointHealth`, load
    its ``last_clean_checkpoint_uri`` and return that (verified-clean) body — the
    recovery point for a fresh run_id (operations.md §3 / architecture.md §5).
    The poisoned checkpoint is NEVER loaded as trusted state.

    Fail-closed (raises :class:`PoisonedCheckpointError`) when:

    * ``health.clean`` is True — there is nothing to recover (call only on a
      poisoned checkpoint).
    * there is no ``last_clean_checkpoint_uri`` (no prior-good to fork to —
      operator must intervene; we never fall back to the poisoned body).
    * the recorded last-clean checkpoint does not ITSELF load to a frozen model
      (it too is poisoned — we refuse to hand back untrusted state).
    """
    if health.clean:
        raise PoisonedCheckpointError(
            f"checkpoint {health.checkpoint_uri!r} is clean — nothing to recover"
        )
    target = health.last_clean_checkpoint_uri
    if target is None:
        raise PoisonedCheckpointError(
            f"poisoned checkpoint {health.checkpoint_uri!r} has no "
            "last_clean_checkpoint_uri to fork to — operator override required"
        )
    try:
        body = resolve_body(target)
    except Exception as exc:
        raise PoisonedCheckpointError(
            f"last clean checkpoint {target!r} did not load — no trusted "
            "recovery point (fail-closed)"
        ) from exc
    if not isinstance(body, FrozenModel):
        raise PoisonedCheckpointError(
            f"last clean checkpoint {target!r} did not deserialize to a frozen "
            "model — refusing to resume on untrusted state (fail-closed)"
        )
    return body
