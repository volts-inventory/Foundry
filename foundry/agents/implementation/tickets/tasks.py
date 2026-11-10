"""Tickets -> tasks, and the built / skipped / cap-stopped readers over them."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from foundry.agents.implementation.tickets.constants import (
    _FEATURE_DIFF_KIND,
    CAP_TRIAGE,
    INTERFACE_DECLINED_TRIAGE,
    INTERFACE_REVISED_TRIAGE,
    NODE_ID,
    PLANNER_NODE_ID,
    REPLAN_TRIAGE,
    SIGNATURE_REJECTED_TRIAGE,
    SKIP_TRIAGE,
    TICKET_KIND,
)
from foundry.canonical import content_digest
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import DEFAULT_TICKET_TIERS, Difficulty, Task, Ticket
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def has_tickets(state: FactoryState) -> bool:
    """True when the planner emitted tickets (else the ownership-unit path runs)."""
    return any(a.kind == TICKET_KIND for a in (state.get("artifacts") or ()))


def read_tickets(state: FactoryState, resolve: ArtifactResolver) -> tuple[Ticket, ...]:
    """The live tickets (newest by Seq per id), in Seq order — the scheduling order.
    A newest record whose ``supersedes`` names its own id is a tombstone: a re-plan
    dropped that ticket."""
    newest: dict[str, Ticket] = {}
    for handle in (a for a in (state.get("artifacts") or ()) if a.kind == TICKET_KIND):
        body = resolve(handle)
        if not isinstance(body, Ticket):
            continue
        prior = newest.get(body.id)
        if prior is None or body.seq > prior.seq:
            newest[body.id] = body
    live = (t for t in newest.values() if t.supersedes != t.id)
    return tuple(sorted(live, key=lambda t: (t.seq, t.id)))


def task_id_for_ticket(ticket_id: str) -> str:
    """A ticket's task id is its own id: stable across re-plans of the same ticket."""
    return ticket_id


def tier_for(difficulty: Difficulty | str, ticket_tiers: Mapping[Any, str] | None) -> str:
    """Model tier for a difficulty: the run's table, else :data:`DEFAULT_TICKET_TIERS`."""
    d = Difficulty(difficulty)
    table = {Difficulty(k): v for k, v in (ticket_tiers or {}).items()}
    return table.get(d) or DEFAULT_TICKET_TIERS[d]


def task_from_ticket(
    ticket: Ticket,
    seq: Seq,
    *,
    ticket_tiers: Mapping[Any, str] | None = None,
    known: Iterable[str] = (),
) -> Task:
    """The deterministic Task for ``ticket``. Dependencies on tickets outside
    ``known`` are dropped (the planner validates the DAG; this only guards)."""
    tid = task_id_for_ticket(ticket.id)
    known_ids = set(known)
    return Task(
        id=tid,
        spec_uri=f"cas://task/{content_digest(ticket.model_dump(mode='json', exclude={'seq'}))}",
        criterion_ids=ticket.scenario_ids,
        branch=f"task/{tid}",
        ticket_id=ticket.id,
        specialty=ticket.specialty,
        tier=tier_for(ticket.difficulty, ticket_tiers),
        depends_on=tuple(task_id_for_ticket(d) for d in ticket.depends_on if d in known_ids),
        seq=seq,
    )


def built_task_ids(state: FactoryState) -> set[str]:
    """Task ids with a diff of their own; a fix-loop rebuild (``restart-<id>``) also
    counts as ``<id>``, so a skipped ticket rebuilt that way unblocks its dependents."""
    from foundry.graph.gate_wiring import RESTART_PREFIX

    out: set[str] = set()
    for a in state.get("artifacts") or ():
        if a.kind == _FEATURE_DIFF_KIND and a.task_id:
            out.add(a.task_id)
            if a.task_id.startswith(RESTART_PREFIX):
                out.add(a.task_id.removeprefix(RESTART_PREFIX))
    return out


def _skipped_task_ids(state: FactoryState) -> set[str]:
    return set(_ticket_notes(state, SKIP_TRIAGE))


def unbuilt_task_ids(state: FactoryState) -> set[str]:
    """Skipped tickets with no build: every attempt failed and no fix-loop rebuild
    (``restart-<id>``) has landed."""
    return _skipped_task_ids(state) - built_task_ids(state)


def signature_rejections(state: FactoryState) -> dict[str, int]:
    """Per task id, how many of its attempts the stub-signature guard rejected."""
    return {
        tid: len(notes) for tid, notes in _ticket_notes(state, SIGNATURE_REJECTED_TRIAGE).items()
    }


def replan_task_ids(state: FactoryState) -> set[str]:
    """Tickets left to a re-plan: never rebuilt again (until the planner revises
    their interface)."""
    return set(_ticket_notes(state, REPLAN_TRIAGE))


def interface_revisions(state: FactoryState) -> dict[str, int]:
    """Per task id, the emission epoch of the planner's revision of its interface."""
    return {
        t.run_against.removeprefix("task/"): t.seq.emission_epoch
        for t in state.get("gate_triage") or ()
        if t.name == INTERFACE_REVISED_TRIAGE and t.produced_by == PLANNER_NODE_ID
    }


def pending_interface_replans(state: FactoryState) -> tuple[str, ...]:
    """Tickets left to a re-plan the planner has not answered yet (it answers each
    once per run)."""
    answered = {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name in (INTERFACE_REVISED_TRIAGE, INTERFACE_DECLINED_TRIAGE)
        and t.produced_by == PLANNER_NODE_ID
    }
    return tuple(sorted(replan_task_ids(state) - answered))


def _ticket_notes(state: FactoryState, prefix: str) -> dict[str, list[GateTriage]]:
    """This node's notes named ``prefix``* per task id, less those an interface
    revision of the ticket superseded (the planner mints it above every earlier
    epoch: :func:`interface_revisions`)."""
    revised = interface_revisions(state)
    out: dict[str, list[GateTriage]] = {}
    for t in state.get("gate_triage") or ():
        if t.produced_by == NODE_ID and t.name.startswith(prefix):
            tid = t.run_against.removeprefix("task/")
            if t.seq.emission_epoch > revised.get(tid, -1):
                out.setdefault(tid, []).append(t)
    return out


def cap_stopped(state: FactoryState) -> tuple[GateTriage, ...]:
    """The token-cap notes of tickets still unbuilt and unskipped: non-empty means the
    last pass stopped on a spent cap and the run must stop. A resumed pass that builds
    those tickets clears it."""
    settled = built_task_ids(state) | _skipped_task_ids(state)
    return tuple(
        note
        for tid, notes in sorted(_ticket_notes(state, CAP_TRIAGE).items())
        if tid not in settled
        for note in notes
    )
