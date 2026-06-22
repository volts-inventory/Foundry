"""Operations artifacts (agents.md Layer 6 — Operations).

DevOps/SRE → `Pipeline`, `IaCProposal` (plan-only), `DeployPlan`;
Release Manager → `Release`. IaC is PROPOSED, never applied, by agents: the
plan diff is gated by policy-as-code and a separate least-privileged
non-agent deployer applies it after admission (governance.md §2/§5).
Bodies are content-addressed handles (DS9); effects go through the outbox.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class Pipeline(FrozenModel):
    """CI/CD definition authored/verified by DevOps/SRE (agents.md Layer 6).
    The pipeline is itself a high-privilege artifact reviewed by the
    Supply-Chain Reviewer (agents.md Layer 5)."""

    pipeline_uri: str  # CAS handle to the pipeline definition (DS9)
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class IaCProposal(FrozenModel):
    """IaC `plan` PROPOSAL (agents.md Layer 6 — DevOps/SRE SOP): gated by
    policy-as-code (Conftest/Checkov, Supply-chain gate) and applied only by
    the non-agent deployer after admission (governance.md §2/§5)."""

    plan_uri: str  # CAS handle to the `terraform plan` diff (DS9)
    plan_only: bool = True  # MUST default True — agents never apply
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class DeployPlan(FrozenModel):
    """Deploy plan + rollback + health checks (agents.md Layer 6 — DevOps/SRE
    SOP). Execution belongs to the non-agent deployer, never this artifact."""

    plan_uri: str  # CAS handle to the deploy plan body (DS9)
    rollback_plan_uri: str  # CAS handle to the rollback procedure (DS9)
    health_checks: tuple[str, ...] = ()
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class Release(FrozenModel):
    """Release Manager output (agents.md Layer 6): semver + changelog + the
    independent evidence pack for human sign-off (governance.md §6).

    `server_nonce` is the per-approval server-issued challenge bound into the
    class-4 human attestation tuple, so the Release-gate sign-off cannot be
    minted or replayed by the pipeline (governance.md §6). Idempotent via the
    external-action ledger; never double-tags on resume.
    """

    semver: str
    artifact_digest: str  # the attested digest admission re-verifies (gov §5)
    changelog_uri: str  # CAS handle (DS9)
    evidence_pack_uri: str  # independent, NON-agent-assembled evidence pack (gov §6)
    server_nonce: str  # binds the human Release attestation (nonce binding field)
    seq: Seq


# ===========================================================================
# Phase-3 (Operability / Day-2) schema foundation. (docs/operations.md)
#
# These records are the SHARED contract the seven M3 ops modules build on
# (run_status, operator, store_resilience, capacity, hitl, trusted_component,
# upgrade). They follow the same conventions as the records above: FrozenModel
# (frozen + extra="forbid"), `seq: Seq` on every persisted record, bodies as
# content-addressed `*_uri` handles (DS9/L4), `default_factory` for mutable
# defaults (S1), and integer micro-USD for money (architecture.md §8).
# ===========================================================================


# --- §2 Run-status model: paused vs. wedged --------------------------------


class RunStatus(StrEnum):
    """First-class run liveness (operations.md §2). The load-bearing
    distinction is PAUSED (a legitimate, machine-readable wait — shows $0/hr +
    an `awaiting`) vs WEDGED (no superstep advanced past the derived deadline
    and the state is NOT a legitimate pause reason — alerted). The supervisor
    watchdog/heartbeat is what escalates a stalled run to WEDGED."""

    RUNNING = "running"
    PAUSED = "paused"  # legitimate wait (HITL / store / trusted-component / budget)
    WEDGED = "wedged"  # no heartbeat past the derived deadline — alert
    HIBERNATED = "hibernated"  # long pause: worktree spilled to CAS (§6)
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"  # poisoned checkpoint / dangling handle (§3)


class PauseReason(StrEnum):
    """Why a run is legitimately PAUSED (operations.md §2/§3/§4/§7). Every value
    here is operator-distinguishable from WEDGED — a paused run with one of
    these reasons burns no budget and is NOT alerted as stuck."""

    STORE_OUTAGE = "store_outage"  # Postgres/object-store unavailable (§3)
    TRUSTED_COMPONENT_UNAVAILABLE = "trusted_component_unavailable"  # §4 outage ≠ compromise
    HITL_WAIT = "hitl_wait"  # awaiting a human touchpoint (§7)
    CAPACITY = "capacity"  # admission/backpressure throttle (§6)
    NON_CONVERGENCE = "non_convergence"  # bounded fix loop exhausted
    LATCHED_BY_SIBLING_FORK = "latched_by_sibling_fork"  # sibling stable-FAILed digest
    OPERATOR = "operator"  # explicit operator hold


class RunStatusRecord(FrozenModel):
    """Machine-readable per-run status — the answer to the most common Day-2
    question, "is this run paused or wedged?" (operations.md §2).

    `pause_reason` is set IFF `status == PAUSED` (a WEDGED/RUNNING run carries
    None). `awaiting` names who/what the pause waits on (e.g. "two-person
    release sign-off"). `last_progress_seq`/`last_progress_ts_ms` are the
    slow-but-progressing heartbeat marker: an adapter heartbeat or
    partial-progress marker advances them even without a superstep, so
    legitimate provider backoff / cold-tier rehydration is not misread as
    WEDGED. `expected_next_transition_deadline_ms` is DERIVED per node type
    (p99 + provider-backoff allowance + expected queue-wait + a cold-tier
    first-byte-restore term on rehydration), never a static constant — only the
    no-heartbeat case past this deadline escalates to WEDGED.

    `cost_rate_microusd_per_hr` makes the "$0/hr when paused" check
    machine-readable. `state_footprint_bytes` is the per-run worktree +
    checkpoint + artifact + index estimate that feeds the §6 paused-run storage
    cap.
    """

    run_id: str
    status: RunStatus
    current_phase: str  # the phase/node the run is in (e.g. "implementation")
    pause_reason: PauseReason | None = None  # set IFF status == PAUSED
    awaiting: str = ""  # who/what the pause waits on (operator surface)
    last_progress_seq: Seq | None = None  # heartbeat / partial-progress marker
    last_progress_ts_ms: int = 0  # wall-clock of the last progress marker
    expected_next_transition_deadline_ms: int = 0  # DERIVED, not constant (§2)
    cost_rate_microusd_per_hr: int = 0  # paused ⇒ 0 (machine-readable $0/hr)
    state_footprint_bytes: int = 0  # feeds the §6 paused-run storage cap
    seq: Seq

    def is_alertable(self) -> bool:
        """A WEDGED run is alerted; a PAUSED run (any legitimate reason) is
        not. The operator surface routes on this rather than re-deriving it."""
        return self.status is RunStatus.WEDGED


# --- §1 Operator surface: SLOs + alert taxonomy ----------------------------


class AlertSeverity(StrEnum):
    """Operator alert severity (operations.md §1). PAGE wakes on-call; the
    rest route to the dashboard / ticket queue."""

    PAGE = "page"
    WARN = "warn"
    INFO = "info"


class SLOTarget(FrozenModel):
    """One operator SLO target (operations.md §1 examples: run success rate,
    time-to-ack per escalation class, p95 wall-clock per feature class, budget
    burn-rate, trusted-component availability, HITL-queue age).

    `objective_micro` is the target for RATIO SLOs expressed in integer
    millionths so ratio SLOs (e.g. 0.995 success) carry no float; `unit` names
    the dimension (e.g. "ratio", "ms", "count", "microusd_per_hr"). Burn-rate /
    threshold SLOs link a runbook.

    COUNT/MS-dimensioned ceiling SLOs (e.g. `max_wedged_count`,
    `max_stuck_age_ms`) carry their ceiling in a DISTINCT, clearly-named integer
    field — `objective_count` / `objective_ms` — rather than overloading the
    millionths `objective_micro` with a raw count/ms (M3-R1-03). `ceiling()`
    resolves the right field for a ceiling SLO and FAILS CLOSED (raises) if the
    expected field is unset, so a mis-configured ceiling SLO is never silently
    read as 0. For backward compatibility a ceiling SLO that supplies only the
    legacy `objective_micro` (the field is non-None) still resolves to it."""

    name: str  # SLO identifier (e.g. "run_success_rate", "hitl_queue_age")
    objective_micro: int | None = None  # RATIO target in millionths (no float)
    objective_count: int | None = None  # COUNT ceiling (e.g. max WEDGED runs)
    objective_ms: int | None = None  # MS ceiling (e.g. oldest stuck-run age)
    unit: str = "ratio"
    window_ms: int = 0  # measurement window (0 = instantaneous/point)
    runbook_uri: str | None = None  # CAS/doc handle to the runbook (§1)

    def ratio_floor(self) -> int:
        """The ratio FLOOR objective in millionths (fail-closed if unset)."""
        if self.objective_micro is None:
            raise SLOConfigError(
                f"ratio SLO {self.name!r} has no objective_micro floor configured"
            )
        return self.objective_micro

    def ceiling(self, *, field: str) -> int:
        """Resolve a COUNT/MS CEILING objective from its dedicated field.

        `field` is "objective_count" or "objective_ms". The dedicated field
        wins; if it is unset we fall back to the legacy `objective_micro`
        (back-compat for callers that pre-date the distinct fields). If NEITHER
        is set we FAIL CLOSED (raise :class:`SLOConfigError`) rather than read a
        silent 0 ceiling — a configured-but-unevaluable SLO must be visible
        (M3-R1-03)."""
        dedicated = getattr(self, field)
        if dedicated is not None:
            return dedicated
        if self.objective_micro is not None:  # legacy: raw ceiling in _micro
            return self.objective_micro
        raise SLOConfigError(
            f"ceiling SLO {self.name!r} has neither {field} nor a legacy "
            "objective_micro configured"
        )


class SLOConfigError(ValueError):
    """A configured SLO target is missing the objective field its kind needs.

    Raised rather than silently reading a 0 objective, so a mis-configured SLO
    fails loud at evaluation instead of becoming a dead (never-alerting) monitor
    (operations.md §1; M3-R1-03)."""


class SLO(FrozenModel):
    """A measured SLO observation against its `target` (operations.md §1).

    `observed_micro` is the current value in the same millionths unit as
    `SLOTarget.objective_micro`; `breaching` flips when the burn-rate/threshold
    is crossed (computed by the monitor, recorded here for the dashboard)."""

    target: SLOTarget
    observed_micro: int
    breaching: bool = False
    seq: Seq


class Alert(FrozenModel):
    """Operator-surface alert (operations.md §1). The alert taxonomy maps 1:1
    to the deterministic monitors (loop/non-convergence, cost threshold, OPA
    deny, retry-until-green, oracle/config-edit, pinned-ID drift) plus infra
    (§3/§4) and HITL-backlog (§7). Every alert links a runbook.

    `monitor` names the deterministic source so the 1:1 mapping is explicit;
    `run_id` is None for fleet-level alerts (e.g. fleet-budget burn, store
    outage). `detail_uri` is a CAS handle to the full context (DS9)."""

    name: str  # the alert class (e.g. "non_convergence", "store_outage")
    severity: AlertSeverity
    monitor: str  # the deterministic monitor / source it maps to (§1)
    summary: str  # short operator-facing description
    run_id: str | None = None  # None ⇒ fleet-level alert
    runbook_uri: str | None = None  # linked runbook (§1)
    detail_uri: str | None = None  # CAS handle to full context (DS9)
    seq: Seq


# --- §6 Multi-tenancy, capacity, and fairness ------------------------------


class TenantQuota(FrozenModel):
    """Per-tenant resource + credential + budget isolation (operations.md §6).

    `cost_rate_cap_microusd_per_hr` is the tenant's slice of the FLEET
    cost-rate cap (admission-side throttling, not kill-side); `fair_share_micro`
    is the tenant's weight (millionths) fed into the fairness scheduler.
    `max_concurrent_runs` / `max_send_branches_per_run` are the noisy-neighbor
    guards so one tenant's fan-out can't starve others."""

    tenant_id: str
    cost_rate_cap_microusd_per_hr: int  # tenant slice of the fleet cost-rate cap
    fair_share_micro: int = 0  # scheduler weight in millionths
    max_concurrent_runs: int = 0  # 0 = unset/unbounded
    max_send_branches_per_run: int = 0  # noisy-neighbor guard (§6)
    seq: Seq


class QueueEntry(FrozenModel):
    """A run waiting in the admission / fairness queue (operations.md §6). A
    heavy run QUEUES rather than starving the fleet; admission is throttled
    (not killed) on a fleet cost-rate breach.

    `lineage` names the per-lineage backpressure dimension when the wait is on
    provider quota (§4: each of the ≥3 provisioned lineages is tracked
    separately); None for non-lineage waits (sandbox / gate-runner)."""

    run_id: str
    tenant_id: str
    priority: int = 0  # higher = sooner; preemption order is low-tier/burst first
    enqueued_ts_ms: int = 0
    requested_cost_rate_microusd_per_hr: int = 0
    lineage: str | None = None  # per-lineage quota dimension (§4/§6)
    seq: Seq


class FairnessDecision(FrozenModel):
    """A scheduler admission decision over the queue (operations.md §6).

    `admitted` False ⇒ the run stays queued (admission-side throttle). NB:
    in-flight mid-trust-root attestation is NEVER preempted (§4/§6); the
    fleet cost-rate cap also gates the named in-flight cost sources
    (`reason` records which). `fleet_cost_rate_microusd_per_hr` /
    `fleet_cost_rate_cap_microusd_per_hr` carry the fleet aggregate the
    decision was made against (integer micro-USD)."""

    run_id: str
    admitted: bool
    reason: str  # e.g. "fleet_cost_rate_cap" / "fair_share" / "lineage_saturated"
    fleet_cost_rate_microusd_per_hr: int = 0  # current fleet aggregate rate
    fleet_cost_rate_cap_microusd_per_hr: int = 0  # the fleet cost-rate cap (§6)
    preempts: tuple[str, ...] = ()  # run_ids preempted (low-tier/burst first)
    seq: Seq


# --- §7 Human-in-the-loop load model ---------------------------------------


class ReviewerQueueEntry(FrozenModel):
    """One HITL touchpoint waiting in the reviewer queue (operations.md §7).

    `decision_class` distinguishes service-time classes that share one pool —
    e.g. routine "release_signoff" vs a fleet-drift "drift_decision" batch — so
    drift decisions get a distinct queue and don't serialize behind release
    sign-offs. `two_person` is the trust-root forced-High path (governance.md
    §1e): it deadlocks safely as PAUSED, never auto-approves. `sla_deadline_ms`
    is the per-class latency budget; `enqueued_ts_ms` ages it for the
    backlog/age SLO."""

    run_id: str
    decision_class: str  # e.g. "design_approval" / "release_signoff" / "drift_decision"
    subject_artifact_uri: str  # what awaits the human (CAS handle, DS9)
    two_person: bool = False  # trust-root forced-High two-person sign-off (§7)
    enqueued_ts_ms: int = 0
    sla_deadline_ms: int = 0  # per-class latency budget (§7)
    seq: Seq


class HITLLoad(FrozenModel):
    """Fleet HITL-load snapshot for the operator surface (operations.md §7).

    `backlog_depth` is the reviewer-queue depth; `oldest_age_ms` ages the
    oldest entry against the High-tier release-queue-age SLO (a first-class
    SLO with capacity-derived warn/page thresholds). `available_reviewer_pairs`
    drives the two-person-pool queueing model — High-tier throughput collapses
    to pair availability. `alert_age_ms` is the capacity-derived threshold
    (tied to pair count + arrival rate) so the alert fires before the queue is
    unrecoverable. `per_class_backlog` breaks the depth down by decision class
    (S1: default_factory)."""

    backlog_depth: int = 0
    oldest_age_ms: int = 0  # age of the oldest queued entry (§7 age SLO)
    available_reviewer_pairs: int = 0  # two-person-pool capacity (§7)
    arrival_rate_micro_per_hr: int = 0  # High-tier arrival rate (millionths/hr)
    alert_age_ms: int = 0  # capacity-derived warn/page threshold (§7)
    flake_recovery_count: int = 0  # bounded+counted flake-recovery overrides (§7)
    per_class_backlog: dict[str, int] = Field(default_factory=dict)  # S1
    seq: Seq


# --- §3 Stateful-store availability + corruption recovery ------------------


class CheckpointHealth(FrozenModel):
    """Health of a run's durable checkpoint (operations.md §3).

    `clean` False marks a POISONED checkpoint — one that deterministically
    crashes on resume, or whose out-of-band `history_ref`/artifact handle
    dangles (object-store blob lost). Recovery is fork-to-prior-good
    (`last_clean_checkpoint_uri`), a NEW run_id, with operator override; a
    periodic integrity sweep sets `dangling_handle_uris`. For a hibernated
    worktree a missing object is a HARD alert BEFORE wake (not a wake-time
    quarantine), so `quarantined` + the dangling list surface while the run is
    still paused-recoverable."""

    run_id: str
    checkpoint_uri: str  # CAS handle of the checkpoint under inspection (DS9)
    clean: bool = True  # False ⇒ poisoned (crash-on-resume or dangling handle)
    quarantined: bool = False  # quarantined pending fork-to-prior-good (§3)
    last_clean_checkpoint_uri: str | None = None  # fork-to-prior-good pointer
    dangling_handle_uris: tuple[str, ...] = ()  # integrity-sweep findings (§3)
    hibernated: bool = False  # whole worktree depends on CAS durability (§6)
    seq: Seq


# --- §5 / §8 Upgrade & migration with runs in flight -----------------------


class VersionStamp(FrozenModel):
    """The version-stamp recorded on EVERY checkpoint / run manifest
    (operations.md §5): a run resumes on its PINNED graph/prompt version and
    never silently mis-resumes against an incompatible schema.

    `model_pins` is the per-role pinned model id map (S1: default_factory);
    `toolchain_digest` couples the builder-image/toolchain pins into `tree_sha`
    so a bump invalidates cached verdicts (§8)."""

    state_schema_ver: int
    graph_ver: str
    prompt_ver: str
    toolchain_digest: str | None = None  # builder/runner image + toolchain pins (§8)
    model_pins: dict[str, str] = Field(default_factory=dict)  # role -> model id (S1)


class UpgradeAction(StrEnum):
    """Operator/fleet response to an upgrade or mid-flight drift event
    (operations.md §5). Drift NEVER silently continues a T1 judgment on
    drifted weights."""

    CONTINUE = "continue"  # accept recalibration (auto for Low/Standard drift)
    FORK = "fork"  # branch onto the new version
    ABORT = "abort"
    DRAIN_BEFORE_UPGRADE = "drain_before_upgrade"  # breaking schema change (§5)
    MIGRATE = "migrate"  # additive change via a migration function (§5)


class UpgradePlan(FrozenModel):
    """An upgrade / migration plan for runs in flight (operations.md §5/§8).

    `from_version`/`to_version` are the VersionStamps the plan moves between;
    `action` is the per-run/fleet response. `drift_recalibration` records a
    fleet-wide model-drift recalibration flag (auto-continue Low/Standard,
    surfaced on the scorecard, not blocking); High / trust-root runs PAUSE for
    explicit decision. `affected_run_ids` scopes the correlated spike (drift /
    held-out-mutant-rotation exposure) so the §7 reviewer queue can be sized
    for it. `migration_uri` is the CAS handle to the migration function for an
    additive schema change (DS9)."""

    from_version: VersionStamp
    to_version: VersionStamp
    action: UpgradeAction
    drift_recalibration: bool = False  # fleet model-drift recalibration flag (§5)
    affected_run_ids: tuple[str, ...] = ()  # correlated-spike scope (§5/§7)
    migration_uri: str | None = None  # CAS handle to the migration function (§5)
    seq: Seq
