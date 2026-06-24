# RUNBOOKS — Foundry operator reference (Day-2)

**This is the operator's first-stop reference.** It is the operator-facing
companion to [docs/operations.md](../docs/operations.md) (the authoritative Day-2
model): the SLO + alert catalog and one runbook per failure scenario. Every
diagnose/resolve step cites the **real shipped module + function** that surfaces
the signal — no invented APIs. Where a step is dev-grade scaffold (per
implementation-plan §0.3), it is flagged.

The single load-bearing distinction throughout: **PAUSED vs WEDGED**
(`foundry/operations/run_status.py`). A PAUSED run is a *legitimate* wait — it
carries a `PauseReason`, an `awaiting` string, and burns **$0/hr** by
construction; it is **not** alerted. A WEDGED run has no progress heartbeat past
its **derived** deadline and no legitimate pause reason — it **is** alerted
(`RunStatusRecord.is_alertable()`). Status, money (integer micro-USD), and
ratios (integer millionths, `*_micro`) are all float-free; the clock is always
injected (`now_ms`), so every signal below is deterministic and reproducible.

---

## SLO catalog

Operators set `SLOTarget` records (`foundry/schemas/operations.py`:
`SLOTarget`, `objective_micro` in millionths, optional `runbook_uri`). The
operator surface evaluates them against the aggregated fleet view and emits
`Alert`s (`AlertSeverity`: `PAGE` / `WARN` / `INFO`). PAGE wakes on-call; WARN/INFO
route to the dashboard/ticket queue.

| SLO target (`SLOTarget.name`) | Direction | Emitter (module → function) | Alert (`Alert.name`) | Severity |
|---|---|---|---|---|
| `run_health_ratio` — fraction of NON-terminal runs that are healthy (RUNNING+PAUSED) | floor | `operations/operator.py` → `evaluate_slos` (via `OperatorView.health_ratio_micro`) | `run_health_slo_breach` | **WARN** |
| `max_wedged_count` — ceiling on simultaneously WEDGED runs | ceiling | `operations/operator.py` → `evaluate_slos` (via `OperatorView.wedged_count`) | `wedged_count_slo_breach` | **PAGE** |
| `max_stuck_age_ms` — ceiling on the oldest stuck run's lateness past deadline | ceiling | `operations/operator.py` → `evaluate_slos` (via `OperatorView.oldest_stuck_overdue_ms`) | `stuck_run_age_slo_breach` | **PAGE** |
| HITL backlog depth — ceiling (`ReviewerQueue.max_backlog`) | ceiling | `operations/hitl.py` → `ReviewerQueue.alerts` / `backlog_alert` | `hitl_backlog_exceeded` | **PAGE** |
| HITL per-class SLA (`ReviewerQueueEntry.sla_deadline_ms`) | ceiling | `operations/hitl.py` → `ReviewerQueue.alerts` / `sla_breach_alerts` | `hitl_sla_breach` | **WARN** |
| Fleet cost-rate cap (`FleetCapacity.fleet_cost_rate_cap_microusd_per_hr`) | ceiling | `operations/capacity.py` → `CapacityScheduler.admit` / `fleet_cost_rate_ok` | `FairnessDecision(reason="fleet_cost_rate_cap")` | (admission throttle) |
| Pinned-ID drift (always-on, cannot be disabled) | any event | `observability/drift_alerts.py` → `pinned_id_drift_alerts` / `detect_drift` | `PinnedIdDriftAlert(kind="pinned-id-drift")` | **CRITICAL** |
| Cost / usage / latency threshold (`ThresholdPolicy`) | ceiling | `observability/drift_alerts.py` → `threshold_alerts` | `cost-drift` / `usage-drift` / `latency-drift` | WARNING |

Notes:
- `evaluate_slos(view, slos, *, seq)` recognises `run_health_ratio`,
  `max_wedged_count`, `max_stuck_age_ms` by name; **unknown names are skipped**
  (forward-compatible). Within SLO it emits nothing.
- The catalog maps 1:1 to the deterministic monitors — `Alert.monitor` names the
  source (`operator_dashboard`, `hitl_backlog`, `hitl_sla`, …). Fleet-level
  alerts carry `run_id is None`.
- `operations.md §1` lists further SLO *examples* (run success rate, time-to-ack
  per escalation class, p95 wall-clock per feature class, budget burn-rate,
  trusted-component availability) that are **modeled in the schema but not yet
  wired into a built-in monitor** — see the gap note at the end.

---

## Runbook 1 — Stuck (WEDGED) run

**Symptom.** `wedged_count_slo_breach` (PAGE) and/or `stuck_run_age_slo_breach`
(PAGE). A run is burning budget but not advancing.

**Diagnose.**
1. Open the fleet view: `operator.dashboard(runs, now_ms=...)` (or
   `dashboard_from_signals(...)` from raw `RunSignals`). The
   `OperatorView.stuck` tuple is the diagnosable WEDGED runs, **ordered
   most-overdue-first**.
2. Read the `StuckRunSummary` for the run (`operations/operator.py`): it answers
   "what is it wedged on" without opening the run — `current_phase`,
   `last_progress_seq` / `last_progress_ts_ms` (the heartbeat that failed to
   advance), the **derived** `deadline_ms`, `overdue_ms`, and `reason`
   (the explicit "no heartbeat past the derived deadline and no legitimate pause
   reason" explanation). `cost_rate_microusd_per_hr` is the live burn the wedge
   is wasting.
3. **Distinguish from a legitimate PAUSE.** This is the most common Day-2
   question. A PAUSED run never appears in `stuck` (`RunStatusRecord.is_alertable()`
   is False for it); it shows a `pause_reason` + an `awaiting` and **$0/hr**.
   A run in WEDGED has `pause_reason is None` and a non-zero cost rate. The
   classification is made by `run_status.derive_status` / `watchdog`; the
   operator surface **composes** it, never re-derives.
4. Sanity-check the deadline before treating it as a true wedge: the deadline is
   derived (`DeadlineModel.derive`) as `p99 + provider_backoff + queue_wait
   (+ cold_tier_restore on rehydration)`. A run reconstituting from cold/archival
   hibernation legitimately reads slow — confirm `RunSignals.rehydrating` and the
   cold-tier term are accounted for so you do not chase a *false* wedge.

**Resolve.**
- If it is a genuine wedge (no heartbeat, not paused, past a correctly-derived
  deadline): treat as a hung run — capture the checkpoint, fail or fork the run
  per the responsible phase. A wedge is a code/infra defect, not a planned wait;
  escalate to the owning team.
- If diagnosis shows it is actually a legitimate pause (a `PauseReason` is set),
  the SLO/threshold was mis-tuned — adjust the `DeadlineModel` term for that node
  type (e.g. add provider-backoff/queue-wait headroom), not the run.

**Verify.** Re-run `dashboard(...)` at a later `now_ms`; the run should leave the
`stuck` tuple (advanced to RUNNING, or terminal DONE/FAILED). Re-run
`evaluate_slos(...)` — `wedged_count_slo_breach` / `stuck_run_age_slo_breach`
should clear.

---

## Runbook 2 — Store outage (Postgres / object store / WORM)

**Symptom.** Runs transition to `PAUSED` with `PauseReason.STORE_OUTAGE`
(`awaiting="store recovery"`, $0/hr). NOT a wedge, NOT a failure — block-and-pause.

**Diagnose.**
- The pause is produced by `store_resilience.with_store_outage_handling(...)`:
  the superstep body runs inside `FoundryCheckpointer.superstep_txn()`; on a
  `StoreUnavailable` (or the backend `sqlite3.OperationalError` connection-error
  analogue) the transaction **rolls back** — *nothing persists, no partial
  checkpoint* — and `on_store_unavailable(...)` emits the
  `PauseReason.STORE_OUTAGE` `RunStatusRecord`. Any *other* exception is a genuine
  run error and is re-raised (never masked as a pause).
- Confirm the run shows `pause_reason == STORE_OUTAGE` on the dashboard; the
  outage itself is a separate fleet-level operator alert (§1/§3).

**Resolve.**
1. Restore the store (HA failover / restart) — this is the infra task.
2. Resume each paused-by-outage run with
   `store_resilience.resume_after_outage(checkpointer, reconcile_relay=...)`.
   It is **reconciliation, not replay**: the run resumes from the last COMMITTED
   checkpoint (LangGraph re-folds committed writes; nothing since the outage was
   ever persisted), then drains the outbox crash window via `reconcile_relay`
   (typically `OutboxRelay.recover`).

**Verify — no duplicate external action.** `resume_after_outage` returns `True`
only after a probing `SELECT 1` inside a real superstep txn succeeds (if the
store is still down it raises and returns `False` — the run stays paused). The
relay is **at-least-once + idempotent** (process-stable idempotency keys,
LEARNINGS L7), so reconciliation resolves any `intent`-stuck row **without
double-firing** an external action. Confirm the relay backlog drains and no
duplicate effect is emitted.

---

## Runbook 3 — Poisoned / corrupt checkpoint

**Symptom.** A run quarantines (`RunStatus.QUARANTINED`) — a checkpoint that
deterministically crashes on resume, dangling content-addressed handle, or a
broken WORM audit chain.

**Diagnose.** `store_resilience.check_checkpoint_health(...)` returns a
`CheckpointHealth` with `clean=False`, `quarantined=True`. Poisoned iff ANY of:
- **Crash-on-resume** — `resolve_body(checkpoint_uri)` raises, or the payload
  does not round-trip to a `FrozenModel` (a non-allowlisted type degrades to a
  `dict` under the serde, LEARNINGS I5).
- **Dangling handle** — a uri in `handle_uris` fails `probe_handle` (the periodic
  integrity sweep; object-store blob lost). The offenders are listed on
  `dangling_handle_uris`.
- **Broken WORM chain** — `verify_audit_chain(conn)` is False.

For a **hibernated** worktree (`hibernated=True`) a dangling object is a **HARD
alert BEFORE wake** — it surfaces while the run is still paused-recoverable, not
at wake-time (a lost hibernation blob means full re-run).

**Resolve.** `store_resilience.recover_to_last_clean(health, resolve_body=...)`
does **fork-to-prior-good**: it loads `last_clean_checkpoint_uri` (after
re-verifying *that* one is clean) and returns its body as the recovery point for
a fresh run_id (architecture.md §5). The poisoned checkpoint is **never** loaded
as trusted state. Fail-closed: it raises `PoisonedCheckpointError` if the health
is clean (nothing to recover), if there is no `last_clean_checkpoint_uri`
(operator override required), or if the recorded last-clean checkpoint does not
itself load to a frozen model.

**Verify.** Re-run `check_checkpoint_health` on the recovered fork's checkpoint —
expect `clean=True`. The poisoned checkpoint stays quarantined (never resumed).

---

## Runbook 4 — Trusted-component outage (proxy / signer / gate-runner)

**Symptom.** Runs depending on a trusted chokepoint go `PAUSED` with
`PauseReason.TRUSTED_COMPONENT_UNAVAILABLE` ($0/hr, `awaiting="<component>
recovery"`). **Outage ≠ compromise; PAUSED, never WEDGED.**

**Diagnose.** The trusted chokepoints are `trusted_component.TrustedComponent`:
`MODEL_PROVIDER_PROXY` (single egress + trust SPOF), `ZONE_B_SIGNER`
(judgment-binding signer), `GATE_RUNNER` (two-zone attestor). Build/read the
`AvailabilityModel`:
- `AvailabilityModel.unavailable()` lists components currently in outage
  (deterministic enum order); `is_available(component)` /
  `all_available(deps)` is the precondition a judgment/gate node checks before
  proceeding.
- `trusted_component.classify_for_dependencies(model, ..., dependencies=...)`
  is the end-to-end derivation a node uses: if ANY dependency is unavailable it
  returns the `PAUSED(trusted_component_unavailable)` record (via
  `on_unavailable`) on the first unavailable dep — it **never proceeds** without
  a trustworthy verdict and is **never misclassified as WEDGED** for waiting.

**Resolve.** Restore/replace the component (the attestor and admission checker are
replicable; the proxy/signer are sized for HA — operations.md §4). On recovery,
resume each affected run with
`trusted_component.on_recovery(...)`, which returns a `RUNNING` record (no pause
reason). The round-trip the EXIT property asserts is `on_unavailable → on_recovery`
(PAUSED → RUNNING), queue-and-drain semantics.

**Verify.** The affected runs show RUNNING with no pause reason and resume budget
burn; `AvailabilityModel.unavailable()` no longer lists the component. Budget was
not burned during the outage ($0/hr while paused).

> Per-lineage capacity: failover load redistributes across the surviving lineages
> (≥3 provisioned). Reserve/admit headroom with `CapacityReservation` /
> `LineageReservation` — `admit_for_lineage(lineage, amount)` draws from a
> lineage's OWN reserved slice plus the shared `burst` pool only, never another
> lineage's reservation (the anti-starvation invariant). A refusal returns
> `(self, False)` so the run queues, never an unbounded retry storm.

---

## Runbook 5 — HITL backlog / SLA breach

**Symptom.** `hitl_backlog_exceeded` (PAGE) and/or `hitl_sla_breach` (WARN). The
human review queue is over its bound or an entry is past its per-class SLA.

**Diagnose.** The queue is `hitl.ReviewerQueue` holding `ReviewerQueueEntry`
touchpoints (each with `decision_class`, optional `two_person`,
`sla_deadline_ms`). Drift decisions and release sign-offs are different
service-time classes — `ReviewerQueue.load(...)` reports `HITLLoad`
(`backlog_depth`, `oldest_age_ms`, `available_reviewer_pairs`,
`per_class_backlog`) so a fleet-drift batch (operations.md §5) does not serialize
behind routine release sign-offs.
- `ReviewerQueue.alerts(now_ms=..., seq=...)` is the CONTROL: empty within bound
  and SLA; a PAGE `hitl_backlog_exceeded` over `max_backlog` (`backlog_alert`);
  a WARN per entry past its SLA (`sla_breach_alerts` / `sla_breached`). This is
  why the backlog can never grow unbounded **silently**.

**Profile gate (dormant vs live).** `HITLProfile.SELF_JUDGE` (the zero-HITL
solo-lineage profile, plan §0.2) makes the queue **DORMANT** — a non-regulated
`enqueue` is dropped (returns False; the sign-off is recorded-but-no-op
elsewhere) so no HITL piles up. `HITLProfile.REGULATED` (or a `regulated=True`
touchpoint, which vetoes zero-HITL) makes it **LIVE** — every touchpoint is
enqueued, measured, bounded, alerted. If you see HITL alerts under self_judge,
they are regulated-forced touchpoints — expected.

**Resolve.** Apply the documented order (operations.md §7): batch sign-off
windows → delegated reviewer tiers → **admission-throttle new High-tier runs**
(§6 capacity). Two-person sign-off is staffed by reviewer **pairs**:
`ReviewerQueue.assign(run_id=..., decision_class=...)` dequeues the oldest
matching entry FIFO; a `two_person` entry with zero `available_reviewer_pairs`
**deadlocks safely** (stays queued, returns None) — it **never auto-approves**.

**Verify.** After draining/staffing, `ReviewerQueue.alerts(...)` returns empty
and `HITLLoad.backlog_depth` / `oldest_age_ms` fall back within bound + SLA.

---

## Runbook 6 — Capacity / starvation + fleet cost-rate cap

**Symptom.** Runs queue rather than admitting (`FairnessDecision.admitted ==
False`); a tenant starves; or the fleet cost-rate cap is breached.

**Diagnose.** Admission is `capacity.CapacityScheduler` against a
`FleetCapacity` (`max_concurrent_runs`, `fleet_cost_rate_cap_microusd_per_hr`)
and per-tenant `TenantQuota`. Read the `FairnessDecision.reason` for each queued
run:
- `no_tenant_quota`, `no_slots`, `tenant_concurrency` (noisy-neighbor guard:
  `TenantQuota.max_concurrent_runs`), or `fleet_cost_rate_cap`.
- `CapacityScheduler.fleet_cost_rate_microusd_per_hr()` is the current aggregate
  burn (a paused/hibernated run is $0/hr); `fleet_cost_rate_ok(rate)` is the
  admission-side projected-vs-cap check (the cross-run analog of
  `BudgetController`).
- Fairness is **deficit-weighted, no-starvation**: `CapacityScheduler.schedule(
  queue, ...)` repeatedly admits the most-starved entry (fewest slots held, then
  most accrued fair-share credit, then FIFO), re-ranking after each admission so a
  heavy tenant cannot sweep every slot in one round.

**Resolve.** Capacity is **admission-side, never kill-side** — never hard-stall or
kill in-flight work. On a fleet-cap breach, admission throttles automatically
(runs stay queued). To relieve pressure, hibernate long-idle runs:
- `CapacityScheduler.should_hibernate(run, now_ms=...)` flags a run idle ≥
  `hibernate_idle_ms` (default 30 min). A run `mid_trust_root_attestation` is
  **NEVER** hibernated (non-preemptible, §4/§6); terminal/already-hibernated runs
  are skipped; PAUSED runs are eligible (a long HITL/store pause is the canonical
  trigger).
- `CapacityScheduler.hibernate(run_id, ...)` **frees the slot**, zeroes the cost
  rate ($0/hr), and returns a `RunStatus.HIBERNATED` record carrying the
  (dedup/compacted) incremental footprint. Distinct from PAUSED: a paused run
  still holds its slot; a hibernated run has surrendered it.

**Verify.** After hibernation `CapacityScheduler.used_slots()` drops and queued
runs admit on the next `schedule(...)` round; `fleet_cost_rate_microusd_per_hr()`
falls back within cap.

---

## Runbook 7 — Upgrade / migration with runs in flight

**Symptom.** A deploy or provider model-rotation lands mid-run; in-flight runs
must not be corrupted or stranded.

**Diagnose.** Every checkpoint carries a `VersionStamp`
(`{state_schema_ver, graph_ver, prompt_ver, toolchain_digest, model_pins}`). Plan
the cohort with `upgrade.plan_fleet_upgrade(current, target, in_flight_runs,
migration_uri=...)` → one `RunUpgradeDecision` per run (no run is silently lost).
Each decision is **DRAIN / MIGRATE / PAUSE** (`upgrade.plan_upgrade`):
- **DRAIN** (`UpgradeAction.DRAIN_BEFORE_UPGRADE`) — schema-compatible
  (`compatible(stamp, target)`: same `state_schema_ver`, no model-pin conflict).
  The run resumes on its **pinned** graph/prompt and completes on the old version;
  the new version applies to new runs only. The safe default. Also used for a
  breaking change the run can complete on the old version (`can_complete_on_old`).
- **MIGRATE** (`UpgradeAction.MIGRATE`) — an *additive* schema bump with a
  `migration_uri` and a serde-loadable checkpoint, proven via
  `migrate_checkpoint(...)`.
- **PAUSE** (`UpgradeAction.ABORT`, `stranded=True`) — fail-closed. A breaking
  change that cannot drain, a downgrade (forward-only), a pin-drift, or an
  unloadable/orphaned checkpoint (`serde_loadable` False / `MigrationError`).
  The checkpoint is left intact on its pinned version — **never** a lossy resume.

**Resolve.**
- DRAIN: let the cohort finish on the old version, then roll the new version to
  new runs.
- MIGRATE: run the migration function behind `migration_uri` via the trusted
  (non-agent) upgrade tooling, version-stamped (§8). **Dev-grade seam:**
  `migrate_checkpoint` proves the checkpoint round-trips under the target's serde
  allowlist; a real channel-rewriting migration is the wired ops task.
- PAUSE: operator action required — fork onto the new version, or hold.
- Mid-flight model drift: pinned-ID drift has a defined per-run response
  (`UpgradeAction.CONTINUE` / `FORK` / `ABORT`) — never silently continue a T1
  judgment on drifted weights (see Runbook 8).

**Verify.** `plan_fleet_upgrade` is deterministic and ordered by each run's
`seq`; re-run it and confirm every run has a non-`stranded` outcome (DRAIN /
MIGRATE) or an explicit operator-handled PAUSE. No checkpoint is mutated by the
plan.

---

## Runbook 8 — Pinned-ID drift / cost drift

**Symptom.** `pinned-id-drift` (CRITICAL) — the provider served a model id that
departs from the pinned request; and/or `cost-drift` / `usage-drift` /
`latency-drift` (WARNING) over the run telemetry.

**Diagnose.**
- Pinned-ID drift is **authoritative from the proxy** — the single egress already
  flagged every call where `served_model != requested`
  (`observability/drift_alerts.py:pinned_id_drift_alerts(proxy)` lifts
  `proxy.drift_events` **verbatim**, no re-derivation, no second egress,
  LEARNINGS L6). Each `PinnedIdDriftAlert` carries `node_id`, `requested_model`,
  `served_model`, `request_id`. It is **always CRITICAL**: drift invalidates prior
  calibration / eval baselines (architecture.md §8), so a T1 judgment must not
  silently continue.
- Threshold drift: `threshold_alerts(telemetry, policy)` over collected
  `RunTelemetry` spans against an operator-set `ThresholdPolicy`
  (`max_total_input_tokens`, `max_total_output_tokens`, `max_call_total_tokens`,
  `max_node_latency_ms`; integer token counts, float-free).
- One call for both: `detect_drift(proxy=..., telemetry=..., policy=...)`. The
  pinned-ID family is **always evaluated** (cannot be disabled); threshold alerts
  only when both telemetry and policy are supplied.

**Resolve.**
- Drift is **fail-loud** (governance §2): `raise_on_drift(...)` / `raise_if(alerts)`
  raise `DriftAlertError` carrying the full alert set the moment any alert exists —
  never swallowed. For triage instead, call `detect_drift(...)` and inspect.
- For pinned-ID drift, apply the upgrade-path response (Runbook 7): per run,
  **CONTINUE** (accept recalibration, flag it), **FORK**, or **ABORT**. Fleet-wide
  drift is correlated — auto-continue Low/Standard with a recalibration flag; pause
  only High / trust-root runs for explicit decision (operations.md §5). New runs
  re-pin and re-calibrate.
- For cost/latency threshold drift, investigate the offending node/call
  (`Alert.context` carries `span_digest` / `usage` / `latency_ms`) and adjust the
  budget/node, or the `ThresholdPolicy` if mis-tuned.

**Verify.** After re-pinning (new runs) or forking, `detect_drift(...)` returns no
`pinned-id-drift` for the affected runs; threshold alerts clear once the
offending cost/latency source is addressed.

---

## The five guardrail planes (policy/)

The guardrail model is **inspectable, not just wired** (`foundry/policy/planes.py`:
`FIVE_PLANE_MODEL`). An operator/auditor can enumerate all five planes and, per
plane, the modules that implement it — `FivePlaneModel.is_complete()` is true iff
every `Plane` is present with ≥1 control and ≥1 implementing module
(`missing_planes()` reports any gap).

| Plane | Controls (summary) | Implementing modules |
|---|---|---|
| **Policy** | OPA-style default-deny eval; run-immutable scanner config; segregation of duties | `foundry.policy.planes` / `scanner_config` / `authorization` |
| **Execution** | worktree-only writes; credential-scrubbed env; hard CPU/walltime kill | `foundry.execution.sandbox` |
| **Data** | single-egress proxy; scoped short-lived creds; egress redaction | `foundry.proxy.model_provider_proxy` |
| **Observability** | OTel superstep tracing; served-model fingerprint; drift alerts | `foundry.observability` / proxy |
| **Assurance** | two-zone attested verdicts; SBOM/SLSA; fail-closed admission; WORM audit | `foundry.gates.*` / `foundry.durability.outbox` / `foundry.trust_root.admission` |

The Policy plane is **fail-closed default-deny** (`PolicyEvaluator.evaluate`):
allows only when the subject names a known policy AND no `DENY_RULES` predicate
fires; an unknown/missing policy, any deny rule, or an evaluator exception all
DENY. Segregation of duties (`authorize_segregation_of_duties`) denies an agent
signing its own work or signing from a non-attestor zone.

## Deferred production hardening (implementation-plan §0.3)

The seams above are **real and correct**; their production *roots* are an ops
deployment task, explicitly out of scope for this build:
- **Observability:** a real OTel exporter (the telemetry collector is in-memory;
  a real exporter drains `export()`).
- **Durability:** true HA Postgres + content-addressed store (exercised here
  against local/dev backends, not a production fleet).
- **Policy:** OPA / Conftest / Checkov (the in-process evaluator is the dev-grade
  fail-closed seam; production swaps the engine, the fail-closed deny contract is
  identical).
- **Trust root:** SLSA-L3 hermetic build, keyless hardware-rooted signing, real
  OIDC federation, isolated build infra (the trust-root code is real; its roots
  are deployment concerns).
- **Upgrade:** `migrate_checkpoint` is a dev-grade round-trip proof; a concrete
  channel-rewriting migration function (behind `migration_uri`) is the wired ops
  task.

---

*Owner: T-M3-12 (operator SLOs + alerts + runbooks). All cited APIs verified
against the shipped `foundry/operations/`, `foundry/observability/`, and
`foundry/policy/` modules.*
