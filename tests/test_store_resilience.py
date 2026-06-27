"""EXIT proof for T-M3-04: store HA / outage-pause + poisoned-checkpoint recovery.

Two Phase-3 exit criteria (docs/operations.md §3), proved against the REAL
durability surface (FoundryCheckpointer one-txn/superstep, ArtifactStore
allowlist serde, OutboxRelay idempotency, the WORM audit chain):

1. A store outage PAUSES in-flight runs (status `paused: store_outage`) and does
   NOT corrupt them — the last committed checkpoint is intact and resume
   continues from it with no duplicate external action.
2. A poisoned / corrupt checkpoint (bad serde / dangling handle / broken WORM
   chain) is DETECTED and recovered by rolling back to the last clean
   checkpoint; a poisoned checkpoint is never loaded as trusted state.

Determinism: clock + Seq are injected, so the produced records are reproducible.
"""

from __future__ import annotations

import sqlite3

import pytest

from foundry.durability.artifact_store import ArtifactStore, UnknownArtifactError
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.outbox import (
    DispatchResult,
    OutboxRelay,
    append_audit,
    enqueue,
    verify_audit_chain,
)
from foundry.operations.store_resilience import (
    PoisonedCheckpointError,
    StoreUnavailable,
    check_checkpoint_health,
    on_store_unavailable,
    recover_to_last_clean,
    resume_after_outage,
    with_store_outage_handling,
)
from foundry.schemas.base import BudgetLedger, ExternalAction, IdempotencyPath
from foundry.schemas.operability import (
    PauseReason,
    RunStatus,
)
from foundry.state.seq import Seq

RUN = "run-resilience-1"
SEQ = Seq(run_id=RUN, emission_epoch=1, superstep_index=3, node_id="store_resilience", local=0)


def _action(key: str) -> ExternalAction:
    return ExternalAction(
        idempotency_key=key,
        kind="git_push",
        payload_uri="cas://sha256/deadbeef",
        path=IdempotencyPath.CLIENT_KEY,
        seq=SEQ,
    )


# ===========================================================================
# 1. Store outage PAUSES, does not corrupt
# ===========================================================================


def test_outage_pauses_not_corrupts_and_resumes_from_last_committed() -> None:
    """A store-unavailable signal mid-superstep → no partial state persists, the
    last committed checkpoint is intact, and the run pauses(store_outage)."""
    ckpt = FoundryCheckpointer()
    conn = ckpt.conn

    # --- a CLEAN prior superstep commits one outbox row (the resume point). ---
    with ckpt.superstep_txn() as c:
        enqueue(c, _action("push-A"), {"branch": "feature/x"})
    committed_rows = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert committed_rows == 1

    # --- the NEXT superstep hits a store outage mid-write. ---
    paused: list = []
    with with_store_outage_handling(
        ckpt,
        run_id=RUN,
        current_phase="implementation",
        now_ms=1_000,
        seq=SEQ,
        on_pause=paused.append,
    ) as c:
        # a partial write that WOULD corrupt state if it persisted...
        enqueue(c, _action("push-B"), {"branch": "feature/y"})
        # ...then the store goes down before commit.
        raise StoreUnavailable("postgres unreachable")

    # PAUSE, not corrupt: exactly one paused-status record, store_outage, $0/hr.
    assert len(paused) == 1
    rec = paused[0]
    assert rec.status is RunStatus.PAUSED
    assert rec.pause_reason is PauseReason.STORE_OUTAGE
    assert rec.cost_rate_microusd_per_hr == 0
    assert not rec.is_alertable()  # a legitimate pause is never alerted

    # NO partial/corrupt state persisted: the outbox still has ONLY the prior
    # committed row (push-B rolled back with the superstep txn).
    rows = conn.execute(
        "SELECT idempotency_key FROM outbox ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["push-A"]
    # the WORM chain is intact (the rolled-back audit row never landed).
    assert verify_audit_chain(conn)

    # --- store returns: resume from the last committed checkpoint. ---
    dispatched: list[str] = []

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        dispatched.append(key)
        return DispatchResult(ok=True)

    relay = OutboxRelay(conn, dispatch)
    resumed = resume_after_outage(ckpt, reconcile_relay=relay.recover)
    assert resumed is True

    # the committed effect drains exactly once (no duplicate external action).
    relay.drain()
    assert dispatched == ["push-A"]
    # re-draining is idempotent — the confirmed row never re-fires.
    relay.drain()
    assert dispatched == ["push-A"]
    ckpt.close()


def test_resume_reports_not_recovered_while_store_still_down() -> None:
    """resume_after_outage is fail-closed: if the store probe still fails, it
    returns False (the run stays paused) and never reconciles."""
    ckpt = FoundryCheckpointer()
    ckpt.close()  # simulate the store being unreachable

    reconciled: list[int] = []

    def reconcile() -> int:
        reconciled.append(1)
        return 0

    # a closed sqlite connection raises ProgrammingError, not OperationalError;
    # use a connection whose execute raises OperationalError to mimic Postgres
    # connection loss.
    broken = FoundryCheckpointer()

    def _down(_sql: str, *_a: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    broken.conn.execute = _down  # type: ignore[method-assign]
    assert resume_after_outage(broken, reconcile_relay=reconcile) is False
    assert reconciled == []  # never reconciled against a down store


def test_non_outage_error_is_not_masked_as_pause() -> None:
    """A genuine run error inside the seam is re-raised (rolled back), NOT
    swallowed as a store_outage pause — fail-closed the other direction."""
    ckpt = FoundryCheckpointer()
    paused: list = []
    with pytest.raises(ValueError, match="real bug"), with_store_outage_handling(
        ckpt,
        run_id=RUN,
        current_phase="quality",
        now_ms=5,
        seq=SEQ,
        on_pause=paused.append,
    ):
        raise ValueError("real bug")
    assert paused == []  # not paused — a real error must surface
    ckpt.close()


def test_on_store_unavailable_is_deterministic() -> None:
    """The pause record is a pure function of its inputs (clock + seq injected)."""
    a = on_store_unavailable(
        run_id=RUN, current_phase="implementation", now_ms=42, seq=SEQ
    )
    b = on_store_unavailable(
        run_id=RUN, current_phase="implementation", now_ms=42, seq=SEQ
    )
    assert a == b
    assert a.content_identity() == b.content_identity()
    assert a.status is RunStatus.PAUSED
    assert a.pause_reason is PauseReason.STORE_OUTAGE


# ===========================================================================
# 2. Poisoned-checkpoint detection + recovery
# ===========================================================================


def _clean_store() -> tuple[ArtifactStore, str, str]:
    """A store with a last-clean checkpoint body and a current (clean) one."""
    store = ArtifactStore()
    clean = BudgetLedger(spend_microusd=1000, cap_microusd=10_000)
    current = BudgetLedger(spend_microusd=2000, cap_microusd=10_000)
    clean_uri = store.put_body(clean)
    current_uri = store.put_body(current)
    return store, current_uri, clean_uri


def test_clean_checkpoint_reports_healthy() -> None:
    store, current_uri, clean_uri = _clean_store()
    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri=current_uri,
        seq=SEQ,
        resolve_body=store.get_body,
        handle_uris=[current_uri],
        probe_handle=store.get_body,
        last_clean_checkpoint_uri=clean_uri,
    )
    assert h.clean is True
    assert h.quarantined is False
    assert h.dangling_handle_uris == ()


def test_poisoned_bad_serde_detected_and_recovered() -> None:
    """A checkpoint uri that does not load (crash-on-resume) → poisoned →
    recover_to_last_clean returns the clean body; poisoned body never loaded."""
    store, _current_uri, clean_uri = _clean_store()
    poisoned_uri = "cas://sha256/this-uri-was-never-stored"

    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri=poisoned_uri,
        seq=SEQ,
        resolve_body=store.get_body,
        last_clean_checkpoint_uri=clean_uri,
    )
    assert h.clean is False
    assert h.quarantined is True

    recovered = recover_to_last_clean(h, resolve_body=store.get_body)
    assert isinstance(recovered, BudgetLedger)
    assert recovered.spend_microusd == 1000  # the LAST CLEAN body, not poisoned


def test_poisoned_dangling_handle_detected() -> None:
    """An integrity-sweep dangling content-addressed handle → poisoned, the
    dangling uri is surfaced."""
    store, current_uri, clean_uri = _clean_store()
    dangling = "cas://sha256/object-store-blob-lost"

    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri=current_uri,
        seq=SEQ,
        resolve_body=store.get_body,
        handle_uris=[current_uri, dangling],
        probe_handle=store.get_body,
        last_clean_checkpoint_uri=clean_uri,
        hibernated=True,
    )
    assert h.clean is False
    assert h.quarantined is True  # hard alert BEFORE wake (hibernated)
    assert h.dangling_handle_uris == (dangling,)


def test_poisoned_broken_worm_chain_detected() -> None:
    """A tampered WORM audit chain backing the checkpoint → poisoned."""
    store, current_uri, clean_uri = _clean_store()
    conn = sqlite3.connect(":memory:")
    ckpt = FoundryCheckpointer()  # gives us the schema via its own conn
    # build a small chain on a fresh conn, then tamper an interior row.
    from foundry.durability.outbox import ensure_outbox_schema

    ensure_outbox_schema(conn)
    append_audit(conn, {"event": "a"})
    append_audit(conn, {"event": "b"})
    assert verify_audit_chain(conn)  # clean before tamper
    conn.execute("UPDATE worm_audit SET event = ? WHERE id = 1", ("TAMPERED",))
    assert not verify_audit_chain(conn)

    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri=current_uri,
        seq=SEQ,
        resolve_body=store.get_body,
        conn=conn,
        last_clean_checkpoint_uri=clean_uri,
    )
    assert h.clean is False
    assert h.quarantined is True
    ckpt.close()
    conn.close()


def test_recover_fails_closed_with_no_last_clean_pointer() -> None:
    """A poisoned checkpoint with no last-clean pointer raises — it NEVER falls
    back to the poisoned body."""
    store = ArtifactStore()
    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri="cas://sha256/poisoned",
        seq=SEQ,
        resolve_body=store.get_body,
        last_clean_checkpoint_uri=None,
    )
    assert h.clean is False
    with pytest.raises(PoisonedCheckpointError, match="no last_clean"):
        recover_to_last_clean(h, resolve_body=store.get_body)


def test_recover_on_clean_checkpoint_raises() -> None:
    """Calling recovery on a clean checkpoint is a programming error (fail-closed)."""
    store, current_uri, clean_uri = _clean_store()
    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri=current_uri,
        seq=SEQ,
        resolve_body=store.get_body,
        last_clean_checkpoint_uri=clean_uri,
    )
    assert h.clean is True
    with pytest.raises(PoisonedCheckpointError, match="nothing to recover"):
        recover_to_last_clean(h, resolve_body=store.get_body)


def test_recover_fails_closed_when_last_clean_also_poisoned() -> None:
    """If the recorded last-clean checkpoint ITSELF will not load, recovery
    raises rather than hand back untrusted state."""
    store, _current_uri, _clean_uri = _clean_store()
    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri="cas://sha256/poisoned",
        seq=SEQ,
        resolve_body=store.get_body,
        last_clean_checkpoint_uri="cas://sha256/also-missing",
    )
    assert h.clean is False
    with pytest.raises(PoisonedCheckpointError, match="did not load"):
        recover_to_last_clean(h, resolve_body=store.get_body)


def test_poisoned_never_loaded_as_trusted_state() -> None:
    """End-to-end fail-closed: a poisoned body that resolves to a non-frozen
    object is detected as poisoned and is NEVER returned as trusted state."""

    def resolve_body(uri: str):  # type: ignore[no-untyped-def]
        if uri == "cas://clean":
            return BudgetLedger(spend_microusd=7, cap_microusd=100)
        if uri == "cas://poisoned-dict":
            return {"not": "a frozen model"}  # degraded serde load
        raise UnknownArtifactError(uri)

    h = check_checkpoint_health(
        run_id=RUN,
        checkpoint_uri="cas://poisoned-dict",
        seq=SEQ,
        resolve_body=resolve_body,  # type: ignore[arg-type]
        last_clean_checkpoint_uri="cas://clean",
    )
    assert h.clean is False  # non-frozen body ⇒ crash-on-resume ⇒ poisoned
    recovered = recover_to_last_clean(h, resolve_body=resolve_body)  # type: ignore[arg-type]
    assert isinstance(recovered, BudgetLedger)
    assert recovered.spend_microusd == 7
