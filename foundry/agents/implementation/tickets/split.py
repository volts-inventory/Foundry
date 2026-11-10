"""Ticket splits (ADaPT, arXiv 2311.05772): decompose a ticket only where it failed.

A plan may stay coarse: a ticket is split only once its build failed after its
retry (:data:`MAX_TICKET_ATTEMPTS`), or a fix-loop check kept failing on it after
its restart and, in hybrid, its escalation. Then, if it owns two or more scenarios
or work files, a :data:`SPLIT_REQUEST_TRIAGE` note asks the planner, once per
ticket, to split it into 2-3 tickets along its scenarios and files. A ticket whose
failure a dependency owns is not split: the dependency's failure is the one to
answer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents.implementation.tickets.constants import (
    PLANNER_NODE_ID,
    SPLIT_CARRY_TRIAGE,
    SPLIT_DECLINED_TRIAGE,
    SPLIT_REQUEST_TRIAGE,
    SPLIT_TRIAGE,
)
from foundry.agents.implementation.tickets.tasks import (
    read_tickets,
    replan_task_ids,
    task_id_for_ticket,
    unbuilt_task_ids,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import FixPass, GateResult, GateTriage
from foundry.schemas.implementation import CONTRACTS_TICKET_ID, SKELETON_TASK_ID, Ticket
from foundry.telemetry import count

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.graph.gate_wiring.restarts import FailureCluster
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def splittable(ticket: Ticket) -> bool:
    """A ticket there is something to split along: two or more scenarios or work
    files (:func:`foundry.agents.planner.sizing.work_files`), and not one every
    other ticket builds against."""
    from foundry.agents.planner.sizing import work_files

    if ticket.id in (CONTRACTS_TICKET_ID, SKELETON_TASK_ID):
        return False
    return len(ticket.scenario_ids) >= 2 or len(work_files(ticket.files)) >= 2


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


def split_carries(state: FactoryState) -> dict[str, str]:
    """Child task id -> the snapshot blob of its split parent's last attempt."""
    return {
        tid: t.explanation
        for tid, t in _notes(state, SPLIT_CARRY_TRIAGE).items()
        if t.produced_by == PLANNER_NODE_ID
    }


def _upstream(ticket: Ticket, by_id: Mapping[str, Ticket]) -> set[str]:
    seen: set[str] = set()
    todo = list(ticket.depends_on)
    while todo:
        d = todo.pop()
        if d in seen or d not in by_id:
            continue
        seen.add(d)
        todo.extend(by_id[d].depends_on)
    return {task_id_for_ticket(d) for d in seen}


def failing_owners(
    state: FactoryState, resolve: ArtifactResolver, gates: Iterable[GateResult]
) -> set[str]:
    """The tickets the failure clusters of ``gates`` pin a failure on, plus the
    unbuilt ones (their scenarios fail unexecuted)."""
    from foundry.graph.gate_wiring.restarts import cluster_owners, failure_clusters

    clusters = failure_clusters(gates, read_tickets(state, resolve), cluster_owners(state))
    return {t for c in clusters for t in c.tickets} | unbuilt_task_ids(state)


def may_split(
    state: FactoryState,
    resolve: ArtifactResolver,
    tid: str,
    failing: set[str],
    *,
    asked: Iterable[str] = (),
) -> bool:
    """True when ``tid`` is a planned, splittable ticket no split was asked for yet
    (nor left to an interface re-plan), none of whose dependencies (transitively)
    is among the ``failing`` owners: a failure a dependency owns is that
    dependency's to answer."""
    by_id = {t.id: t for t in read_tickets(state, resolve)}
    ticket = next((t for t in by_id.values() if task_id_for_ticket(t.id) == tid), None)
    if ticket is None or not splittable(ticket):
        return False
    if tid in split_requested(state) | replan_task_ids(state) | set(asked):
        return False
    return not _upstream(ticket, by_id) & failing


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
) -> tuple[str, ...]:
    """The tickets of the ``target`` cluster a fix-loop check asks to split: each
    already spent its restart (its retry) and, when a local tier could escalate,
    its escalation too, and a repair of the cluster after the restart (``passes``:
    the loop's window, in order) did not improve the tree either."""
    from foundry.agents.implementation.junior_engineer import ready_tasks
    from foundry.agents.implementation.tickets.escalation import (
        _escalation_settled,
        _is_local,
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
    restarted = restarted_task_ids(state)
    escalate = escalation_target(rt) is not None
    settled = _escalation_settled(state) if escalate else set()
    tiers = {t.id: t.tier for t in ready_tasks(state)}
    failing = {t for c in clusters for t in c.tickets} | unbuilt_task_ids(state)
    return tuple(
        tid
        for tid in cluster.tickets
        if tid in restarted
        and (not escalate or not _is_local(rt, tiers.get(tid)) or tid in settled)
        and may_split(state, resolve, tid, failing)
    )
