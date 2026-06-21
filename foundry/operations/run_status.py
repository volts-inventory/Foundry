"""Run-status liveness model + supervisor watchdog (operations.md §2).

The load-bearing Day-2 distinction is **PAUSED vs WEDGED**:

* **PAUSED** — a *legitimate, intentional* wait (HITL, store outage,
  trusted-component outage, capacity/backpressure, non-convergence, an explicit
  operator hold, …). It is machine-readably alive: it carries a `PauseReason` +
  an `awaiting` string and burns `$0/hr`. A paused run is NOT alerted.
* **WEDGED** — *stuck*. No progress heartbeat (superstep / checkpoint /
  adapter / partial-progress marker) has advanced past the run's **derived**
  deadline, and the run is NOT in a legitimate pause reason. A wedged run is
  alerted (`RunStatusRecord.is_alertable()`).

This module is the deterministic derivation that turns a run's observable
signals into a :class:`~foundry.schemas.operations.RunStatusRecord`, plus the
:func:`watchdog` the operator relies on to flag stuck runs. The operator
surface (T-M3-03) composes this; it never re-derives the classification.

Determinism: the clock is **injected** as ``now_ms: Callable[[], int]`` (or a
captured ``now_ms`` reading), never read off the wall clock here, so tests are
reproducible. Money is integer micro-USD (architecture.md §8); a paused run is
``$0/hr`` by construction.

Deadline derivation (operations.md §2) is **not a static constant** — a fixed
deadline is either noisy (false ``wedged`` on slow nodes) or slow (late
detection on fast ones). It is derived per node type from a maintained latency
distribution:

    p99(node_type) + provider-backoff allowance + expected queue-wait
                   + (cold-tier first-byte-restore term, on rehydration)

The cold-tier term is added only for a run reconstituting from a
cold/archival-evicted hibernation (operations.md §6), so a rehydrating run does
not page false ``wedged`` before its first byte returns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from foundry.schemas.operations import (
    PauseReason,
    RunStatus,
    RunStatusRecord,
)
from foundry.state.seq import Seq

#: Terminal statuses are not subject to liveness derivation — they never become
#: WEDGED and never carry a pause reason.
_TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.DONE, RunStatus.FAILED, RunStatus.QUARANTINED}
)


@dataclass(frozen=True)
class DeadlineModel:
    """The per-node-type latency model the derived deadline is built from.

    All terms are in milliseconds. ``p99_ms`` is the maintained p99 wall-clock
    for the node type; ``provider_backoff_ms`` covers cross-family failover
    backoff (§4); ``queue_wait_ms`` covers runner/admission queueing (§6);
    ``cold_tier_restore_ms`` is the first-byte-restore term added ONLY when the
    run is rehydrating from cold/archival-evicted hibernation (§6) — otherwise a
    rehydrating run would page a false ``wedged`` before its first byte returns.
    """

    p99_ms: int
    provider_backoff_ms: int = 0
    queue_wait_ms: int = 0
    cold_tier_restore_ms: int = 0

    def derive(self, *, rehydrating: bool = False) -> int:
        """The derived deadline budget (ms past last progress).

        The cold-tier term is included only while reconstituting from a
        cold/archival-evicted hibernation.
        """
        budget = self.p99_ms + self.provider_backoff_ms + self.queue_wait_ms
        if rehydrating:
            budget += self.cold_tier_restore_ms
        return budget


@dataclass(frozen=True)
class RunSignals:
    """The observable signals a run's liveness is derived from.

    Data inputs only — the clock is injected separately so derivation stays
    deterministic. A run is *legitimately paused* iff ``pause_reason`` is set;
    that single field is what keeps a slow-but-blocked run from being misread as
    WEDGED. ``last_progress_ts_ms``/``last_progress_seq`` are the heartbeat
    marker that an adapter heartbeat or partial-progress marker advances even
    without a superstep (so legitimate provider backoff / cold-tier rehydration
    reads as slow-but-progressing). ``deadline`` is the per-node-type latency
    model the deadline is derived from. ``rehydrating`` adds the cold-tier
    restore term to that derivation (§6).
    """

    run_id: str
    current_phase: str
    last_progress_ts_ms: int
    deadline: DeadlineModel
    seq: Seq
    pause_reason: PauseReason | None = None
    awaiting: str = ""
    last_progress_seq: Seq | None = None
    cost_rate_microusd_per_hr: int = 0  # ignored (forced 0) when paused
    state_footprint_bytes: int = 0
    rehydrating: bool = False
    terminal: RunStatus | None = None  # DONE/FAILED/QUARANTINED, if already so


def derive_deadline_ms(signals: RunSignals, *, since_ms: int | None = None) -> int:
    """Absolute wall-clock deadline (ms) by which the next progress is expected.

    ``since_ms`` defaults to the run's last progress timestamp — the deadline is
    a budget measured forward from the last heartbeat, not from now.
    """
    base = signals.last_progress_ts_ms if since_ms is None else since_ms
    return base + signals.deadline.derive(rehydrating=signals.rehydrating)


def is_past_deadline(signals: RunSignals, now_ms: int) -> bool:
    """True iff no heartbeat has advanced past the run's DERIVED deadline.

    The strict ``>`` makes the boundary exact: a run observed exactly AT its
    deadline is still alive (not yet wedged); only past it is the no-heartbeat
    case escalated (operations.md §2).
    """
    return now_ms > derive_deadline_ms(signals)


def derive_status(signals: RunSignals, now_ms: int) -> RunStatusRecord:
    """Classify a run into a :class:`RunStatusRecord` from its signals + clock.

    Order of decision (operations.md §2):

    1. A terminal run (DONE/FAILED/QUARANTINED) is reported verbatim — liveness
       does not apply.
    2. A run with a legitimate ``pause_reason`` is **PAUSED** (alive, ``$0/hr``,
       carries ``awaiting``) — even past its deadline, because the wait is
       intentional. This is the half of the boundary that must NOT alert.
    3. Otherwise, if no heartbeat advanced past the derived deadline, the run is
       **WEDGED** (alertable).
    4. Otherwise it is **RUNNING** (making progress, or within deadline).
    """
    deadline_ms = derive_deadline_ms(signals)

    if signals.terminal is not None:
        if signals.terminal not in _TERMINAL:
            raise ValueError(
                f"terminal must be DONE/FAILED/QUARANTINED, got {signals.terminal}"
            )
        return _record(
            signals,
            status=signals.terminal,
            pause_reason=None,
            awaiting="",
            deadline_ms=deadline_ms,
            cost_rate=0,
        )

    if signals.pause_reason is not None:
        # Legitimate, intentional wait: alive at $0/hr regardless of deadline.
        return _record(
            signals,
            status=RunStatus.PAUSED,
            pause_reason=signals.pause_reason,
            awaiting=signals.awaiting,
            deadline_ms=deadline_ms,
            cost_rate=0,
        )

    if now_ms > deadline_ms:
        # No heartbeat, not legitimately paused, past the derived deadline.
        return _record(
            signals,
            status=RunStatus.WEDGED,
            pause_reason=None,
            awaiting="",
            deadline_ms=deadline_ms,
            cost_rate=signals.cost_rate_microusd_per_hr,
        )

    return _record(
        signals,
        status=RunStatus.RUNNING,
        pause_reason=None,
        awaiting="",
        deadline_ms=deadline_ms,
        cost_rate=signals.cost_rate_microusd_per_hr,
    )


def watchdog(
    signals: RunSignals,
    now_ms: int | Callable[[], int],
) -> RunStatusRecord | None:
    """The stuck-run detector the operator relies on (operations.md §2).

    Given a run's heartbeat + derived deadline, returns an **alertable**
    :class:`RunStatusRecord` (status WEDGED) iff the run is stuck — i.e. it has
    no heartbeat past its derived deadline AND is not legitimately paused/
    terminal. Returns ``None`` for any live run (RUNNING / PAUSED) and for
    terminal runs, so the supervisor escalates exactly the wedged case and
    nothing else. Deterministic, no LLM.

    ``now_ms`` may be an int or a zero-arg clock callable (injected, never a
    wall-clock read), mirroring how the rest of the codebase injects time.
    """
    clock = now_ms() if callable(now_ms) else now_ms
    record = derive_status(signals, clock)
    return record if record.is_alertable() else None


def _record(
    signals: RunSignals,
    *,
    status: RunStatus,
    pause_reason: PauseReason | None,
    awaiting: str,
    deadline_ms: int,
    cost_rate: int,
) -> RunStatusRecord:
    """Assemble the immutable record (single construction site, one invariant).

    Invariant enforced here: ``pause_reason`` is set IFF ``status == PAUSED``;
    a PAUSED run is ``$0/hr`` by construction (machine-readable $0/hr check).
    """
    return RunStatusRecord(
        run_id=signals.run_id,
        status=status,
        current_phase=signals.current_phase,
        pause_reason=pause_reason,
        awaiting=awaiting if status is RunStatus.PAUSED else "",
        last_progress_seq=signals.last_progress_seq,
        last_progress_ts_ms=signals.last_progress_ts_ms,
        expected_next_transition_deadline_ms=deadline_ms,
        cost_rate_microusd_per_hr=cost_rate,
        state_footprint_bytes=signals.state_footprint_bytes,
        seq=signals.seq,
    )


# Re-exported for callers that want the model + status without importing two
# modules (the operator surface composes these).
__all__ = [
    "DeadlineModel",
    "RunSignals",
    "derive_deadline_ms",
    "derive_status",
    "is_past_deadline",
    "watchdog",
]
