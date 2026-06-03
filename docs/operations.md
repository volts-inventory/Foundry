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
                non_convergence | monitor_trip | trusted_component_down |
                latched_by_sibling_fork | None,
  awaiting: who/what (e.g. "two-person release sign-off"),
  last_progress: (seq, wall_clock_ts),
  expected_next_transition_deadline: ts
}
```

- A **supervisor watchdog/heartbeat** escalates `wedged` when no superstep advances
  by `expected_next_transition_deadline` and the state is not a legitimate
  `paused` reason — distinct from the per-branch deadlines in
  [architecture.md §5](./architecture.md), which catch a single hung branch.
- **Deadline derivation (not a static constant).** A fixed deadline is either noisy
  (false `wedged` on slow nodes) or slow (late detection on fast ones), because
  adapter inner-loop latency, cross-family failover backoff (§4), and runner
  queueing (§6) vary by node type. Derive it per node type from a maintained
  latency distribution: `p99(node_type) + provider-backoff allowance + expected
  queue-wait`, **refreshed when a node enters a known-slow state** (e.g. failover,
  backpressure, **reconstituting-from-hibernation** §6). For a hibernated run that was
  **cold/archival-evicted** (§6 $/byte lever), reconstitution also adds a **cold-tier
  first-byte-restore term** (minutes-to-hours) to that deadline — otherwise a rehydrating
  run pages false `wedged` before its first byte returns. Distinguish **"slow but
  progressing"** — an adapter heartbeat or partial-progress marker advances
  `last_progress` even without a superstep, and reconstitution emits a
  partial-progress/heartbeat marker so a rehydrating run reads as slow-but-progressing,
  not falsely `wedged` — from
  **"no heartbeat"**, so legitimate provider backoff is not misread as `wedged`.
  Only the no-heartbeat case past the derived deadline escalates.
- A legitimately-paused run (e.g. awaiting HITL) shows `$0/hr` and a clear
  `awaiting`; a wedged run is alerted. This is the most common Day-2 question and
  it now has a machine-readable answer.
- **`latched_by_sibling_fork`** is set when a run pauses because another fork of the
  same work **stable-FAILed the shared digest**: the latch is then explained
  operationally in run status, rather than surfacing only at deploy-admission. It also
  covers the case where an in-flight **Release sign-off REQUEST is aborted** because a
  sibling fork stable-FAILed the digest mid-sign-off (the TOCTOU re-read,
  [architecture.md §5](./architecture.md)), so operators see why a sign-off was
  cancelled rather than silently vanishing from the reviewer queue.
  *(SRE-F2.)*

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
  For a **hibernated worktree** (§6) the sweep treats a missing object as a **hard
  alert before wake**, not a wake-time quarantine — a lost hibernation blob means
  full re-run, so it must surface while the run is still paused-recoverable, not at
  the moment an operator tries to resume.

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
- **Second-family capacity reservation.** T1 failover and always-on high-risk
  judging ([architecture.md §8](./architecture.md)) both shift load onto a *second*
  provider family with its own rate limits and prices. Nothing currently reserves
  that capacity, so a primary outage fleet-stalls on the secondary's `429`s.
  **Reserve and price** secondary-family capacity for the **sum of concurrent
  second-family consumers**, not peak T1 alone: **(a)** always-on high-risk judging on
  every feature, **(b)** burst headroom for the worst-case T1 **failover fan-in**,
  **(c)** flake-recovery cross-family re-judges (§7), and **(d)** the **held-out-mutant-
  rotation re-grounding spike** (§5), which re-verifies high-risk criteria fleet-wide and
  so re-fires the mandatory second-family cross-family judge per high-risk criterion
  ([evaluation.md §4/§8](./evaluation.md)) — these can co-occur, so sizing to
  "peak T1 load" alone deadlocks. Secondary-provider rate-limit/quota becomes a
  **capacity dimension in §6 backpressure**; behavior when the **secondary is also
  saturated** is defined there (queue against reserved capacity, noisy-neighbor guard
  covers provider quota — never an unbounded retry storm).
  **Non-preemptible demand vs reserved capacity.** Under a primary outage co-occurring
  with a cost-cap breach, all T1 fails over to the pricier second family *and* in-flight
  mid-attestation runs are non-preemptible by rule (§6), all contending for the
  reservation. When non-preemptible mid-attestation demand would exceed reserved
  capacity, **gate at admit-to-attestation** (do not start an attestation that cannot be
  guaranteed second-family capacity) or hold it in a **bounded attestation queue** —
  rather than leaving non-preemptible work to "queue against reserved capacity" and
  deadlock.

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
  response**: per run, **continue** (accept recalibration), **fork**, or **abort** —
  never silently continue a T1 judgment on drifted weights. New runs re-pin and
  re-calibrate.
- **Fleet-wide drift is a correlated event, not a per-run one.** A provider
  weight-rotation hits *all* in-flight runs simultaneously; a per-run HITL decision
  then becomes a fleet-wide HITL thundering herd. A **fleet-level drift policy**
  sets the default action by risk tier ([governance.md §3](./governance.md)):
  **auto-continue Low/Standard with a recalibration flag** (recorded, surfaced on
  the scorecard, not blocking); **pause only High / trust-root and human-witnessed
  runs** for explicit decision. The §7 reviewer queue must be sized for these
  **correlated spikes**, not just steady per-feature touchpoints.
- **Held-out-mutant-rotation exposure is a second correlated spike.** When a held-out
  mutant rotation is **exposed** ([architecture.md §4](./architecture.md)), every
  in-flight run reusing that rotation must **re-ground / re-verify** at once — a
  correlated load spike like fleet model-drift, not a per-run event. Treat it as
  first-class: it **schedules** re-grounding as background re-verification work (it does
  *not* drain or hard-stall in-flight runs), admitted through §6 backpressure, and
  fleet capacity (compute + any High-tier decisions it raises) is **sized for the spike**
  alongside the drift spike above.

*(SRE-F5.)*

## 6. Multi-tenancy, capacity, and fairness

Foundry is built around parallelism and clearly expects many concurrent runs (the
A/B alone is `3 arms × n × k`). A run-admission layer is required:

- **Global concurrency cap and fleet cost-rate cap** (in addition to per-run
  budgets in [governance.md §7](./governance.md)) so the fleet cannot overspend in
  aggregate.
- **Fleet cost-rate-cap enforcement is admission-side, not kill-side.** On
  fleet-cap breach, **throttle admission** (queue new runs) rather than hard-stall
  or kill in-flight work — killing wastes spend and starves long-horizon runs. The
  global rate budget is allocated by **tenant fair-share** and fed into the fairness
  scheduler below. Preemption, when unavoidable, follows a defined **priority
  order** (low-tier/burst work first); **never preempt a run mid-trust-root
  attestation**. **Fleet-budget-burn is a first-class SLO** with its own
  burn-rate alert. (The A/B's `3 × n × k` is exactly this workload.) Admission-throttle
  alone cannot reach **in-flight re-execution** spend, so the cost-rate cap also **gates
  in-flight quorum re-runs** (§7 flake-recovery) — the one in-flight cost source large
  enough to outrun an admission freeze.
- **Queue + fairness scheduler** across runs/tenants; **per-tenant resource +
  credential + budget isolation**; **noisy-neighbor guard** = per-run cap on
  concurrent `Send` branches *and* per-run/per-tenant cap on secondary-provider
  quota draw so one fan-out can't starve others.
- **Capacity limits + backpressure** for sandboxes, the trusted gate runner, **and
  provider rate-limits/quota** (primary *and* secondary family — see §4), with
  queue-depth alerting; a heavy run queues rather than starving the fleet.
  Secondary-family quota is a first-class backpressure dimension: when the secondary
  is **also saturated** (T1 failover + always-on high-risk judging + **mutant-rotation
  re-grounding** §5 colliding), the noisy-neighbor guard applies to provider quota too —
  runs queue against the
  reserved secondary capacity (§4) instead of fleet-stalling on shared `429`s.
- **Paused-run state footprint.** Releasing compute on HITL/store/trust pause (§7,
  §3, §4) does **not** release state — worktrees, checkpoints, artifacts, indexes
  persist and are **unbounded at fleet scale**. Each run carries a **state-footprint
  estimate** (worktree + checkpoint + artifact + index bytes). A **fleet paused-run
  storage cap with alerting** bounds the aggregate. **Cap-hit with no drainable
  consumer:** when the cap is reached but every consumer is a legitimately-paused,
  un-staffable High-tier run — admission is already throttled to zero, so nothing
  drains it — alerting alone is insufficient. A defined policy applies. The cap is
  **byte-denominated**, so tier-ordered eviction to a cheaper CAS storage tier changes
  **$/byte, not byte count**, and cannot relieve it; only **reducing bytes** does. So the
  byte-relief order is: **storage-pressure feedback that forces the §7 reviewer-queue
  escalation (batch/delegate) sooner** to drive paused runs to terminal (the only
  byte-reducing drain, itself gated on saturated reviewer-pairs §7), with an **explicit
  operator decision point** if that does not relieve pressure. Tier-ordered cold/archival
  eviction remains available as a **$/byte (cost-rate) lever** feeding the fleet cost-rate
  cap above — not a byte-cap relief. **Long-paused runs hibernate:**
  spill the worktree to the **content-addressed store** and reconstitute on resume.
  Hibernation makes the *whole* worktree depend on CAS durability for an unbounded
  HITL pause, so spilled artifacts carry an explicit **retention lease/pin** keyed to
  live paused-run state, **excluded from object-store lifecycle expiry and from
  worktree GC until the run reaches a terminal state** — a blob-loss/GC/TTL event must
  not silently quarantine a paused run (recovery = full re-run; the integrity sweep
  alerts on a missing hibernated object before wake, §3).
  **Worktree GC must be resume-safe** — beyond the pin, it may reclaim a hibernated
  worktree only because every handle is re-resolvable by `tree_sha`/content address
  on wake, so GC
  cannot break resume.
  **Cold-tier restore latency feeds back to the §2 deadline.** A run hibernated to and
  then cold/archival-evicted (the $/byte lever above) rehydrates with **minutes-to-hours
  first-byte latency** on resume; the §2 reconstitution watchdog deadline must carry that
  cold-tier-restore term (§2) or a resuming run pages false `wedged`.

*(SRE-F6; bounds the trusted-runner serialization in COST-F3.)*

## 7. Human-in-the-loop load model

HITL is load-bearing for safety, so its **volume** is a first-class operational
constraint, not an externality.

- **Enumerate expected human touchpoints per feature/run** and a **latency budget**
  per class: Design approval, Release sign-off (two-person on trust-root), new-
  dependency source-diff, flake-recovery override, oracle/gate/criteria/config-edit
  trips, monitor escalations.
- **A HITL-blocked run releases its compute** (sandbox/runner slots) rather than
  holding it — ties to capacity (§6); its **state** is bounded/hibernated per §6.
- **Reviewer queue with SLAs + backlog/age alerting**; a **staffing/escalation
  fallback** when two-person sign-off can't be staffed (a release **deadlocks
  safely** as `paused`, never auto-approves).
- **Two-person reviewer-pool queueing model.** Every build / CI / IaC / deps /
  scanner / auth / trust-root change is forced **High → two-person Release**
  regardless of size ([governance.md §1e](./governance.md)). "Deadlocks safely as
  `paused`" is correct but collapses High-tier throughput to **reviewer-PAIR
  availability**, so it must be capacity-planned, not just alerted. Model it as a
  queue: High-tier release **arrival rate × service time** vs the number of
  **available reviewer-pairs**, with utilization kept below saturation. The
  steady-state (Poisson) model is **not sufficient on its own**: a fleet-wide model
  rotation (§5) injects a near-simultaneous **batch of `B` High-tier drift decisions**
  into the **same** reviewer-pair pool, so steady-state utilization understates the
  **transient queue depth (~`B/c`** for `c` pairs). Size the **transient**, not just
  steady-state utilization, modelling the drift spike as a **batch/bulk arrival
  superimposed on the steady stream**. Because **drift decisions and release sign-offs
  are different service-time classes sharing one pool**, give drift decisions a
  **distinct decision-class/queue** (or cautiously extend the §5 auto-continue default)
  so they don't **serialize behind routine release sign-offs**. A
  **staffing target is SLA-driven** (sized to the High-tier queue-age SLO below,
  including these correlated drift spikes of §5 — not just steady per-feature
  touchpoints). On **sustained backlog**, a defined policy applies in order:
  **batch sign-off windows**, **delegated reviewer tiers**, then **admission-throttle
  new High-tier runs** (§6) so the backlog cannot grow unbounded.
- **High-tier release-queue age is a first-class SLO**, with **capacity-derived
  alert thresholds** (warn/page tied to pair count and arrival rate, so the alert
  fires before the queue is unrecoverable rather than at a fixed wall-clock).
- **Flake-recovery quorum is bounded and counted.** The flake-recovery path
  (multiple trusted-runner re-runs + two-person override,
  [governance.md §5](./governance.md)) was unmodeled and unbounded per run, and
  incentivizes mislabeling genuinely-broken behavior as "flake". Each run carries a
  **flake-recovery counter**: quorum size is **bounded** per attempt, and crossing a
  **per-run threshold escalates as a quality / non-convergence design smell** rather
  than allowing unbounded per-incident overrides. Quorum re-runs are **counted in
  the §6 / cost model** compute, and flake-override decisions are **counted in
  human-decisions-per-feature** below.
  **Correlated flake is fleet-level, not per-run.** A shared trusted-infra fault under
  load makes *every* in-flight High-tier run enter the quorum-re-run path at once — an
  unbounded compute amplifier (`quorum_size × in-flight High runs`) plus a two-person-
  override thundering herd — and the **admission-side** fleet cost-cap (§6) cannot stanch
  it because the spend is **in-flight, not new**. So: the **flake-recovery counter +
  quorum compute are a fleet-level rate-limited resource** (not just a per-run bound);
  **many runs entering quorum on the same infra is detected as an INFRA INCIDENT that
  pauses — not re-runs** (status `paused: trusted_component_down`, §4, queue-and-drain on
  recovery) rather than amplifying a shared fault; and the **fleet cost-rate cap reaches
  in-flight re-execution compute** (§6), not admission alone.
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
