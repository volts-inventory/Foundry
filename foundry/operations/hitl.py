"""HITL-load model: reviewer queue + SLA + bounded/alerted backlog. (operations.md §7)

HITL is load-bearing for safety, so its **volume** is a first-class operational
constraint (operations.md §7), not an externality. This module is the Phase-3
EXIT proof that the HITL backlog is **bounded and alerted**:

- A :class:`ReviewerQueue` holds :class:`~foundry.schemas.operations.ReviewerQueueEntry`
  touchpoints (decision_class, optional two_person sign-off, a per-class
  ``sla_deadline_ms``). Drift decisions and release sign-offs are different
  service-time classes; the queue keys ``per_class_backlog`` by ``decision_class``
  so a drift batch (§5 model-rotation spike) doesn't serialize behind routine
  release sign-offs.
- :meth:`ReviewerQueue.load` computes a deterministic
  :class:`~foundry.schemas.operations.HITLLoad` snapshot (backlog depth,
  oldest-age against an injected clock, two-person reviewer-pair availability).
- :meth:`ReviewerQueue.alerts` is the CONTROL: the backlog is BOUNDED by
  ``max_backlog`` and an :class:`~foundry.schemas.operations.Alert` is raised
  when the bound is exceeded OR any entry breaches its SLA. The bound + alert
  together are why the backlog can never grow unbounded *silently* — admission
  throttle is the documented response (operations.md §7).

**Profile (operations.md §7 / governance.md §1e / plan §0.2).** In the
``self_judge`` zero-HITL profile the sign-offs are recorded-but-no-op
(``deferred_log`` delivery — see :mod:`foundry.governance.sign_off` /
:mod:`foundry.governance.human_liaison`), so the queue is DORMANT: a self-judge
sign-off enqueue is dropped (the record is still made elsewhere; it just does
not pile HITL load). In the ``regulated`` / live-HITL fallback the queue goes
LIVE — every touchpoint is enqueued, measured, bounded, and alerted. The
profile is an input (:class:`HITLProfile`); a regulated touchpoint enqueues even
under the self-judge profile (the regulated pack vetoes zero-HITL, mirroring
``HumanLiaison``).

Pure / deterministic: the clock and the SLA/bound thresholds are INJECTED, never
read from wall-clock — same inputs ⇒ same :class:`HITLLoad` and the same alert
list, so the EXIT proof is reproducible.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.operations import (
    Alert,
    AlertSeverity,
    HITLLoad,
    ReviewerQueueEntry,
)
from foundry.state.seq import Seq

__all__ = [
    "HITLProfile",
    "ReviewerQueue",
    "backlog_alert",
    "sla_breach_alerts",
    "sla_breached",
]


class HITLProfile(StrEnum):
    """Which HITL operating profile the reviewer queue runs under (operations.md §7).

    ``SELF_JUDGE`` is the zero-HITL solo-lineage profile (governance.md §1e,
    plan §0.2): sign-offs are recorded-but-no-op, so the queue is DORMANT —
    non-regulated enqueues are dropped and the load model carries no backlog.
    ``REGULATED`` is the live-HITL fallback: every touchpoint is enqueued,
    measured, bounded and alerted.
    """

    SELF_JUDGE = "self_judge"
    REGULATED = "regulated"


# --- pure threshold predicates (clock + thresholds injected) ---------------


def sla_breached(entry: ReviewerQueueEntry, *, now_ms: int) -> bool:
    """True iff `entry` is past its per-class SLA deadline at the injected clock.

    A ``sla_deadline_ms`` of 0 means "no SLA set" — never a breach (so an
    unsized entry cannot spuriously alert).
    """
    return entry.sla_deadline_ms > 0 and now_ms > entry.sla_deadline_ms


def backlog_alert(
    *,
    backlog_depth: int,
    max_backlog: int,
    seq: Seq,
    run_id: str | None = None,
) -> Alert | None:
    """The BOUND control: an :class:`Alert` iff the backlog exceeds its max.

    The backlog cannot grow unbounded *silently* — crossing ``max_backlog``
    deterministically pages so the documented response (batch windows →
    delegated tiers → admission-throttle new High-tier runs, operations.md §7)
    kicks in. ``max_backlog <= 0`` disables the bound (returns None).
    """
    if max_backlog > 0 and backlog_depth > max_backlog:
        return Alert(
            name="hitl_backlog_exceeded",
            severity=AlertSeverity.PAGE,
            monitor="hitl_backlog",
            summary=(
                f"HITL reviewer backlog {backlog_depth} exceeds bound {max_backlog} "
                "— admission-throttle new High-tier runs (operations.md §7)"
            ),
            run_id=run_id,
            seq=seq,
        )
    return None


def sla_breach_alerts(
    entries: tuple[ReviewerQueueEntry, ...],
    *,
    now_ms: int,
    seq: Seq,
) -> list[Alert]:
    """One WARN :class:`Alert` per entry past its SLA deadline (deterministic order).

    Ordered by the entries' own order; each alert gets a distinct
    ``seq._replace(local=i)`` so records under one key stay distinct (mirrors the
    per-record seq convention, LEARNINGS PR2/AC1).
    """
    alerts: list[Alert] = []
    for entry in entries:
        if sla_breached(entry, now_ms=now_ms):
            alerts.append(
                Alert(
                    name="hitl_sla_breach",
                    severity=AlertSeverity.WARN,
                    monitor="hitl_sla",
                    summary=(
                        f"HITL {entry.decision_class} for run {entry.run_id} breached "
                        f"SLA deadline {entry.sla_deadline_ms}ms at {now_ms}ms"
                    ),
                    run_id=entry.run_id,
                    seq=seq._replace(local=seq.local + len(alerts)),
                )
            )
    return alerts


# --- the reviewer queue + load model ---------------------------------------


class ReviewerQueue:
    """A HITL reviewer queue + load model (operations.md §7).

    Pure/deterministic: holds entries in enqueue order; the clock and thresholds
    are passed to :meth:`load`/:meth:`alerts`. The same queue + same inputs
    always yield the same :class:`HITLLoad` and the same alert list.

    The ``profile`` gates DORMANT vs LIVE: under ``SELF_JUDGE`` a non-regulated
    :meth:`enqueue` is a no-op (recorded-but-no-op sign-offs don't pile HITL),
    so the queue stays empty and the load model is dormant; ``REGULATED`` (or a
    ``regulated=True`` touchpoint) enqueues live.
    """

    def __init__(
        self,
        *,
        profile: HITLProfile = HITLProfile.REGULATED,
        max_backlog: int = 0,
        available_reviewer_pairs: int = 0,
    ) -> None:
        self.profile = profile
        self.max_backlog = max_backlog
        self.available_reviewer_pairs = available_reviewer_pairs
        self._entries: list[ReviewerQueueEntry] = []

    @property
    def entries(self) -> tuple[ReviewerQueueEntry, ...]:
        """The live queue, in enqueue order (immutable view)."""
        return tuple(self._entries)

    def enqueue(self, entry: ReviewerQueueEntry, *, regulated: bool = False) -> bool:
        """Add a HITL touchpoint to the queue. Returns True iff it was enqueued.

        DORMANT-vs-LIVE: under the ``SELF_JUDGE`` profile a non-regulated
        touchpoint is DROPPED (the sign-off is recorded-but-no-op elsewhere; it
        does not pile HITL load) and this returns False. A ``regulated=True``
        touchpoint vetoes zero-HITL and is always enqueued (mirrors
        :class:`~foundry.governance.human_liaison.HumanLiaison`'s regulated
        force-blocking), as is any enqueue under the ``REGULATED`` profile.
        """
        if self.profile is HITLProfile.SELF_JUDGE and not regulated:
            return False  # dormant: recorded-but-no-op sign-off, no HITL pile-up
        self._entries.append(entry)
        return True

    def assign(self, *, run_id: str, decision_class: str) -> ReviewerQueueEntry | None:
        """Assign (dequeue) the oldest matching entry to a reviewer pair.

        A ``two_person`` entry requires an available reviewer PAIR — with zero
        pairs it deadlocks SAFELY (stays queued, returns None), never
        auto-approves (operations.md §7). The oldest matching entry (by
        ``enqueued_ts_ms``, then queue order) is selected so service is FIFO per
        class. Decrements the pair count for a two-person assignment.
        """
        candidates = [
            (i, e)
            for i, e in enumerate(self._entries)
            if e.run_id == run_id and e.decision_class == decision_class
        ]
        if not candidates:
            return None
        idx, entry = min(candidates, key=lambda ie: (ie[1].enqueued_ts_ms, ie[0]))
        if entry.two_person:
            if self.available_reviewer_pairs <= 0:
                return None  # safe deadlock — never auto-approve without a pair
            self.available_reviewer_pairs -= 1
        del self._entries[idx]
        return entry

    def load(self, *, now_ms: int, seq: Seq, arrival_rate_micro_per_hr: int = 0) -> HITLLoad:
        """Compute the deterministic :class:`HITLLoad` snapshot (operations.md §7).

        ``oldest_age_ms`` ages the oldest queued entry against the injected
        clock; ``alert_age_ms`` is left at the capacity-derived threshold the
        caller supplies via :meth:`alerts` (recorded here as the bound used).
        """
        backlog = len(self._entries)
        per_class: dict[str, int] = {}
        oldest_age = 0
        for e in self._entries:
            per_class[e.decision_class] = per_class.get(e.decision_class, 0) + 1
            age = now_ms - e.enqueued_ts_ms
            if age > oldest_age:
                oldest_age = age
        return HITLLoad(
            backlog_depth=backlog,
            oldest_age_ms=oldest_age,
            available_reviewer_pairs=self.available_reviewer_pairs,
            arrival_rate_micro_per_hr=arrival_rate_micro_per_hr,
            alert_age_ms=0,
            per_class_backlog=per_class,
            seq=seq,
        )

    def alerts(self, *, now_ms: int, seq: Seq) -> list[Alert]:
        """The CONTROL: alerts iff the backlog exceeds its bound OR an entry breaches SLA.

        This is the EXIT proof that the backlog is bounded + alerted: within the
        bound and within SLA → empty list; over the bound → a PAGE
        backlog-exceeded alert; any entry past its SLA → a WARN per breach. So
        the backlog can never grow unbounded silently.
        """
        out: list[Alert] = []
        run_id = self._entries[0].run_id if self._entries else None
        bound = backlog_alert(
            backlog_depth=len(self._entries),
            max_backlog=self.max_backlog,
            seq=seq,
            run_id=run_id,
        )
        if bound is not None:
            out.append(bound)
        # SLA breaches ride a distinct epoch lane (local offset) so they don't
        # collide with the single backlog alert's seq.
        out.extend(sla_breach_alerts(self.entries, now_ms=now_ms, seq=seq._replace(local=1)))
        return out
