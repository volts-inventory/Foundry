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

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.agents.implementation.personas import persona_for
from foundry.agents.implementation.senior_engineer import (
    DESIGN_KIND,
    OWNERSHIP_KIND,
    task_id_for_unit,
)
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.architecture import (
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
    lane_of_area,
)
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff, Task, TaskStatus
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.reducers import live_constraints

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.execution.work_pool import ExecutionTask
    from foundry.schemas.journal import JournalEntry
    from foundry.state.factory_state import FactoryState

    # Reconstitutes a typed body from its content-addressed handle (the same INFRA
    # seam the Senior takes; CAS in prod, an in-memory map in tests).
    ArtifactResolver = Callable[[Artifact], Any]

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
    worktree IS the base.

    KNOWN LIMITATION (LEARNINGS IM3 / T-M1-10): in the WIRED subgraph the `tasks`
    reducer (`union_by_id`) is FIRST-keyed-wins for dicts, so the Senior's plain
    Task survives and the Junior's done-marker is dropped — this read therefore
    returns None there and the reset-to-base never fires. The retry still re-derives
    deterministically (content-addressed `idempotency_key`); true supersession would
    require minting a fresh task id/version, NOT relying on the reducer's later-wins.
    The reset path is exercised when a done-marker is presented directly (the durable
    `snapshot_store` retry test), not via the reduced channel."""
    for entry in state.get("tasks") or ():
        if isinstance(entry, dict) and entry.get("id") == task.id:
            base = entry.get("base_tree_sha")
            return str(base) if base else None
    return None


def _read_ownership(state: FactoryState, resolve: ArtifactResolver) -> OwnershipMap | None:
    """Reconstitute the OwnershipMap off ``state['artifacts']`` (newest by Seq), or
    None if absent / wrong type. READ-ONLY — the Junior derives the per-area budget
    lineage from it (Wave D); it never mutates ownership."""
    handles = [a for a in (state.get("artifacts") or ()) if a.kind == OWNERSHIP_KIND]
    if not handles:
        return None
    body = resolve(max(handles, key=lambda a: a.seq))
    return body if isinstance(body, OwnershipMap) else None


def _area_by_task_id(ownership: OwnershipMap) -> dict[str, str]:
    """task_id -> canonical AREA. A pure function of the OwnershipMap (Wave A
    ``area_of``): it recomputes each unit's task id (``task_id_for_unit``) so it
    joins to the Tasks the Senior emitted WITHOUT storing area on the Task
    (preserves the Task's content identity). Used for org telemetry (Wave E)."""
    return {
        task_id_for_unit(unit): ownership.area_of(owner)
        for unit, owner in ownership.owners.items()
    }


def _domain_by_task_id(ownership: OwnershipMap) -> dict[str, str]:
    """task_id -> DOMAIN (owner) — the per-domain budget lineage (Wave D). Same
    content-identity-preserving join as :func:`_area_by_task_id`, keyed to the
    owner (the Tech-Lead's domain) so each domain meters against its own quota."""
    return {task_id_for_unit(unit): owner for unit, owner in ownership.owners.items()}


def _persona_by_task(state: FactoryState, resolve: ArtifactResolver | None) -> dict[str, str]:
    """task_id -> area-expert persona text. Derived from the OwnershipMap's per-task
    AREA (the same join the org telemetry uses), so a frontend task is written by a
    frontend specialist, etc. Empty when no resolver/ownership is wired — leaving the
    prompt byte-identical to the pre-persona path."""
    if resolve is None:
        return {}
    ownership = _read_ownership(state, resolve)
    if ownership is None:
        return {}
    return {tid: persona_for(area) for tid, area in _area_by_task_id(ownership).items()}


# Kind literal for AcceptanceCriterion handles (matches gate_wiring.CRITERION_KIND;
# duplicated here so the implementation layer never imports the graph layer).
_CRITERION_KIND = "acceptance_criterion"

# Per-span character cap: context enrichment must never blow the prompt up (the
# product layer already truncates specs at 24k chars); a criterion/design summary
# that exceeds this is cut with an explicit marker rather than silently.
_SPAN_CHAR_CAP = 4_000


def _capped(text: str) -> str:
    return text if len(text) <= _SPAN_CHAR_CAP else text[: _SPAN_CHAR_CAP] + " …[truncated]"


def _retrieved_spans(
    state: FactoryState, resolve: ArtifactResolver | None, task: Task
) -> tuple[str, ...]:
    """Ground THIS task's codegen call in the spec context it traces to.
    (context-grounding: the coder must see the spec, not a handle.)

    Until now the packet's ``retrieved_spans`` was always empty and ``spec_uri``
    is a synthetic content-address (no stored body), so the coding substrate saw
    only the pinned constraints + a task id. This resolves, through the SAME typed
    ``resolve`` seam the ownership read uses:

    * the OWNERSHIP UNIT the task implements (recovered content-identity-
      preservingly via ``task_id_for_unit``, exactly like the budget lineage);
    * the ACCEPTANCE CRITERIA bodies for ``task.criterion_ids`` (sorted by id,
      newest-by-Seq per id, so the packet is byte-stable across folds/resume);
    * the SYSTEM DESIGN summary (newest by Seq): title, components, interfaces.

    Deterministic order + per-span cap keep the packet content-stable (the pool's
    content-addressed task id derives from it). ``resolve`` unwired ⇒ empty, the
    packet stays byte-identical to the pre-enrichment path.
    """
    if resolve is None:
        return ()
    spans: list[str] = []
    ownership_handles = [a for a in (state.get("artifacts") or ()) if a.kind == OWNERSHIP_KIND]
    if ownership_handles:
        body = resolve(max(ownership_handles, key=lambda a: a.seq))
        if isinstance(body, OwnershipMap):
            for unit, owner in sorted(body.owners.items()):
                if task_id_for_unit(unit) == task.id:
                    spans.append(
                        f"OWNERSHIP UNIT: this task implements {unit!r} "
                        f"(owner: {owner}, area: {body.area_of(owner)})"
                    )
                    break
    wanted = set(task.criterion_ids)
    if wanted:
        newest: dict[str, tuple[Any, AcceptanceCriterion]] = {}
        for handle in (a for a in (state.get("artifacts") or ()) if a.kind == _CRITERION_KIND):
            body = resolve(handle)
            if isinstance(body, AcceptanceCriterion) and body.id in wanted:
                prior = newest.get(body.id)
                if prior is None or handle.seq > prior[0]:
                    newest[body.id] = (handle.seq, body)
        for cid in sorted(newest):
            c = newest[cid][1]
            oracle = f" [oracle: {c.oracle_hint}]" if c.oracle_hint else ""
            spans.append(_capped(f"ACCEPTANCE CRITERION {c.id}: {c.text}{oracle}"))
    design_handles = [a for a in (state.get("artifacts") or ()) if a.kind == DESIGN_KIND]
    if design_handles:
        body = resolve(max(design_handles, key=lambda a: a.seq))
        if isinstance(body, SystemDesign):
            spans.append(
                _capped(
                    "SYSTEM DESIGN: "
                    + json.dumps(
                        {
                            "title": body.title,
                            "components": list(body.components),
                            "interfaces": list(body.interface_ids),
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                )
            )
    return tuple(spans)


def _prior_failures(state: FactoryState) -> tuple[str, ...]:
    """Findings from any gate that FAILED/ERRORed on the previous pass — fed back to
    the engineer (via the TaskPacket) so a fix attempt targets the SPECIFIC failure
    instead of re-rolling blind. Empty on the first pass (no gates yet)."""
    out: list[str] = []
    for g in state.get("gates") or ():
        if g.status in (GateStatus.FAIL, GateStatus.ERROR) and g.findings:
            out.append(f"[{g.name} gate FAILED]")
            out.extend(g.findings)
    return tuple(out)


def _build_packet(
    task: Task,
    constraints: Sequence[PinnedConstraint],
    sandbox: Sandbox,
    *,
    turn_budget: int,
    cost_budget_microusd: int,
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
) -> TaskPacket:
    """Assemble the immutable packet: pinned constraints VERBATIM + FIRST.

    The constraint texts are placed in ``pinned_constraints`` byte-for-byte (no
    paraphrase, no reorder behind the task body); ``assemble_prompt`` renders
    them first. ``retrieved_spans`` grounds the call in the criteria/design the
    task traces to (ROADMAP Phase 4). The proxy endpoint is intentionally NOT
    handed to the adapter here (egress stays None) — the StubAdapter holds no
    egress and the dev sandbox's network denial is "nothing to talk to"."""
    return TaskPacket(
        task={
            "id": task.id,
            "title": task.id,
            "spec_uri": task.spec_uri,
            "branch": task.branch,
            "criterion_ids": list(task.criterion_ids),
        },
        persona=persona,  # area-expert role preamble (empty == unchanged prompt)
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=retrieved_spans,  # spec grounding (empty == unchanged prompt)
        prior_failures=prior_failures,
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
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
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
        persona=persona,  # area-expert role preamble (empty == unchanged prompt)
        pinned_constraints=pinned_texts(constraints),  # VERBATIM + FIRST (arch §2)
        retrieved_spans=retrieved_spans,  # spec grounding (empty == unchanged prompt)
        prior_failures=prior_failures,
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


def _apply_overlay(worktree: Path, overlay: Mapping[str, str | None]) -> None:
    """Write predecessor slices onto a fresh clone (content ⇒ write, None ⇒
    delete). Paths are containment-checked defensively even though they come
    from our own CAS (same posture as the integration boundary, D-R1-03)."""
    root = worktree.resolve()
    for rel, content in sorted(overlay.items()):
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        target = (root / pure).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        if content is None:
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _slice_of(rt: AgentRuntime, writes: dict[str, Any]) -> dict[str, str | None]:
    """The tree slice ONE finished task contributes, for the next task's overlay.

    Reconstitutes the just-minted FeatureDiff body from ``rt.cas`` and parses it
    with the SAME op parser the integration boundary uses (``_diff_ops``), so
    sibling visibility and final integration agree. Modification-hunk patches are
    NOT projected into the overlay (they need the integrated file; the boundary
    still applies them — only sibling VISIBILITY of a patch lags). Empty when no
    CAS is wired (synthetic-resolver tests: overlay off, path unchanged)."""
    if rt.cas is None:
        return {}
    from foundry.graph.gate_wiring import FeatureDiffBodyError, _diff_ops

    artifacts = writes.get("artifacts") or []
    if not artifacts:
        return {}
    diff = rt.cas.resolve(artifacts[0])
    try:
        ops = _diff_ops(diff, rt.cas.resolve_blob)
    except FeatureDiffBodyError:
        # Overlay is best-effort VISIBILITY; the integration boundary stays the
        # authoritative (fail-closed) applier of this body.
        return {}
    out: dict[str, str | None] = dict(ops.writes)
    for rel in ops.deletes:
        out[rel] = None
    return out


def _sandbox_for(rt: AgentRuntime, source_dir: str, snapshot_store: str | None) -> Sandbox:
    """Build the per-task Sandbox honoring the adapter's DECLARED model-auth needs.

    An agentic CLI substrate (claude-code) declares ``credential_grant`` /
    ``home_passthrough`` on the adapter; the Sandbox validates the grant
    fail-closed against ``GRANTABLE_MODEL_AUTH`` (ROADMAP Phase 1b — model-auth
    only, target-system credentials can never be granted). Adapters that declare
    nothing (stub, raw-react) get the fully-scrubbed cage, byte-identical to
    before."""
    return Sandbox(
        source_dir,
        snapshot_store=snapshot_store,
        credential_grant=tuple(getattr(rt.adapter, "credential_grant", ()) or ()),
        home_passthrough=bool(getattr(rt.adapter, "home_passthrough", False)),
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
    prior_failures: tuple[str, ...] = (),
    persona: str = "",
    retrieved_spans: tuple[str, ...] = (),
    overlay: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Run the adapter for ONE task in a fresh sandbox; return its channel writes.

    A FRESH ``Sandbox`` per task (arch §2(d): no reuse). Establishes / resets to
    the base tree FIRST (idempotent retry = supersession, L4), runs the adapter,
    then keys the diff by ``content_digest`` (L7) so a re-run re-derives the same
    key and the keyed-union channels coalesce the supersession.

    ``overlay`` (ROADMAP Phase 2b — the shared evolving tree): the accumulated
    slices of EARLIER tasks in this node invocation (merge order), written onto
    the fresh clone BEFORE the base snapshot is taken, so this task builds ON the
    code its predecessors authored instead of blind against the scaffold. Because
    it precedes ``snapshot()``, the recorded base (and thus the idempotency key)
    reflects what the task actually saw — a retry resets to the SAME overlaid
    base, and a change in a predecessor's output supersedes correctly. A retry
    presenting a recorded ``base_snapshot`` skips the overlay (that base is
    already authoritative)."""
    assert rt.adapter is not None  # guaranteed by the factory's guard
    sandbox = _sandbox_for(rt, source_dir, snapshot_store)
    try:
        if base_snapshot is None:
            if overlay:
                _apply_overlay(sandbox.worktree, overlay)
            base = sandbox.snapshot()
        else:
            base = sandbox.restore(base_snapshot)
        packet = _build_packet(
            task,
            constraints,
            sandbox,
            turn_budget=turn_budget,
            cost_budget_microusd=cost_budget_microusd,
            prior_failures=prior_failures,
            persona=persona,
            retrieved_spans=retrieved_spans,
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
    resolver). This is the ONLY shared-state write a pool worker makes — and it is
    safe to call concurrently: ``put_blob`` is content-addressed and idempotent
    (in-memory store = a single GIL-atomic dict setitem; filesystem store =
    thread-unique temp + ``os.replace``), so two workers mirroring the same digest
    converge on identical bytes."""
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
    domain_by_task: Mapping[str, str],
    domain_area: Mapping[str, str],
    prior_failures: tuple[str, ...] = (),
    persona_by_task: Mapping[str, str] | None = None,
    spans_by_task: Mapping[str, tuple[str, ...]] | None = None,
    weight_tasks: Sequence[Task] | None = None,
) -> dict[str, Any]:
    """Run the ready tasks' adapter work in PARALLEL through a bounded
    :class:`LocalWorkPool` (``max_slots`` physical slots), then post-process the
    results SEQUENTIALLY. Only the isolated adapter run (sandbox + adapter, no
    ``rt.seq``) executes concurrently; the diff blob is mirrored to CAS in-worker
    by :class:`_MirroringAdapter`, and the FeatureDiff/Artifact minting runs
    single-threaded in ready order — so the output is identical to the serial
    loop modulo execution order.

    Budget tree (Wave D): each task's ``lineage`` is its DOMAIN (owner); the global
    cap flows down the two-level org tree — Program Manager -> per-AREA -> per-DOMAIN
    (``allocate_budget_tree``) — so a domain that exhausts ITS share is REJECTED at
    admit even when sibling domains in its area are idle and the global ledger has
    room (true per-domain isolation + the noisy-neighbor guard). A rejected task
    never runs, produces no diff, and the downstream gate fails closed. When no
    domain mapping is available the lineage falls back to ``"default"`` (a single
    bucket = Wave C behaviour).

    Admission is in ready (merge) order, so the admitted subset under a binding cap
    is a DETERMINISTIC function of that order — a resume re-derives the identical
    set (the pool's content-addressed task_id also coalesces any re-submit)."""
    from foundry.execution.work_pool import (
        BudgetExceededError,
        LocalWorkPool,
        QuotaPolicy,
        allocate_budget_tree,
    )

    if not tasks:
        return {}

    def lineage_of(task: Task) -> str:
        return domain_by_task.get(task.id, "default")

    # Per-domain task weights + each domain's area, so the global cap can flow down
    # the PM -> area -> domain tree; Σ(domain quotas) ≤ Σ(area quotas) ≤ global cap.
    # The weight spans the WHOLE ready set (``weight_tasks``), not just the tasks THIS
    # node builds — so a per-lane engineer node derives the SAME per-domain quotas it
    # would in one node (the cap is split across every area once), then meters only its
    # own lane's tasks against them. Defaults to ``tasks`` (single-node = unchanged).
    domain_weight: dict[str, int] = {}
    for task in (weight_tasks if weight_tasks is not None else tasks):
        domain_weight[lineage_of(task)] = domain_weight.get(lineage_of(task), 0) + 1
    global_cap = 0 if cost_cap_microusd is None else cost_cap_microusd
    lineage_quotas = allocate_budget_tree(domain_area, domain_weight, global_cap)

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
        sandbox = _sandbox_for(rt, source_dir, snapshot_store)
        prior = recorded.get(str(etask.packet.task.get("id")))
        if prior is not None:
            sandbox.restore(prior)  # retry: reset to base so the diff supersedes (L4)
        return sandbox

    quota = QuotaPolicy(
        ledger=BudgetLedger(cap_microusd=global_cap),  # 0 == unlimited global
        lineage_quota_microusd=lineage_quotas,  # per-area sub-envelopes (Wave D)
    )
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
                lineage=lineage_of(task),  # per-area budget bucket (Wave D)
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
                prior_failures=prior_failures,
                persona=(persona_by_task or {}).get(task.id, ""),  # area-expert voice
                retrieved_spans=(spans_by_task or {}).get(task.id, ()),  # spec grounding
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


def _engineer_note(
    rt: AgentRuntime,
    config: RunnableConfig,
    tasks: Sequence[Task],
    state: FactoryState,
    resolve: ArtifactResolver | None,
    lane: str | None,
) -> JournalEntry | None:
    """A prose "what this engineer built" journal entry for the event log / interview
    affordance: the distinct feature DOMAINS its tasks implement (the Tech-Lead's
    ownership decomposition names — e.g. "web-router, web-auth"), else just the task
    count. STRICTLY ADVISORY (plan [11]) — never read by a gate."""
    n = len(tasks)
    if n == 0:
        return None
    domains: list[str] = []
    if resolve is not None:
        own = _read_ownership(state, resolve)
        if own is not None:
            dbt = _domain_by_task_id(own)
            for t in tasks:
                d = dbt.get(t.id)
                if d and d not in domains:
                    domains.append(d)
    summary = f"Implemented {n} task{'s' if n != 1 else ''}"
    if domains:
        summary += ": " + ", ".join(domains[:4]) + (" …" if len(domains) > 4 else "")
    role = f"{lane.title()} Engineer" if lane else "Junior Engineer"
    return journal_entry(
        node_id=NODE_ID, role=role, seq=rt.seq(config, NODE_ID, "journal"),
        summary=summary,
    )


def make_junior_engineer_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
    resolve: ArtifactResolver | None = None,
    lane: str | None = None,
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
        The Program Manager's GLOBAL micro-USD envelope the pool admits against
        (fail-closed; only used when ``max_slots > 1``). ``None`` (default) =
        unlimited. When set, it is sub-allocated across the present AREAS as
        per-lineage quotas (Wave D), so one area cannot starve another.
    resolve:
        INFRA dependency (CAS in prod, an in-memory map in tests) used to
        reconstitute the OwnershipMap so each task's budget ``lineage`` is its
        AREA (Wave D). ``None`` keeps a single ``"default"`` lineage (Wave C
        behaviour). DATA (which ownership) is read from ``state`` inside the node.
    lane:
        Discipline LANE (``frontend``/``backend``/``data``) this node builds. When set,
        the node builds ONLY the tasks whose owning area folds into this lane
        (:func:`lane_of_area`), but derives the per-domain budget quotas from the WHOLE
        ready set (so the global cap is still split across every area exactly once — no
        lane gets the full cap). Task identity, seq and ``produced_by`` stay on the
        shared internal ``NODE_ID`` (unchanged content-addressing); the distinct per-lane
        TELEMETRY comes from the graph node NAME the subgraph registers. Each task is
        owned by one area in one lane, so no task is built twice. ``None`` (default) =
        build every ready task in one node (byte-identical legacy).
    """
    if rt.adapter is None:
        raise ValueError(
            "junior_engineer requires rt.adapter (a WorkerAdapter) — the "
            "implementation layer runs the coding substrate (LEARNINGS AR1)"
        )

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        constraints: Sequence[PinnedConstraint] = state.get("pinned_constraints") or ()
        all_tasks = ready_tasks(state)
        # Lane scoping: build only THIS lane's tasks, but keep the full ready set for
        # budget weighting (below). A task's lane = the lane its owning area folds into;
        # with no ownership wired (resolver absent) every task defaults to the backend
        # lane, so exactly one lane node builds it — never duplicated across lane nodes.
        if lane is not None:
            ownership = _read_ownership(state, resolve) if resolve is not None else None
            area_by_tid = _area_by_task_id(ownership) if ownership is not None else {}
            def _task_lane(tid: str) -> str:
                return lane_of_area(area_by_tid.get(tid, "backend"))
            tasks = [t for t in all_tasks if _task_lane(t.id) == lane]
        else:
            tasks = all_tasks
        # Advisory journal note: a prose "what this engineer built" line (the feature
        # domains its tasks implement) for the event log / interview affordance.
        jnote = _engineer_note(rt, config, tasks, state, resolve, lane)
        # Fix loop: on a re-entry after a failed gate, feed the SPECIFIC failure back
        # to the engineer (empty on the first pass) so it fixes that instead of re-rolling.
        prior_failures = _prior_failures(state)
        # SYSTEM-DECIDED parallelism. The Tech-Lead's decomposition IS the decision
        # to parallelize: it splits the work into independently-buildable tasks. We
        # size the IC pool to that decomposition — one worker per ready task —
        # capped by ``max_slots`` (a safety ceiling, not a user-tuned target). So a
        # single-task build runs serially with no pool overhead; an N-task build
        # fans N adapter runs across the pool automatically (≤ the cap). Nobody sets
        # a worker count: the system reads it off the lead's plan.
        effective_slots = min(len(tasks), max_slots) if tasks else 1
        # A lane node under a cost cap MUST meter even a lone task: the per-domain
        # quota/ledger live only on the pooled path, so a single-task lane on the serial
        # path would slip its budget (a data lane's one task escaping its starved
        # sub-cap). Force the pooled path for a capped lane build; the legacy single-node
        # run (lane is None) keeps the original serial/pooled selection untouched.
        force_pool = lane is not None and cost_cap_microusd is not None and bool(tasks)
        # Area-expert persona per task (option-2): a frontend task is written by a
        # frontend specialist, etc. Empty map when no ownership/resolver is wired.
        persona_by_task = _persona_by_task(state, resolve)
        # Spec grounding per task (ROADMAP Phase 4): the criteria/design/unit spans
        # this task traces to. Empty when no resolver is wired (unchanged prompt).
        spans_by_task = {t.id: _retrieved_spans(state, resolve, t) for t in tasks}
        # Branch-per-task: each task builds in its own fresh sandbox on its own
        # task/<id> branch. >1 task ⇒ the adapter runs fan out across the IC pool
        # (Wave C); a single task runs serially (the original path).
        if effective_slots > 1 or force_pool:
            # Per-domain budget lineage + each domain's area (Wave D), derived from
            # the OwnershipMap when a resolver is wired; else a single "default".
            domain_by_task: dict[str, str] = {}
            domain_area: dict[str, str] = {}
            if resolve is not None:
                ownership = _read_ownership(state, resolve)
                if ownership is not None:
                    domain_by_task = _domain_by_task_id(ownership)
                    domain_area = {owner: ownership.area_of(owner) for owner in ownership.leads()}
            pooled = _run_tasks_pooled(
                rt,
                tasks,
                constraints,
                state,
                config,
                source_dir=source_dir,
                snapshot_store=snapshot_store,
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
                max_slots=effective_slots,
                cost_cap_microusd=cost_cap_microusd,
                domain_by_task=domain_by_task,
                domain_area=domain_area,
                prior_failures=prior_failures,
                persona_by_task=persona_by_task,
                spans_by_task=spans_by_task,
                weight_tasks=all_tasks,  # per-domain quotas span EVERY lane, not just this one
            )
            if jnote is not None:
                pooled.setdefault("agent_journal", []).append(jnote)
            return pooled
        merged: dict[str, Any] = {}
        # The shared evolving tree (ROADMAP Phase 2b): each SERIAL task builds on
        # its predecessors' slices (merge order), later-by-Seq winning exactly as
        # at the integration boundary. The pooled path stays independent by design
        # (parallel siblings can't see each other mid-flight).
        overlay: dict[str, str | None] = {}
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
                prior_failures=prior_failures,
                persona=persona_by_task.get(task.id, ""),
                retrieved_spans=spans_by_task.get(task.id, ()),
                overlay=overlay,
            )
            overlay.update(_slice_of(rt, writes))
            for channel, records in writes.items():
                merged.setdefault(channel, []).extend(records)
        if jnote is not None:
            merged.setdefault("agent_journal", []).append(jnote)
        return merged

    return _node


def _task_done_marker(task: Task, diff: FeatureDiff) -> dict[str, Any]:
    """The task channel entry, updated with the produced diff handle.

    Keyed by ``id`` (the tasks reducer is a keyed union). NOTE (LEARNINGS IM3 /
    T-M1-10): in the wired subgraph the reducer is FIRST-keyed-wins for dicts, so the
    Senior's plain Task dict survives and THIS marker is dropped on the `tasks`
    channel — the produced `diff_uri`/`base_tree_sha` are read off the **`artifacts`**
    channel (the FeatureDiff), not off this done-marker. The extra keys here are still
    populated for the in-isolation node path (and any future fresh-id supersession);
    do NOT assume a downstream node reads them off the reduced `tasks` channel."""
    d = task.model_dump(mode="json")
    d["status"] = TaskStatus.DONE.value
    d["diff_uri"] = diff.diff_uri
    d["base_tree_sha"] = diff.base_tree_sha
    return d
