"""Patch, don't re-plan: one delta call over only the tickets the findings name."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from foundry import progress
from foundry.agents._common import as_text
from foundry.agents.plan_contracts import CompileResult
from foundry.agents.planner.assembly import _assemble, _Frame
from foundry.agents.planner.build import _EPIC_KEY
from foundry.agents.planner.checks import _goals_text
from foundry.agents.planner.readers import _objs, _steps, _str, _strs
from foundry.agents.planner.schema import _EPIC_OF_TICKET, _GENERATED_TICKETS, _SAFE_TICKET_ID
from foundry.gates.spec import _REQUIRED_FIELDS as _GATE_FIELDS
from foundry.gates.spec import _incomplete_steps
from foundry.schemas.product import AcceptanceCriterion, IntentBrief


def _ticket_frame(frame: _Frame) -> _Frame:
    """``frame`` checking only what a ticket delta can change: the contracts, their
    compile and the run command are held as they are."""
    return replace(
        frame, interfaces=(), language="", compiled=CompileResult(), run=None, stack_findings=()
    )


def _patchable(
    findings: Iterable[str],
    items: Sequence[dict[str, Any]],
    frame: _Frame,
    overflow: Sequence[str],
) -> bool:
    """True when there are tickets to patch, none was dropped at a cap (the full
    re-plan consolidates so no work is lost) and the tickets alone reproduce every
    finding: one the contracts or the run command cause needs a full re-plan."""
    if not items or overflow:
        return False
    fit = _assemble(items, _ticket_frame(frame))
    return not fit.overflow and set(findings) <= set(fit.blocking)


def _structural_findings(criteria: Sequence[AcceptanceCriterion]) -> tuple[str, ...]:
    """What the Spec gate checks of the criteria themselves, beside the critique,
    run over the entries it would materialize (``gate_wiring._criteria_list``)."""
    if not criteria:
        return ("criteria set is empty",)
    out: list[str] = []
    for c in criteria:
        entry: dict[str, Any] = {"id": c.id, "text": c.text, "oracle": c.oracle_hint}
        if c.given or c.when or c.then:
            entry.update(given=list(c.given), when=list(c.when), then=list(c.then))
        missing = [f for f in _GATE_FIELDS if not str(entry.get(f) or "").strip()]
        if missing:
            out.append(f"criterion {c.id}: missing/empty {', '.join(missing)}")
        if steps := _incomplete_steps(entry):
            out.append(f"criterion {c.id}: scenario has no {'/'.join(steps)} step")
    if not any(c.security_abuse for c in criteria):
        out.append("no criterion flags security_abuse=true")
    return tuple(out)


# A plan this small goes to the delta whole: a fix often lands in a ticket no finding
# names ("fold contracts-2 into the ticket it serves"), and a summary-only ticket can't
# be edited.
_SHOW_ALL_TICKETS = 8


def _affected(
    findings: Iterable[str], items: Sequence[dict[str, Any]], contracts: Iterable[str]
) -> set[str]:
    """Ids of the tickets the delta sees in full: every one in a small plan, else the
    ones the findings name: by id, by a scenario they own, or by a contract they
    implement or consume (matched as text: a name may hold spaces)."""
    if len(items) <= _SHOW_ALL_TICKETS:
        return {i["id"] for i in items}
    findings = tuple(findings)
    tokens = {tok.strip(".") for f in findings for tok in _SAFE_TICKET_ID.findall(f)}
    named = {n for n in contracts if n and any(n in f for f in findings)}
    return {
        i["id"]
        for i in items
        if i["id"] in tokens
        or named.intersection((*i["implements"], *i["consumes"]))
        or any(s["id"] in tokens for s in i["scenarios"])
    }


_INDEX_KEYS = (
    "id",
    "title",
    "specialty",
    "difficulty",
    "depends_on",
    "files",
    "implements",
    "consumes",
    "entrypoint",
)


def _delta_text(
    request: str,
    findings: Sequence[str],
    items: Sequence[dict[str, Any]],
    shown: set[str],
    contracts: Sequence[tuple[str, str]],
    brief: IntentBrief | None,
    epic_ids: frozenset[str],
) -> str:
    """The delta call's prompt: the findings, the ``shown`` tickets in full and
    only a summary of the rest, so the reply stays a fraction of the plan."""
    full = [{k: v for k, v in i.items() if k != _EPIC_KEY} for i in items if i["id"] in shown]
    index = [
        {**{k: i[k] for k in _INDEX_KEYS}, "scenario_ids": [s["id"] for s in i["scenarios"]]}
        for i in items
        if i["id"] not in shown
    ]
    used = {
        *(n for n, _ in contracts if any(n in f for f in findings)),
        *(n for i in full for n in (*i["implements"], *i["consumes"])),
    }
    return (
        f"REQUEST:\n{request}\n\nPLAN CHECK FINDINGS:\n- "
        + "\n- ".join(findings)
        + _goals_text(brief)
        + "\n\nINTERFACE CONTRACTS (fixed):\n"
        + (
            "\n".join(f"- {n}: {spec}" if n in used else f"- {n}" for n, spec in contracts)
            or "(none)"
        )
        + "\n\nTICKETS THE FINDINGS NAME (in full; only these may be changed):\n"
        + (as_text(full) if full else "(none)")
        + "\n\nEVERY OTHER TICKET (summary for reference: never change one, list its "
        "scenarios or move one off it; you may still remove the ticket):\n"
        + (as_text(index) if index else "(none)")
        + (
            f"\n\nEPICS: {', '.join(sorted(epic_ids))}. Every ticket id keeps its epic "
            "prefix (E1-T01); a new ticket's id starts with the prefix of its epic, and "
            "its depends_on names only tickets of that epic."
            if epic_ids
            else ""
        )
    )


def _merge_delta(
    items: Sequence[dict[str, Any]],
    delta: dict[str, Any],
    epic_ids: frozenset[str],
    shown: set[str],
) -> list[dict[str, Any]] | None:
    """``items`` with ``delta`` applied by id. A listed ticket's fields replace the
    old ones and its scenarios merge by id (one listed on another ticket moves
    there); removals drop tickets and scenarios. None for a delta that changes
    nothing, names a ticket outside the plan's epics, leaves no ticket, lists one
    scenario on two tickets, or edits a ticket the model saw only as a summary
    (not in ``shown``): changing it, or moving or removing a scenario on it,
    would overwrite content it never saw."""
    upserts: dict[str, dict[str, Any]] = {}
    for u in _objs(delta.get("tickets")):
        tid = _str(u.get("id")).strip()
        if tid and tid not in _GENERATED_TICKETS:
            upserts[tid] = {k: v for k, v in u.items() if not k.startswith("__")}
    removed = set(_strs(delta.get("remove_tickets")))
    dropped = set(_strs(delta.get("remove_scenarios")))
    if not (upserts or removed or dropped):
        _rejected("it changes nothing")
        return None
    summarized = {i["id"] for i in items} - shown
    if edited := summarized & set(upserts):
        _rejected(f"it rewrites summary-only ticket(s) {', '.join(sorted(edited))}")
        return None
    listed: dict[str, str] = {}
    for tid, u in upserts.items():
        for s in _objs(u.get("scenarios")):
            sid = _str(s.get("id")).strip()
            if sid and listed.setdefault(sid, tid) != tid:
                _rejected(f"it lists scenario {sid} on two tickets")
                return None
    owner = {s["id"]: i["id"] for i in items for s in i["scenarios"]}
    if moved := [sid for sid in (*listed, *dropped) if owner.get(sid) in summarized]:
        _rejected(f"it moves or removes scenario(s) {', '.join(moved)} of a summary-only ticket")
        return None
    out: dict[str, dict[str, Any]] = {
        i["id"]: {
            **i,
            "scenarios": [
                s
                for s in i["scenarios"]
                if s["id"] not in dropped and listed.get(s["id"], i["id"]) == i["id"]
            ],
        }
        for i in items
        if i["id"] not in removed
    }
    for tid, u in upserts.items():
        epic = _EPIC_OF_TICKET.match(tid)
        if epic_ids and (epic is None or epic.group(1) not in epic_ids):
            _rejected(f"ticket {tid} is outside the plan's epics")
            return None
        old = out.get(tid, {})
        new = _objs(u.get("scenarios"))
        mine = {sid: s for s in new if (sid := _str(s.get("id")).strip())}
        kept = [mine.pop(s["id"], s) for s in _objs(old.get("scenarios"))]
        entry = {
            **old,
            **u,
            "id": tid,
            "scenarios": [
                s
                for s in (*kept, *mine.values(), *(s for s in new if not _str(s.get("id")).strip()))
                if _str(s.get("id")).strip() not in dropped
            ],
        }
        if epic_ids and epic is not None:
            eid = epic.group(1)
            entry[_EPIC_KEY] = eid
            entry["depends_on"] = [
                d for d in _steps(entry.get("depends_on")) if d.startswith(f"{eid}-")
            ]
        out[tid] = entry
    if not out:
        _rejected("it leaves no ticket")
        return None
    return list(out.values())


def _rejected(why: str) -> None:
    progress.log(f"planner delta rejected: {why}; re-planning in full")
    return None


def _patch_note(before: int) -> str:
    return (
        f" Patched the plan in-node with one delta call after its own plan check: "
        f"{before} -> 0 finding(s)."
    )
