r"""Implementation (Layer 3) role subgraph (T-M1-10).

Composes the Layer-3 agent node factories into an UNCOMPILED
``StateGraph(FactoryState)`` the supervisor mounts at the implementation phase
boundary. Topology (agents.md Layer 3 SOP) — org fan-out lifted to the graph, one
LEAD (decompose) + ENGINEER (build) node per discipline lane:

    START -> {fe,be,da}_lead (decompose) -> {fe,be,da}_engineer (build) -> END

Each hop is CONDITIONAL on the next lane having work: a lane with no Tech-Lead (and
so no task) is skipped, not run as a no-op — an empty lane executed for free but
still reported itself as a working agent. The routers key on the same
``leads_for_lane`` / ``tasks_for_lane`` predicates the nodes partition with, so the
edge and the node can never disagree (the architecture subgraph's AC2 pattern).

The Senior Engineer (no model call) reads the SystemDesign + OwnershipMap off
``state["artifacts"]`` (by ``Artifact.kind``, reconstituted through the injected
``resolve``) and decomposes them into a deterministic, ownership-ordered
``Task[]`` on the ``tasks`` channel (one ``task/<id>`` branch each). The Junior
Engineer then reads that ready task set back off ``state["tasks"]`` and the
pinned constraints off ``state["pinned_constraints"]``, runs the swappable
substrate (``rt.adapter``) per task in its own fresh sandbox, and emits a
content-addressed :class:`FeatureDiff` per branch to ``artifacts`` (branch-per-
task preserved). The integration / conflict-resolution step is typed but stubbed
(``stub_conflict_resolution`` lives with the Senior); it is not yet a graph node.

State-reading convention (LEARNINGS AR1-AR4 / IM4): both nodes take their DATA
inputs from the state channels the upstream layer emitted — never build-time
kwargs. Only INFRA flows in as factory args: ``rt`` (the shared runtime,
including ``rt.adapter``), the ``resolve`` callable the Senior reconstitutes the
design/ownership bodies with, and the sandbox ``source_dir`` / ``snapshot_store``
/ budgets the Junior runs against. This keeps the subgraph composable — the same
builder serves any run; the per-run work flows in on the channels.

This builder holds NO agent logic — only topology (LEARNINGS AR3). It returns the
UNCOMPILED graph so the caller owns checkpointer/compile (mirrors ``skeleton.py``
and the architecture/product subgraphs).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation import (
    lanes_with_leads,
    make_fixer_node,
    make_junior_engineer_node,
    make_senior_engineer_node,
    ready_tasks,
    tasks_for_lane,
)
from foundry.agents.implementation.tickets import (
    cap_stopped,
    has_tickets,
    make_engineering_node,
    pending_wave,
    tickets_settled,
)
from foundry.graph.gate_wiring import ADVANCE, FEATURE_DIFF_KIND, FIX, HALT, _of_kind
from foundry.schemas.architecture import ENGINEERING_LANES
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.execution.work_pool import TaskEventHook
    from foundry.graph.gate_wiring import BlobResolver, WaveBuildCheck

__all__ = ["build_implementation_subgraph"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]


def build_implementation_subgraph(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver | None = None,
    snapshot_store: str | None = None,
    turn_budget: int = 8,
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
        Junior requires ``rt.adapter`` (a ``WorkerAdapter``) set.
    source_dir:
        Base tree cloned into each per-task sandbox worktree (INFRA).
    resolve:
        INFRA dependency the Senior reconstitutes the SystemDesign / OwnershipMap
        bodies from their content-addressed ``Artifact`` handles with (CAS in
        prod, an in-memory map in tests). The DATA (WHICH design/ownership) is
        read from ``state["artifacts"]`` inside the node (LEARNINGS IM4).
    resolve_blob:
        The SAME diff-body seam the gates materialize with; the fixer rebuilds the
        integrated tree through it. Defaults to ``rt.cas.resolve_blob``.
    snapshot_store:
        DURABLE snapshot/blob store path so a retry's base snapshot survives the
        sandbox destroy/rebuild (LEARNINGS IM1).
    turn_budget / cost_budget_microusd:
        Per-task adapter budgets the Junior carries into each ``TaskPacket``.
    workers / ticket_tiers / on_task:
        Ticket path only: per-specialty concurrent ticket limits (under
        ``max_slots``), the difficulty -> model-tier table, and the per-ticket
        start/done/failed telemetry hook.
    wave_check:
        Ticket path only: the per-wave integration check (§2b). Between waves (never
        after the last — the supervisor's build/test gate covers that) it runs the
        attested Build gate on the integrated tree; a FAIL routes to the fixer and
        back to the check, bounded by the shared fix-attempt budget, and a spent
        budget exits with waves unbuilt (the supervisor halts). Needs the fixer
        (``rt.cas``); ``None`` builds every wave back to back.

    When the planner emitted ``ticket`` artifacts, START routes to the looping
    ``engineering`` node instead of the lane chain: each pass builds one wave of
    tickets and loops back while a wave remains, then exits to the supervisor's
    build/test gate. With no tickets the lane chain below runs unchanged.

    Returns the ``StateGraph`` UNCOMPILED so the caller owns compilation
    (checkpointer, etc.).
    """
    g: StateGraph[FactoryState] = StateGraph(FactoryState)

    # The factory closures are typed as the ``_Node`` alias; LangGraph's
    # ``add_node`` resolves its node argument against the ``_NodeWithConfig``
    # Protocol, and mypy cannot bind that Protocol's contravariant ``NodeInputT``
    # TypeVar through an opaque ``Callable`` alias (only through a named, concrete
    # ``def``). ``_wrap`` returns a concrete ``def`` per node to restore structural
    # matching WITHOUT a private import or ``cast`` (mirrors the architecture subgraph).
    def _wrap(fn: _Node) -> _Node:
        def _n(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
            return fn(state, config)
        return _n

    # Org fan-out lifted to the GRAPH: one LEAD node (decompose) + one ENGINEER node
    # (build) per discipline LANE (frontend/backend/data). The leads run first (canonical
    # order), then the engineers — same "decompose all, then build all" phasing as the
    # single-node Senior->Junior, but now each lane is a DISTINCT node so the telemetry
    # (and the office) can tell FE-lead / BE-engineer / … apart. Every owner/task folds
    # into exactly one lane (``lane_of_area``: infra+security -> backend), so nothing is
    # decomposed or built twice, and task identity/seq/merge are unchanged (see the two
    # node factories).
    lead_ids: list[str] = []
    eng_ids: list[str] = []
    for _lane in ENGINEERING_LANES:
        lead = make_senior_engineer_node(rt, resolve=resolve, lane=_lane)
        eng = make_junior_engineer_node(
            rt,
            source_dir=source_dir,
            snapshot_store=snapshot_store,
            turn_budget=turn_budget,
            cost_budget_microusd=cost_budget_microusd,
            max_slots=max_slots,
            cost_cap_microusd=cost_cap_microusd,
            resolve=resolve,  # per-area budget lineage from the OwnershipMap (Wave D)
            lane=_lane,
        )
        lead_id, eng_id = f"{_lane}_lead", f"{_lane}_engineer"
        g.add_node(lead_id, _wrap(lead))
        g.add_node(eng_id, _wrap(eng))
        lead_ids.append(lead_id)
        eng_ids.append(eng_id)

    # START -> (fe/be/da lead) -> (fe/be/da engineer) -> END: leads decompose in lane
    # order, then engineers build in lane order — but a lane with NO work is skipped
    # rather than run as a no-op. An empty lane cost no model call either way, yet it
    # still executed, and so still reported itself as a working agent: a "Frontend Lead"
    # decomposing on a data-only service, both in the quest log and at a desk on the
    # floor. The chain is therefore conditional, each hop routing to the NEXT lane with
    # work (else END).
    #
    # The routers key on the SAME helpers the nodes partition with — `leads_for_lane` /
    # `tasks_for_lane` — so the edge and the node can never disagree and no lane can be
    # skipped out of work it would have done (the architecture subgraph's AC2 pattern).
    # An unreadable OwnershipMap is UNKNOWN, not empty: the node runs and raises, exactly
    # as before. Order and lane membership are unchanged, so task ids, seqs and the
    # merge order are byte-identical to the unconditional chain.
    chain = [*lead_ids, *eng_ids]
    lane_of_node = {f"{ln}_lead": ln for ln in ENGINEERING_LANES}
    lane_of_node.update({f"{ln}_engineer": ln for ln in ENGINEERING_LANES})

    def _has_work(node_id: str, state: FactoryState) -> bool:
        lane = lane_of_node[node_id]
        if node_id.endswith("_lead"):
            lanes = lanes_with_leads(state, resolve)
            return lanes is None or lane in lanes
        return bool(tasks_for_lane(state, resolve, lane))

    def _next_with_work(after: int) -> Callable[[FactoryState], str]:
        def _route(state: FactoryState) -> str:
            for node_id in chain[after + 1:]:
                if _has_work(node_id, state):
                    return node_id
            return END
        return _route

    # The path map's keys are the legal router return values (the reachable node ids +
    # END); add_conditional_edges drives branch-target validation and the rendered
    # topology off it.
    # A re-entry from the build/test fix loop repairs the integrated tree in ONE
    # session instead of re-decomposing and rebuilding every task.
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
    first_lane = _next_with_work(-1)

    # Ticket path: one looping node, one wave per pass (a checkpoint per wave).
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
        if has_tickets(state):
            if (can_fix and state.get("phase") == "build_test_fix"
                    and tickets_settled(state, resolve)):
                return fixer_id
            # The node also emits the ticket tasks, so it runs even with no wave.
            return eng_id if pending_wave(state, resolve) or not ready_tasks(state) else END
        if can_fix and state.get("phase") == "build_test_fix":
            built = {a.task_id for a in _of_kind(state, FEATURE_DIFF_KIND)}
            # A task with no diff yet still needs its own build, not a repair pass.
            if built and all(t.id in built for t in ready_tasks(state)):
                return fixer_id
        return first_lane(state)

    g.add_conditional_edges(START, _start,
                            {**{n: n for n in chain}, eng_id: eng_id,
                             **({fixer_id: fixer_id} if can_fix else {}), END: END})
    for _i, _node_id in enumerate(chain):
        g.add_conditional_edges(_node_id, _next_with_work(_i),
                                {**{n: n for n in chain[_i + 1:]}, END: END})

    return g
