"""Junior Engineer node factory (agents.md Layer 3 — Implementation).

The Junior Engineer is the *production* half of the implementation layer: it
takes ONE :class:`~foundry.schemas.implementation.Task`, runs the swappable
coding substrate (``rt.adapter``, a
:class:`~foundry.execution.adapter_contract.WorkerAdapter`) inside a scoped
:class:`~foundry.execution.sandbox.Sandbox`, and emits the resulting
:class:`~foundry.schemas.implementation.FeatureDiff` (a content-addressed diff
handle on its ``task/<id>`` branch) to the ``artifacts`` channel.

What this node is careful about (the contract, LEARNINGS L4/L5/L7, arch §2/§2b):

* **Pinned constraints VERBATIM + FIRST.** The architecture-pinned constraint
  texts are pulled from the ``pinned_constraints`` channel (the live read-time
  view), injected into the :class:`TaskPacket` byte-for-byte and ahead of the
  task body — never paraphrased, summarized, or reordered. ``assemble_prompt``
  then emits them first; the packet field documents the same invariant.
* **Idempotent retry = supersession, not edit (L4).** A re-run RESETS the
  worktree to the base tree first (``Sandbox.restore`` of the base snapshot),
  so the adapter always starts from the same base — a retried diff supersedes
  its predecessor rather than stacking on it. The diff/idempotency key is
  derived with ``content_digest`` (L7), never builtin ``hash()``, so the SAME
  inputs produce the SAME key across a kill-then-resume in a fresh process.
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
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import FeatureDiff, Task
from foundry.state.reducers import live_constraints

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.state.factory_state import FactoryState

__all__ = ["idempotency_key", "make_junior_engineer_node", "pinned_texts"]

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


def make_junior_engineer_node(
    rt: AgentRuntime,
    *,
    task: Task,
    source_dir: str,
    base_snapshot: str | None = None,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
) -> NodeFn:
    """Build the Junior Engineer node for ONE task.

    Parameters
    ----------
    task:
        The single task to implement on its ``task/<id>`` branch.
    source_dir:
        Base tree to clone into the sandbox worktree.
    base_snapshot:
        Digest handle of the base tree to RESET to on a re-run (idempotent
        retry, L4). When None, the freshly-cloned worktree IS the base and a
        snapshot of it is taken as the reset point; on a re-run pass the
        recorded base digest so the worktree is restored to it before the
        adapter runs again — a retry supersedes, never stacks.
    snapshot_store:
        DURABLE snapshot/blob store path (``Sandbox`` arg). A re-run builds a
        FRESH sandbox (arch §2(d): no reuse), so the base snapshot from the
        first run only survives for the retry's ``restore`` when it was written
        to a store that outlives ``destroy()`` — pass the SAME path on the
        first run and the retry. When None each run gets an ephemeral store
        (fine when ``base_snapshot`` is None, i.e. the first run).
    """
    if rt.adapter is None:
        raise ValueError(
            "junior_engineer requires rt.adapter (a WorkerAdapter) — the "
            "implementation layer runs the coding substrate (LEARNINGS AR1)"
        )
    adapter = rt.adapter

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        constraints: Sequence[PinnedConstraint] = state.get("pinned_constraints") or ()
        sandbox = Sandbox(source_dir, snapshot_store=snapshot_store)
        try:
            # Establish / reset to the base tree FIRST so the adapter always
            # starts from the same base (idempotent retry = supersession, L4).
            base = sandbox.snapshot() if base_snapshot is None else sandbox.restore(base_snapshot)

            packet = _build_packet(
                task,
                constraints,
                sandbox,
                turn_budget=turn_budget,
                cost_budget_microusd=cost_budget_microusd,
            )
            result: AdapterResult = adapter.run(packet, sandbox)
        finally:
            sandbox.destroy()

        # Idempotency key + diff identity keyed by content_digest (L7), stable
        # across resume; a re-run with the same inputs re-derives the same key.
        key = idempotency_key(task, constraints, base)
        seq = rt.seq(config, NODE_ID, f"diff:{key}")
        diff = FeatureDiff(
            diff_uri=result.diff_uri,  # cas:// handle from the adapter (DS9)
            task_id=task.id,
            branch=task.branch,
            base_tree_sha=base,
            seq=seq,
        )
        # FeatureDiff travels on `artifacts` as a content-addressed handle; the
        # uri carries the idempotency key so the keyed-union coalesces a retry.
        artifact = Artifact(
            kind="feature_diff",
            uri=f"cas://feature_diff/{key}",
            produced_by=NODE_ID,
            task_id=task.id,
            seq=rt.seq(config, NODE_ID, f"artifact:{key}"),
        )
        writes: dict[str, Any] = {
            "artifacts": [artifact],
            # carry the FeatureDiff record where downstream review can read it;
            # the body (diff bytes) stays out-of-band behind result.diff_uri.
            "tasks": [_task_done_marker(task, diff)],
        }
        # Forward adapter triage as ADVISORY notes only (L5) — never a verdict.
        if result.triage:
            writes["gate_triage"] = list(result.triage)
        return writes

    return _node


def _task_done_marker(task: Task, diff: FeatureDiff) -> dict[str, Any]:
    """The task channel entry, updated with the produced diff handle.

    Keyed by ``id`` (the tasks reducer is a keyed union); later-by-Seq wins, so
    a retried diff's marker supersedes the prior one for the same task."""
    d = task.model_dump(mode="json")
    d["diff_uri"] = diff.diff_uri
    d["base_tree_sha"] = diff.base_tree_sha
    return d
