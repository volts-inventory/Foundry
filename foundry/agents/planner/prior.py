"""The prior plan: an earlier pass recovered so a re-plan re-runs only what failed."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from foundry.agents.planner.assembly import _Fit, _Frame
from foundry.agents.planner.build import _BRIEF_CAP, _EPIC_KEY, _contract_text, _Epic
from foundry.agents.planner.checks import _declaration_only, _goals_text
from foundry.agents.planner.prompt import _GATE
from foundry.agents.planner.readers import (
    _newest_by_id,
    _newest_intent,
    _objs,
    _prose,
    _section,
    _str,
    _uniq,
)
from foundry.agents.planner.schema import (
    _EPIC_OF_TICKET,
    _GENERATED_TICKETS,
    _SAFE_TICKET_ID,
    EPIC_KIND,
    NODE_ID,
    TICKET_KIND,
)
from foundry.schemas.architecture import InterfaceContract, SystemDesign
from foundry.schemas.implementation import Epic, StackDecision, Ticket
from foundry.schemas.product import PRD, AcceptanceCriterion, CriteriaCritique, IntentBrief

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq


def _ticket_epics(tickets: Sequence[Ticket]) -> dict[str, str]:
    return {
        t.id: m.group(1)
        for t in tickets
        if t.id not in _GENERATED_TICKETS and (m := _EPIC_OF_TICKET.match(t.id))
    }


@dataclass(frozen=True)
class _PriorPlan:
    """An earlier epic-mode pass, recovered from this node's artifacts so a
    Spec-gate re-plan can re-run only the epics its findings name."""

    epics: tuple[_Epic, ...]
    epic_of: dict[str, str]  # live ticket id -> epic id
    tickets: tuple[Ticket, ...]  # live, Seq order (the planner's own tickets aside)
    generated: tuple[Ticket, ...]  # the planner's own tickets, carried as they were
    criteria: dict[str, AcceptanceCriterion]  # newest per id, live tickets only
    prd: PRD
    critique: CriteriaCritique | None
    interfaces: tuple[tuple[str, str], ...]  # (name, spec)
    declared: frozenset[str]  # type-kind contracts the contracts ticket writes in full
    brief: str
    intent: IntentBrief | None


def _latest_epics(state: FactoryState, rt: AgentRuntime) -> list[_Epic]:
    """The epics of this node's newest call-1 pass (one Seq but for ``local``), so
    an epic an earlier pass had and a later one dropped never comes back."""
    if rt.cas is None:
        return []
    handles = sorted(
        (
            a
            for a in state.get("artifacts") or ()
            if a.kind == EPIC_KIND and a.seq.node_id == NODE_ID
        ),
        key=lambda a: a.seq,
    )
    if not handles:
        return []
    newest = handles[-1].seq._replace(local=0)
    out: list[_Epic] = []
    for a in handles:
        if a.seq._replace(local=0) != newest:
            continue
        try:
            body = rt.cas.resolve(a)
        except Exception:
            continue
        if isinstance(body, Epic):
            out.append(
                _Epic(
                    id=body.id,
                    title=body.title,
                    summary=body.summary,
                    specialty_mix=body.specialty_mix,
                    depends_on=body.depends_on,
                    provides=body.provides,
                    consumes=body.consumes,
                )
            )
    return out


def _epics_from_tickets(live: Sequence[Ticket], epic_of: dict[str, str]) -> list[_Epic]:
    """Epics rebuilt from ticket-id prefixes, for a pass that predates the
    ``epic`` records: titles and declared contracts are approximations."""
    order = sorted(set(epic_of.values()), key=lambda e: int(e[1:]))
    epics: list[_Epic] = []
    for eid in order:
        mine = [t for t in live if epic_of[t.id] == eid]
        upstream = {epic_of[d] for t in mine for d in t.depends_on if epic_of.get(d, eid) != eid}
        epics.append(
            _Epic(
                id=eid,
                title=eid,
                summary="; ".join(t.title for t in mine)[:_BRIEF_CAP],
                specialty_mix=_uniq(t.specialty.value for t in mine),
                depends_on=tuple(e for e in order if e in upstream),
                provides=_uniq(x for t in mine for x in t.implements),
                consumes=_uniq(x for t in mine for x in t.consumes),
            )
        )
    return epics


def _prior_plan(
    state: FactoryState, rt: AgentRuntime, *, inline: bool = False
) -> _PriorPlan | None:
    """None unless every live planner ticket carries the prefix of a known epic
    (an inline plan has no epics to target) and the PRD resolves. ``inline`` asks
    instead for a plan none of whose tickets carries an epic prefix, with no epics."""
    every = sorted(
        (t for t in _newest_by_id(state, rt, TICKET_KIND, Ticket).values() if t.supersedes != t.id),
        key=lambda t: t.seq,
    )
    live = [t for t in every if t.id not in _GENERATED_TICKETS]
    prds = list(_newest_by_id(state, rt, "prd", PRD).values())
    if not live or not prds:
        return None
    epic_of: dict[str, str] = {}
    epics: list[_Epic] = []
    if inline:
        if any(_EPIC_OF_TICKET.match(t.id) for t in live):
            return None
    else:
        for t in live:
            m = _EPIC_OF_TICKET.match(t.id)
            if m is None:
                return None
            epic_of[t.id] = m.group(1)
        epics = _latest_epics(state, rt) or _epics_from_tickets(live, epic_of)
        if not set(epic_of.values()) <= {e.id for e in epics}:
            return None
    owned = {t.id for t in live}
    prd = max(prds, key=lambda p: p.seq)
    # Criteria are never tombstoned: only the current PRD's ids are live, so a
    # scenario a later pass dropped stays dropped.
    current = set(prd.criterion_ids)
    criteria = {
        cid: c
        for cid, c in _newest_by_id(state, rt, "acceptance_criterion", AcceptanceCriterion).items()
        if c.ticket_id in owned and cid in current
    }
    critiques = list(_newest_by_id(state, rt, "criteria_findings", CriteriaCritique).values())
    designs = list(_newest_by_id(state, rt, "system_design", SystemDesign).values())
    design = max(designs, key=lambda d: d.seq) if designs else None
    contracts = _newest_by_id(state, rt, "interface_contract", InterfaceContract)
    interfaces = tuple((name, _prose(rt, i.spec_uri)) for name, i in contracts.items())
    declared = frozenset(name for name, i in contracts.items() if _declaration_only(i))
    intent = _newest_intent(state, rt)
    brief = (
        "PLAN (from the previous pass):\n"
        f"Product: {prd.title}\n{_prose(rt, prd.prd_uri)[:_BRIEF_CAP]}"
        + (
            f"\nArchitecture: {design.title}\n{_prose(rt, design.design_uri)[:_BRIEF_CAP]}\n"
            f"Components: {', '.join(design.components) or '(none)'}"
            if design
            else ""
        )
        + _goals_text(intent)
    )
    return _PriorPlan(
        epics=tuple(epics),
        epic_of=epic_of,
        tickets=tuple(live),
        generated=tuple(t for t in every if t.id in _GENERATED_TICKETS),
        criteria=criteria,
        prd=prd,
        critique=max(critiques, key=lambda c: c.seq) if critiques else None,
        interfaces=interfaces,
        declared=declared,
        brief=brief,
        intent=intent,
    )


def _target_epics(findings: tuple[str, ...], prior: _PriorPlan) -> tuple[str, ...] | None:
    """The epics a Spec-gate failure names by epic, ticket or criterion id, or
    None when any finding names none of them: a plan-level issue re-plans all."""
    ids = {e.id for e in prior.epics}
    hit: set[str] = set()
    for finding in findings:
        tokens = {tok.strip(".") for tok in _SAFE_TICKET_ID.findall(finding)}
        named = {t for t in tokens if t in ids}
        named |= {prior.epic_of[t] for t in tokens if t in prior.epic_of}
        named |= {
            prior.epic_of[c.ticket_id]
            for cid, c in prior.criteria.items()
            if cid in tokens and c.ticket_id in prior.epic_of
        }
        if not named:
            return None
        hit |= named
    return tuple(e.id for e in prior.epics if e.id in hit)


def _ticket_items(
    tickets: Iterable[Ticket],
    criteria: Iterable[AcceptanceCriterion],
    epic_of: dict[str, str],
) -> list[dict[str, Any]]:
    """Checked tickets back as raw items (scenarios from their criteria), the
    planner's own tickets aside. With ``epic_of`` each item is tagged with its epic
    and keeps only its same-epic deps (:func:`_link_epics` re-derives the rest)."""
    scenarios: dict[str, list[dict[str, Any]]] = {}
    for c in criteria:
        scenarios.setdefault(c.ticket_id or "", []).append(
            {
                "id": c.id,
                "text": c.text,
                "given": list(c.given),
                "when": list(c.when),
                "then": list(c.then),
                "criticality": c.criticality.value,
                "security_abuse": c.security_abuse,
                "covers": list(c.covers),
            }
        )
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "specialty": t.specialty.value,
            "difficulty": t.difficulty.value,
            "depends_on": [
                d
                for d in t.depends_on
                if (epic_of.get(d) == epic_of.get(t.id) if epic_of else d not in _GENERATED_TICKETS)
            ],
            "files": list(t.files),
            "reads": list(t.reads),
            "implements": list(t.implements),
            "consumes": list(t.consumes),
            "entrypoint": t.entrypoint,
            "scenarios": scenarios.get(t.id, []),
            **({_EPIC_KEY: epic_of.get(t.id, "")} if epic_of else {}),
        }
        for t in tickets
        if t.id not in _GENERATED_TICKETS
    ]


def _prior_items(prior: _PriorPlan, epic_id: str) -> list[dict[str, Any]]:
    """An untouched epic's live tickets back as raw items, so the merge re-derives
    every dependency against the re-planned epics."""
    return _ticket_items(
        (t for t in prior.tickets if prior.epic_of[t.id] == epic_id),
        prior.criteria.values(),
        prior.epic_of,
    )


def _norm_title(title: str) -> str:
    return " ".join(title.lower().split())


def _match_prior_epics(epics: Sequence[_Epic], prior: _PriorPlan) -> dict[str, str]:
    """New epic id -> the previous pass's epic it continues, by title or by its
    declared contracts. A re-plan's call 1 may reorder or split epics, so a
    position never pairs them, and an ambiguous match pairs nothing."""

    def contracts(e: _Epic) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(e.provides), frozenset(e.consumes)

    found: dict[str, str] = {}
    for e in epics:
        hits = {
            p.id
            for p in prior.epics
            if (_norm_title(e.title) and _norm_title(p.title) == _norm_title(e.title))
            or ((e.provides or e.consumes) and contracts(p) == contracts(e))
        }
        if len(hits) == 1:
            found[e.id] = hits.pop()
    claimed = Counter(found.values())
    return {new: old for new, old in found.items() if claimed[old] == 1}


def _epic_revision_text(
    findings: tuple[str, ...],
    prior: _PriorPlan | None,
    epic_id: str,
    continues: str | None,
    source: tuple[str, str] = _GATE,
) -> str:
    """The revision block of one epic call. ``continues`` is the previous pass's
    epic this one carries on: its tickets are reproduced under this epic's id.
    Without one, the previous plan's tickets are listed for reference only."""
    head = (
        f"\n\nREVISION PASS — {source[0]} FAILED the previous plan. Return this "
        "epic's FULL ticket list again"
    )
    tail = f"{source[1]}:\n- " + "\n- ".join(findings) + "\n\n"
    if prior is None or continues is None:
        refs = (
            "\n".join(f"- {t.id} [{t.specialty.value}] {t.title}" for t in prior.tickets)[
                :_BRIEF_CAP
            ]
            if prior is not None
            else ""
        )
        return (
            head + ", numbered afresh, AND add scenarios that close every finding below "
            "that concerns this epic.\n"
            + tail
            + "The previous plan's tickets (reference only — the epics were re-split, so "
            f"do not reuse these ids):\n{refs or '(none)'}"
        )

    def rebased(xid: str) -> str:
        return f"{epic_id}-{xid.removeprefix(f'{continues}-')}"

    lines: list[str] = []
    for t in (t for t in prior.tickets if prior.epic_of[t.id] == continues):
        lines.append(f"- {rebased(t.id)} [{t.specialty.value}] {t.title} files={list(t.files)}")
        lines += [
            f"    - {rebased(c.id)}: {c.text} (criticality={c.criticality.value}; "
            f"security_abuse={c.security_abuse})"
            for c in prior.criteria.values()
            if c.ticket_id == t.id
        ]
    return (
        head + ": reproduce every ticket and scenario below (same ids, never weakened; "
        "fix in place any a finding names) AND add scenarios that close every finding "
        "below that concerns this epic.\n"
        + tail
        + f"This epic's previous tickets:\n{chr(10).join(lines) or '(none)'}"
    )


def _retired_tickets(
    state: FactoryState, rt: AgentRuntime, emitted: list[Ticket], seq: Seq
) -> list[Ticket]:
    """Tombstones for this node's prior live tickets a re-plan dropped, the same
    pattern as :func:`_retired_constraints`: ``supersedes`` names the ticket's own id
    and the newer Seq makes it the id's latest record, so ``read_tickets`` drops it."""
    newest = _newest_by_id(state, rt, TICKET_KIND, Ticket)
    kept = {t.id for t in emitted}
    stale = sorted(
        (t for t in newest.values() if t.supersedes != t.id and t.id not in kept),
        key=lambda t: t.id,
    )
    return [
        t.model_copy(
            update={
                "supersedes": t.id,
                "version": t.version + 1,
                "seq": seq._replace(local=len(emitted) + i),
            }
        )
        for i, t in enumerate(stale)
    ]


@dataclass(frozen=True)
class _Draft:
    """One call-1 plan turned into checked tickets, before anything is emitted."""

    payload: dict[str, Any]
    brief: IntentBrief
    interfaces: list[InterfaceContract]
    epics: list[_Epic]
    per_epic: bool
    raw: list[dict[str, Any]]  # the ticket items (epic-tagged when per epic)
    epic_overflow: tuple[str, ...]  # work dropped at the epic caps, before assembly
    epic_inputs: tuple[str, str, str]  # intake, plan brief, contracts text
    missing: tuple[str, ...]
    omissions: tuple[str, ...]
    revised_epics: tuple[str, ...]
    concerns: tuple[str, ...]  # plan-level: epics, stack, generated tickets
    decided: StackDecision | None
    stack_update: StackDecision | None
    frame: _Frame
    fit: _Fit


def _with_fit(prior: _PriorPlan, fit: _Fit) -> _PriorPlan:
    """``prior`` describing ``fit``'s tickets and scenarios, so the epic targeting
    and revision text of a Spec-gate re-plan serve an in-node revision too."""
    epic_of = _ticket_epics(fit.tickets)
    return replace(
        prior,
        epic_of=epic_of,
        tickets=tuple(t for t in fit.tickets if t.id in epic_of),
        criteria={c.id: c for c in fit.criteria if c.ticket_id in epic_of},
    )


def _draft_prior(draft: _Draft, prd_seq: Seq) -> _PriorPlan:
    payload = draft.payload
    prd = _section(payload, "prd")
    base = _PriorPlan(
        epics=tuple(draft.epics),
        epic_of={},
        tickets=(),
        generated=tuple(t for t in draft.fit.tickets if t.id in _GENERATED_TICKETS),
        criteria={},
        prd=PRD(title=_str(prd.get("title")), prd_uri="", seq=prd_seq),
        critique=None,
        interfaces=tuple(
            (_str(i.get("name")), _contract_text(i)) for i in _objs(payload.get("interfaces"))
        ),
        declared=draft.frame.declared,
        brief=draft.epic_inputs[1],
        intent=draft.brief,
    )
    return _with_fit(base, draft.fit)


def _named_epics(findings: Iterable[str], prior: _PriorPlan) -> tuple[str, ...]:
    """Every epic some finding names (a finding naming none is skipped)."""
    hit = {e for f in findings for e in (_target_epics((f,), prior) or ())}
    return tuple(e.id for e in prior.epics if e.id in hit)


def _untagged(gaps: Iterable[str], epic_ids: Iterable[str]) -> tuple[str, ...]:
    tags = tuple(f"[{e}] " for e in epic_ids)
    return tuple(g for g in gaps if not g.startswith(tags))


def _not_of(overflow: Iterable[str], epic_ids: Iterable[str]) -> list[str]:
    heads = tuple(f"Epic {e}:" for e in epic_ids)
    return [o for o in overflow if not o.startswith(heads)]


def _revision_note(what: str, before: int, after: int, kept: bool) -> str:
    if kept:
        return (
            f" Revised {what} once in-node after its own plan check but kept the "
            f"original ({before} finding(s); the revision had {after})."
        )
    return f" Revised {what} once in-node after its own plan check: {before} -> {after} finding(s)."
