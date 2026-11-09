"""Escalation (hybrid): a local ticket re-runs once on the cloud level."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.agents.implementation.junior_engineer import _failed_gates
from foundry.agents.implementation.tickets.constants import NODE_ID
from foundry.agents.implementation.tickets.rules import _BDD_FINDING
from foundry.agents.implementation.tickets.tasks import (
    _skipped_task_ids,
    built_task_ids,
    read_tickets,
    task_id_for_ticket,
    tier_for,
)
from foundry.agents.implementation.tickets.waves import next_wave
from foundry.execution.adapter_contract import TaskPacket
from foundry.execution.adapters.tiered import (
    DIGEST_CAP,
    ESCALATE_DECLINED_TRIAGE,
    ESCALATE_FALLBACK_TRIAGE,
    ESCALATE_REQUEST_TRIAGE,
    ESCALATED_TRIAGE,
    STOPPED_DIGEST,
    STOPPED_TRIAGE,
)
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import Ticket

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


def escalation_target(rt: AgentRuntime) -> str | None:
    """The cloud tier a local ticket escalates to, or None (cloud and local modes)."""
    target = getattr(rt.adapter, "escalation_tier", None)
    return str(target) if target else None


def _is_local(rt: AgentRuntime, tier: str | None) -> bool:
    is_local = getattr(rt.adapter, "is_local", None)
    return bool(callable(is_local) and is_local(tier))


def escalated_task_ids(state: FactoryState) -> set[str]:
    """Tickets that already spent their one escalation this run (whatever its outcome)."""
    return {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name == ESCALATED_TRIAGE
    }


def pending_escalations(state: FactoryState, resolve: ArtifactResolver) -> tuple[str, ...]:
    """Task ids of planned tickets a wave check asked to re-run on the cloud level,
    not yet re-run."""
    asked = {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name == ESCALATE_REQUEST_TRIAGE
    }
    planned = {task_id_for_ticket(t.id) for t in read_tickets(state, resolve)}
    return tuple(sorted((asked & planned) - _escalation_settled(state)))


def stopped_tickets(state: FactoryState) -> dict[str, str]:
    """Task id -> why its kept local session stopped early (turn cap, repeat or
    read-thrash detector), for tickets whose stopped attempt was integrated."""
    return {
        t.run_against.removeprefix("task/"): t.explanation
        for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
        if t.name == STOPPED_TRIAGE and t.produced_by == NODE_ID
    }


def _escalation_settled(state: FactoryState) -> set[str]:
    """Tickets whose escalation is spent, or whose request the quota declined."""
    declined = {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name == ESCALATE_DECLINED_TRIAGE
    }
    return escalated_task_ids(state) | declined


def _latest_built_wave(
    tickets: Sequence[Ticket], built: set[str], skipped: set[str]
) -> list[Ticket]:
    """The built tickets of the newest wave, replaying :func:`next_wave` in build order."""
    done: set[str] = set()
    last: list[Ticket] = []
    while True:
        part = [t for t in next_wave(tickets, done, skipped) if task_id_for_ticket(t.id) in built]
        if not part:
            return last
        last = part
        done.update(task_id_for_ticket(t.id) for t in part)


def escalation_candidates(
    state: FactoryState,
    resolve: ArtifactResolver,
    rt: AgentRuntime,
    ticket_tiers: Mapping[Any, str] | None = None,
    *,
    stopped_only: bool = False,
) -> tuple[str, ...]:
    """The task ids :func:`escalation_pick` chooses."""
    return escalation_pick(state, resolve, rt, ticket_tiers, stopped_only=stopped_only)[0]


def escalation_pick(
    state: FactoryState,
    resolve: ArtifactResolver,
    rt: AgentRuntime,
    ticket_tiers: Mapping[Any, str] | None = None,
    *,
    stopped_only: bool = False,
) -> tuple[tuple[str, ...], bool]:
    """The local-tier tickets of the newest wave to re-run on the cloud level after
    the wave check failed twice, and whether the whole-wave fallback chose them:
    those the failing gates name (a failing scenario or one of the ticket's files).
    With none named, every unsettled local ticket of that wave, but only if the
    fallback has not run on that wave yet (:data:`ESCALATE_FALLBACK_TRIAGE`; named
    and stopped-early escalations don't count). Settled tickets
    (:func:`_escalation_settled`) never are again.
    ``ticket_tiers`` is the engineering node's difficulty -> tier table.

    ``stopped_only`` (the check's FIRST failure) picks only tickets whose kept
    session stopped early (:func:`stopped_tickets`): those the failures name, or
    the stopped ticket of a single-ticket wave whatever the failures say."""
    if escalation_target(rt) is None:
        return (), False
    tickets = read_tickets(state, resolve)
    settled = _escalation_settled(state)
    latest = _latest_built_wave(tickets, built_task_ids(state), _skipped_task_ids(state))
    wave = [t for t in latest if _is_local(rt, tier_for(t.difficulty, ticket_tiers))]
    fresh = [t for t in wave if task_id_for_ticket(t.id) not in settled]
    if stopped_only:
        stops = stopped_tickets(state)
        fresh = [t for t in fresh if task_id_for_ticket(t.id) in stops]
    findings = [line for g in _failed_gates(state) for line in g.findings]
    scenarios = {m.group(1) for line in findings if (m := _BDD_FINDING.match(line))}
    tokens = {tok for line in findings for tok in _PATH_TOKEN.findall(line)}

    def names_file(f: str) -> bool:
        if f.endswith("/"):
            return any(tok.startswith(f) or f"/{f}" in tok for tok in tokens)
        return any(tok == f or tok.endswith(f"/{f}") for tok in tokens)

    named = [
        t for t in fresh if scenarios & set(t.scenario_ids) or any(names_file(f) for f in t.files)
    ]
    fell_back = {
        t.run_against.removeprefix("task/")
        for t in state.get("gate_triage") or ()
        if t.name == ESCALATE_FALLBACK_TRIAGE
    }
    fallback = False
    if stopped_only:
        chosen = named or (fresh if len(latest) == 1 else [])
    elif named:
        chosen = named
    elif any(task_id_for_ticket(t.id) in fell_back for t in wave):
        chosen = []
    else:
        chosen, fallback = fresh, bool(fresh)
    return tuple(task_id_for_ticket(t.id) for t in chosen), fallback


# A path-like token in a gate finding: "app.py" in "app.py:3: error" or in
# "tests/test_app.py::test_x" is a whole token; "webapp.py" never matches "app.py".
_PATH_TOKEN = re.compile(r"[\w./-]+")


def _escalation_context(
    *,
    tier: str | None,
    target: str,
    reason: str,
    touched: str,
    reported: Iterable[str],
    kept: bool,
    failures: Sequence[str],
) -> tuple[str, ...]:
    """The head of an escalated attempt's input: why the cheaper level stopped, what
    it touched and reported, then the failing gates (the fixer's context)."""
    tree = (
        "the working directory holds that attempt and every fix since"
        if kept
        else "that attempt's writes were discarded; you start from the integrated tree"
    )
    return (
        "[escalation]",
        f"this ticket first ran on {tier or 'a local level'} (a local model), which could "
        f"not finish it: {reason[:300]}",
        f"you are re-running it once on {target}; {tree}",
        f"the earlier attempt touched: {touched}",
        *(
            "the earlier attempt reported: "
            + r[: DIGEST_CAP if r.startswith(STOPPED_DIGEST) else 300]
            for r in reported
        ),
        *failures,
    )


def _escalated_packet(
    packet: TaskPacket, target: str, context: tuple[str, ...], turn_budget: int
) -> TaskPacket:
    """``packet`` re-aimed at the cloud ``target``, on the cloud turn budget (a local
    packet carries :data:`_LOCAL_TURN_BUDGET`)."""
    return packet.model_copy(
        update={
            "task": {**packet.task, "tier": target, "escalated": True},
            "prior_failures": context,
            "turn_budget": turn_budget,
        }
    )
