r"""Implementation (Layer 3) role subgraph (T-M1-10).

Composes the Layer-3 node factories into an UNCOMPILED ``StateGraph(FactoryState)``
the supervisor mounts at the implementation phase boundary. The planner's
``ticket`` artifacts drive it:

    START -> engineering (one wave per pass, loops while a wave remains) -> END

with an optional per-wave integration check (``wave_gate``) between waves and a
``fixer`` that a build/test fix re-entry routes to once every ticket is settled.

A run with ZERO tickets has nothing to build: START routes straight to END and
the supervisor's gates fail on the empty tree. The planner's coverage check
requires tickets, so this is a fail-closed edge, not a supported path.

This builder holds NO agent logic — only topology (LEARNINGS AR3). It returns the
UNCOMPILED graph so the caller owns checkpointer/compile.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation import make_fixer_node, ready_tasks
from foundry.agents.implementation.tickets import (
    cap_stopped,
    has_tickets,
    make_engineering_node,
    pending_wave,
    tickets_settled,
)
from foundry.graph.gate_wiring import ADVANCE, FIX, HALT
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.execution.work_pool import TaskEventHook
    from foundry.graph.gate_wiring import BlobResolver, WaveBuildCheck

__all__ = ["build_implementation_subgraph"]


def build_implementation_subgraph(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver | None = None,
    snapshot_store: str | None = None,
    turn_budget: int = 25,
    cost_budget_microusd: int = 1_000_000,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
    workers: Mapping[str, int] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
    on_task: TaskEventHook | None = None,
    wave_check: WaveBuildCheck | None = None,
) -> StateGraph[FactoryState]:
    """Build the UNCOMPILED implementation (Layer 3) role subgraph.

    Parameters
    ----------
    rt:
        The shared :class:`AgentRuntime` threaded into every node factory; the
        engineering node requires ``rt.adapter`` (a ``WorkerAdapter``) set.
    source_dir:
        Base tree cloned into each per-task sandbox worktree (INFRA).
    resolve:
        INFRA dependency that reconstitutes typed bodies (tickets, contracts,
        criteria) from their content-addressed ``Artifact`` handles (CAS in prod,
        an in-memory map in tests). The DATA is read from ``state["artifacts"]``
        inside the node (LEARNINGS IM4).
    resolve_blob:
        The SAME diff-body seam the gates materialize with; the fixer rebuilds the
        integrated tree through it. Defaults to ``rt.cas.resolve_blob``.
    snapshot_store:
        DURABLE snapshot/blob store path so a retry's base snapshot survives the
        sandbox destroy/rebuild (LEARNINGS IM1).
    turn_budget / cost_budget_microusd:
        Per-task adapter budgets carried into each ``TaskPacket``.
    workers / ticket_tiers / on_task:
        Per-specialty concurrent ticket limits (under
        ``max_slots``), the difficulty -> model-tier table, and the per-ticket
        start/done/failed telemetry hook.
    wave_check:
        The per-wave integration check (§2b). Between waves (never
        after the last — the supervisor's build/test gate covers that) it runs the
        attested Build gate on the integrated tree; a FAIL routes to the fixer and
        back to the check, bounded by the shared fix-attempt budget, and a spent
        budget exits with waves unbuilt (the supervisor halts). Needs the fixer
        (``rt.cas``); ``None`` builds every wave back to back.

    START routes to the looping ``engineering`` node: each pass builds one wave of
    tickets and loops back while a wave remains, then exits to the supervisor's
    build/test gate. With no tickets START goes straight to END.

    Returns the ``StateGraph`` UNCOMPILED so the caller owns compilation
    (checkpointer, etc.).
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # A re-entry from the build/test fix loop repairs the integrated tree in ONE
    # session instead of rebuilding every ticket.
    fixer_id = "fixer"
    blobs = resolve_blob or (rt.cas.resolve_blob if rt.cas is not None else None)
    can_fix = rt.cas is not None and blobs is not None
    # Per-wave integration check (§2b): only with a fixer to route a FAIL to.
    gate_id = "wave_gate"
    check = wave_check if can_fix else None
    if can_fix:
        assert blobs is not None
        fixer = make_fixer_node(
            rt, source_dir=source_dir, resolve=resolve, resolve_blob=blobs,
            snapshot_store=snapshot_store,
            turn_budget=turn_budget, cost_budget_microusd=cost_budget_microusd,
        )

        def _fixer(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
            return fixer(state, config)

        g.add_node(fixer_id, _fixer)
        if check is None:
            g.add_edge(fixer_id, END)
        else:
            # A mid-wave repair is re-checked before the next wave; the final
            # build/test fix loop has no wave left and exits as before.
            def _after_fix(state: FactoryState) -> str:
                return gate_id if pending_wave(state, resolve) else END

            g.add_conditional_edges(fixer_id, _after_fix, {gate_id: gate_id, END: END})

    # One looping node, one wave per pass (a checkpoint per wave).
    eng_id = "engineering"
    engineering = make_engineering_node(
        rt, source_dir=source_dir, resolve=resolve, resolve_blob=blobs,
        snapshot_store=snapshot_store, turn_budget=turn_budget,
        cost_budget_microusd=cost_budget_microusd, max_slots=max_slots,
        cost_cap_microusd=cost_cap_microusd, workers=workers,
        ticket_tiers=ticket_tiers, on_task=on_task,
    )

    def _engineering(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return engineering(state, config)

    g.add_node(eng_id, _engineering)

    if check is not None:
        def _wave_gate(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
            assert check is not None
            return check.run(state, config)

        def _after_check(state: FactoryState, config: RunnableConfig) -> str:
            assert check is not None
            return {ADVANCE: eng_id, FIX: fixer_id, HALT: END}[check.route(state, config)]

        g.add_node(gate_id, _wave_gate)
        g.add_conditional_edges(gate_id, _after_check,
                                {eng_id: eng_id, fixer_id: fixer_id, END: END})

    def _after_wave(state: FactoryState) -> str:
        # A spent token cap exits so the supervisor commits this wave, then stops.
        if cap_stopped(state) or not pending_wave(state, resolve):
            return END
        return gate_id if check is not None else eng_id

    g.add_conditional_edges(eng_id, _after_wave,
                            {eng_id: eng_id, END: END,
                             **({gate_id: gate_id} if check is not None else {})})

    def _start(state: FactoryState) -> str:
        if not has_tickets(state):
            return END
        if (can_fix and state.get("phase") == "build_test_fix"
                and tickets_settled(state, resolve)):
            return fixer_id
        # The node also emits the ticket tasks, so it runs even with no wave.
        return eng_id if pending_wave(state, resolve) or not ready_tasks(state) else END

    g.add_conditional_edges(START, _start,
                            {eng_id: eng_id,
                             **({fixer_id: fixer_id} if can_fix else {}), END: END})
    return g
