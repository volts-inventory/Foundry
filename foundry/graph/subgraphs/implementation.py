r"""Implementation (Layer 3) role subgraph (T-M1-10).

Composes the Layer-3 agent node factories into an UNCOMPILED
``StateGraph(FactoryState)`` the supervisor mounts at the implementation phase
boundary. Topology (agents.md Layer 3 SOP) — org fan-out lifted to the graph, one
LEAD (decompose) + ENGINEER (build) node per discipline lane:

    START -> {fe,be,da}_lead (decompose) -> {fe,be,da}_engineer (build) -> END

The Senior Engineer (T1) reads the SystemDesign + OwnershipMap off
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

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents.implementation import (
    make_junior_engineer_node,
    make_senior_engineer_node,
)
from foundry.schemas.architecture import ENGINEERING_LANES
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver

__all__ = ["build_implementation_subgraph"]

# The concrete LangGraph node signature (same shape as NodeFn).
_Node = Callable[[FactoryState, RunnableConfig], dict[str, Any]]


def build_implementation_subgraph(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    snapshot_store: str | None = None,
    tier: str = "T1",
    turn_budget: int = 8,
    cost_budget_microusd: int = 1_000_000,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
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
    snapshot_store:
        DURABLE snapshot/blob store path so a retry's base snapshot survives the
        sandbox destroy/rebuild (LEARNINGS IM1).
    tier:
        Model tier for the Senior's decomposition call (T1 default).
    turn_budget / cost_budget_microusd:
        Per-task adapter budgets the Junior carries into each ``TaskPacket``.

    Returns the ``StateGraph`` UNCOMPILED so the caller owns compilation
    (checkpointer, etc.), mirroring ``skeleton.build_skeleton_graph``'s split.
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
    # node factories). Empty lanes (no owner) simply write nothing.
    lead_ids: list[str] = []
    eng_ids: list[str] = []
    for _lane in ENGINEERING_LANES:
        lead = make_senior_engineer_node(rt, resolve=resolve, tier=tier, lane=_lane)
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

    # START -> (fe/be/da lead) -> (fe/be/da engineer) -> END, in a single deterministic
    # chain: leads decompose in lane order, then engineers build in lane order.
    chain = [START, *lead_ids, *eng_ids, END]
    for _a, _b in zip(chain, chain[1:]):
        g.add_edge(_a, _b)

    return g
