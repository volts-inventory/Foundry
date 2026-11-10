"""Ticket split: the planner's answer to a ticket that failed after its retry.

The engineering node (a build that failed after its retry) or a fix-loop check (a
cluster still failing after the ticket's restart and escalation) asks for a split
once per ticket (:mod:`foundry.agents.implementation.tickets.split`). One call splits
it into 2-3 children along its scenarios and files: together they own exactly the
parent's scenarios, files and implemented contracts, each keeps its dependencies
and consumed contracts, and the parent's dependents depend on the children no
sibling depends on (the last of a chain; every child of independent ones). The
re-planned items go through the same assembly and checks as a Spec-gate delta; a
split those checks refuse is declined, and the fix loop's restart stays the
ticket's fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from foundry.agents._common import as_text
from foundry.agents.planner.assembly import _Fit
from foundry.agents.planner.build import _EPIC_KEY
from foundry.agents.planner.readers import _objs, _str, _strs
from foundry.gates.bdd_scaffold import ticket_step_module
from foundry.proxy.model_provider_proxy import ProxyUnavailable, StructuredOutputError

SPLIT_SOP = (
    "You are the Ticket Splitter. One ticket of the plan failed to build even after "
    "its retry: it is likely too much for one session. Split it into 2 or 3 smaller "
    "tickets, in build order, along its scenarios and files. Together the children "
    "must own exactly the ticket's scenarios (each on one child) and its files (each "
    "on at least one child), and exactly the contracts it implements (each on one "
    "child, the one whose files define it). A child may consume a contract an earlier "
    "child implements. 'after' lists the 1-based numbers of earlier children a child "
    "builds on. Use only the ids, paths and names given below; say why in the rationale."
)

_CHILD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "scenario_ids": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "implements": {"type": "array", "items": {"type": "string"}},
        "consumes": {"type": "array", "items": {"type": "string"}},
        "after": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "title",
        "description",
        "scenario_ids",
        "files",
        "implements",
        "consumes",
        "after",
    ],
    "additionalProperties": False,
}

SPLIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "children": {"type": "array", "items": _CHILD},
        "rationale": {"type": "string"},
    },
    "required": ["children", "rationale"],
    "additionalProperties": False,
}

MAX_CHILDREN = 3

Call = Callable[[str, dict[str, Any], str], dict[str, Any]]


def _own_files(parent: Mapping[str, Any]) -> list[str]:
    """The parent's files less the step module named for it: the binding folds give
    each child one of its own."""
    return [f for f in parent["files"] if f != ticket_step_module(parent["id"])]


def ask_split(
    call: Call, request: str, parent: Mapping[str, Any], why: Sequence[str]
) -> dict[str, Any] | None:
    """The split call for the ``parent`` item; None for a reply that did not parse."""
    brief = {k: v for k, v in parent.items() if k != _EPIC_KEY} | {"files": _own_files(parent)}
    content = f"REQUEST:\n{request}\n\nTICKET TO SPLIT:\n{as_text(brief)}\n\nWHY IT FAILED:\n" + (
        "\n".join(f"- {w}" for w in why) or "(none recorded)"
    )
    try:
        return call(SPLIT_SOP, SPLIT_SCHEMA, content)
    except ProxyUnavailable as exc:
        # Only an unparseable reply is an answer: a transport outage stays one.
        if not isinstance(exc.__cause__, StructuredOutputError):
            raise
        return None


def child_ids(parent: str, n: int, taken: set[str]) -> list[str]:
    """``n`` fresh ids under ``parent`` (its epic prefix kept), none in ``taken``:
    a reused id would inherit an earlier ticket's diffs and notes."""
    out: list[str] = []
    k = 1
    while len(out) < n:
        tid = f"{parent}-{k}"
        if tid not in taken:
            out.append(tid)
        k += 1
    return out


def split_items(
    items: Sequence[dict[str, Any]],
    parent_id: str,
    reply: dict[str, Any] | None,
    taken: set[str],
) -> tuple[list[dict[str, Any]] | None, list[str], str]:
    """``(items with the parent replaced by its children, the child ids, why
    not)``: None for a reply that is no valid split of the parent."""
    parent = next((i for i in items if i["id"] == parent_id), None)
    if parent is None:
        return None, [], f"{parent_id} is not a ticket of the plan"
    if reply is None:
        return None, [], "its reply did not parse"
    children = _objs(reply.get("children"))
    if not 2 <= len(children) <= MAX_CHILDREN:
        return None, [], f"it gave {len(children)} children, not 2-{MAX_CHILDREN}"
    scenarios = {s["id"]: s for s in parent["scenarios"]}
    files = _own_files(parent)
    implements = list(parent["implements"])
    usable = {*parent["consumes"], *implements}
    ids = child_ids(parent_id, len(children), taken)
    owned: dict[str, str] = {}
    provided: dict[str, str] = {}
    written: set[str] = set()
    out: list[dict[str, Any]] = []
    for n, (cid, child) in enumerate(zip(ids, children, strict=True), start=1):
        mine = list(dict.fromkeys(_strs(child.get("scenario_ids"))))
        paths = list(dict.fromkeys(_strs(child.get("files"))))
        names = list(dict.fromkeys(_strs(child.get("implements"))))
        # A bool is an int to isinstance: True would read as "after child 1".
        after = [a for a in child.get("after") or () if type(a) is int]
        if unknown := [s for s in mine if s not in scenarios]:
            return None, [], f"child {n} lists scenario(s) {', '.join(unknown)} not the ticket's"
        if not paths or (unknown := [f for f in paths if f not in files]):
            return None, [], f"child {n} lists no files or file(s) not the ticket's: {unknown}"
        if unknown := [c for c in names if c not in implements]:
            return None, [], f"child {n} implements {', '.join(unknown)}, not the ticket's"
        if bad := [a for a in after if not 1 <= a < n]:
            return None, [], f"child {n} builds after {bad}, not an earlier child"
        for sid in mine:
            if sid in owned:
                return None, [], f"scenario {sid} is on two children"
            owned[sid] = cid
        for name in names:
            if name in provided:
                return None, [], f"contract {name} is implemented by two children"
            provided[name] = cid
        written.update(paths)
        consumes = [
            c for c in dict.fromkeys(_strs(child.get("consumes"))) if c in usable and c not in names
        ]
        out.append(
            {
                **{k: v for k, v in parent.items() if k == _EPIC_KEY},
                "id": cid,
                "title": _str(child.get("title")).strip() or f"{parent['title']} ({n})",
                "description": _str(child.get("description")).strip() or parent["description"],
                "specialty": parent["specialty"],
                "difficulty": parent["difficulty"],
                "depends_on": [*parent["depends_on"], *(ids[a - 1] for a in after)],
                "files": paths,
                "reads": list(parent["reads"]),
                "implements": names,
                "consumes": list(dict.fromkeys([*parent["consumes"], *consumes])),
                # The parent's entrypoint duty goes to its last child.
                "entrypoint": bool(parent["entrypoint"]) and n == len(children),
                "scenarios": [scenarios[s] for s in mine],
            }
        )
    if missing := [s for s in scenarios if s not in owned]:
        return None, [], f"no child owns scenario(s) {', '.join(missing)}"
    if missing := [f for f in files if f not in written]:
        return None, [], f"no child owns file(s) {', '.join(missing)}"
    if missing := [c for c in implements if c not in provided]:
        return None, [], f"no child implements {', '.join(missing)}"
    needed = {d for c in out for d in c["depends_on"]}
    last = [c["id"] for c in out if c["id"] not in needed]
    merged: list[dict[str, Any]] = []
    for item in items:
        if item["id"] == parent_id:
            merged.extend(out)
            continue
        deps = list(item.get("depends_on") or ())
        if parent_id in deps:
            at = deps.index(parent_id)
            deps[at : at + 1] = [d for d in last if d not in deps]
            item = {**item, "depends_on": deps}
        merged.append(item)
    return merged, ids, ""


def split_problem(
    fit: _Fit, parent_id: str, children: Sequence[str], scenarios: Sequence[str], known: set[str]
) -> str:
    """Why the assembled plan ``fit`` is no valid split of ``parent_id``: a check
    finding the plan before it did not have (``known``), work dropped at a cap, or
    children that no longer own exactly the parent's scenarios. Empty when valid."""
    if new := [f for f in fit.blocking if f not in known]:
        return f"{len(new)} new check finding(s): " + " | ".join(new)[:400]
    if fit.overflow:
        return "the split plan drops work at a cap"
    by_id = {t.id: t for t in fit.tickets}
    if parent_id in by_id or any(c not in by_id for c in children):
        return "the assembled plan did not keep the children in place of the ticket"
    owned = [c.id for c in fit.criteria if c.ticket_id in children]
    if sorted(owned) != sorted(scenarios):
        return "the assembled children do not own exactly the ticket's scenarios"
    return ""
