"""Operator surface — fleet dashboard aggregation + SLO evaluation. (operations.md §1/§2)

This is the Phase-3 EXIT deliverable: *an operator can diagnose a stuck run*.
The operator does NOT re-derive run liveness — it COMPOSES the per-run
:func:`~foundry.operations.run_status.derive_status` classification across the
whole fleet into one operator-facing view, then evaluates SLO targets over that
aggregate and emits :class:`~foundry.schemas.operations.Alert`s on breach.

Two surfaces:

* :func:`dashboard` — aggregate per-run signals (or already-derived records)
  into an :class:`OperatorView`: counts by :class:`RunStatus`, the list of
  alertable (WEDGED) runs, and for each stuck run a DIAGNOSABLE
  :class:`StuckRunSummary` answering the most common Day-2 question — WHAT is
  this run wedged on (phase, last-progress heartbeat, derived deadline, how
  late, and explicitly *why it is wedged and not legitimately paused*). A
  purely RUNNING/PAUSED fleet yields no stuck runs and no false alerts.
* :func:`evaluate_slos` — evaluate SLO targets (e.g. fraction of runs healthy,
  max WEDGED count, oldest stuck-run age) against the view and emit Alerts with
  the :class:`AlertSeverity` taxonomy on breach; within SLO emits nothing.

Pure / deterministic: consumes structured records only (no live I/O, no
wall-clock read — the clock is the injected ``now_ms`` already baked into the
records via run_status). Ratios are carried as integer millionths (`*_micro`),
mirroring :class:`~foundry.schemas.operations.SLOTarget.objective_micro`, so no
float ever enters an SLO comparison (architecture.md §8 integer discipline).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from foundry.operations.run_status import RunSignals, derive_status
from foundry.schemas.operations import (
    Alert,
    AlertSeverity,
    RunStatus,
    RunStatusRecord,
    SLOTarget,
)
from foundry.state.seq import Seq

#: One million — ratios are carried as integer millionths (no float in SLOs).
MICRO = 1_000_000

# --- SLO names the built-in monitors evaluate (stable strings) -------------

SLO_RUN_HEALTH_RATIO = "run_health_ratio"  # fraction of non-terminal runs that are healthy
SLO_MAX_WEDGED = "max_wedged_count"  # ceiling on simultaneously WEDGED runs
SLO_MAX_STUCK_AGE = "max_stuck_age_ms"  # ceiling on the oldest stuck run's lateness

#: Alert names the operator monitors emit (1:1 with the SLO monitors, §1).
ALERT_RUN_HEALTH = "run_health_slo_breach"
ALERT_WEDGED_COUNT = "wedged_count_slo_breach"
ALERT_STUCK_AGE = "stuck_run_age_slo_breach"

#: The deterministic monitor this surface maps to in the §1 alert taxonomy.
_MONITOR = "operator_dashboard"


@dataclass(frozen=True)
class StuckRunSummary:
    """The DIAGNOSABLE view of one WEDGED run — the EXIT-criterion payload.

    Carries everything an operator needs to tell WHAT a stuck run is stuck on
    WITHOUT opening the run: which `phase` it wedged in, the `last_progress`
    heartbeat marker (seq + wall-clock ts) that failed to advance, the DERIVED
    `deadline_ms` it blew past, `overdue_ms` (how far past — `now_ms` minus the
    deadline), and `reason` — the explicit, operator-facing explanation of why
    this read WEDGED rather than legitimately PAUSED (no progress heartbeat
    advanced past the derived deadline and no legitimate pause reason is set).
    `cost_rate_microusd_per_hr` is the live burn the wedge is wasting (a paused
    run would be $0/hr; a wedged run is not).
    """

    run_id: str
    current_phase: str
    last_progress_seq: Seq | None
    last_progress_ts_ms: int
    deadline_ms: int
    overdue_ms: int
    cost_rate_microusd_per_hr: int
    reason: str


@dataclass(frozen=True)
class OperatorView:
    """The aggregated fleet view the operator dashboard renders. (§1/§2)

    `counts` is the run tally by :class:`RunStatus` (every status key present,
    zero-filled, so the dashboard never has a missing tile). `total` is the run
    count; `healthy` is RUNNING+PAUSED (alive); `stuck` is the diagnosable
    summaries of the WEDGED runs, ordered most-overdue-first so the worst wedge
    is at the top. `now_ms` is the (injected) observation clock the view was
    aggregated at — recorded so SLO evaluation is a pure function of the view.
    """

    counts: dict[RunStatus, int]
    total: int
    healthy: int
    stuck: tuple[StuckRunSummary, ...]
    now_ms: int

    def health_ratio_micro(self) -> int:
        """Fraction of NON-TERMINAL runs that are healthy, in millionths.

        Terminal runs (DONE/FAILED/QUARANTINED) are excluded from the
        denominator — a finished or quarantined run is not a liveness signal.
        A fleet with no non-terminal runs is vacuously fully healthy (1.0)."""
        non_terminal = self.total - self._terminal_count()
        if non_terminal <= 0:
            return MICRO
        return (self.healthy * MICRO) // non_terminal

    def wedged_count(self) -> int:
        return self.counts[RunStatus.WEDGED]

    def oldest_stuck_overdue_ms(self) -> int:
        """How far past deadline the WORST wedge is (0 if none stuck)."""
        return self.stuck[0].overdue_ms if self.stuck else 0

    def _terminal_count(self) -> int:
        return (
            self.counts[RunStatus.DONE]
            + self.counts[RunStatus.FAILED]
            + self.counts[RunStatus.QUARANTINED]
        )


# --- aggregation -----------------------------------------------------------

_HEALTHY: frozenset[RunStatus] = frozenset({RunStatus.RUNNING, RunStatus.PAUSED})


def dashboard(
    runs: Iterable[RunStatusRecord],
    *,
    now_ms: int,
) -> OperatorView:
    """Aggregate per-run :class:`RunStatusRecord`s into the fleet view.

    `runs` are already-derived records (produced by
    :func:`~foundry.operations.run_status.derive_status` per run — the operator
    NEVER re-derives the classification, it composes it). `now_ms` is the
    observation clock the records were derived at; it is used to compute how
    overdue each wedged run is. Deterministic: identical records + clock yield
    an identical view (stuck summaries are ordered most-overdue-then-run_id, a
    total order)."""
    counts: dict[RunStatus, int] = {status: 0 for status in RunStatus}
    healthy = 0
    stuck: list[StuckRunSummary] = []

    for record in runs:
        counts[record.status] += 1
        if record.status in _HEALTHY:
            healthy += 1
        if record.is_alertable():  # WEDGED — build the diagnosable summary.
            stuck.append(_summarize_stuck(record, now_ms))

    stuck.sort(key=lambda s: (-s.overdue_ms, s.run_id))
    total = sum(counts.values())
    return OperatorView(
        counts=counts,
        total=total,
        healthy=healthy,
        stuck=tuple(stuck),
        now_ms=now_ms,
    )


def dashboard_from_signals(
    signals: Iterable[RunSignals],
    *,
    now_ms: int,
) -> OperatorView:
    """Convenience: derive each run's status from raw signals, then aggregate.

    Composes :func:`~foundry.operations.run_status.derive_status` across the
    fleet — the same classification a per-run watchdog uses — so a caller
    holding raw :class:`RunSignals` gets the dashboard in one call."""
    records = [derive_status(sig, now_ms) for sig in signals]
    return dashboard(records, now_ms=now_ms)


def _summarize_stuck(record: RunStatusRecord, now_ms: int) -> StuckRunSummary:
    """Build the diagnosable summary for one WEDGED run (the EXIT payload)."""
    overdue = now_ms - record.expected_next_transition_deadline_ms
    reason = (
        f"WEDGED: no progress heartbeat advanced past the derived deadline "
        f"({overdue}ms overdue) and no legitimate pause reason is set "
        f"(phase={record.current_phase!r})"
    )
    return StuckRunSummary(
        run_id=record.run_id,
        current_phase=record.current_phase,
        last_progress_seq=record.last_progress_seq,
        last_progress_ts_ms=record.last_progress_ts_ms,
        deadline_ms=record.expected_next_transition_deadline_ms,
        overdue_ms=overdue,
        cost_rate_microusd_per_hr=record.cost_rate_microusd_per_hr,
        reason=reason,
    )


# --- SLO evaluation --------------------------------------------------------


def evaluate_slos(
    view: OperatorView,
    slos: Iterable[SLOTarget],
    *,
    seq: Seq,
) -> tuple[Alert, ...]:
    """Evaluate SLO targets against the fleet view; emit Alerts on breach.

    Each recognised :class:`SLOTarget` (by `name`) is compared against the
    aggregate; a breach emits one :class:`Alert` at the taxonomy severity. The
    `run_health_ratio` SLO is a FLOOR (observed below objective breaches);
    `max_wedged_count` / `max_stuck_age_ms` are CEILINGS (observed above
    objective breaches). A WEDGED-count or stuck-age breach pages (a stuck run
    wastes spend and blocks delivery); a health-ratio dip below objective warns.
    Within SLO emits nothing. Unknown SLO names are skipped (forward-compatible).
    Deterministic: alerts are returned in the order the SLOs are supplied, each
    carrying `seq` with a distinct `local` so the records are distinct
    identities in the WORM/predicate stores."""
    alerts: list[Alert] = []
    local = 0
    for target in slos:
        breach = _evaluate_one(view, target)
        if breach is None:
            continue
        name, severity, summary = breach
        alerts.append(
            Alert(
                name=name,
                severity=severity,
                monitor=_MONITOR,
                summary=summary,
                run_id=None,  # fleet-level operator SLO
                runbook_uri=target.runbook_uri,
                seq=seq._replace(local=seq.local + local),
            )
        )
        local += 1
    return tuple(alerts)


def _evaluate_one(
    view: OperatorView, target: SLOTarget
) -> tuple[str, AlertSeverity, str] | None:
    """Compare one SLO target against the view; return breach tuple or None."""
    if target.name == SLO_RUN_HEALTH_RATIO:
        observed = view.health_ratio_micro()
        if observed < target.objective_micro:  # floor breached
            return (
                ALERT_RUN_HEALTH,
                AlertSeverity.WARN,
                f"run health {observed}µ below objective "
                f"{target.objective_micro}µ ({view.wedged_count()} wedged "
                f"of {view.total} runs)",
            )
        return None

    if target.name == SLO_MAX_WEDGED:
        observed = view.wedged_count()
        if observed > target.objective_micro:  # ceiling breached
            return (
                ALERT_WEDGED_COUNT,
                AlertSeverity.PAGE,
                f"{observed} runs WEDGED, over the max of "
                f"{target.objective_micro}",
            )
        return None

    if target.name == SLO_MAX_STUCK_AGE:
        observed = view.oldest_stuck_overdue_ms()
        if observed > target.objective_micro:  # ceiling breached
            worst = view.stuck[0]
            return (
                ALERT_STUCK_AGE,
                AlertSeverity.PAGE,
                f"oldest stuck run {worst.run_id!r} is {observed}ms overdue, "
                f"over the max of {target.objective_micro}ms "
                f"(phase={worst.current_phase!r})",
            )
        return None

    return None  # unknown SLO name — forward-compatible, skip.


__all__ = [
    "ALERT_RUN_HEALTH",
    "ALERT_STUCK_AGE",
    "ALERT_WEDGED_COUNT",
    "MICRO",
    "SLO_MAX_STUCK_AGE",
    "SLO_MAX_WEDGED",
    "SLO_RUN_HEALTH_RATIO",
    "OperatorView",
    "StuckRunSummary",
    "dashboard",
    "dashboard_from_signals",
    "evaluate_slos",
]
