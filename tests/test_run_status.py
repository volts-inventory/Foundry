"""Tests for the run-status liveness model + watchdog (T-M3-02).

Covers the load-bearing PAUSED-vs-WEDGED distinction (operations.md §2): a run
making progress reads RUNNING; a legitimately-paused run reads PAUSED and is
NOT alertable; a run with no heartbeat past its DERIVED deadline reads WEDGED
and IS alertable; the paused/wedged boundary is exercised on both sides; the
watchdog flags only the wedged case; and derivation is deterministic under an
injected clock.
"""

from __future__ import annotations

from foundry.operations.run_status import (
    DeadlineModel,
    RunSignals,
    derive_deadline_ms,
    derive_status,
    is_past_deadline,
    watchdog,
)
from foundry.schemas.operations import PauseReason, RunStatus
from foundry.state.seq import Seq

# A simple per-node-type latency model: deadline budget = 100 + 20 + 30 = 150ms
# (cold-tier term applies only when rehydrating).
DEADLINE = DeadlineModel(
    p99_ms=100,
    provider_backoff_ms=20,
    queue_wait_ms=30,
    cold_tier_restore_ms=600_000,  # 10 min cold-tier first-byte restore
)

LAST_PROGRESS = 1_000  # ms


def _signals(**overrides: object) -> RunSignals:
    base: dict[str, object] = {
        "run_id": "run-1",
        "current_phase": "implementation",
        "last_progress_ts_ms": LAST_PROGRESS,
        "deadline": DEADLINE,
        "seq": Seq("run-1", 1, 5, "supervisor", 0),
        "last_progress_seq": Seq("run-1", 1, 5, "junior_engineer", 0),
        "cost_rate_microusd_per_hr": 500_000,
        "state_footprint_bytes": 4096,
    }
    base.update(overrides)
    return RunSignals(**base)  # type: ignore[arg-type]


# --- deadline derivation ----------------------------------------------------


def test_deadline_is_derived_not_constant() -> None:
    # base = last_progress + (p99 + backoff + queue_wait) = 1000 + 150
    assert derive_deadline_ms(_signals()) == LAST_PROGRESS + 150


def test_rehydration_adds_cold_tier_restore_term() -> None:
    # A cold/archival-evicted rehydrating run gets the cold-tier first-byte term
    # so it does NOT page false-wedged before its first byte returns (§6/§2).
    warm = derive_deadline_ms(_signals(rehydrating=False))
    cold = derive_deadline_ms(_signals(rehydrating=True))
    assert cold - warm == 600_000


# --- RUNNING ----------------------------------------------------------------


def test_making_progress_is_running() -> None:
    # Observed well within the derived deadline.
    rec = derive_status(_signals(), now_ms=LAST_PROGRESS + 10)
    assert rec.status is RunStatus.RUNNING
    assert rec.pause_reason is None
    assert not rec.is_alertable()
    # A running run reports its real cost rate (not forced to 0).
    assert rec.cost_rate_microusd_per_hr == 500_000


# --- PAUSED (legitimate wait) -----------------------------------------------


def test_legitimate_pause_within_deadline_is_paused_not_alertable() -> None:
    rec = derive_status(
        _signals(
            pause_reason=PauseReason.HITL_WAIT,
            awaiting="two-person release sign-off",
        ),
        now_ms=LAST_PROGRESS + 10,
    )
    assert rec.status is RunStatus.PAUSED
    assert rec.pause_reason is PauseReason.HITL_WAIT
    assert rec.awaiting == "two-person release sign-off"
    assert not rec.is_alertable()
    # $0/hr when paused — the machine-readable check (operations.md §2).
    assert rec.cost_rate_microusd_per_hr == 0


def test_legitimate_pause_PAST_deadline_is_still_paused_not_wedged() -> None:
    # The half of the boundary that must NOT alert: a store-outage pause that
    # has sat well past its deadline is still PAUSED (intentional wait), never
    # escalated to WEDGED. This is the load-bearing distinction.
    rec = derive_status(
        _signals(
            pause_reason=PauseReason.STORE_OUTAGE,
            awaiting="postgres availability",
            cost_rate_microusd_per_hr=500_000,
        ),
        now_ms=LAST_PROGRESS + 10_000_000,  # absurdly past deadline
    )
    assert rec.status is RunStatus.PAUSED
    assert not rec.is_alertable()
    assert rec.cost_rate_microusd_per_hr == 0  # forced $0/hr regardless of input


# --- WEDGED (stuck) ---------------------------------------------------------


def test_no_heartbeat_past_deadline_and_not_paused_is_wedged() -> None:
    rec = derive_status(_signals(), now_ms=LAST_PROGRESS + 151)  # 1ms past 150
    assert rec.status is RunStatus.WEDGED
    assert rec.pause_reason is None
    assert rec.is_alertable()


# --- the paused/wedged boundary, both sides ---------------------------------


def test_boundary_exactly_at_deadline_is_alive_just_past_is_wedged() -> None:
    sig = _signals()
    deadline = derive_deadline_ms(sig)  # 1150

    at_deadline = derive_status(sig, now_ms=deadline)
    assert at_deadline.status is RunStatus.RUNNING  # exactly at == still alive
    assert not is_past_deadline(sig, deadline)

    one_past = derive_status(sig, now_ms=deadline + 1)
    assert one_past.status is RunStatus.WEDGED  # one ms past == wedged
    assert is_past_deadline(sig, deadline + 1)


def test_same_clock_paused_alive_but_unpaused_wedged() -> None:
    # Identical heartbeat + deadline + clock: the ONLY difference is whether a
    # legitimate pause reason is present. PAUSED stays alive; absent it, WEDGED.
    now = LAST_PROGRESS + 5_000  # far past the 150ms deadline
    paused = derive_status(
        _signals(pause_reason=PauseReason.CAPACITY, awaiting="admission slot"),
        now_ms=now,
    )
    wedged = derive_status(_signals(), now_ms=now)
    assert paused.status is RunStatus.PAUSED
    assert wedged.status is RunStatus.WEDGED
    assert paused.is_alertable() is False
    assert wedged.is_alertable() is True


# --- watchdog: flags ONLY the wedged case -----------------------------------


def test_watchdog_flags_only_wedged() -> None:
    now = LAST_PROGRESS + 5_000

    # Wedged -> alertable record returned.
    wedged = watchdog(_signals(), now)
    assert wedged is not None
    assert wedged.status is RunStatus.WEDGED
    assert wedged.is_alertable()

    # Legitimately paused -> None (not stuck).
    assert watchdog(_signals(pause_reason=PauseReason.OPERATOR), now) is None

    # Running within deadline -> None.
    assert watchdog(_signals(), LAST_PROGRESS + 10) is None


def test_watchdog_ignores_terminal_runs() -> None:
    now = LAST_PROGRESS + 5_000
    assert watchdog(_signals(terminal=RunStatus.DONE), now) is None
    assert watchdog(_signals(terminal=RunStatus.FAILED), now) is None
    assert watchdog(_signals(terminal=RunStatus.QUARANTINED), now) is None


def test_watchdog_accepts_injected_clock_callable() -> None:
    # The clock is injected (not a wall-clock read), so a callable works too.
    calls = {"n": 0}

    def clock() -> int:
        calls["n"] += 1
        return LAST_PROGRESS + 5_000

    rec = watchdog(_signals(), clock)
    assert rec is not None and rec.status is RunStatus.WEDGED
    assert calls["n"] == 1  # called exactly once, deterministically


# --- terminal passthrough ---------------------------------------------------


def test_terminal_status_reported_verbatim_no_pause_reason() -> None:
    rec = derive_status(_signals(terminal=RunStatus.DONE), now_ms=LAST_PROGRESS)
    assert rec.status is RunStatus.DONE
    assert rec.pause_reason is None
    assert rec.cost_rate_microusd_per_hr == 0
    assert not rec.is_alertable()


# --- determinism ------------------------------------------------------------


def test_derivation_is_deterministic_under_fixed_clock() -> None:
    sig = _signals()
    now = LAST_PROGRESS + 151
    a = derive_status(sig, now_ms=now)
    b = derive_status(sig, now_ms=now)
    # Same signals + same injected clock -> byte-identical record (frozen model).
    assert a == b
    assert a.content_identity() == b.content_identity()
