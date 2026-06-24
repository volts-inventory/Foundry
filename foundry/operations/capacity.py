"""Multi-tenancy, capacity, queue, and fairness scheduler. (operations.md §6)

Foundry runs many concurrent runs (the A/B alone is `3 arms × n × k`), so a
run-admission layer is required: concurrent runs must SHARE limited capacity
(concurrency slots + a FLEET cost-rate cap) without starving each other.

Design (operations.md §6):

- **Admission-side, never kill-side.** On a fleet cost-rate-cap breach or a slot
  shortage the scheduler THROTTLES admission (a run stays queued) — it never
  hard-stalls or kills an in-flight run. Killing wastes spend and starves
  long-horizon runs.
- **Weighted-fair, no-starvation scheduling.** The fleet rate budget is allocated
  by per-tenant fair-share (`TenantQuota.fair_share_micro`) and fed into a
  deficit-style weighted round-robin: each scheduling round every contending
  tenant accrues credit proportional to its weight; the queue is ordered by
  ACCRUED-but-unspent credit (most-starved first), then by enqueue time (FIFO
  within a tie). So a heavy tenant is THROTTLED — it cannot monopolize the slots
  across rounds — and every tenant makes progress over successive rounds.
- **Noisy-neighbor guard.** A per-tenant `max_concurrent_runs` caps one tenant's
  in-flight fan-out so it cannot consume every slot.
- **Fleet cost-rate cap.** `fleet_cost_rate_ok` bounds the aggregate spend RATE
  (integer micro-USD/hr): a candidate is admitted only if the projected fleet
  rate (current in-flight rate + the candidate's requested rate) stays within the
  cap. This mirrors `governance.budget_controller.BudgetController` (the per-run
  budget) — the fleet cap is the cross-run analog, same integer-µ$ accounting.
  Admission-throttle alone cannot reach IN-FLIGHT spend ("resume is not new-run
  admission"), so `resume_admit` applies the SAME cap to the in-flight cost
  sources large enough to outrun an admission freeze — cold-tier rehydration
  retrieval/egress on resume of an archival-evicted hibernated run and in-flight
  quorum re-runs (§7) — queueing/serializing a cap-breaching resume rather than
  letting it spike the rate uncapped (operations.md §6, M3-R1-01).
- **Hibernation.** A long-idle run hibernates: it frees its concurrency slot and
  its worktree spills to the CAS (resumable). This is DISTINCT from PAUSED — a
  paused run still holds its slot/state; a hibernated run has surrendered its
  slot and shrunk its footprint to incremental CAS blobs.

Pure / deterministic: the clock and per-run usage are INJECTED, never read from
the wall clock. All money is integer micro-USD (pinned global constraint).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from foundry.schemas.base import FrozenModel
from foundry.schemas.operations import (
    FairnessDecision,
    QueueEntry,
    RunStatus,
    RunStatusRecord,
    TenantQuota,
)
from foundry.state.seq import Seq

#: A run idle at least this long (no progress marker) is a hibernation candidate.
DEFAULT_HIBERNATE_IDLE_MS = 30 * 60 * 1000  # 30 minutes


# --- reasons (stable strings, mirror FairnessDecision.reason vocabulary) ----

REASON_ADMITTED = "admitted"
REASON_FAIR_SHARE = "fair_share"
REASON_FLEET_COST_RATE_CAP = "fleet_cost_rate_cap"
REASON_TENANT_CONCURRENCY = "tenant_concurrency"
REASON_NO_SLOTS = "no_slots"
REASON_NO_QUOTA = "no_tenant_quota"
#: A resume/rehydration that would breach the fleet cost-rate cap is
#: queued/serialized at resume-admit, NOT allowed to spike spend (§6 in-flight).
REASON_RESUME_RATE_CAP = "resume_cost_rate_cap"
REASON_RESUME_ADMITTED = "resume_admitted"


class FleetCapacity(FrozenModel):
    """The fleet-wide capacity envelope the scheduler admits against (§6).

    `max_concurrent_runs` is the global concurrency cap (slots shared by all
    tenants); `fleet_cost_rate_cap_microusd_per_hr` is the fleet cost-rate cap
    (integer µ$/hr) bounding aggregate spend RATE in addition to per-run budgets
    (governance.md §7)."""

    max_concurrent_runs: int
    fleet_cost_rate_cap_microusd_per_hr: int


@dataclass
class RunningRun:
    """An in-flight run holding a slot + contributing to the fleet cost rate.

    `mid_trust_root_attestation` runs are NON-PREEMPTIBLE (§4/§6). `idle_since_ms`
    / `last_progress_ts_ms` drive hibernation; `cost_rate_microusd_per_hr` feeds
    the fleet aggregate (a paused/hibernated run is $0/hr)."""

    run_id: str
    tenant_id: str
    cost_rate_microusd_per_hr: int = 0
    last_progress_ts_ms: int = 0
    status: RunStatus = RunStatus.RUNNING
    mid_trust_root_attestation: bool = False


@dataclass
class CapacityScheduler:
    """Deterministic weighted-fair admission/throttle scheduler (§6).

    Capacity (slots + fleet cost-rate cap) and per-tenant quotas are injected;
    the clock is passed per call (`now_ms`) so the scheduler is pure. State is
    the set of in-flight runs (`_running`), the deficit-RR credit ledger
    (`_credit`), and the anti-starvation wait-age ledger (`_waited_rounds`) —
    all mutated only through the public methods below.
    """

    capacity: FleetCapacity
    quotas: Mapping[str, TenantQuota]
    hibernate_idle_ms: int = DEFAULT_HIBERNATE_IDLE_MS
    _running: dict[str, RunningRun] = field(default_factory=dict)
    # deficit round-robin: accrued-but-unspent fair-share credit per tenant.
    _credit: dict[str, int] = field(default_factory=dict)
    # Anti-starvation AGE: consecutive scheduling rounds a tenant has CONTENDED
    # without being admitted. Reset to 0 on a win, bumped each round it loses.
    # This is the DOMINANT fairness key (after the held-slots anti-monopoly
    # guard) so even a tiny-weight tenant under extreme weight skew + held slots
    # is eventually admitted — credit/weight only breaks ties among tenants of
    # EQUAL wait age (M3-R1-02: no-starvation under unequal weights).
    _waited_rounds: dict[str, int] = field(default_factory=dict)

    # -- in-flight bookkeeping --------------------------------------------
    def register_running(self, run: RunningRun) -> None:
        """Record an admitted/in-flight run (it holds a slot + cost rate)."""
        self._running[run.run_id] = run

    def release(self, run_id: str) -> None:
        """Free a run's slot (terminal/hibernated). Idempotent."""
        self._running.pop(run_id, None)

    def active_runs(self) -> tuple[RunningRun, ...]:
        """Runs holding a slot, in deterministic run_id order."""
        return tuple(self._running[k] for k in sorted(self._running))

    def used_slots(self) -> int:
        return len(self._running)

    def tenant_active_count(self, tenant_id: str) -> int:
        return sum(1 for r in self._running.values() if r.tenant_id == tenant_id)

    # -- fleet cost-rate cap (the cross-run analog of BudgetController) -----
    def fleet_cost_rate_microusd_per_hr(self) -> int:
        """Current aggregate spend RATE of all in-flight runs (integer µ$/hr).

        A paused/hibernated run contributes $0/hr (its `cost_rate` is 0 by the
        run-status model), so the aggregate reflects only live spend."""
        return sum(r.cost_rate_microusd_per_hr for r in self._running.values())

    def fleet_cost_rate_ok(self, requested_rate_microusd_per_hr: int) -> bool:
        """Would admitting a run at `requested_rate` keep the fleet within cap?

        Admission-side check (§6): projected fleet rate must not EXCEED the cap.
        Mirrors `BudgetController.authorize_superstep`'s projected-vs-cap test,
        on the rate dimension instead of cumulative spend."""
        projected = self.fleet_cost_rate_microusd_per_hr() + requested_rate_microusd_per_hr
        return projected <= self.capacity.fleet_cost_rate_cap_microusd_per_hr

    def resume_admit(
        self, entry: QueueEntry, *, now_ms: int, seq: Seq
    ) -> FairnessDecision:
        """Resume-admit gate for an IN-FLIGHT cost spike (operations.md §6).

        Admission-throttle alone cannot reach in-flight spend ("resume is not
        new-run admission"), so the fleet cost-rate cap ALSO gates the in-flight
        cost sources large enough to outrun an admission freeze — chiefly
        cold-tier rehydration retrieval/egress on resume of an
        archival-evicted hibernated run, and in-flight quorum re-runs (§7
        flake-recovery). This mirrors the §4 admit-to-attestation gate: a
        rehydration/re-run whose `requested_cost_rate_microusd_per_hr` would
        breach the cap is QUEUED/serialized at resume-admit
        (``admitted=False``, ``reason=resume_cost_rate_cap``) rather than
        allowed to spike the cost-rate uncapped — never killed (M3-R1-01).

        Unlike :meth:`admit`, this does NOT consume a concurrency slot or the
        tenant fan-out guard (a resuming run already holds its lineage/slot); it
        gates ONLY on the fleet cost-rate dimension. The caller registers the
        resumed run's live rate on success, exactly as for a fresh admission."""
        cap = self.capacity.fleet_cost_rate_cap_microusd_per_hr
        cur = self.fleet_cost_rate_microusd_per_hr()
        ok = self.fleet_cost_rate_ok(entry.requested_cost_rate_microusd_per_hr)
        return FairnessDecision(
            run_id=entry.run_id,
            admitted=ok,
            reason=REASON_RESUME_ADMITTED if ok else REASON_RESUME_RATE_CAP,
            fleet_cost_rate_microusd_per_hr=cur,
            fleet_cost_rate_cap_microusd_per_hr=cap,
            seq=seq,
        )

    # -- fairness scheduling ----------------------------------------------
    def _accrue_round_credit(self, tenants: Iterable[str]) -> None:
        """Accrue this round's fair-share credit for every contending tenant."""
        for tid in tenants:
            self._credit[tid] = self._credit.get(tid, 0) + self._weight(tid)

    def _most_starved(self, entries: Iterable[QueueEntry]) -> QueueEntry | None:
        """The single most-deserving queued entry RIGHT NOW (re-evaluated each
        time an admission consumes capacity, so the ranking reflects slots just
        taken — that's what stops a heavy tenant sweeping every slot in one
        round). Order: fewest slots already held (anti-monopoly), then LONGEST
        unserved wait age (anti-starvation aging — the dominant fairness key so
        no tenant is starved regardless of weight skew), then most accrued credit
        (weighted-fair deficit, breaks ties among equally-aged tenants), then
        FIFO, then run_id."""
        items = list(entries)
        if not items:
            return None

        def rank(e: QueueEntry) -> tuple[int, int, int, int, str]:
            held = self.tenant_active_count(e.tenant_id)
            waited = self._waited_rounds.get(e.tenant_id, 0)
            credit = self._credit.get(e.tenant_id, 0)
            return (held, -waited, -credit, e.enqueued_ts_ms, e.run_id)

        return min(items, key=rank)

    def _weight(self, tenant_id: str) -> int:
        q = self.quotas.get(tenant_id)
        if q is None:
            return 0
        # an unset weight still earns a unit credit so it is not starved forever.
        return q.fair_share_micro if q.fair_share_micro > 0 else 1

    def _spend_credit(self, tenant_id: str) -> None:
        """A tenant that WON a slot spends its accrued credit (deficit reset)
        AND resets its anti-starvation wait age, so the next round favors the
        tenants that lost (no-starvation)."""
        self._credit[tenant_id] = 0
        self._waited_rounds[tenant_id] = 0

    def admit(self, entry: QueueEntry, *, now_ms: int, seq: Seq) -> FairnessDecision:
        """Decide admission for ONE queued run (admission-side throttle).

        Returns a `FairnessDecision`; `admitted=False` means the run stays
        queued (NEVER killed). On admission the caller is expected to
        `register_running(...)` the run. Checks, in order: tenant quota exists →
        global slots free → tenant concurrency guard → fleet cost-rate cap."""
        cap = self.capacity.fleet_cost_rate_cap_microusd_per_hr
        cur = self.fleet_cost_rate_microusd_per_hr()

        def decide(admitted: bool, reason: str) -> FairnessDecision:
            return FairnessDecision(
                run_id=entry.run_id,
                admitted=admitted,
                reason=reason,
                fleet_cost_rate_microusd_per_hr=cur,
                fleet_cost_rate_cap_microusd_per_hr=cap,
                seq=seq,
            )

        quota = self.quotas.get(entry.tenant_id)
        if quota is None:
            return decide(False, REASON_NO_QUOTA)

        if self.used_slots() >= self.capacity.max_concurrent_runs:
            return decide(False, REASON_NO_SLOTS)

        if (
            quota.max_concurrent_runs > 0
            and self.tenant_active_count(entry.tenant_id) >= quota.max_concurrent_runs
        ):
            return decide(False, REASON_TENANT_CONCURRENCY)

        if not self.fleet_cost_rate_ok(entry.requested_cost_rate_microusd_per_hr):
            return decide(False, REASON_FLEET_COST_RATE_CAP)

        return decide(True, REASON_ADMITTED)

    def schedule(
        self, queue: Iterable[QueueEntry], *, now_ms: int, seq: Seq
    ) -> tuple[FairnessDecision, ...]:
        """Run one fair scheduling round over the whole `queue`.

        Repeatedly picks the most-starved queued entry and admits it against the
        live slot/concurrency/cost-rate budget — REGISTERING each admitted run so
        the NEXT pick sees the consumed capacity (re-ranking each step is what
        stops one tenant sweeping every slot in a single round). Every entry gets
        exactly one `FairnessDecision`; un-admitted entries stay queued.
        Deterministic: identical inputs (same clock, same queue) → identical
        decisions and identical admission order."""
        items = list(queue)
        self._accrue_round_credit({e.tenant_id for e in items})
        decisions: dict[str, FairnessDecision] = {}
        pending = list(items)
        local = 0
        while pending:
            entry = self._most_starved(pending)
            if entry is None:  # pragma: no cover - guarded by while
                break
            pending.remove(entry)
            d = self.admit(entry, now_ms=now_ms, seq=seq._replace(local=local))
            local += 1
            decisions[entry.run_id] = d
            if d.admitted:
                self.register_running(
                    RunningRun(
                        run_id=entry.run_id,
                        tenant_id=entry.tenant_id,
                        cost_rate_microusd_per_hr=entry.requested_cost_rate_microusd_per_hr,
                        last_progress_ts_ms=now_ms,
                        status=RunStatus.RUNNING,
                    )
                )
                self._spend_credit(entry.tenant_id)
        # Anti-starvation aging: every tenant that CONTENDED this round but won
        # NO slot ages by one round (a winner was already reset to 0 in
        # _spend_credit). The next round's _most_starved then favors the
        # longest-waiting tenant — so no tenant is starved across rounds even
        # under extreme weight skew with slots held in flight (M3-R1-02).
        admitted_tenants = {e.tenant_id for e in items if decisions[e.run_id].admitted}
        for tid in {e.tenant_id for e in items}:
            if tid not in admitted_tenants:
                self._waited_rounds[tid] = self._waited_rounds.get(tid, 0) + 1
        # returned in original-queue order for caller stability.
        return tuple(decisions[e.run_id] for e in items)

    # -- hibernation (distinct from PAUSED) -------------------------------
    def should_hibernate(self, run: RunningRun, *, now_ms: int) -> bool:
        """A long-IDLE run is a hibernation candidate (§6).

        Idle = no progress for at least `hibernate_idle_ms`. A run mid-trust-root
        attestation is NEVER hibernated (non-preemptible, §4/§6); an already
        terminal/hibernated run is not a candidate. PAUSED runs are eligible
        (a long HITL/store pause is the canonical hibernation trigger)."""
        if run.mid_trust_root_attestation:
            return False
        if run.status in (RunStatus.DONE, RunStatus.FAILED, RunStatus.HIBERNATED):
            return False
        idle_ms = now_ms - run.last_progress_ts_ms
        return idle_ms >= self.hibernate_idle_ms

    def hibernate(
        self, run_id: str, *, now_ms: int, seq: Seq, incremental_footprint_bytes: int = 0
    ) -> RunStatusRecord:
        """Hibernate an idle run: FREE its slot, spill to CAS, stay resumable.

        Frees the concurrency slot (so a contending run can be admitted) and
        zeroes the run's cost rate ($0/hr while hibernated). The returned
        `RunStatusRecord` carries `status=HIBERNATED` and the (already
        dedup/compacted, §6) incremental footprint. Resumption is the caller's
        concern: re-`register_running(...)` on wake. Raises if the run is
        unknown (you cannot hibernate a run that holds no slot)."""
        run = self._running.get(run_id)
        if run is None:
            raise KeyError(f"cannot hibernate unknown run {run_id!r}")
        self.release(run_id)  # frees the slot — admission can now make progress
        return RunStatusRecord(
            run_id=run_id,
            status=RunStatus.HIBERNATED,
            current_phase="hibernated",
            cost_rate_microusd_per_hr=0,  # $0/hr while hibernated (machine-readable)
            last_progress_ts_ms=run.last_progress_ts_ms,
            state_footprint_bytes=incremental_footprint_bytes,
            seq=seq,
        )
