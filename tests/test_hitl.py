"""EXIT proof for T-M3-06: HITL-load model + reviewer queue/SLA. (operations.md §7)

Proves the Phase-3 EXIT criterion: the HITL backlog is BOUNDED and ALERTED.

- Bounded + alerted: a backlog over its max raises a PAGE Alert; an entry past
  its SLA raises a WARN Alert; within bound + within SLA → no alert. The bound +
  alert are the control — the backlog can never grow unbounded silently.
- Profile: the self-judge zero-HITL profile keeps the queue DORMANT
  (recorded-but-no-op sign-offs don't pile HITL); the regulated/HITL profile
  goes LIVE and is measured. A regulated touchpoint vetoes zero-HITL.
- Two-person review entries handled (safe deadlock without a pair); determinism.
"""

from __future__ import annotations

from foundry.operations.hitl import (
    HITLProfile,
    ReviewerQueue,
    backlog_alert,
    sla_breach_alerts,
    sla_breached,
)
from foundry.schemas.operations import AlertSeverity, ReviewerQueueEntry
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="hitl", local=0)


def _entry(
    *,
    run_id: str = "r1",
    decision_class: str = "release_signoff",
    two_person: bool = False,
    enqueued_ts_ms: int = 0,
    sla_deadline_ms: int = 0,
) -> ReviewerQueueEntry:
    return ReviewerQueueEntry(
        run_id=run_id,
        decision_class=decision_class,
        subject_artifact_uri=f"cas://sha256/{run_id}-{decision_class}",
        two_person=two_person,
        enqueued_ts_ms=enqueued_ts_ms,
        sla_deadline_ms=sla_deadline_ms,
        seq=SEQ,
    )


# --- Bounded + alerted -----------------------------------------------------


def test_within_bound_and_sla_no_alert() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=3)
    for i in range(2):
        assert q.enqueue(_entry(enqueued_ts_ms=i, sla_deadline_ms=10_000))
    assert q.alerts(now_ms=5_000, seq=SEQ) == []


def test_backlog_over_bound_pages() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=2)
    for i in range(3):  # 3 > bound of 2
        assert q.enqueue(_entry(enqueued_ts_ms=i, sla_deadline_ms=10_000))
    alerts = q.alerts(now_ms=1_000, seq=SEQ)
    assert len(alerts) == 1
    assert alerts[0].name == "hitl_backlog_exceeded"
    assert alerts[0].severity is AlertSeverity.PAGE
    assert alerts[0].monitor == "hitl_backlog"


def test_entry_past_sla_warns() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=10)
    q.enqueue(_entry(sla_deadline_ms=1_000))  # deadline 1s
    alerts = q.alerts(now_ms=2_000, seq=SEQ)  # now past it
    assert len(alerts) == 1
    assert alerts[0].name == "hitl_sla_breach"
    assert alerts[0].severity is AlertSeverity.WARN


def test_bound_and_sla_both_fire() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=1)
    q.enqueue(_entry(decision_class="design_approval", sla_deadline_ms=1_000))
    q.enqueue(_entry(decision_class="release_signoff", sla_deadline_ms=1_000))
    alerts = q.alerts(now_ms=5_000, seq=SEQ)
    names = sorted(a.name for a in alerts)
    # 1 backlog (2 > 1) + 2 SLA breaches:
    assert names == ["hitl_backlog_exceeded", "hitl_sla_breach", "hitl_sla_breach"]


def test_zero_max_backlog_disables_bound() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=0)
    for i in range(100):
        q.enqueue(_entry(enqueued_ts_ms=i, sla_deadline_ms=1_000_000))
    # No bound configured → no backlog alert (but the queue is still measured):
    assert q.alerts(now_ms=0, seq=SEQ) == []
    assert q.load(now_ms=0, seq=SEQ).backlog_depth == 100


def test_zero_sla_deadline_never_breaches() -> None:
    e = _entry(sla_deadline_ms=0)
    assert not sla_breached(e, now_ms=10**9)


def test_backlog_alert_helper_none_within_bound() -> None:
    assert backlog_alert(backlog_depth=2, max_backlog=5, seq=SEQ) is None
    over = backlog_alert(backlog_depth=6, max_backlog=5, seq=SEQ)
    assert over is not None and over.severity is AlertSeverity.PAGE


# --- Profile: self-judge dormant vs regulated live -------------------------


def test_self_judge_profile_dormant() -> None:
    q = ReviewerQueue(profile=HITLProfile.SELF_JUDGE, max_backlog=1)
    # Non-regulated sign-offs are recorded-but-no-op → dropped from the queue:
    assert q.enqueue(_entry()) is False
    assert q.enqueue(_entry()) is False
    assert q.entries == ()
    load = q.load(now_ms=10_000, seq=SEQ)
    assert load.backlog_depth == 0
    assert load.per_class_backlog == {}
    # Dormant → no alerts even though max_backlog is tiny:
    assert q.alerts(now_ms=10_000, seq=SEQ) == []


def test_regulated_touchpoint_vetoes_zero_hitl() -> None:
    q = ReviewerQueue(profile=HITLProfile.SELF_JUDGE, max_backlog=10)
    # A regulated pack forces the touchpoint live even under self_judge:
    assert q.enqueue(_entry(sla_deadline_ms=1_000), regulated=True) is True
    assert q.load(now_ms=0, seq=SEQ).backlog_depth == 1


def test_regulated_profile_live_and_measured() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=10)
    assert q.enqueue(_entry(decision_class="design_approval", enqueued_ts_ms=0)) is True
    assert q.enqueue(_entry(decision_class="release_signoff", enqueued_ts_ms=100)) is True
    assert q.enqueue(_entry(decision_class="release_signoff", enqueued_ts_ms=200)) is True
    load = q.load(now_ms=1_000, seq=SEQ)
    assert load.backlog_depth == 3
    assert load.per_class_backlog == {"design_approval": 1, "release_signoff": 2}
    assert load.oldest_age_ms == 1_000  # now - oldest enqueued_ts (0)


# --- Two-person review + assignment ----------------------------------------


def test_two_person_safe_deadlock_without_pair() -> None:
    q = ReviewerQueue(
        profile=HITLProfile.REGULATED, max_backlog=10, available_reviewer_pairs=0
    )
    q.enqueue(_entry(decision_class="release_signoff", two_person=True))
    # No pair available → deadlocks SAFELY (stays queued, never auto-approves):
    assert q.assign(run_id="r1", decision_class="release_signoff") is None
    assert q.load(now_ms=0, seq=SEQ).backlog_depth == 1


def test_two_person_assigns_with_pair_and_decrements() -> None:
    q = ReviewerQueue(
        profile=HITLProfile.REGULATED, max_backlog=10, available_reviewer_pairs=1
    )
    q.enqueue(_entry(decision_class="release_signoff", two_person=True))
    assigned = q.assign(run_id="r1", decision_class="release_signoff")
    assert assigned is not None and assigned.two_person
    assert q.available_reviewer_pairs == 0
    assert q.load(now_ms=0, seq=SEQ).backlog_depth == 0


def test_assign_picks_oldest_matching_fifo() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=10)
    q.enqueue(_entry(decision_class="design_approval", enqueued_ts_ms=300))
    q.enqueue(_entry(decision_class="design_approval", enqueued_ts_ms=100))
    q.enqueue(_entry(decision_class="design_approval", enqueued_ts_ms=200))
    assigned = q.assign(run_id="r1", decision_class="design_approval")
    assert assigned is not None and assigned.enqueued_ts_ms == 100  # oldest first


def test_assign_no_match_returns_none() -> None:
    q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=10)
    q.enqueue(_entry(decision_class="design_approval"))
    assert q.assign(run_id="r1", decision_class="release_signoff") is None
    assert q.assign(run_id="other", decision_class="design_approval") is None


# --- Determinism -----------------------------------------------------------


def test_load_and_alerts_deterministic() -> None:
    def build() -> ReviewerQueue:
        q = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=1)
        q.enqueue(_entry(decision_class="design_approval", enqueued_ts_ms=0, sla_deadline_ms=1000))
        q.enqueue(_entry(decision_class="release_signoff", enqueued_ts_ms=50, sla_deadline_ms=1000))
        return q

    a, b = build(), build()
    assert a.load(now_ms=5_000, seq=SEQ) == b.load(now_ms=5_000, seq=SEQ)
    alerts_a = a.alerts(now_ms=5_000, seq=SEQ)
    alerts_b = b.alerts(now_ms=5_000, seq=SEQ)
    assert alerts_a == alerts_b
    # Distinct seqs across the alert list (no collision):
    assert len({al.seq for al in alerts_a}) == len(alerts_a)


def test_sla_breach_alerts_distinct_seq_per_entry() -> None:
    entries = (
        _entry(decision_class="design_approval", sla_deadline_ms=1),
        _entry(decision_class="release_signoff", sla_deadline_ms=1),
    )
    alerts = sla_breach_alerts(entries, now_ms=10, seq=SEQ)
    assert len(alerts) == 2
    assert alerts[0].seq.local != alerts[1].seq.local
