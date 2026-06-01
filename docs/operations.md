# Operations — running Foundry in production (Day 2)

The prior review rounds hardened *correctness, security, and agent/LLM design*.
This document covers the operability dimension they did not: who runs Foundry,
how they tell a healthy run from a wedged one, how it survives infrastructure
outages and upgrades, how it scales across concurrent runs, and how the human
review load is bounded. None of this is optional for production; it is part of
implementation-readiness.

## 1. Operator role, SLOs, alerting, runbooks

Foundry needs its **own** operations surface, distinct from the observability the
eval harness consumes.

- **Operator role & on-call.** A named platform operator owns the running fleet
  (separate from the *Human Liaison*, who makes in-run product/release decisions).
  On-call receives infrastructure and liveness alerts; the Liaison receives in-run
  HITL decisions (§7).
- **SLOs (examples, tuned in Phase 5).** Run success rate; **time-to-ack** for
  each escalation class; p95 wall-clock per feature *class* (§ cost model); budget
  burn-rate; trusted-component availability; HITL-queue age.
- **Alert taxonomy** maps 1:1 to the deterministic monitors (loop/non-convergence,
  cost threshold, OPA deny, retry-until-green, oracle/config-edit, pinned-ID
  drift) plus infra alerts (§3, §4) and HITL-backlog (§7). Every alert links a
  runbook.
- **Operator dashboard** (not the Langfuse/eval view): per-run status (§2), fleet
  concurrency/queue depth (§6), budget burn, trusted-component health, HITL
  backlog. Golden signals: throughput, latency, error/escalation rate, saturation.
- **Runbook skeleton per escalation class** lives alongside
  [governance.md §10](./governance.md): non-convergence, budget breach, monitor
  trip, store outage, trusted-component down, poisoned checkpoint, mid-flight
  upgrade/drift.

*(Addresses SRE-F1.)*

## 2. Run-status model — paused vs. wedged

The design pauses constantly (HITL, budget, mirror admission, non-convergence), so
"paused" must be operator-distinguishable from "stuck". A first-class run status:

```
RunStatus = {
  state: running | paused | wedged | failed | done,
  pause_reason: hitl_design | hitl_release | budget | mirror_admission |
                non_convergence | monitor_trip | trusted_component_down | None,
  awaiting: who/what (e.g. "two-person release sign-off"),
  last_progress: (seq, wall_clock_ts),
  expected_next_transition_deadline: ts
}
```

- A **supervisor watchdog/heartbeat** escalates `wedged` when no superstep advances
  by `expected_next_transition_deadline` and the state is not a legitimate
  `paused` reason — distinct from the per-branch deadlines in
  [architecture.md §5](./architecture.md), which catch a single hung branch.
- A legitimately-paused run (e.g. awaiting HITL) shows `$0/hr` and a clear
  `awaiting`; a wedged run is alerted. This is the most common Day-2 question and
  it now has a machine-readable answer. *(SRE-F2.)*

## 3. Stateful-store availability, backup, and corruption recovery

Foundry's durability story assumes Postgres (checkpoints + ledgers + outbox), the
content-addressed object store (artifacts + out-of-band history), and the WORM
store are up and uncorrupted. Operations makes that real:

- **HA + backup/restore (RPO/RTO):** each store has an availability target,
  replication, and point-in-time backup with a stated RPO/RTO. WORM is
  append-only + externally anchored (already specified) — backups must preserve
  the hash chain.
- **In-flight behavior on outage:** Postgres or object-store unavailable → runs
  **block-and-pause** (status `paused: store_outage`), never proceed on partial
  state; the relay halts external effects (it is at-least-once and idempotent, so
  a halted relay is safe). **Relay-backlog bound** with alerting so a stuck relay
  surfaces before the outbox grows without limit.
- **Poisoned/corrupt checkpoint recovery:** a checkpoint that deterministically
  crashes on resume, or whose out-of-band `history_ref`/artifact handle dangles
  (object-store blob lost), is **quarantined**; recovery is **fork-to-prior-good
  checkpoint** (new run_id, per [architecture.md §5](./architecture.md)) with
  operator override, and a periodic **integrity sweep** detects dangling handles.

*(SRE-F3.)*

## 4. Trusted-component availability and blast radius

The trusted (non-agent) components — gate runner, hermetic builder, admission
controller, deployer, WORM appender, dependency mirror — are deliberately
mandatory chokepoints (no agent bypass). That is correct for security but makes
their *availability* a fleet-wide concern:

- **Failure ≠ compromise.** §1c/§4/§5 of governance cover *compromise*; here we
  cover *outage*. A gate-runner or admission outage must degrade to **`paused:
  trusted_component_down`** (clear reason, §2), **never `wedged`**, with
  queue-and-drain on recovery.
- **HA where the trust model permits:** the attestor can be **replicated**
  instances sharing one keyless signing identity; **admission is a replicated,
  stateless checker** over the WORM store; the builder is horizontally scalable
  behind a work queue. These are non-agent and replicable, so this is *sizing*,
  not redesign.
- **Per-component availability target + runbook**; an outage alert; explicit
  "fleet paused, not failed" semantics so budget isn't burned during the outage.

*(SRE-F4; also bounds the serialization concern in COST-F3.)*

## 5. Upgrade & migration with runs in flight

Long-horizon runs span hours/days, so deploys and provider model rotations *will*
land mid-run.

- **Version-stamp every checkpoint** with `{state_schema_ver, graph_ver,
  prompt_ver, model_pins}` recorded in the run manifest.
- **State-schema migration:** additive channel changes ship **migration
  functions**; breaking changes require **drain-before-upgrade** (no old-shape
  checkpoints in flight). A run never resumes against an incompatible schema — it
  fails to the operator instead of silently mis-resuming.
- **Graph/prompt changes:** a run **resumes on its pinned graph/prompt version**;
  the new version applies only to new runs (or an explicit, operator-approved
  migration). This prevents a topology/prompt change from silently altering
  routing on resume.
- **Mid-flight model drift:** the pinned-ID-drift alert
  ([architecture.md §8](./architecture.md)) now has a defined **operator
  response**: pause the affected runs; per run, **continue** (accept
  recalibration), **fork**, or **abort** — never silently continue a T1 judgment
  on drifted weights. New runs re-pin and re-calibrate.

*(SRE-F5.)*

## 6. Multi-tenancy, capacity, and fairness

Foundry is built around parallelism and clearly expects many concurrent runs (the
A/B alone is `3 arms × n × k`). A run-admission layer is required:

- **Global concurrency cap and fleet cost-rate cap** (in addition to per-run
  budgets in [governance.md §7](./governance.md)) so the fleet cannot overspend in
  aggregate.
- **Queue + fairness scheduler** across runs/tenants; **per-tenant resource +
  credential + budget isolation**; **noisy-neighbor guard** = per-run cap on
  concurrent `Send` branches so one fan-out can't starve others.
- **Capacity limits + backpressure** for sandboxes and the trusted gate runner,
  with queue-depth alerting; a heavy run queues rather than starving the fleet.

*(SRE-F6; bounds the trusted-runner serialization in COST-F3.)*

## 7. Human-in-the-loop load model

HITL is load-bearing for safety, so its **volume** is a first-class operational
constraint, not an externality.

- **Enumerate expected human touchpoints per feature/run** and a **latency budget**
  per class: Design approval, Release sign-off (two-person on trust-root), new-
  dependency source-diff, flake-recovery override, oracle/gate/criteria/config-edit
  trips, monitor escalations.
- **A HITL-blocked run releases its compute** (sandbox/runner slots) rather than
  holding it — ties to capacity (§6).
- **Reviewer queue with SLAs + backlog/age alerting**; a **staffing/escalation
  fallback** when two-person sign-off can't be staffed (a release **deadlocks
  safely** as `paused`, never auto-approves).
- **Scorecard threshold on human-decisions-per-feature** ([evaluation.md §7](./evaluation.md))
  so HITL volume is a falsifiable design constraint. If it trends up, that is a
  design smell to be driven down by **risk-tiering** ([governance.md §1e](./governance.md)),
  not absorbed by humans.

*(SRE-F7, COST-F2.)*

## 8. Trusted-component lifecycle & bootstrap

- **Rotation cadence + procedure** for the OIDC trust/federation policy, the
  builder/runner base images, the toolchain pins, and the external scanner-config
  repo. A builder-image bump changes `toolchain_digest` → `tree_sha`, invalidating
  cached verdicts; such bumps are **version-stamped and rolled out by drain or
  scheduled re-verification** (§5), never silently mid-fleet.
- **Named ownership + change process** for the scanner-config repo (human-only,
  two-person, revision hash already bound into the predicate).
- **Bootstrap-trust posture:** the **trusted non-agent components are built and
  audited by humans through a conventional, human-reviewed pipeline — never by
  Foundry's own agents** — so the "agents can't bless their own work" invariant is
  not violated at construction time. Foundry may build *application* software; it
  does not build its own trust root.

*(SRE-F8.)*
