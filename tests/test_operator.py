"""Tests for the operator surface — dashboard aggregation + SLOs (T-M3-03).

This is the Phase-3 EXIT proof: *an operator can diagnose a stuck run*. Covers
aggregation of a mixed-state fleet into correct per-status counts; the WEDGED
runs surfaced with a DIAGNOSABLE summary (phase + last-progress heartbeat +
derived deadline + how overdue + the why-wedged-not-paused reason); a purely
RUNNING/PAUSED fleet raising no false stuck alerts; SLO breach emitting an
Alert at the right severity (and within-SLO emitting nothing); and determinism.
"""

from __future__ import annotations

from foundry.operations.operator import (
    ALERT_RUN_HEALTH,
    ALERT_STUCK_AGE,
    ALERT_WEDGED_COUNT,
    MICRO,
    SLO_MAX_STUCK_AGE,
    SLO_MAX_WEDGED,
    SLO_RUN_HEALTH_RATIO,
    dashboard,
    dashboard_from_signals,
    evaluate_slos,
)
from foundry.operations.run_status import DeadlineModel, RunSignals
from foundry.schemas.operations import (
    AlertSeverity,
    PauseReason,
    RunStatus,
    RunStatusRecord,
    SLOTarget,
)
from foundry.state.seq import Seq

DEADLINE = DeadlineModel(p99_ms=100, provider_backoff_ms=20, queue_wait_ms=30)
BUDGET = 150  # p99 + backoff + queue_wait
LAST_PROGRESS = 1_000
SEQ = Seq("op", 1, 0, "operator", 0)


def _signals(run_id: str, **overrides: object) -> RunSignals:
    base: dict[str, object] = {
        "run_id": run_id,
        "current_phase": "implementation",
        "last_progress_ts_ms": LAST_PROGRESS,
        "deadline": DEADLINE,
        "seq": Seq(run_id, 1, 5, "supervisor", 0),
        "last_progress_seq": Seq(run_id, 1, 5, "junior_engineer", 0),
        "cost_rate_microusd_per_hr": 500_000,
        "state_footprint_bytes": 4096,
    }
    base.update(overrides)
    return RunSignals(**base)  # type: ignore[arg-type]


def _record(run_id: str, status: RunStatus, **overrides: object) -> RunStatusRecord:
    base: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "current_phase": "implementation",
        "last_progress_ts_ms": LAST_PROGRESS,
        "expected_next_transition_deadline_ms": LAST_PROGRESS + BUDGET,
        "seq": Seq(run_id, 1, 0, "supervisor", 0),
    }
    base.update(overrides)
    return RunStatusRecord(**base)  # type: ignore[arg-type]


# A clock well past the deadline so wedged runs are overdue.
NOW_WEDGED = LAST_PROGRESS + BUDGET + 1_000  # 1000ms overdue
NOW_LIVE = LAST_PROGRESS + 10  # before the deadline


# --- aggregation ------------------------------------------------------------


def test_mixed_fleet_counts_by_status() -> None:
    records = [
        _record("r-run", RunStatus.RUNNING),
        _record("r-pause", RunStatus.PAUSED, pause_reason=PauseReason.HITL_WAIT,
                cost_rate_microusd_per_hr=0),
        _record("r-wedge", RunStatus.WEDGED),
        _record("r-done", RunStatus.DONE),
        _record("r-fail", RunStatus.FAILED),
    ]
    view = dashboard(records, now_ms=NOW_WEDGED)

    assert view.total == 5
    assert view.counts[RunStatus.RUNNING] == 1
    assert view.counts[RunStatus.PAUSED] == 1
    assert view.counts[RunStatus.WEDGED] == 1
    assert view.counts[RunStatus.DONE] == 1
    assert view.counts[RunStatus.FAILED] == 1
    # every status tile present (zero-filled), so the dashboard never gaps.
    assert set(view.counts) == set(RunStatus)
    assert view.counts[RunStatus.QUARANTINED] == 0
    assert view.healthy == 2  # RUNNING + PAUSED


def test_wedged_run_is_diagnosable() -> None:
    # The EXIT criterion: an operator can tell WHAT a stuck run is stuck on.
    rec = _record("r-wedge", RunStatus.WEDGED, current_phase="implementation",
                  cost_rate_microusd_per_hr=500_000)
    view = dashboard([rec], now_ms=NOW_WEDGED)

    assert len(view.stuck) == 1
    s = view.stuck[0]
    assert s.run_id == "r-wedge"
    # phase present
    assert s.current_phase == "implementation"
    # last-progress heartbeat present
    assert s.last_progress_ts_ms == LAST_PROGRESS
    # derived deadline + how overdue present
    assert s.deadline_ms == LAST_PROGRESS + BUDGET
    assert s.overdue_ms == 1_000
    # the why-wedged-not-paused reason is present and explanatory
    assert "WEDGED" in s.reason
    assert "implementation" in s.reason
    assert "pause" in s.reason.lower()
    # a wedged run is wasting live spend (a paused run would be $0/hr)
    assert s.cost_rate_microusd_per_hr > 0


def test_healthy_fleet_has_no_stuck_runs() -> None:
    # A purely RUNNING/PAUSED fleet → no false stuck alerts.
    records = [
        _record("r-run", RunStatus.RUNNING),
        _record("r-pause", RunStatus.PAUSED, pause_reason=PauseReason.CAPACITY,
                cost_rate_microusd_per_hr=0),
    ]
    view = dashboard(records, now_ms=NOW_WEDGED)
    assert view.stuck == ()
    assert view.wedged_count() == 0
    assert view.health_ratio_micro() == MICRO  # fully healthy


def test_stuck_runs_ordered_most_overdue_first() -> None:
    near = _record("r-near", RunStatus.WEDGED,
                   expected_next_transition_deadline_ms=NOW_WEDGED - 100)
    far = _record("r-far", RunStatus.WEDGED,
                  expected_next_transition_deadline_ms=NOW_WEDGED - 5_000)
    view = dashboard([near, far], now_ms=NOW_WEDGED)
    assert [s.run_id for s in view.stuck] == ["r-far", "r-near"]
    assert view.oldest_stuck_overdue_ms() == 5_000


def test_health_ratio_excludes_terminal_runs() -> None:
    records = [
        _record("r-run", RunStatus.RUNNING),
        _record("r-wedge", RunStatus.WEDGED),
        _record("r-done", RunStatus.DONE),  # terminal → out of denominator
    ]
    view = dashboard(records, now_ms=NOW_WEDGED)
    # 1 healthy / 2 non-terminal = 0.5
    assert view.health_ratio_micro() == MICRO // 2


def test_dashboard_from_signals_composes_derive_status() -> None:
    # Raw signals: one progressing (RUNNING), one paused, one no-heartbeat (WEDGED).
    sigs = [
        # progressing: last heartbeat is recent (within the derived deadline).
        _signals("r-run", last_progress_ts_ms=NOW_WEDGED - 10),
        _signals("r-pause", pause_reason=PauseReason.HITL_WAIT, awaiting="design sign-off"),
        _signals("r-wedge"),  # no heartbeat past the deadline → WEDGED
    ]
    view = dashboard_from_signals(sigs, now_ms=NOW_WEDGED)
    assert view.counts[RunStatus.RUNNING] == 1
    assert view.counts[RunStatus.PAUSED] == 1
    assert view.counts[RunStatus.WEDGED] == 1
    assert [s.run_id for s in view.stuck] == ["r-wedge"]


# --- SLO evaluation ---------------------------------------------------------


def _view(*records: RunStatusRecord, now_ms: int = NOW_WEDGED):
    return dashboard(list(records), now_ms=now_ms)


def test_wedged_count_slo_breach_pages() -> None:
    view = _view(
        _record("w1", RunStatus.WEDGED),
        _record("w2", RunStatus.WEDGED),
    )
    slo = SLOTarget(name=SLO_MAX_WEDGED, objective_micro=1, unit="count")
    alerts = evaluate_slos(view, [slo], seq=SEQ)
    assert len(alerts) == 1
    assert alerts[0].name == ALERT_WEDGED_COUNT
    assert alerts[0].severity is AlertSeverity.PAGE
    assert alerts[0].run_id is None  # fleet-level


def test_wedged_count_within_slo_no_alert() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))
    slo = SLOTarget(name=SLO_MAX_WEDGED, objective_micro=1, unit="count")
    assert evaluate_slos(view, [slo], seq=SEQ) == ()


def test_health_ratio_slo_breach_warns() -> None:
    view = _view(
        _record("r-run", RunStatus.RUNNING),
        _record("r-wedge", RunStatus.WEDGED),
    )  # health = 0.5
    slo = SLOTarget(name=SLO_RUN_HEALTH_RATIO, objective_micro=int(0.99 * MICRO))
    alerts = evaluate_slos(view, [slo], seq=SEQ)
    assert len(alerts) == 1
    assert alerts[0].name == ALERT_RUN_HEALTH
    assert alerts[0].severity is AlertSeverity.WARN


def test_health_ratio_within_slo_no_alert() -> None:
    view = _view(_record("r-run", RunStatus.RUNNING))  # health = 1.0
    slo = SLOTarget(name=SLO_RUN_HEALTH_RATIO, objective_micro=int(0.99 * MICRO))
    assert evaluate_slos(view, [slo], seq=SEQ) == ()


def test_stuck_age_slo_breach_pages() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))  # 1000ms overdue
    slo = SLOTarget(name=SLO_MAX_STUCK_AGE, objective_micro=500, unit="ms")
    alerts = evaluate_slos(view, [slo], seq=SEQ)
    assert len(alerts) == 1
    assert alerts[0].name == ALERT_STUCK_AGE
    assert alerts[0].severity is AlertSeverity.PAGE
    assert "w1" in alerts[0].summary


def test_stuck_age_within_slo_no_alert() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))  # 1000ms overdue
    slo = SLOTarget(name=SLO_MAX_STUCK_AGE, objective_micro=5_000, unit="ms")
    assert evaluate_slos(view, [slo], seq=SEQ) == ()


def test_unknown_slo_name_skipped() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))
    slo = SLOTarget(name="not_a_real_slo", objective_micro=0)
    assert evaluate_slos(view, [slo], seq=SEQ) == ()


def test_multiple_breaches_distinct_seq_locals() -> None:
    view = _view(
        _record("r-run", RunStatus.RUNNING),
        _record("w1", RunStatus.WEDGED),
        _record("w2", RunStatus.WEDGED),
    )
    slos = [
        SLOTarget(name=SLO_MAX_WEDGED, objective_micro=1, unit="count"),
        SLOTarget(name=SLO_MAX_STUCK_AGE, objective_micro=0, unit="ms"),
    ]
    alerts = evaluate_slos(view, slos, seq=SEQ)
    assert len(alerts) == 2
    locals_ = [a.seq.local for a in alerts]
    assert len(set(locals_)) == 2  # distinct identities in the WORM/predicate stores


def test_runbook_uri_propagated_to_alert() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))
    slo = SLOTarget(name=SLO_MAX_WEDGED, objective_micro=0, unit="count",
                    runbook_uri="cas://runbook/wedged")
    alerts = evaluate_slos(view, [slo], seq=SEQ)
    assert alerts[0].runbook_uri == "cas://runbook/wedged"


# --- determinism ------------------------------------------------------------


def test_dashboard_is_deterministic() -> None:
    records = [
        _record("w2", RunStatus.WEDGED,
                expected_next_transition_deadline_ms=NOW_WEDGED - 200),
        _record("r-run", RunStatus.RUNNING),
        _record("w1", RunStatus.WEDGED,
                expected_next_transition_deadline_ms=NOW_WEDGED - 200),
    ]
    v1 = dashboard(list(records), now_ms=NOW_WEDGED)
    v2 = dashboard(list(records), now_ms=NOW_WEDGED)
    assert v1 == v2
    # tie on overdue → ordered by run_id (a total order)
    assert [s.run_id for s in v1.stuck] == ["w1", "w2"]


def test_evaluate_slos_is_deterministic() -> None:
    view = _view(_record("w1", RunStatus.WEDGED))
    slo = SLOTarget(name=SLO_MAX_WEDGED, objective_micro=0, unit="count")
    a1 = evaluate_slos(view, [slo], seq=SEQ)
    a2 = evaluate_slos(view, [slo], seq=SEQ)
    assert a1 == a2
