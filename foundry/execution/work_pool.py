"""Execution-worker pool — the scalable SANDBOX dispatch seam. (plan §4 [15])

WHAT SCALES, AND WHO OWNS WHICH KNOB
====================================

Reasoning nodes (product / architecture / review judgment) stay **in-process**:
they are the orchestrator's own graph, and their concurrency is the orchestrator's
LangGraph fan-out. Only the untrusted execution **sandbox** (a ``WorkerAdapter``
running arbitrary bash inside a :class:`~foundry.execution.sandbox.Sandbox`) is
the thing that scales horizontally — so it is the only thing this pool dispatches.

The design principle (plan §4 [15]) splits the two concurrency knobs cleanly:

* **LOGICAL concurrency — owned by the orchestrator (application code).** How many
  parallel execution *tasks* exist at all: the per-run ``Send`` fan-out, the A/B
  ``3 × n × k`` arms. This is graph shape, decided in Python.
* **PHYSICAL slots — owned by INFRA, never the app.** How many *workers* actually
  exist to run those tasks: ``docker compose up --scale worker=N`` or a Kubernetes
  ``Deployment.replicas`` + an HPA. The application MUST NOT imperatively spawn a
  container (no ``docker run`` / subprocess-of-docker from app code) — that would
  put the physical-slot knob inside the logical layer and defeat both autoscaling
  and the budget ceiling below.

The bridge between the two is THIS seam: a durable, content-addressed **task
record** placed on a queue, and a pool of **generic** workers that each claim one
task and run whatever adapter the task names. A worker has no role of its own —
the role/work is a property of the *task* it claims, not of the worker (a worker
is interchangeable; this is what lets infra scale replicas blindly).

    orchestrator ──submit(ExecutionTask)──▶ durable queue ──claim──▶ generic worker
       (logical)                              (this seam)              (physical slot)
                                                                            │
                                                          runs task.adapter in a Sandbox
                                                                            │
         result(task_id) ◀────────────── content-addressed result record ◀─┘

THE REAL CEILING IS BUDGET + PROVIDER QUOTA, NOT SLOT COUNT (ops §6)
====================================================================

A slot cap alone does not bound spend — a saturated pool of cheap slots can still
outspend the fleet. So the pool refuses to *admit* a task that would exceed either
(a) the configured slot cap (excess **queues**, never spawns a slot) or (b) the
budget / per-lineage quota in :class:`QuotaPolicy`. A task that would breach budget
is **rejected at admit** (ops §6: throttle admission, do not kill in-flight work)
and surfaces a terminal ``rejected`` record — it is never run. Per-lineage quota is
a first-class dimension (ops §6 / arch §4): each of the ≥3 provisioned lineages is
metered separately so one lineage's fan-out cannot starve the others.

DURABILITY — IDEMPOTENT CLAIM, NO LOST / DOUBLE-RUN WORK (arch §5)
==================================================================

Task records are **content-addressed** (``task_id = content_digest(packet, adapter,
lineage)``) and **claimable**: a worker must win an atomic claim before running.
A worker dying mid-task leaves the claim recoverable (lease), and a re-submission
of an identical task coalesces onto the same record rather than double-running —
the same supersession-not-duplication discipline the resumable checkpointer and the
keyed-union state channels already use. A task whose result is already recorded is
returned from cache, never re-run.

TESTED vs SCAFFOLDING
=====================

* **TESTED (real, in-process):** :class:`LocalWorkPool` — the DEV drop-in. A
  thread-pool that honours the slot cap (bounded concurrency, asserted via a live
  counter), the budget / per-lineage quota (over-budget tasks rejected, not run),
  is deterministic in its results, and has an idempotent claim (no double-run).
  It submits tasks and runs adapters; it NEVER calls docker / spawns a container.
* **SCAFFOLDING (documented seam, NOT built here):** ``ContainerWorkPool`` and
  ``K8sJobPool`` would implement the SAME :class:`WorkPool` interface against a real
  broker (Redis/SQS/NATS) + worker replicas. They are described in their docstrings
  below as the production shape; no container, broker, or cluster exists in this
  repo, so they are intentionally left unimplemented. The compose ``--scale`` / K8s
  ``replicas`` + HPA mapping is the *physical-slot* knob for those backends.

Nothing in this module — or anything that uses it — spawns a container. The app's
only verb is ``submit``; making slots is infra's job.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from foundry.canonical import content_digest
from foundry.schemas.base import BudgetLedger

if TYPE_CHECKING:
    from foundry.execution.adapter_contract import AdapterResult, TaskPacket, WorkerAdapter
    from foundry.execution.sandbox import Sandbox

__all__ = [
    "AdapterFactory",
    "BudgetExceededError",
    "ExecutionTask",
    "LocalWorkPool",
    "QuotaPolicy",
    "SandboxFactory",
    "TaskRejectedError",
    "TaskState",
    "TaskStatus",
    "WorkPool",
    "allocate_budget_tree",
    "allocate_lineage_quotas",
]

# Lineage bucket for work with no resolved domain/area (fallback; also the global
# axis label). Kept in sync with ExecutionTask.lineage's default.
_DEFAULT_LINEAGE = "default"


def allocate_lineage_quotas(weights: Mapping[str, int], global_cap_microusd: int) -> dict[str, int]:
    """Split a global micro-USD envelope across lineages proportional to ``weights``
    — the budget tree the org fan-out spends down (org_fanout_design.md, Wave D).

    The Program Manager owns one global cap; it sub-allocates a per-lineage (per-
    AREA) quota so one area's fan-out cannot starve the others (the noisy-neighbor
    guard, ops §6 / arch §4). FLOOR division guarantees **Σ(allocations) ≤
    global_cap** — a fail-closed invariant: the sum of the children's quotas can
    never let total committed spend exceed the parent envelope (any rounding
    remainder stays as un-allocated global headroom, never over-commits).

    ``global_cap_microusd <= 0`` (unlimited) or no positive weight => ``{}`` (no
    per-lineage limit; lineages remain bound only by the global ledger). Integer
    micro-USD throughout — never floats (arch §8). Deterministic: a pure function
    of its inputs, so a resume re-derives the identical tree."""
    total = sum(w for w in weights.values() if w > 0)
    if global_cap_microusd <= 0 or total <= 0:
        return {}
    return {
        lineage: (global_cap_microusd * weight) // total
        for lineage, weight in weights.items()
        if weight > 0
    }


def allocate_budget_tree(
    domain_area: Mapping[str, str],
    domain_weight: Mapping[str, int],
    global_cap_microusd: int,
) -> dict[str, int]:
    """The two-level org budget tree: global envelope -> per-AREA -> per-DOMAIN
    (org_fanout_design.md, Wave D). The Program Manager splits the global cap
    across present areas (by area task-weight), then each Area Manager splits ITS
    area cap across the domains it owns (by domain task-weight) — both levels by
    the floor-division :func:`allocate_lineage_quotas`.

    Returns a flat ``{domain -> quota}`` (the per-domain lineage quotas the pool
    meters against), so a domain that exhausts its share is rejected even when its
    SIBLING domains in the same area are idle (true per-domain isolation) and even
    when the global ledger has room. Both levels are floor-allocated, so
    **Σ(domain quotas) ≤ Σ(area quotas) ≤ global_cap** — fail-closed at every tier.
    ``global_cap <= 0`` (unlimited) => ``{}`` (no per-domain limit). Pure +
    deterministic: a resume re-derives the identical tree."""
    if global_cap_microusd <= 0:
        return {}
    area_weight: dict[str, int] = {}
    for domain, weight in domain_weight.items():
        if weight > 0:
            area = domain_area.get(domain, _DEFAULT_LINEAGE)
            area_weight[area] = area_weight.get(area, 0) + weight
    area_caps = allocate_lineage_quotas(area_weight, global_cap_microusd)
    out: dict[str, int] = {}
    for area, area_cap in area_caps.items():
        in_area = {
            domain: weight
            for domain, weight in domain_weight.items()
            if weight > 0 and domain_area.get(domain, _DEFAULT_LINEAGE) == area
        }
        out.update(allocate_lineage_quotas(in_area, area_cap))
    return out

# A worker, having claimed a task, builds its OWN sandbox + adapter from the task.
# The pool is handed FACTORIES (not live instances) precisely because a real
# worker is a separate process/replica that constructs these itself — the seam is
# identical whether the worker is a thread (LocalWorkPool) or a container.
SandboxFactory = Callable[["ExecutionTask"], "Sandbox"]
AdapterFactory = Callable[[str], "WorkerAdapter"]


class TaskRejectedError(RuntimeError):
    """Admission refused the task (slot cap or budget/quota) — it was NOT run."""


class BudgetExceededError(TaskRejectedError):
    """The task's estimated cost would breach the budget cap or a lineage quota."""


class TaskStatus(StrEnum):
    """Lifecycle of a durable task record. ``rejected``/``done``/``failed`` are
    terminal; ``queued``/``claimed``/``running`` are in-flight."""

    QUEUED = "queued"  # admitted, awaiting a slot
    CLAIMED = "claimed"  # a worker won the atomic claim, not yet running
    RUNNING = "running"  # adapter executing in its sandbox
    DONE = "done"  # result recorded (content-addressed)
    FAILED = "failed"  # adapter raised; recorded, not silently lost
    REJECTED = "rejected"  # refused at admit (budget/quota) — never run


@dataclass(frozen=True)
class ExecutionTask:
    """A durable, content-addressed request to run ONE adapter in ONE sandbox.

    The task — not the worker — carries the role: ``adapter`` names WHICH
    substrate to run, ``packet`` is the immutable input, ``lineage`` is the
    per-lineage quota bucket (ops §6 / arch §4), and ``est_cost_microusd`` is the
    pre-admission spend estimate the budget check meters against (integer
    micro-USD — never floats; arch §8).

    ``task_id`` is the content digest of the request, so two byte-identical
    submissions produce the SAME id and coalesce onto one record (idempotent
    claim, no double-run).
    """

    packet: TaskPacket
    adapter: str
    lineage: str = "default"
    est_cost_microusd: int = 0

    @property
    def task_id(self) -> str:
        """Content-addressed identity — stable across processes (uses
        ``content_digest``, never builtin ``hash()``; LEARNINGS L7)."""
        return content_digest(
            {
                "packet": self.packet.model_dump(mode="json"),
                "adapter": self.adapter,
                "lineage": self.lineage,
                "est_cost_microusd": self.est_cost_microusd,
            }
        )


@dataclass
class TaskState:
    """The pool's view of one task record (mutated under the pool lock)."""

    task: ExecutionTask
    status: TaskStatus
    result: AdapterResult | None = None
    error: str | None = None


@dataclass
class QuotaPolicy:
    """The budget + capacity ceiling the pool admits against (ops §6).

    The real bound on how much execution can run is provider quota + budget, NOT
    the slot count — a slot cap alone cannot stop a saturated pool from
    overspending. So admission meters two things:

    * a global :class:`BudgetLedger` (token + compute spend in micro-USD), refusing
      any task whose ``est_cost_microusd`` would push committed spend past
      ``ledger.cap_microusd``;
    * a **per-lineage** micro-USD quota (``lineage_quota_microusd``), tracked
      separately for each of the ≥3 provisioned lineages so one lineage's fan-out
      cannot starve the others (the noisy-neighbor guard, ops §6 / arch §4). A
      lineage with no explicit quota is unmetered on the per-lineage axis but still
      bound by the global ledger.

    Spend is *committed* at admit and *released* if (and only if) the task is
    rejected before running; a task that runs holds its commit (the dev stand-in
    for the proxy's external metering — adapter-reported cost is advisory only,
    arch §2). All values are integer micro-USD.
    """

    ledger: BudgetLedger = field(default_factory=BudgetLedger)
    lineage_quota_microusd: Mapping[str, int] = field(default_factory=dict)
    _committed_total: int = 0
    _committed_by_lineage: dict[str, int] = field(default_factory=dict)

    def would_exceed(self, task: ExecutionTask) -> str | None:
        """Reason this task is over budget/quota, or ``None`` if it fits."""
        cost = task.est_cost_microusd
        cap = self.ledger.cap_microusd
        if cap > 0 and self._committed_total + cost > cap:
            return (
                f"global budget: committed {self._committed_total} + est {cost} "
                f"> cap {cap} micro-USD"
            )
        quota = self.lineage_quota_microusd.get(task.lineage)
        if quota is not None and self._committed_by_lineage.get(task.lineage, 0) + cost > quota:
            used = self._committed_by_lineage.get(task.lineage, 0)
            return (
                f"lineage {task.lineage!r} quota: committed {used} + est {cost} "
                f"> quota {quota} micro-USD"
            )
        return None

    def commit(self, task: ExecutionTask) -> None:
        """Reserve this task's estimated spend (call only after ``would_exceed``
        returned ``None``, under the pool lock)."""
        self._committed_total += task.est_cost_microusd
        self._committed_by_lineage[task.lineage] = (
            self._committed_by_lineage.get(task.lineage, 0) + task.est_cost_microusd
        )

    def release(self, task: ExecutionTask) -> None:
        """Give back a commit (only for a task rejected before it ran)."""
        self._committed_total -= task.est_cost_microusd
        self._committed_by_lineage[task.lineage] = (
            self._committed_by_lineage.get(task.lineage, 0) - task.est_cost_microusd
        )


@runtime_checkable
class WorkPool(Protocol):
    """The dispatch seam every backend implements (local thread-pool here;
    ContainerWorkPool / K8sJobPool in production — see module docstring).

    The application's ONLY verbs are ``submit`` (place a durable task record) and
    ``result`` (read it back). Making physical slots is infra's job; nothing here
    spawns a container.
    """

    max_slots: int

    def submit(self, task: ExecutionTask) -> str:
        """Admit a task (budget/quota checked) -> durable ``task_id``. Excess over
        ``max_slots`` queues; an over-budget task is rejected, never run."""
        ...

    def result(self, task_id: str) -> AdapterResult:
        """Block until the task's result is recorded; return it (idempotent —
        re-reads the same content-addressed record, never re-runs)."""
        ...

    def status(self, task_id: str) -> TaskStatus:
        """Current lifecycle status of a known task record."""
        ...


class LocalWorkPool:
    """In-process, thread-pool :class:`WorkPool` — the DEV drop-in, fully testable.

    A real ``ContainerWorkPool`` / ``K8sJobPool`` (NOT built — see module docstring)
    would implement this same interface against a broker + worker replicas; this
    class is the substitutable local equivalent. It spawns THREADS, never
    containers: the bounded concurrency (``max_slots``) is a semaphore, not a
    ``docker run``.

    Determinism: ``result(task_id)`` is a pure function of the task (the adapter
    and sandbox are deterministic by contract for the stub substrate), and the
    content-addressed ``task_id`` makes a duplicate submit a cache hit. Workers run
    concurrently up to ``max_slots`` only — asserted in tests via a live counter.

    PRODUCTION SCAFFOLDING (interface-compatible, intentionally absent here)::

        class ContainerWorkPool(WorkPool):
            '''Same seam against a real broker (Redis/SQS/NATS) + worker replicas.
            submit() -> enqueue a content-addressed message; workers (separate
            containers, `docker compose up --scale worker=N`) BLPOP/claim it with
            an atomic lease, build their own Sandbox + adapter, run, and write a
            content-addressed result back. max_slots is advisory here — the true
            physical-slot count is the compose --scale value (infra, not app).'''

        class K8sJobPool(WorkPool):
            '''submit() -> create a Kubernetes Job (or enqueue for a Deployment of
            generic workers); physical slots = Deployment.replicas governed by an
            HPA on queue depth. Logical fan-out stays in the orchestrator; the app
            still never imperatively spawns a Pod — the control plane does.'''
    """

    def __init__(
        self,
        *,
        sandbox_factory: SandboxFactory,
        adapter_factory: AdapterFactory,
        max_slots: int = 1,
        quota: QuotaPolicy | None = None,
    ) -> None:
        if max_slots < 1:
            raise ValueError("max_slots must be >= 1")
        self.max_slots = max_slots
        self._sandbox_factory = sandbox_factory
        self._adapter_factory = adapter_factory
        self._quota = quota or QuotaPolicy()
        # Bounded PHYSICAL slots: a thread pool of exactly max_slots workers. Excess
        # submissions queue inside the executor — the app never makes more slots.
        self._executor = ThreadPoolExecutor(max_workers=max_slots)
        self._lock = threading.Lock()
        self._records: dict[str, TaskState] = {}
        self._futures: dict[str, Future[None]] = {}

    # -- admission (logical) -------------------------------------------------

    def submit(self, task: ExecutionTask) -> str:
        """Admit a task. Budget/quota is checked under the lock BEFORE a worker is
        scheduled; an over-budget task is recorded ``rejected`` and raises — it is
        never run (ops §6: throttle at admit). A duplicate of an in-flight or
        completed task coalesces onto the existing record (idempotent claim)."""
        tid = task.task_id
        with self._lock:
            existing = self._records.get(tid)
            if existing is not None:
                # Idempotent claim: identical task already known — do not double-run.
                if existing.status is TaskStatus.REJECTED:
                    raise BudgetExceededError(existing.error or "previously rejected")
                return tid

            reason = self._quota.would_exceed(task)
            if reason is not None:
                self._records[tid] = TaskState(task, TaskStatus.REJECTED, error=reason)
                raise BudgetExceededError(reason)

            self._quota.commit(task)
            self._records[tid] = TaskState(task, TaskStatus.QUEUED)
            self._futures[tid] = self._executor.submit(self._run, tid)
        return tid

    # -- worker (physical slot) ----------------------------------------------

    def _claim(self, task_id: str) -> ExecutionTask | None:
        """Atomically win the claim for ``task_id``: flip QUEUED -> CLAIMED and
        return the task, or ``None`` if another worker already claimed/ran it.

        This is the idempotent-claim primitive a real broker provides via an
        atomic lease; here it is the pool lock. A task is run by AT MOST one
        worker — a re-delivery (or duplicate future) is a no-op."""
        with self._lock:
            rec = self._records.get(task_id)
            if rec is None or rec.status is not TaskStatus.QUEUED:
                return None
            rec.status = TaskStatus.CLAIMED
            return rec.task

    def _run(self, task_id: str) -> None:
        """A generic worker: claim, build sandbox + adapter FROM THE TASK, run,
        record a content-addressed result. The worker has no role of its own."""
        task = self._claim(task_id)
        if task is None:
            return  # already claimed/ran — idempotent, no double-run
        with self._lock:
            self._records[task_id].status = TaskStatus.RUNNING

        sandbox = self._sandbox_factory(task)
        try:
            adapter = self._adapter_factory(task.adapter)
            result = adapter.run(task.packet, sandbox)
        except Exception as exc:  # record the failure; never silently lose work
            with self._lock:
                self._records[task_id].status = TaskStatus.FAILED
                self._records[task_id].error = repr(exc)
            return
        finally:
            sandbox.destroy()  # arch §2(d): no reuse
        with self._lock:
            rec = self._records[task_id]
            rec.result = result
            rec.status = TaskStatus.DONE

    # -- read-back -----------------------------------------------------------

    def result(self, task_id: str) -> AdapterResult:
        """Block on the task's worker; return the recorded result. Re-reading a
        DONE task is a cache hit (idempotent — never re-runs)."""
        with self._lock:
            fut = self._futures.get(task_id)
            rec = self._records.get(task_id)
        if rec is None:
            raise KeyError(task_id)
        if fut is not None:
            fut.result()  # propagate worker-thread exceptions deterministically
        with self._lock:
            rec = self._records[task_id]
            if rec.status is TaskStatus.DONE and rec.result is not None:
                return rec.result
            if rec.status is TaskStatus.REJECTED:
                raise BudgetExceededError(rec.error or "rejected")
            raise RuntimeError(f"task {task_id} {rec.status}: {rec.error}")

    def status(self, task_id: str) -> TaskStatus:
        with self._lock:
            rec = self._records.get(task_id)
        if rec is None:
            raise KeyError(task_id)
        return rec.status

    def shutdown(self) -> None:
        """Drain and tear down the worker threads (idempotent-friendly)."""
        self._executor.shutdown(wait=True)

    def __enter__(self) -> LocalWorkPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
