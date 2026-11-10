"""Ticket splits (ADaPT, arXiv 2311.05772): decompose a ticket only where it failed.

A plan may stay coarse: a ticket is split only once its build failed after its
retry (:data:`MAX_TICKET_ATTEMPTS`), or a fix-loop check kept failing on it after
its restart, and, in hybrid, after its escalation where one is still open to it.
Then, if it owns two or more scenarios or files, a :data:`SPLIT_REQUEST_TRIAGE`
note asks the planner, once per ticket, to split it into 2-3 tickets along its
scenarios and files. A ticket whose failure a dependency owns is not split: the
dependency's failure is the one to answer. A child of a split is never split again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from foundry.agents.implementation.tickets.constants import (
    CARRY_TRIAGE,
    FAIL_TRIAGE,
    NODE_ID,
    PLANNER_NODE_ID,
    SPLIT_CHILD_TRIAGE,
    SPLIT_DECLINED_TRIAGE,
    SPLIT_REQUEST_TRIAGE,
    SPLIT_TRIAGE,
)
from foundry.agents.implementation.tickets.tasks import (
    _skipped_task_ids,
    _ticket_notes,
    built_task_ids,
    read_tickets,
    replan_task_ids,
    task_id_for_ticket,
    unbuilt_task_ids,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import FixPass, GateTriage
from foundry.schemas.implementation import CONTRACTS_TICKET_ID, SKELETON_TASK_ID, Ticket
from foundry.telemetry import count

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.graph.gate_wiring.restarts import FailureCluster
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def splittable(ticket: Ticket) -> bool:
    """A ticket there is something to split along: two or more scenarios or files
    of its own (:func:`foundry.agents.planner.sizing.ticket_files`: work files plus
    templates and assets, so one module and many templates qualifies; a child must
    own at least one file), and not one every other ticket builds against."""
    from foundry.agents.planner.sizing import ticket_files

    if ticket.id in (CONTRACTS_TICKET_ID, SKELETON_TASK_ID):
        return False
    return len(ticket.scenario_ids) >= 2 or len(ticket_files(ticket.files)) >= 2


def _notes(state: FactoryState, *names: str) -> dict[str, GateTriage]:
    return {
        t.run_against.removeprefix("task/"): t
        for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
        if t.name in names
    }


def split_requested(state: FactoryState) -> set[str]:
    """Tickets a split was asked for (answered or not)."""
    return set(_notes(state, SPLIT_REQUEST_TRIAGE))


def split_answered(state: FactoryState) -> set[str]:
    """Tickets the planner answered a split request of: split, or declined."""
    return {
        tid
        for tid, t in _notes(state, SPLIT_TRIAGE, SPLIT_DECLINED_TRIAGE).items()
        if t.produced_by == PLANNER_NODE_ID
    }


def pending_splits(state: FactoryState) -> tuple[str, ...]:
    """Tickets a split was asked for that the planner has not answered yet."""
    return tuple(sorted(split_requested(state) - split_answered(state)))


def _children(state: FactoryState) -> dict[str, str]:
    return {
        tid: t.explanation
        for tid, t in _notes(state, SPLIT_CHILD_TRIAGE).items()
        if t.produced_by == PLANNER_NODE_ID
    }


def split_children(state: FactoryState) -> set[str]:
    """Task ids of tickets a split made."""
    return set(_children(state))


def split_carries(state: FactoryState) -> dict[str, str]:
    """Child task id -> the snapshot blob of its split parent's failed attempt, for
    the children that start from one."""
    return {tid: uri for tid, uri in _children(state).items() if uri}


def withdraw_splits(out: dict[str, Any]) -> None:
    """Drop the split requests a check put on ``out``: its loop does not go on (a
    spent budget, a stop), so no pass is left to hand the split to."""
    if "gate_triage" in out:
        out["gate_triage"] = [t for t in out["gate_triage"] if t.name != SPLIT_REQUEST_TRIAGE]


def parent_carries(state: FactoryState, tids: Iterable[str]) -> dict[str, str]:
    """Split parent task id -> the snapshot blob of its failed attempt its children
    start from. Only a build failure's split (the engineering node's request)
    carries one, and only while that attempt is the parent's newest and the parent
    never integrated: a fix-loop split's children start from the integrated tree."""
    requests = _notes(state, SPLIT_REQUEST_TRIAGE)
    fails = _ticket_notes(state, FAIL_TRIAGE)
    carries = _ticket_notes(state, CARRY_TRIAGE)
    built = built_task_ids(state)
    out: dict[str, str] = {}
    for tid in tids:
        request = requests.get(tid)
        note = max(carries.get(tid, ()), key=lambda t: t.seq, default=None)
        last = max(fails.get(tid, ()), key=lambda t: t.seq, default=None)
        if (
            request is not None
            and request.produced_by == NODE_ID
            and tid not in built
            and note is not None
            and last is not None
            and not last.seq > note.seq  # the same pass mints both
        ):
            out[tid] = note.explanation
    return out


@dataclass(frozen=True)
class SplitView:
    """What :func:`may_split` reads, gathered once per check (:func:`split_view`)."""

    by_tid: Mapping[str, Ticket]
    deps: dict[str, tuple[str, ...]]
    closed: frozenset[str]  # asked before, left to an interface re-plan, or a split's child


def split_view(state: FactoryState, resolve: ArtifactResolver) -> SplitView:
    tickets = read_tickets(state, resolve)
    return SplitView(
        by_tid={task_id_for_ticket(t.id): t for t in tickets},
        deps={t.id: tuple(t.depends_on) for t in tickets},
        closed=frozenset(split_requested(state) | replan_task_ids(state) | split_children(state)),
    )


def may_split(view: SplitView, tid: str, failing: set[str], *, asked: Iterable[str] = ()) -> bool:
    """True when ``tid`` is a planned, splittable ticket no split was asked for yet
    (nor left to an interface re-plan, nor made by a split), none of whose
    dependencies (transitively) is among the ``failing`` owners: a failure a
    dependency owns is that dependency's to answer."""
    from foundry.agents.planner.folds import _ancestors

    ticket = view.by_tid.get(tid)
    if ticket is None or not splittable(ticket) or tid in view.closed or tid in set(asked):
        return False
    return not {task_id_for_ticket(d) for d in _ancestors(view.deps, ticket.id)} & failing


def split_request(
    rt: AgentRuntime, config: RunnableConfig, node_id: str, tid: str, why: str
) -> GateTriage:
    """The note asking the planner to split ``tid``."""
    count("planner.split.requested")
    return GateTriage(
        name=SPLIT_REQUEST_TRIAGE,
        run_against=f"task/{tid}",
        explanation=why[:2000],
        produced_by=node_id,
        seq=rt.seq(config, node_id, f"{SPLIT_REQUEST_TRIAGE}:{tid}"),
    )


def split_after_restart(
    state: FactoryState,
    resolve: ArtifactResolver,
    rt: AgentRuntime,
    clusters: Sequence[FailureCluster],
    target: str | None,
    passes: Sequence[FixPass],
    *,
    escalates: bool = False,
) -> tuple[str, ...]:
    """The tickets of the ``target`` cluster a fix-loop check asks to split: each
    already spent its restart (its retry) and a repair of the cluster after the
    restart (``passes``: the loop's window, in order) did not improve the tree
    either. ``escalates``: the check can escalate (a wave check; the final gates
    never do), so a local ticket its escalation could still pick (one of the newest
    built wave, not escalated or declined yet) escalates first."""
    from foundry.agents.implementation.junior_engineer import ready_tasks
    from foundry.agents.implementation.tickets.escalation import (
        _escalation_settled,
        _is_local,
        _latest_built_wave,
        escalation_target,
    )
    from foundry.graph.gate_wiring.restarts import restarted_task_ids

    cluster = next((c for c in clusters if c.key == target), None)
    if cluster is None or not cluster.tickets:
        return ()
    # The pass whose check planned the restart: a restart that never landed is spent too.
    since = max(
        (i for i, p in enumerate(passes) if set(p.restart_tickets) & set(cluster.tickets)),
        default=-1,
    )
    if not any(
        p.cluster == target and (p.fix_task or "").startswith("fix-") and p.outcome != "improved"
        for p in passes[since + 1 :]
    ):
        return ()
    view = split_view(state, resolve)
    escalating: set[str] = set()
    if escalates and escalation_target(rt) is not None:
        tiers = {t.id: t.tier for t in ready_tasks(state)}
        latest = _latest_built_wave(
            list(view.by_tid.values()), built_task_ids(state), _skipped_task_ids(state)
        )
        escalating = {
            tid for t in latest if _is_local(rt, tiers.get(tid := task_id_for_ticket(t.id)))
        } - _escalation_settled(state)
    restarted = restarted_task_ids(state)
    failing = {t for c in clusters for t in c.tickets} | unbuilt_task_ids(state)
    return tuple(
        tid
        for tid in cluster.tickets
        if tid in restarted and tid not in escalating and may_split(view, tid, failing)
    )
