"""Phase-3 (Operability & scale) EXIT integration test (T-M3-13).

Proves the four roadmap Phase-3 EXIT criteria (docs/roadmap.md) end-to-end by
COMPOSING the shipped M3 operations modules against their real durability +
schema surfaces — not by re-deriving any of their logic here:

    (a) an operator can DIAGNOSE a stuck run
        — foundry.operations.run_status (derive_status / watchdog) feeding
          foundry.operations.operator (dashboard / StuckRunSummary / evaluate_slos).
    (b) a store outage PAUSES (not corrupts) in-flight runs and RECOVERS
        — foundry.operations.store_resilience over the REAL FoundryCheckpointer
          one-txn/superstep + OutboxRelay idempotency + WORM audit chain, plus
          poisoned-checkpoint detect → fork-to-prior-good recovery.
    (c) concurrent runs DON'T STARVE each other
        — foundry.operations.capacity (weighted-fair CapacityScheduler +
          fleet cost-rate cap + hibernation) run over multiple scheduling rounds.
    (d) HITL backlog is BOUNDED and ALERTED
        — foundry.operations.hitl (ReviewerQueue.alerts bound + SLA control).

Determinism: every clock (``now_ms``) and every ``Seq`` is INJECTED, so each
assertion is reproducible. Each test exercises the load-bearing property
itself (no-corrupt is checked by reading the persisted store; no-starvation is
checked by per-tenant admission counts over rounds), never a smoke proxy.

HONESTY NOTE (mirrored into build/LEARNINGS.md). What is genuinely end-to-end
here vs a dev-mode stand-in:
* (b) outage-no-corrupt + resume is FULLY E2E against the real durability stack:
  a real ``FoundryCheckpointer.superstep_txn`` rolls back, the SQLite outbox is
  read back to prove no partial row persisted, and a real ``OutboxRelay`` drains
  the prior committed effect exactly once. The "store outage" itself is the
  injected ``StoreUnavailable`` signal (the documented dev analogue of a
  Postgres/object-store connection loss; ``sqlite3.OperationalError`` is the
  other recognised signal) — the trip is real code, the unavailability is
  simulated, exactly as operations.md §3 specifies for the dev backend.
* (a)/(c)/(d) are pure deterministic compositions of real module code over
  in-memory fleets; the "fleet", "tenants", and "reviewer queue" are test
  fixtures, but the classification / scheduling / alerting logic under test is
  the shipped production code, unmodified.
"""

from __future__ import annotations

import pytest

from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.outbox import (
    DispatchResult,
    OutboxRelay,
    enqueue,
    verify_audit_chain,
)
from foundry.operations.capacity import (
    REASON_FLEET_COST_RATE_CAP,
    CapacityScheduler,
    FleetCapacity,
    RunningRun,
)
from foundry.operations.hitl import HITLProfile, ReviewerQueue
from foundry.operations.operator import (
    ALERT_STUCK_AGE,
    ALERT_WEDGED_COUNT,
    SLO_MAX_STUCK_AGE,
    SLO_MAX_WEDGED,
    dashboard_from_signals,
    evaluate_slos,
)
from foundry.operations.run_status import (
    DeadlineModel,
    RunSignals,
    watchdog,
)
from foundry.operations.store_resilience import (
    StoreUnavailable,
    check_checkpoint_health,
    recover_to_last_clean,
    resume_after_outage,
    with_store_outage_handling,
)
from foundry.schemas.base import (
    BudgetLedger,
    ExternalAction,
    IdempotencyPath,
)
from foundry.schemas.operability import (
    AlertSeverity,
    PauseReason,
    QueueEntry,
    ReviewerQueueEntry,
    RunStatus,
    SLOTarget,
    TenantQuota,
)
from foundry.state.seq import Seq

# A single injected per-record Seq is enough — these tests assert behaviour, not
# the seq total order (which the per-module suites already pin).
SEQ = Seq(run_id="phase3-exit", emission_epoch=1, superstep_index=0, node_id="exit", local=0)

# A per-node-type derived-deadline model: budget = 100 + 20 + 30 = 150ms.
DEADLINE = DeadlineModel(p99_ms=100, provider_backoff_ms=20, queue_wait_ms=30)
BUDGET = 150
LAST_PROGRESS = 1_000


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


# ===========================================================================
# (a) An operator can DIAGNOSE a stuck run.
# ===========================================================================

# A clock well past the derived deadline so a no-heartbeat run reads WEDGED.
NOW = LAST_PROGRESS + BUDGET + 2_000  # the wedged run is 2000ms overdue


def test_a_operator_diagnoses_stuck_run_in_mixed_fleet() -> None:
    """A mixed RUNNING/PAUSED/WEDGED fleet — derived per-run via the real
    run_status model — surfaces the WEDGED run with a DIAGNOSABLE summary, while
    the legitimately PAUSED run is NOT flagged as stuck (the load-bearing
    paused-vs-wedged distinction). This is the EXIT criterion's whole point."""
    fleet = [
        # progressing: a recent heartbeat keeps it within the derived deadline.
        _signals("r-running", last_progress_ts_ms=NOW - 10),
        # legitimately paused on HITL — alive, $0/hr, must NOT read as stuck.
        _signals(
            "r-paused-hitl",
            pause_reason=PauseReason.HITL_WAIT,
            awaiting="two-person release sign-off",
            cost_rate_microusd_per_hr=0,
        ),
        # legitimately paused on backpressure — also must NOT read as stuck.
        _signals(
            "r-paused-capacity",
            pause_reason=PauseReason.CAPACITY,
            awaiting="admission slot",
            cost_rate_microusd_per_hr=0,
        ),
        # WEDGED: no heartbeat advanced past the derived deadline, no pause reason.
        _signals("r-wedged", current_phase="quality"),
    ]

    view = dashboard_from_signals(fleet, now_ms=NOW)

    # The fleet is classified correctly by the composed derive_status.
    assert view.counts[RunStatus.RUNNING] == 1
    assert view.counts[RunStatus.PAUSED] == 2
    assert view.counts[RunStatus.WEDGED] == 1
    assert view.healthy == 3  # RUNNING + the two PAUSED runs are alive

    # Exactly the wedged run is surfaced as stuck — neither paused run is.
    assert [s.run_id for s in view.stuck] == ["r-wedged"]
    s = view.stuck[0]
    # Diagnosable WITHOUT opening the run: phase + last-progress + how-overdue +
    # an explicit why-wedged-not-paused reason + the live spend it is wasting.
    assert s.current_phase == "quality"
    assert s.last_progress_ts_ms == LAST_PROGRESS
    assert s.deadline_ms == LAST_PROGRESS + BUDGET
    assert s.overdue_ms == 2_000
    assert "WEDGED" in s.reason and "pause" in s.reason.lower()
    assert "quality" in s.reason
    assert s.cost_rate_microusd_per_hr > 0  # a paused run would be $0/hr

    # The watchdog the supervisor relies on flags ONLY the wedged run.
    flagged = [
        sig.run_id
        for sig in fleet
        if watchdog(sig, NOW) is not None
    ]
    assert flagged == ["r-wedged"]

    # And the operator SLO surface pages on the wedge (not on the paused runs).
    alerts = evaluate_slos(
        view,
        [
            SLOTarget(name=SLO_MAX_WEDGED, objective_micro=0, unit="count"),
            SLOTarget(name=SLO_MAX_STUCK_AGE, objective_micro=1_000, unit="ms"),
        ],
        seq=SEQ,
    )
    names = {a.name: a for a in alerts}
    assert names[ALERT_WEDGED_COUNT].severity is AlertSeverity.PAGE
    assert names[ALERT_STUCK_AGE].severity is AlertSeverity.PAGE  # 2000ms > 1000ms
    assert "r-wedged" in names[ALERT_STUCK_AGE].summary


def test_a_paused_run_past_deadline_is_not_alerted() -> None:
    """The boundary that must NOT alert: a legitimately PAUSED run observed FAR
    past its derived deadline is still alive (intentional wait), not wedged."""
    paused = _signals(
        "r-paused",
        pause_reason=PauseReason.STORE_OUTAGE,
        awaiting="store recovery",
        cost_rate_microusd_per_hr=0,
    )
    # 100x past the deadline — a wedge detector keyed on the clock alone would
    # false-page here; the pause-reason is what keeps it alive.
    assert watchdog(paused, LAST_PROGRESS + BUDGET * 100) is None
    view = dashboard_from_signals([paused], now_ms=LAST_PROGRESS + BUDGET * 100)
    assert view.stuck == ()
    assert view.wedged_count() == 0


# ===========================================================================
# (b) A store outage PAUSES (not corrupts) in-flight runs and RECOVERS.
# ===========================================================================


def _action(key: str) -> ExternalAction:
    return ExternalAction(
        idempotency_key=key,
        kind="git_push",
        payload_uri="cas://sha256/deadbeef",
        path=IdempotencyPath.CLIENT_KEY,
        seq=SEQ,
    )


def test_b_store_outage_pauses_no_corruption_then_resumes_once() -> None:
    """A store-unavailability signal mid-superstep → the run goes
    PAUSED(store_outage), NO partial/corrupt state persists (the last committed
    checkpoint/outbox row is intact + WORM chain unbroken), and resume continues
    from the last committed effect with NO duplicate external action.

    Genuinely E2E: the no-corrupt property is proved by READING the real SQLite
    outbox back after the rollback, and the no-duplicate property by draining a
    real OutboxRelay twice."""
    ckpt = FoundryCheckpointer()
    conn = ckpt.conn

    # A clean PRIOR superstep commits one outbox row — the resume point.
    with ckpt.superstep_txn() as c:
        enqueue(c, _action("push-A"), {"branch": "feature/x"})
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

    # The NEXT superstep writes a row that WOULD corrupt state if persisted,
    # then the store goes down before commit.
    paused: list = []
    with with_store_outage_handling(
        ckpt,
        run_id="run-outage",
        current_phase="implementation",
        now_ms=NOW,
        seq=SEQ,
        on_pause=paused.append,
    ) as c:
        enqueue(c, _action("push-B"), {"branch": "feature/y"})
        raise StoreUnavailable("postgres unreachable mid-superstep")

    # PAUSE, not corrupt nor fail: exactly one paused record, store_outage, $0/hr,
    # NOT alertable (a legitimate wait, consistent with criterion (a)).
    assert len(paused) == 1
    rec = paused[0]
    assert rec.status is RunStatus.PAUSED
    assert rec.pause_reason is PauseReason.STORE_OUTAGE
    assert rec.cost_rate_microusd_per_hr == 0
    assert not rec.is_alertable()

    # NO partial state persisted: the in-flight 'push-B' rolled back with the
    # superstep txn — only the prior committed 'push-A' remains, chain intact.
    rows = conn.execute("SELECT idempotency_key FROM outbox ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["push-A"]
    assert verify_audit_chain(conn)

    # Store returns → resume reconciles the relay and drains the committed effect
    # EXACTLY once (the at-least-once + idempotent outbox never double-fires).
    dispatched: list[str] = []

    def dispatch(kind: str, payload: dict, key: str) -> DispatchResult:
        dispatched.append(key)
        return DispatchResult(ok=True)

    relay = OutboxRelay(conn, dispatch)
    assert resume_after_outage(ckpt, reconcile_relay=relay.recover) is True
    relay.drain()
    relay.drain()  # re-draining is a no-op — the confirmed row never re-fires.
    assert dispatched == ["push-A"]
    ckpt.close()


def test_b_poisoned_checkpoint_detected_and_rolled_back() -> None:
    """A poisoned checkpoint is DETECTED and recovery rolls back to the last
    clean checkpoint; the poisoned body is NEVER loaded as trusted state."""
    store = ArtifactStore()
    clean = BudgetLedger(spend_microusd=1_000, cap_microusd=10_000)
    clean_uri = store.put_body(clean)
    poisoned_uri = "cas://sha256/never-stored-crash-on-resume"

    health = check_checkpoint_health(
        run_id="run-poison",
        checkpoint_uri=poisoned_uri,  # does not resolve → crash-on-resume
        seq=SEQ,
        resolve_body=store.get_body,
        last_clean_checkpoint_uri=clean_uri,
    )
    assert health.clean is False
    assert health.quarantined is True

    recovered = recover_to_last_clean(health, resolve_body=store.get_body)
    # We get the LAST CLEAN body back — never the poisoned checkpoint.
    assert isinstance(recovered, BudgetLedger)
    assert recovered.spend_microusd == 1_000


# ===========================================================================
# (c) Concurrent runs DON'T STARVE each other.
# ===========================================================================


def _quota(tid: str, *, weight: int = 1) -> TenantQuota:
    return TenantQuota(
        tenant_id=tid,
        cost_rate_cap_microusd_per_hr=10_000_000,
        fair_share_micro=weight,
        seq=SEQ,
    )


def _entry(run_id: str, tenant: str, *, rate: int = 0, ts: int = 0) -> QueueEntry:
    return QueueEntry(
        run_id=run_id,
        tenant_id=tenant,
        enqueued_ts_ms=ts,
        requested_cost_rate_microusd_per_hr=rate,
        seq=SEQ,
    )


def test_c_no_starvation_every_tenant_makes_progress() -> None:
    """N tenants contend for M < N slots over several rounds; admitted runs
    finish each round so the next round re-contends. EVERY tenant must be
    admitted at least once (no starvation), and no tenant monopolizes the fleet.
    Genuinely exercised: we count per-tenant admissions across rounds, not a
    single-round smoke check."""
    tenants = ["t1", "t2", "t3", "t4", "t5"]
    sched = CapacityScheduler(
        capacity=FleetCapacity(
            max_concurrent_runs=2, fleet_cost_rate_cap_microusd_per_hr=10**12
        ),
        quotas={t: _quota(t) for t in tenants},
    )
    admitted_per_tenant: dict[str, int] = {t: 0 for t in tenants}

    rounds = 10
    for rnd in range(rounds):
        queue = [_entry(f"{t}-r{rnd}", t, ts=rnd) for t in tenants]
        decisions = sched.schedule(queue, now_ms=rnd, seq=SEQ)
        admitted = [d for d in decisions if d.admitted]
        assert len(admitted) == 2  # exactly M slots filled each round
        for d in admitted:
            admitted_per_tenant[d.run_id.split("-")[0]] += 1
            sched.release(d.run_id)  # run finishes; slot freed for next round

    # NO tenant starved — every one made progress.
    assert all(c > 0 for c in admitted_per_tenant.values()), admitted_per_tenant
    # No monopoly: a runaway tenant would hold most of 20 admits; the spread is
    # tight under equal weight (anti-monopoly + deficit-RR fairness).
    assert max(admitted_per_tenant.values()) <= 6, admitted_per_tenant


def test_c_fleet_cost_rate_cap_throttles_admission_never_kills() -> None:
    """The fleet cost-rate cap bounds aggregate spend by THROTTLING admission
    (the over-cap run stays queued), never by killing/overspending. A run
    already in flight is never preempted by the cap."""
    sched = CapacityScheduler(
        capacity=FleetCapacity(
            max_concurrent_runs=10, fleet_cost_rate_cap_microusd_per_hr=100
        ),
        quotas={"t1": _quota("t1")},
    )
    # An already-running run at 60µ$/hr is registered (in flight).
    sched.register_running(
        RunningRun(run_id="inflight", tenant_id="t1", cost_rate_microusd_per_hr=60)
    )
    # A second 60µ$/hr run would push the fleet to 120 > cap 100 → throttled.
    decisions = sched.schedule([_entry("b", "t1", rate=60)], now_ms=0, seq=SEQ)
    assert decisions[0].admitted is False
    assert decisions[0].reason == REASON_FLEET_COST_RATE_CAP
    # The in-flight run was NOT killed — it still holds its slot + spend rate,
    # and the fleet never exceeded the cap.
    assert sched.fleet_cost_rate_microusd_per_hr() == 60
    assert sched.fleet_cost_rate_microusd_per_hr() <= 100
    assert any(r.run_id == "inflight" for r in sched.active_runs())


def test_c_hibernation_frees_a_slot_and_is_resumable() -> None:
    """A long-idle run hibernates: it FREES its slot (so a starved contender is
    admitted) and stays RESUMABLE — distinct from PAUSED (which keeps its slot)."""
    sched = CapacityScheduler(
        capacity=FleetCapacity(
            max_concurrent_runs=1, fleet_cost_rate_cap_microusd_per_hr=10**12
        ),
        quotas={"t1": _quota("t1"), "t2": _quota("t2")},
        hibernate_idle_ms=1_000,
    )
    sched.register_running(
        RunningRun(
            run_id="idle", tenant_id="t1", cost_rate_microusd_per_hr=500, last_progress_ts_ms=0
        )
    )
    # Single slot taken → the contender from t2 is throttled (queued, not killed).
    blocked = sched.schedule([_entry("waiting", "t2")], now_ms=500, seq=SEQ)
    assert blocked[0].admitted is False

    run = sched.active_runs()[0]
    assert sched.should_hibernate(run, now_ms=2_000) is True  # past idle threshold
    hib = sched.hibernate("idle", now_ms=2_000, seq=SEQ, incremental_footprint_bytes=4096)
    assert hib.status is RunStatus.HIBERNATED
    assert hib.cost_rate_microusd_per_hr == 0  # $0/hr while hibernated

    # The freed slot lets the previously-starved contender in.
    after = sched.schedule([_entry("waiting", "t2")], now_ms=2_000, seq=SEQ)
    assert after[0].admitted is True
    # Resumable: re-register on wake.
    sched.register_running(RunningRun(run_id="idle", tenant_id="t1", last_progress_ts_ms=2_000))
    assert any(r.run_id == "idle" for r in sched.active_runs())


# ===========================================================================
# (d) HITL backlog is BOUNDED and ALERTED.
# ===========================================================================


def _review_entry(
    *,
    decision_class: str = "release_signoff",
    enqueued_ts_ms: int = 0,
    sla_deadline_ms: int = 0,
    run_id: str = "r-hitl",
) -> ReviewerQueueEntry:
    return ReviewerQueueEntry(
        run_id=run_id,
        decision_class=decision_class,
        subject_artifact_uri=f"cas://sha256/{run_id}-{decision_class}",
        enqueued_ts_ms=enqueued_ts_ms,
        sla_deadline_ms=sla_deadline_ms,
        seq=SEQ,
    )


def test_d_hitl_backlog_bounded_alerted_and_within_bound_silent() -> None:
    """The backlog cannot grow unbounded SILENTLY: over the bound pages; an SLA
    breach warns; within bound + within SLA emits nothing."""
    # Within bound + within SLA → no alert.
    quiet = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=3)
    for i in range(2):
        assert quiet.enqueue(_review_entry(enqueued_ts_ms=i, sla_deadline_ms=10_000))
    assert quiet.alerts(now_ms=5_000, seq=SEQ) == []

    # Over the bound → a PAGE backlog-exceeded alert.
    flooded = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=2)
    for i in range(3):  # 3 > bound of 2
        assert flooded.enqueue(_review_entry(enqueued_ts_ms=i, sla_deadline_ms=10_000))
    bound_alerts = flooded.alerts(now_ms=1_000, seq=SEQ)
    assert len(bound_alerts) == 1
    assert bound_alerts[0].name == "hitl_backlog_exceeded"
    assert bound_alerts[0].severity is AlertSeverity.PAGE

    # An SLA breach → a WARN, even when the backlog itself is within bound.
    late = ReviewerQueue(profile=HITLProfile.REGULATED, max_backlog=10)
    late.enqueue(_review_entry(sla_deadline_ms=1_000))
    sla_alerts = late.alerts(now_ms=2_000, seq=SEQ)  # now past the deadline
    assert len(sla_alerts) == 1
    assert sla_alerts[0].name == "hitl_sla_breach"
    assert sla_alerts[0].severity is AlertSeverity.WARN


def test_d_self_judge_profile_keeps_queue_dormant() -> None:
    """The chosen zero-HITL self-judge profile keeps the queue DORMANT
    (recorded-but-no-op sign-offs don't pile load) — so it cannot grow a phantom
    backlog — while a regulated touchpoint still vetoes zero-HITL and goes live."""
    q = ReviewerQueue(profile=HITLProfile.SELF_JUDGE, max_backlog=1)
    assert q.enqueue(_review_entry()) is False  # non-regulated → dropped
    assert q.entries == ()
    assert q.alerts(now_ms=10_000, seq=SEQ) == []  # dormant → never alerts
    # A regulated touchpoint forces the queue live even under self-judge.
    assert q.enqueue(_review_entry(sla_deadline_ms=1_000), regulated=True) is True
    assert q.load(now_ms=0, seq=SEQ).backlog_depth == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
