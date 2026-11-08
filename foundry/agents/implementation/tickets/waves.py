"""Wave scheduling: dependency-ready tickets minus write-scope collisions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents.implementation.tickets.constants import _FEATURE_DIFF_KIND
from foundry.agents.implementation.tickets.tasks import (
    _skipped_task_ids,
    built_task_ids,
    read_tickets,
    task_id_for_ticket,
)
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import Ticket

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def next_wave(
    tickets: Sequence[Ticket], done: Iterable[str], skipped: Iterable[str] = ()
) -> tuple[Ticket, ...]:
    """The tickets to build next: not done/skipped, every known dependency done, and
    no write-scope overlap with an earlier (by order) ticket of the same wave. An
    empty ``files`` (unrestricted scope) overlaps everything, so it runs alone."""
    known = {t.id for t in tickets}
    done, skipped = set(done), set(skipped)
    wave: list[Ticket] = []
    for t in tickets:
        tid = task_id_for_ticket(t.id)
        if tid in done or tid in skipped:
            continue
        if any(task_id_for_ticket(d) not in done for d in t.depends_on if d in known):
            continue
        if any(_scopes_overlap(w.files, t.files) for w in wave):
            continue
        wave.append(t)
    return tuple(wave)


def _scopes_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    if not a or not b:
        return True
    return any(
        x == y or (x.endswith("/") and y.startswith(x)) or (y.endswith("/") and x.startswith(y))
        for x in a
        for y in b
    )


def pending_wave(state: FactoryState, resolve: ArtifactResolver) -> tuple[Ticket, ...]:
    """The wave the engineering node would build now (empty ⇒ nothing left to run)."""
    return next_wave(read_tickets(state, resolve), built_task_ids(state), _skipped_task_ids(state))


def tickets_settled(state: FactoryState, resolve: ArtifactResolver) -> bool:
    """No ticket is left to build (the rest are built, skipped, or blocked behind a
    skipped one) and at least one was built — the fix loop may repair the
    integrated tree instead of building tickets."""
    return bool(built_task_ids(state)) and not pending_wave(state, resolve)


def plan_wave_count(tickets: Sequence[Ticket]) -> int:
    """How many waves the plan takes when every ticket builds."""
    done: set[str] = set()
    waves = 0
    while wave := next_wave(tickets, done):
        done.update(task_id_for_ticket(t.id) for t in wave)
        waves += 1
    return waves


def _order_after_integrated(state: FactoryState, merged: dict[str, list[Any]]) -> None:
    """Sort this wave's diffs after every diff already integrated.

    Integration applies diffs in Seq order, but a ticket diff's own Seq does not
    follow build order: the fixer lifts its epoch past every diff it repairs (a
    per-wave fix lands BEFORE the next wave), and a resumed pass restarts the
    superstep index. A later wave builds on the integrated tree and may rewrite a
    file an earlier diff wrote, so its diff must apply last or its edit is lost."""
    top = max(
        (
            a.seq.emission_epoch
            for a in (state.get("artifacts") or ())
            if a.kind == _FEATURE_DIFF_KIND
        ),
        default=0,
    )
    if top and merged.get("artifacts"):
        merged["artifacts"] = [
            a.model_copy(update={"seq": a.seq._replace(emission_epoch=top + 1)})
            if a.kind == _FEATURE_DIFF_KIND and a.seq.emission_epoch <= top
            else a
            for a in merged["artifacts"]
        ]


def _round_robin(wave: Sequence[Ticket]) -> list[Ticket]:
    """Interleave specialties so a busy specialty's queued tickets don't hold every
    pool thread while another specialty has free slots."""
    by: dict[str, list[Ticket]] = {}
    for t in wave:
        by.setdefault(t.specialty.value, []).append(t)
    out: list[Ticket] = []
    while any(by.values()):
        for q in by.values():
            if q:
                out.append(q.pop(0))
    return out
