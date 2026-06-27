"""EXIT proof for T-M3-05 — multi-tenancy / capacity / queue / fairness +
fleet cost-rate cap + hibernation (operations.md §6; Phase-3 exit criterion:
concurrent runs don't starve each other).

Proves: (1) no-starvation — N tenants contend for M<N slots and every tenant
makes progress across rounds while a heavy tenant is throttled, not allowed to
monopolize; (2) the fleet cost-rate cap throttles admission (never silently
overspends); (3) an idle run hibernates (frees its slot) and is resumable;
(4) determinism.
"""

from __future__ import annotations

from foundry.operations.capacity import (
    REASON_ADMITTED,
    REASON_FLEET_COST_RATE_CAP,
    REASON_NO_SLOTS,
    REASON_RESUME_ADMITTED,
    REASON_RESUME_RATE_CAP,
    REASON_TENANT_CONCURRENCY,
    CapacityScheduler,
    FleetCapacity,
    RunningRun,
)
from foundry.schemas.operability import (
    QueueEntry,
    RunStatus,
    TenantQuota,
)
from foundry.state.seq import Seq

SEQ = Seq(run_id="sched", emission_epoch=1, superstep_index=0, node_id="capacity", local=0)


def _quota(
    tid: str, *, rate_cap: int = 10_000_000, weight: int = 1, max_conc: int = 0
) -> TenantQuota:
    return TenantQuota(
        tenant_id=tid,
        cost_rate_cap_microusd_per_hr=rate_cap,
        fair_share_micro=weight,
        max_concurrent_runs=max_conc,
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


def _fresh_scheduler(
    *, slots: int, tenants: list[str], rate_cap: int = 10_000_000_000
) -> CapacityScheduler:
    return CapacityScheduler(
        capacity=FleetCapacity(
            max_concurrent_runs=slots, fleet_cost_rate_cap_microusd_per_hr=rate_cap
        ),
        quotas={t: _quota(t) for t in tenants},
    )


# --- (1) no starvation: N tenants, M < N slots, fairness across rounds ------


def test_no_starvation_every_tenant_makes_progress() -> None:
    """5 tenants contend for 2 slots over several rounds. Each round one run per
    tenant is queued; admitted runs immediately finish (release) so the next
    round re-contends. Every tenant must be admitted at least once across the
    rounds — none is starved."""
    tenants = ["t1", "t2", "t3", "t4", "t5"]
    sched = _fresh_scheduler(slots=2, tenants=tenants)
    admitted_per_tenant: dict[str, int] = {t: 0 for t in tenants}

    rounds = 10
    for rnd in range(rounds):
        queue = [_entry(f"{t}-r{rnd}", t, ts=rnd) for t in tenants]
        decisions = sched.schedule(queue, now_ms=rnd, seq=SEQ)
        admitted = [d for d in decisions if d.admitted]
        assert len(admitted) == 2  # exactly M slots filled each round
        for d in admitted:
            tid = d.run_id.split("-")[0]
            admitted_per_tenant[tid] += 1
            sched.release(d.run_id)  # run finishes; slot freed for next round

    # NO tenant is starved — every one made progress across the rounds.
    assert all(count > 0 for count in admitted_per_tenant.values()), admitted_per_tenant
    # Fairness: with equal weights and 10 rounds × 2 slots = 20 admits over 5
    # tenants (avg 4 each), the spread is tight — no tenant monopolizes. A small
    # spread remains from the deterministic run_id tie-break on the last rounds.
    spread = max(admitted_per_tenant.values()) - min(admitted_per_tenant.values())
    assert spread <= 2, admitted_per_tenant
    # and no tenant ran away with the fleet (a monopolist would be ~10+).
    assert max(admitted_per_tenant.values()) <= 6


def test_no_starvation_unequal_weights_long_lived_runs_hold_slots() -> None:
    """M3-R1-02: unequal-weight, NO-release multi-round contention. Runs are
    long-lived — once admitted they HOLD their slot across rounds (never
    released), so admission only happens as slots trickle free. Even so, with
    very unequal fair-share weights NO tenant starves: every tenant gets at
    least one run in flight over the rounds, and the heavily-weighted tenant
    does not monopolize every slot while a lighter tenant waits.

    This stresses the ranking that the held-slots-first tie-break (anti-monopoly)
    composes with the accrued-credit deficit (no-starvation) even when slots are
    held rather than freed each round."""
    tenants = ["heavy", "mid", "light"]
    weights = {"heavy": 1_000_000, "mid": 100_000, "light": 1}
    sched = CapacityScheduler(
        capacity=FleetCapacity(
            max_concurrent_runs=2, fleet_cost_rate_cap_microusd_per_hr=10**12
        ),
        quotas={t: _quota(t, weight=weights[t]) for t in tenants},
    )

    ever_admitted: set[str] = set()
    # Each round every tenant offers a fresh run; admitted runs are NEVER
    # released (long-lived). One in-flight run finishes only every 3rd round, so
    # capacity trickles — the no-starvation property must hold under scarcity.
    rounds = 30
    for rnd in range(rounds):
        # Re-offer only tenants without a currently in-flight run, plus everyone
        # always re-contends so the queue is non-empty each round.
        queue = [_entry(f"{t}-r{rnd}", t, ts=rnd) for t in tenants]
        decisions = sched.schedule(queue, now_ms=rnd, seq=SEQ)
        for d in decisions:
            if d.admitted:
                ever_admitted.add(d.run_id.split("-")[0])
        # A single long-lived run drains every 3rd round (a slot trickles free),
        # NOT a full per-round release — runs genuinely hold slots across rounds.
        if rnd % 3 == 2 and sched.used_slots() > 0:
            oldest = sched.active_runs()[0]
            sched.release(oldest.run_id)

    # NO tenant starved despite wildly unequal weights + held slots.
    assert ever_admitted == set(tenants), ever_admitted
    # The heaviest tenant did not run away with every slot ever granted — the
    # lighter tenants collectively got in too (anti-monopoly holds under hold).
    assert "light" in ever_admitted and "mid" in ever_admitted


def test_heavy_tenant_is_throttled_not_allowed_to_monopolize() -> None:
    """A heavy tenant floods the queue with many runs; with equal fair-share it
    cannot take both slots while another tenant is also waiting — the scheduler
    spreads admission so the light tenant still gets in."""
    sched = _fresh_scheduler(slots=2, tenants=["heavy", "light"])
    # heavy enqueues 5 runs, light enqueues 1, all same round.
    queue = [_entry(f"heavy-{i}", "heavy", ts=0) for i in range(5)]
    queue.append(_entry("light-0", "light", ts=0))

    decisions = sched.schedule(queue, now_ms=0, seq=SEQ)
    admitted = {d.run_id for d in decisions if d.admitted}
    assert len(admitted) == 2
    # the heavy tenant did NOT take both slots — light got one.
    assert "light-0" in admitted
    assert sum(1 for r in admitted if r.startswith("heavy")) == 1


def test_tenant_concurrency_guard_blocks_monopoly() -> None:
    """The per-tenant max_concurrent_runs (noisy-neighbor guard) throttles a
    tenant's fan-out even when global slots remain."""
    sched = CapacityScheduler(
        capacity=FleetCapacity(max_concurrent_runs=4, fleet_cost_rate_cap_microusd_per_hr=10**12),
        quotas={"t1": _quota("t1", max_conc=1), "t2": _quota("t2", max_conc=1)},
    )
    queue = [_entry(f"t1-{i}", "t1", ts=0) for i in range(3)]
    queue += [_entry(f"t2-{i}", "t2", ts=0) for i in range(3)]
    decisions = sched.schedule(queue, now_ms=0, seq=SEQ)
    admitted = [d for d in decisions if d.admitted]
    # only 1 per tenant despite 4 free slots.
    assert sum(1 for d in admitted if d.run_id.startswith("t1")) == 1
    assert sum(1 for d in admitted if d.run_id.startswith("t2")) == 1
    blocked = [d for d in decisions if not d.admitted]
    assert all(d.reason == REASON_TENANT_CONCURRENCY for d in blocked)


def test_no_slots_reason_when_full() -> None:
    sched = _fresh_scheduler(slots=1, tenants=["t1"])
    decisions = sched.schedule([_entry("a", "t1"), _entry("b", "t1")], now_ms=0, seq=SEQ)
    reasons = {d.run_id: (d.admitted, d.reason) for d in decisions}
    assert sum(1 for v in reasons.values() if v[0]) == 1
    assert any(v == (False, REASON_NO_SLOTS) for v in reasons.values())


# --- (2) fleet cost-rate cap: admission throttled, never overspends ---------


def test_fleet_cost_rate_cap_throttles_admission() -> None:
    """The fleet cost-rate cap bounds aggregate spend rate. With a cap of 100µ$/hr
    and each run requesting 60µ$/hr, only ONE run is admitted; the second is
    throttled (queued) rather than silently overspending."""
    sched = CapacityScheduler(
        capacity=FleetCapacity(max_concurrent_runs=10, fleet_cost_rate_cap_microusd_per_hr=100),
        quotas={"t1": _quota("t1")},
    )
    queue = [_entry("a", "t1", rate=60), _entry("b", "t1", rate=60)]
    decisions = sched.schedule(queue, now_ms=0, seq=SEQ)
    admitted = [d for d in decisions if d.admitted]
    blocked = [d for d in decisions if not d.admitted]
    assert len(admitted) == 1
    assert blocked[0].reason == REASON_FLEET_COST_RATE_CAP
    # the fleet never exceeded the cap.
    assert sched.fleet_cost_rate_microusd_per_hr() == 60
    assert sched.fleet_cost_rate_microusd_per_hr() <= 100
    # the decision carries the aggregate it was made against.
    assert blocked[0].fleet_cost_rate_cap_microusd_per_hr == 100


def test_fleet_cost_rate_ok_projection() -> None:
    sched = _fresh_scheduler(slots=10, tenants=["t1"], rate_cap=100)
    sched.register_running(RunningRun(run_id="x", tenant_id="t1", cost_rate_microusd_per_hr=70))
    assert sched.fleet_cost_rate_ok(30) is True  # 70 + 30 == 100, within cap
    assert sched.fleet_cost_rate_ok(31) is False  # would exceed
    # a paused/hibernated run contributes $0/hr.
    sched.register_running(
        RunningRun(run_id="p", tenant_id="t1", cost_rate_microusd_per_hr=0, status=RunStatus.PAUSED)
    )
    assert sched.fleet_cost_rate_microusd_per_hr() == 70


# --- (2b) resume-admit: the cost-rate cap also gates in-flight rehydration --


def test_resume_admit_throttles_cap_breaching_rehydration() -> None:
    """M3-R1-01: a cold-tier rehydration whose retrieval/egress rate would breach
    the fleet cost-rate cap is QUEUED at resume-admit (not killed, not allowed to
    spike uncapped), mirroring admission-side throttling on the rate dimension."""
    sched = _fresh_scheduler(slots=10, tenants=["t1"], rate_cap=100)
    sched.register_running(
        RunningRun(run_id="inflight", tenant_id="t1", cost_rate_microusd_per_hr=70)
    )
    # a resume whose rehydration rate (40) would push the fleet to 110 > 100.
    decision = sched.resume_admit(_entry("resuming", "t1", rate=40), now_ms=0, seq=SEQ)
    assert decision.admitted is False
    assert decision.reason == REASON_RESUME_RATE_CAP
    assert decision.fleet_cost_rate_cap_microusd_per_hr == 100
    # the in-flight run was NOT killed by the resume-admit check.
    assert any(r.run_id == "inflight" for r in sched.active_runs())


def test_resume_admit_allows_rehydration_within_cap() -> None:
    sched = _fresh_scheduler(slots=10, tenants=["t1"], rate_cap=100)
    sched.register_running(
        RunningRun(run_id="inflight", tenant_id="t1", cost_rate_microusd_per_hr=70)
    )
    decision = sched.resume_admit(_entry("resuming", "t1", rate=30), now_ms=0, seq=SEQ)
    assert decision.admitted is True  # 70 + 30 == 100, within cap
    assert decision.reason == REASON_RESUME_ADMITTED


# --- (3) hibernation: idle run frees its slot, resumable --------------------


def test_idle_run_hibernates_frees_slot_and_resumable() -> None:
    sched = CapacityScheduler(
        capacity=FleetCapacity(max_concurrent_runs=1, fleet_cost_rate_cap_microusd_per_hr=10**12),
        quotas={"t1": _quota("t1"), "t2": _quota("t2")},
        hibernate_idle_ms=1000,
    )
    sched.register_running(
        RunningRun(
            run_id="idle", tenant_id="t1", cost_rate_microusd_per_hr=500, last_progress_ts_ms=0
        )
    )
    # single slot taken → a contending run from another tenant is throttled.
    blocked = sched.admit(_entry("waiting", "t2"), now_ms=500, seq=SEQ)
    assert blocked.admitted is False and blocked.reason == REASON_NO_SLOTS

    run = sched.active_runs()[0]
    # not idle long enough yet.
    assert sched.should_hibernate(run, now_ms=500) is False
    # past the idle threshold → hibernation candidate.
    assert sched.should_hibernate(run, now_ms=2000) is True

    rec = sched.hibernate("idle", now_ms=2000, seq=SEQ, incremental_footprint_bytes=4096)
    assert rec.status is RunStatus.HIBERNATED
    assert rec.cost_rate_microusd_per_hr == 0  # $0/hr while hibernated
    assert rec.state_footprint_bytes == 4096  # incremental CAS footprint
    # the slot is freed → the waiting run can now be admitted.
    assert sched.used_slots() == 0
    now_admit = sched.admit(_entry("waiting", "t2"), now_ms=2000, seq=SEQ)
    assert now_admit.admitted is True and now_admit.reason == REASON_ADMITTED

    # resumable: re-register on wake.
    sched.register_running(RunningRun(run_id="idle", tenant_id="t1", last_progress_ts_ms=2000))
    assert any(r.run_id == "idle" for r in sched.active_runs())


def test_mid_attestation_run_never_hibernated() -> None:
    sched = _fresh_scheduler(slots=2, tenants=["t1"])
    run = RunningRun(
        run_id="att",
        tenant_id="t1",
        last_progress_ts_ms=0,
        mid_trust_root_attestation=True,
    )
    # idle far past threshold, but non-preemptible → never a candidate.
    assert sched.should_hibernate(run, now_ms=10**9) is False


def test_terminal_run_not_hibernation_candidate() -> None:
    sched = _fresh_scheduler(slots=2, tenants=["t1"])
    for st in (RunStatus.DONE, RunStatus.FAILED, RunStatus.HIBERNATED):
        run = RunningRun(run_id="r", tenant_id="t1", last_progress_ts_ms=0, status=st)
        assert sched.should_hibernate(run, now_ms=10**9) is False


# --- (4) determinism --------------------------------------------------------


def test_schedule_is_deterministic() -> None:
    tenants = ["a", "b", "c", "d"]
    queue = [_entry(f"{t}-0", t, ts=i) for i, t in enumerate(tenants)]

    def run_once() -> tuple[tuple[str, bool, str], ...]:
        sched = _fresh_scheduler(slots=2, tenants=tenants)
        decisions = sched.schedule(queue, now_ms=0, seq=SEQ)
        return tuple((d.run_id, d.admitted, d.reason) for d in decisions)

    assert run_once() == run_once()


def test_decisions_returned_in_queue_order() -> None:
    sched = _fresh_scheduler(slots=10, tenants=["t1"])
    queue = [_entry("z", "t1"), _entry("a", "t1"), _entry("m", "t1")]
    decisions = sched.schedule(queue, now_ms=0, seq=SEQ)
    assert [d.run_id for d in decisions] == ["z", "a", "m"]
