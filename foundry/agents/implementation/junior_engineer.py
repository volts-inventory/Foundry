"""Junior Engineer node factory (agents.md Layer 3 — Implementation).

The Junior Engineer is the *production* half of the implementation layer: it
takes the ready :class:`~foundry.schemas.implementation.Task`(s) the Senior
decomposed, runs the swappable coding substrate (``rt.adapter``, a
:class:`~foundry.execution.adapter_contract.WorkerAdapter`) inside a scoped
:class:`~foundry.execution.sandbox.Sandbox` per task, and emits the resulting
:class:`~foundry.schemas.implementation.FeatureDiff`(s) (content-addressed diff
handles on each ``task/<id>`` branch) to the ``artifacts`` channel.

State-reading convention (LEARNINGS AR1-AR4, IM4): the node's DATA inputs — the
Task(s) to build and the pinned constraints to inject — are read from
``state["tasks"]`` and ``state["pinned_constraints"]`` INSIDE the closure, NEVER
captured as build-time factory kwargs. Only the INFRA inputs (``source_dir``,
``snapshot_store``, the budgets) stay as factory args, exactly like ``rt.adapter``
— so the factory is composable: one ``rt`` + infra, the per-run work flows in on
the state channels exactly as the Senior emitted it. For Phase 1 the whole ready
task set is processed in one node invocation (branch-per-task is preserved by the
per-task ``task/<id>`` branch + per-task ``FeatureDiff``).

What this node is careful about (the contract, LEARNINGS L4/L5/L7, arch §2/§2b):

* **Pinned constraints VERBATIM + FIRST.** The architecture-pinned constraint
  texts are pulled from the ``pinned_constraints`` channel (the live read-time
  view), injected into the :class:`TaskPacket` byte-for-byte and ahead of the
  task body — never paraphrased, summarized, or reordered. ``assemble_prompt``
  then emits them first; the packet field documents the same invariant.
* **Idempotent retry = supersession, not edit (L4).** A re-run RESETS the
  worktree to the base tree first (``Sandbox.restore`` of the base snapshot),
  so the adapter always starts from the same base — a retried diff supersedes
  its predecessor rather than stacking on it. A task dict that already carries a
  recorded ``base_tree_sha`` (from a prior run's done-marker) is the reset point
  for the retry; a durable ``snapshot_store`` (IM1) is what makes that base
  survive the destroy/rebuild between runs. The diff/idempotency key is derived
  with ``content_digest`` (L7), never builtin ``hash()``, so the SAME inputs
  produce the SAME key across a kill-then-resume in a fresh process.
* **Lineage is proxy-observed, never self-reported (arch §2b).** The adapter's
  ``adapter_self_reported_model`` is IGNORED for lineage; nothing here reads it
  as provenance.
* **Agents never mint verdicts (L5).** The adapter returns advisory
  ``GateTriage`` only; this node forwards it to ``gate_triage`` and NEVER writes
  a ``GateResult`` to ``gates``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.architecture import PinnedConstraint
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.implementation import FeatureDiff, Task, TaskStatus
from foundry.state.reducers import live_constraints

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.execution.work_pool import ExecutionTask
    from foundry.state.factory_state import FactoryState

__all__ = [
    "execution_task_for",
    "idempotency_key",
    "make_junior_engineer_node",
    "pinned_texts",
    "ready_tasks",
]

NODE_ID = "junior_engineer"

_DEFAULT_TURN_BUDGET = 8
_DEFAULT_COST_BUDGET_MICROUSD = 1_000_000  # integer micro-USD, never floats


def pinned_texts(constraints: Sequence[PinnedConstraint]) -> tuple[str, ...]:
    """The constraint TEXTS to inject, in a deterministic order.

    Reads the live (non-superseded) view (``live_constraints``) so a retired
    constraint is not injected, then orders by ``(id)`` for a stable, fold-order-
    independent sequence — the packet must be byte-identical across a resume so
    its idempotency key is stable (L7)."""
    live = live_constraints(list(constraints))
    return tuple(c.text for c in sorted(live, key=lambda c: c.id))


def idempotency_key(task: Task, constraints: Sequence[PinnedConstraint], base_tree_sha: str) -> str:
    """Process-stable idempotency key for this task's diff (LEARNINGS L7).

    Derived with ``content_digest`` over the task identity + the verbatim
    pinned-constraint texts + the base tree — NEVER builtin ``hash()`` (seed-
    randomized per process; would defeat outbox dedup on resume). A re-run with
    identical inputs re-derives the identical key, so the keyed-union channel
    coalesces the supersession instead of duplicating it."""
    return content_digest(
        {
            "task_id": task.id,
            "branch": task.branch,
            "pinned_constraints": list(pinned_texts(constraints)),
            "base_tree_sha": base_tree_sha,
        }
    )


def ready_tasks(state: FactoryState) -> tuple[Task, ...]:
    """The Task(s) the Junior should build, read off ``state['tasks']`` (AR1).

    The ``tasks`` channel carries plain ``dict``s (keyed by id, ``union_by_id``);
    reconstitute the typed :class:`Task` from each and order by ``Seq`` so the
    build order is the Senior's deterministic merge order (recoverable from the
    records alone). A task re-presented carrying a recorded ``base_tree_sha`` is
    a RETRY: it is reset to that base first so its diff supersedes rather than
    stacks (idempotent retry, L4) — the build is otherwise idempotent (same
    inputs re-derive the same content-addressed diff key, L7)."""
    raw = state.get("tasks") or ()
    fields = set(Task.model_fields)
    out: list[Task] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # A done-marker carries extra keys (diff_uri / base_tree_sha) the frozen
        # Task model forbids; keep only the Task fields to reconstitute it.
        out.append(Task.model_validate({k: v for k, v in entry.items() if k in fields}))
    return tuple(sorted(out, key=lambda t: t.seq))


def _base_snapshot_of(task: Task, state: FactoryState) -> str | None:
    """The base tree to RESET to for a retry of ``task`` (L4), or None.

    A prior run's done-marker for this task carries the ``base_tree_sha`` it was
    built against; a retry restores the worktree to it first so the diff
    supersedes rather than stacks. Absent (first run), the freshly-cloned
    worktree IS the base."""
    for entry in state.get("tasks") or ():
        if isinstance(entry, dict) and entry.get("id") == task.id:
            base = entry.get("base_tree_sha")
            return str(base) if base else None
    return None


def _build_packet(
    task: Task,
    constraints: Sequence[PinnedConstraint],
    sandbox: Sandbox,
    *,
    turn_budget: int,
    cost_budget_microusd: int,
) -> TaskPacket:
    """Assemble the immutable packet: pinned constraints VERBATIM + FIRST.

    The constraint texts are placed in ``pinned_constraints`` byte-for-byte (no
    paraphrase, no reorder behind the task body); ``assemble_prompt`` renders
    them first. The proxy endpoint is intentionally NOT handed to the adapter
    here (egress stays None) — the StubAdapter holds no egress and the dev
    sandbox's network denial is "nothing to talk to"."""
    return TaskPacket(
        task={
            "id": task.id,
            "title": task.id,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
        },
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        worktree_path=str(sandbox.worktree),
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )


def execution_task_for(
    task: Task,
    constraints: Sequence[PinnedConstraint],
    *,
    adapter: str,
    worktree_path: str,
    lineage: str = "default",
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
) -> ExecutionTask:
    """OPT-IN seam (additive — NOT on the default path): build the durable
    :class:`~foundry.execution.work_pool.ExecutionTask` that would dispatch THIS
    task's adapter run through a :class:`~foundry.execution.work_pool.WorkPool`
    instead of running it in-process here.

    The default :func:`make_junior_engineer_node` path is unchanged — it still runs
    the adapter directly in a per-task :class:`Sandbox`. This helper exists only so
    a future implementation subgraph can opt into pooled dispatch (logical fan-out
    in the orchestrator; physical slots in infra; plan §4 [15]) WITHOUT the app ever
    spawning a container: it emits a content-addressed task record (a generic worker
    builds its own sandbox + adapter from it) and meters spend by ``lineage`` /
    ``cost_budget_microusd`` against the pool's :class:`QuotaPolicy`.

    The packet is byte-identical to the in-process path (pinned constraints VERBATIM
    + FIRST), so the pool's content-addressed ``task_id`` is stable across a resume.
    """
    from foundry.execution.work_pool import ExecutionTask

    packet = TaskPacket(
        task={
            "id": task.id,
            "title": task.id,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
        },
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        worktree_path=worktree_path,
        turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd,
        proxy_endpoint=None,
    )
    return ExecutionTask(
        packet=packet,
        adapter=adapter,
        lineage=lineage,
        est_cost_microusd=cost_budget_microusd,
    )


def _run_one_task(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    base_snapshot: str | None,
    config: RunnableConfig,
    *,
    source_dir: str,
    snapshot_store: str | None,
    turn_budget: int,
    cost_budget_microusd: int,
) -> dict[str, Any]:
    """Run the adapter for ONE task in a fresh sandbox; return its channel writes.

    A FRESH ``Sandbox`` per task (arch §2(d): no reuse). Establishes / resets to
    the base tree FIRST (idempotent retry = supersession, L4), runs the adapter,
    then keys the diff by ``content_digest`` (L7) so a re-run re-derives the same
    key and the keyed-union channels coalesce the supersession."""
    assert rt.adapter is not None  # guaranteed by the factory's guard
    sandbox = Sandbox(source_dir, snapshot_store=snapshot_store)
    try:
        base = sandbox.snapshot() if base_snapshot is None else sandbox.restore(base_snapshot)
        packet = _build_packet(
            task,
            constraints,
            sandbox,
            turn_budget=turn_budget,
            cost_budget_microusd=cost_budget_microusd,
        )
        result: AdapterResult = rt.adapter.run(packet, sandbox)
        _mirror_diff_blob(rt, sandbox, result)  # while the sandbox is alive
    finally:
        sandbox.destroy()

    return _finalize_diff(rt, task, constraints, base, result, config)


def _mirror_diff_blob(rt: AgentRuntime, sandbox: Sandbox, result: AdapterResult) -> None:
    """Mirror the adapter's diff BLOB out of the per-task sandbox (destroyed after
    — arch §2(d): no reuse) into the run-scoped CAS so a store-backed resolve_blob
    reads it end to end. Both stores key by the RAW sha256, so the mirrored uri ==
    ``result.diff_uri`` (asserted). No-op when ``rt.cas`` is None (synthetic
    resolver). Content-addressed + atomic, so it is safe to call from a pool worker
    thread (the only shared-state touch the parallel path makes concurrently)."""
    if rt.cas is None:
        return
    mirrored = rt.cas.put_blob(sandbox.get_blob(result.diff_uri))
    assert mirrored == result.diff_uri, (
        f"mirrored diff uri {mirrored!r} != adapter uri {result.diff_uri!r}"
    )


def _finalize_diff(
    rt: AgentRuntime,
    task: Task,
    constraints: Sequence[PinnedConstraint],
    base: str,
    result: AdapterResult,
    config: RunnableConfig,
) -> dict[str, Any]:
    """The SEQUENTIAL post-processing for one task's adapter result: key the diff
    (``content_digest``, L7), mint the FeatureDiff + Artifact (``rt.seq``), persist
    the typed body, and emit the done-marker + advisory triage. Shared by the serial
    loop and the parallel pool path — ALWAYS called single-threaded from the node so
    ``rt.seq`` (a non-thread-safe epoch counter) and ``rt.cas`` writes never race."""
    key = idempotency_key(task, constraints, base)
    diff = FeatureDiff(
        diff_uri=result.diff_uri,  # cas:// handle from the adapter (DS9)
        task_id=task.id,
        branch=task.branch,
        base_tree_sha=base,
        seq=rt.seq(config, NODE_ID, f"diff:{key}"),
    )
    artifact = Artifact(
        kind="feature_diff",
        uri=f"cas://feature_diff/{key}",  # uri carries the idempotency key
        produced_by=NODE_ID,
        task_id=task.id,
        seq=rt.seq(config, NODE_ID, f"artifact:{key}"),
    )
    # Store the TYPED FeatureDiff body keyed by the handle's OWN uri (which
    # carries the idempotency key, not content_identity), so gate_wiring's
    # resolve(feature_diff_handle) == get_body(artifact.uri) reconstitutes it.
    if rt.cas is not None:
        rt.cas.put_body_at(artifact.uri, diff)
    writes: dict[str, Any] = {
        "artifacts": [artifact],
        "tasks": [_task_done_marker(task, diff)],
    }
    if result.triage:
        writes["gate_triage"] = list(result.triage)
    return writes


class _MirroringAdapter:
    """Wraps ``rt.adapter`` to mirror the diff blob into ``rt.cas`` WHILE the
    pool worker's sandbox is still alive (the pool destroys it immediately after
    ``run``). This is the ONLY shared-state write a pool worker makes — and it is
    content-addressed + atomic (``put_blob`` keys by sha256, ``os.replace``), so
    concurrent workers are safe. All ``rt.seq``/``rt.cas`` body writes stay in the
    node's SEQUENTIAL post-processing (``_finalize_diff``)."""

    def __init__(self, rt: AgentRuntime) -> None:
        assert rt.adapter is not None
        self._rt = rt
        self.supports_turn_snapshots = rt.adapter.supports_turn_snapshots

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        result = self._rt.adapter.run(packet, sandbox)  # type: ignore[union-attr]
        _mirror_diff_blob(self._rt, sandbox, result)
        return result


def _adapter_name(rt: AgentRuntime) -> str:
    """Stable substrate id for the ExecutionTask (only an identity component —
    the pool's adapter_factory returns the mirroring wrapper regardless)."""
    return str(getattr(rt.adapter, "name", type(rt.adapter).__name__))


def _run_tasks_pooled(
    rt: AgentRuntime,
    tasks: Sequence[Task],
    constraints: Sequence[PinnedConstraint],
    state: FactoryState,
    config: RunnableConfig,
    *,
    source_dir: str,
    snapshot_store: str | None,
    turn_budget: int,
    cost_budget_microusd: int,
    max_slots: int,
    cost_cap_microusd: int | None,
) -> dict[str, Any]:
    """Run the ready tasks' adapter work in PARALLEL through a bounded
    :class:`LocalWorkPool` (``max_slots`` physical slots), then post-process the
    results SEQUENTIALLY. Only the isolated adapter run (sandbox + adapter, no
    ``rt.seq``) executes concurrently; the diff blob is mirrored to CAS in-worker
    by :class:`_MirroringAdapter`, and the FeatureDiff/Artifact minting runs
    single-threaded in ready order — so the output is identical to the serial
    loop modulo execution order. Over-budget tasks are REJECTED at admit
    (fail-closed): they never run, produce no diff, and the downstream gate fails
    closed. ``lineage`` stays ``"default"`` here; the per-area budget tree is
    Wave D."""
    from foundry.execution.work_pool import (
        BudgetExceededError,
        LocalWorkPool,
        QuotaPolicy,
    )

    if not tasks:
        return {}

    # base per task: the recorded base (a retry) or the fresh-clone snapshot (first
    # run). All first-run tasks clone the SAME source_dir, and snapshot() is a
    # content-only tree digest, so one computed base is correct for them all.
    recorded: dict[str, str | None] = {t.id: _base_snapshot_of(t, state) for t in tasks}
    base_first: str | None = None

    def base_of(task: Task) -> str:
        nonlocal base_first
        if recorded[task.id] is not None:
            return str(recorded[task.id])
        if base_first is None:
            probe = Sandbox(source_dir, snapshot_store=snapshot_store)
            try:
                base_first = probe.snapshot()
            finally:
                probe.destroy()
        return base_first

    def make_sandbox(etask: ExecutionTask) -> Sandbox:
        sandbox = Sandbox(source_dir, snapshot_store=snapshot_store)
        prior = recorded.get(str(etask.packet.task.get("id")))
        if prior is not None:
            sandbox.restore(prior)  # retry: reset to base so the diff supersedes (L4)
        return sandbox

    cap = 0 if cost_cap_microusd is None else cost_cap_microusd  # 0 == unlimited
    quota = QuotaPolicy(ledger=BudgetLedger(cap_microusd=cap))
    adapter_name = _adapter_name(rt)
    merged: dict[str, Any] = {}

    with LocalWorkPool(
        sandbox_factory=make_sandbox,
        adapter_factory=lambda _name: _MirroringAdapter(rt),
        max_slots=max_slots,
        quota=quota,
    ) as pool:
        submitted: dict[str, str | None] = {}
        for task in tasks:
            etask = execution_task_for(
                task,
                constraints,
                adapter=adapter_name,
                worktree_path=task.branch,  # stable placeholder; adapters use sandbox.worktree
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
            )
            try:
                submitted[task.id] = pool.submit(etask)
            except BudgetExceededError:
                submitted[task.id] = None  # rejected at admit (fail-closed) — never run

        # Collect + finalize SEQUENTIALLY in ready order: rt.seq (a non-thread-safe
        # epoch counter) and rt.cas body writes stay single-threaded, so the seqs
        # and artifacts are byte-identical to the serial path. A pool.result that
        # FAILED propagates (parity with the serial loop's adapter exception).
        for task in tasks:
            tid = submitted[task.id]
            if tid is None:
                continue  # over budget -> no diff -> downstream gate fails closed
            result = pool.result(tid)
            writes = _finalize_diff(rt, task, constraints, base_of(task), result, config)
            for channel, records in writes.items():
                merged.setdefault(channel, []).extend(records)
    return merged


def make_junior_engineer_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
) -> NodeFn:
    """Build the Junior Engineer node.

    The DATA inputs (WHICH tasks, WHICH constraints) are read from
    ``state["tasks"]`` / ``state["pinned_constraints"]`` inside the closure
    (LEARNINGS AR1-AR4 / IM4) — never build-time kwargs. Only the INFRA inputs
    below stay as factory args.

    Parameters
    ----------
    source_dir:
        Base tree to clone into each per-task sandbox worktree.
    snapshot_store:
        DURABLE snapshot/blob store path (``Sandbox`` arg). A retry builds a
        FRESH sandbox (arch §2(d): no reuse), so the base snapshot from the first
        run only survives for the retry's ``restore`` when it was written to a
        store that outlives ``destroy()`` — pass the SAME path on the first run
        and the retry (LEARNINGS IM1). When None each run gets an ephemeral store
        (fine for a first run, where the freshly-cloned worktree IS the base).
    turn_budget / cost_budget_microusd:
        Per-task adapter budgets carried into the ``TaskPacket``.
    max_slots:
        IC-pool size — how many tasks build in PARALLEL (org_fanout_design.md,
        Wave C). ``1`` (default) keeps the original serial loop, byte-identical to
        before. ``>1`` dispatches the per-task adapter runs through a bounded
        :class:`~foundry.execution.work_pool.LocalWorkPool`; the physical worker
        count is this knob (infra owns it — nothing here spawns a container).
    cost_cap_microusd:
        Global micro-USD cap the pool admits against (fail-closed; only used when
        ``max_slots > 1``). ``None`` (default) = unlimited (every task admits).
        Wave D replaces this single cap with the per-area/per-domain budget tree.
    """
    if rt.adapter is None:
        raise ValueError(
            "junior_engineer requires rt.adapter (a WorkerAdapter) — the "
            "implementation layer runs the coding substrate (LEARNINGS AR1)"
        )

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        constraints: Sequence[PinnedConstraint] = state.get("pinned_constraints") or ()
        tasks = ready_tasks(state)
        # Branch-per-task: each task builds in its own fresh sandbox on its own
        # task/<id> branch. With max_slots>1 the adapter runs fan out across the
        # IC pool (Wave C); otherwise they run serially (the original path).
        if max_slots > 1:
            return _run_tasks_pooled(
                rt,
                tasks,
                constraints,
                state,
                config,
                source_dir=source_dir,
                snapshot_store=snapshot_store,
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
                max_slots=max_slots,
                cost_cap_microusd=cost_cap_microusd,
            )
        merged: dict[str, Any] = {}
        for task in tasks:
            writes = _run_one_task(
                rt,
                task,
                constraints,
                _base_snapshot_of(task, state),
                config,
                source_dir=source_dir,
                snapshot_store=snapshot_store,
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
            )
            for channel, records in writes.items():
                merged.setdefault(channel, []).extend(records)
        return merged

    return _node


def _task_done_marker(task: Task, diff: FeatureDiff) -> dict[str, Any]:
    """The task channel entry, updated with the produced diff handle.

    Keyed by ``id`` (the tasks reducer is a keyed union); within one fold the
    Senior's plain Task dict wins (IM3), but the marker carries the produced diff
    handle + base tree so a downstream review / retry can read them off state."""
    d = task.model_dump(mode="json")
    d["status"] = TaskStatus.DONE.value
    d["diff_uri"] = diff.diff_uri
    d["base_tree_sha"] = diff.base_tree_sha
    return d
