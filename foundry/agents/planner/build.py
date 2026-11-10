"""Record builders: criteria, epics and tickets, ADRs and interface contracts."""

from __future__ import annotations

import contextvars
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from foundry.agents._common import as_text, persist_prose
from foundry.agents.planner.checks import _goals_text
from foundry.agents.planner.folds import _is_python_test
from foundry.agents.planner.readers import _bool, _enum, _objs, _section, _steps, _str, _strs, _uniq
from foundry.agents.planner.schema import (
    _EPIC_WORKERS,
    _GENERATED_TICKETS,
    _SAFE_TICKET_ID,
    CONTRACT_KINDS,
    MAX_EPIC_TICKETS,
    MAX_EPICS,
    MAX_TICKETS,
)
from foundry.schemas.architecture import ADR, InterfaceContract
from foundry.schemas.implementation import FEATURE_DIR, Difficulty, Specialty, Ticket, scenario_ref
from foundry.schemas.product import AcceptanceCriterion, Criticality, IntentBrief, render_scenario

if TYPE_CHECKING:
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.state.seq import Seq


def _build_criterion(
    item: dict[str, Any], *, ticket_id: str, default_id: str, seq: Seq
) -> AcceptanceCriterion:
    """One scenario -> one criterion. Its oracle is the scenario itself; ``text``
    falls back to a free-text ``text`` only when no step was given."""
    cid = _str(item.get("id")).strip() or default_id
    given, when, then = (_steps(item.get(k)) for k in ("given", "when", "then"))
    return AcceptanceCriterion(
        id=cid,
        text=render_scenario(given, when, then) or _str(item.get("text")),
        criticality=_enum(Criticality, item.get("criticality"), Criticality.NORMAL),
        oracle_hint=scenario_ref(ticket_id, cid),
        security_abuse=_bool(item.get("security_abuse")),
        given=given,
        when=when,
        then=then,
        ticket_id=ticket_id,
        covers=_uniq(_strs(item.get("covers"))),
        seq=seq,
    )


def _break_cycles(deps: dict[str, list[str]]) -> list[str]:
    """Depth-first in ticket order; an edge back into the current path closes a
    cycle and is dropped. Mutates ``deps``; returns one concern per dropped edge."""
    concerns: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def visit(tid: str) -> None:
        on_path.add(tid)
        kept: list[str] = []
        for dep in deps[tid]:
            if dep in on_path:
                concerns.append(f"Dropped dependency {tid} -> {dep}: it closed a cycle.")
                continue
            if dep not in done:
                visit(dep)
            kept.append(dep)
        deps[tid] = kept
        on_path.discard(tid)
        done.add(tid)

    for tid in deps:
        if tid not in done:
            visit(tid)
    return concerns


_FIX_DIFF_PREFIXES = ("fix-", "revert-")


def _safe_ticket_ids(ids: list[str], reserved: frozenset[str] = frozenset()) -> dict[str, str]:
    """Raw id -> an id safe as a file name (``features/<id>.feature``, ``task/<id>``):
    other characters become ``-``; a collision gets the first free ``-<n>`` suffix,
    in plan order, so the mapping is deterministic. ``reserved`` ids (a base tree's
    feature names) are renamed the same way."""
    out: dict[str, str] = {}
    safe = {
        tid
        for tid in ids
        if _SAFE_TICKET_ID.fullmatch(tid)
        and tid not in (".", "..", *_GENERATED_TICKETS)
        # `fix-<n>` / `revert-<n>` are the fix loop's diff namespaces: a ticket there
        # would read as built.
        and not tid.startswith(_FIX_DIFF_PREFIXES)
        and tid not in reserved
    }
    taken = safe | _GENERATED_TICKETS | reserved
    for tid in ids:
        if tid in safe:
            out[tid] = tid
            continue
        base = re.sub(r"[^A-Za-z0-9._-]", "-", tid).strip(".") or "T"
        if base.startswith(_FIX_DIFF_PREFIXES):
            base = f"T-{base}"
        new, n = base, 2
        while new in taken:
            new, n = f"{base}-{n}", n + 1
        taken.add(new)
        out[tid] = new
    return out


# Tags an epic call's ticket item with its epic for _link_epics; never emitted.
_EPIC_KEY = "__epic__"


# Tags an item dropped at the per-epic cap; never emitted.
_DROPPED_KEY = "__dropped__"


_BRIEF_CAP = 6_000
_TRUNCATED = " …[truncated]"


def _clip(text: str) -> str:
    """``text`` cut at ``_BRIEF_CAP``, visibly: a repair shown it must rewrite the
    field rather than copy the cut."""
    return text if len(text) <= _BRIEF_CAP else text[:_BRIEF_CAP] + _TRUNCATED


@dataclass(frozen=True)
class _Epic:
    id: str
    title: str
    summary: str
    specialty_mix: tuple[str, ...]
    depends_on: tuple[str, ...]
    provides: tuple[str, ...]
    consumes: tuple[str, ...]


def _dropped_text(items: Iterable[tuple[str, dict[str, Any]]]) -> str:
    return ", ".join(
        f"{xid} ({title[:60]})" if (title := _str(item.get("title")).strip()) else xid
        for xid, item in items
    )


def _build_epics(
    raw: Any, interface_names: set[str], overflow: list[str] | None = None
) -> tuple[list[_Epic], list[str]]:
    """The plan's epics, ids made positional (``E1``..``En``) so ticket ids built
    on them stay file-safe and a re-plan can recover the epic from a ticket id.
    Over ``MAX_EPICS`` the tail is dropped (named in ``overflow``); unknown
    dependencies and contract names are dropped and a cycle loses its closing edge."""
    concerns: list[str] = []
    items = _objs(raw)
    if len(items) > MAX_EPICS:
        concerns.append(
            f"{len(items)} epics exceed the cap of {MAX_EPICS}; kept the first {MAX_EPICS}."
        )
        if overflow is not None:
            tail = items[MAX_EPICS:]
            overflow.append(
                f"{len(items)} epics exceed the cap of {MAX_EPICS}; dropped "
                + _dropped_text(
                    (_str(e.get("id")).strip() or f"epic #{MAX_EPICS + i + 1}", e)
                    for i, e in enumerate(tail)
                )
                + f": merge epics so the plan has at most {MAX_EPICS} and no work is lost."
            )
        items = items[:MAX_EPICS]
    ids = [f"E{i + 1}" for i in range(len(items))]
    alias: dict[str, str] = {}
    for eid, item in zip(ids, items, strict=True):
        alias.setdefault(_str(item.get("id")).strip() or eid, eid)
    deps: dict[str, list[str]] = {}
    contracts: dict[tuple[str, str], tuple[str, ...]] = {}
    for eid, item in zip(ids, items, strict=True):
        wanted = _uniq(alias.get(d.strip(), d.strip()) for d in _strs(item.get("depends_on")))
        unknown = [d for d in wanted if d not in ids]
        if unknown:
            concerns.append(f"Epic {eid}: dropped unknown dependencies {', '.join(unknown)}.")
        deps[eid] = [d for d in wanted if d in ids]
        for key in ("provides", "consumes"):
            named = _uniq(_strs(item.get(key)))
            bad = [x for x in named if x not in interface_names]
            if bad:
                concerns.append(f"Epic {eid}: dropped unknown {key} contracts {', '.join(bad)}.")
            contracts[eid, key] = tuple(x for x in named if x in interface_names)
    concerns += [
        c.replace("Dropped dependency", "Dropped epic dependency") for c in _break_cycles(deps)
    ]
    epics = [
        _Epic(
            id=eid,
            title=_str(item.get("title")),
            summary=_str(item.get("summary")),
            specialty_mix=_uniq(_strs(item.get("specialty_mix"))),
            depends_on=tuple(deps[eid]),
            provides=contracts[eid, "provides"],
            consumes=contracts[eid, "consumes"],
        )
        for eid, item in zip(ids, items, strict=True)
    ]
    return epics, concerns


def _ns(epic_id: str, raw_id: str) -> str:
    return raw_id if raw_id.startswith(f"{epic_id}-") else f"{epic_id}-{raw_id}"


def _epic_items(
    epic: _Epic, raw: Any, overflow: list[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One epic call's tickets, capped at ``MAX_EPIC_TICKETS`` (the dropped tail
    named in ``overflow`` and kept marked dropped, so :func:`_build_tickets` can
    rehome its scenarios), with ticket, dependency and scenario ids namespaced
    ``<epic>-<id>`` (missing ids become ``<epic>-T<nn>`` / ``<epic>-AC<nn>``) and
    each item tagged with its epic. A dependency on another epic's ticket cannot
    resolve and is dropped later: cross-epic order comes from :func:`_link_epics`."""
    concerns: list[str] = []
    items = _objs(raw)
    if len(items) > MAX_EPIC_TICKETS:
        concerns.append(
            f"Epic {epic.id}: {len(items)} tickets exceed the per-epic cap of "
            f"{MAX_EPIC_TICKETS}; kept the first {MAX_EPIC_TICKETS}."
        )
        if overflow is not None:
            overflow.append(
                f"Epic {epic.id}: {len(items)} tickets exceed the per-epic cap of "
                f"{MAX_EPIC_TICKETS}; dropped "
                + _dropped_text(
                    (
                        _ns(
                            epic.id, _str(t.get("id")).strip() or f"T{MAX_EPIC_TICKETS + i + 1:02d}"
                        ),
                        t,
                    )
                    for i, t in enumerate(items[MAX_EPIC_TICKETS:])
                )
                + ": consolidate this epic's tickets so no work is lost."
            )
    out: list[dict[str, Any]] = []
    n = 0
    for i, item in enumerate(items):
        scenarios: list[dict[str, Any]] = []
        for scenario in _objs(item.get("scenarios")):
            n += 1
            sid = _str(scenario.get("id")).strip() or f"AC{n:02d}"
            scenarios.append({**scenario, "id": _ns(epic.id, sid)})
        out.append(
            {
                **item,
                "id": _ns(epic.id, _str(item.get("id")).strip() or f"T{i + 1:02d}"),
                "depends_on": [_ns(epic.id, d) for d in _steps(item.get("depends_on"))],
                "scenarios": scenarios,
                _EPIC_KEY: epic.id,
                **({_DROPPED_KEY: True} if i >= MAX_EPIC_TICKETS else {}),
            }
        )
    return out, concerns


def _link_epics(
    deps: dict[str, list[str]],
    items: dict[str, dict[str, Any]],
    epics: Sequence[_Epic],
    interface_names: set[str],
) -> None:
    """Order each epic's tickets after the epics it depends on. Its root tickets
    (no dependency inside the epic) gain deps on each upstream epic's tickets
    that implement an interface the epic consumes, else on that epic's sink
    tickets (none of its tickets depends on them). An upstream epic with no
    tickets is looked through to its own nearest upstream epics that have some,
    so the chain keeps its order. Mutates ``deps``."""
    members = {
        e.id: [tid for tid, item in items.items() if item.get(_EPIC_KEY) == e.id] for e in epics
    }
    by_id = {e.id: e for e in epics}

    def nearest(ups: Iterable[str], seen: set[str]) -> list[str]:
        out: list[str] = []
        for up in ups:
            if up in seen or up not in by_id:
                continue
            seen.add(up)
            out += [up] if members[up] else nearest(by_id[up].depends_on, seen)
        return out

    for epic in epics:
        mine = members[epic.id]
        needs = interface_names & {
            *epic.consumes,
            *(c for tid in mine for c in _strs(items[tid].get("consumes"))),
        }
        upstream: list[str] = []
        for up in nearest(epic.depends_on, set()):
            theirs = members[up]
            providers = [tid for tid in theirs if needs & set(_strs(items[tid].get("implements")))]
            if not providers:
                depended = {d for tid in theirs for d in deps[tid]}
                providers = [tid for tid in theirs if tid not in depended]
            upstream += providers
        if not upstream:
            continue
        own = set(mine)
        for tid in mine:
            if not own.intersection(deps[tid]):
                deps[tid] = list(_uniq((*deps[tid], *upstream)))


def _upstream_epics(epics: Sequence[_Epic], epic_id: str) -> set[str]:
    by_id = {e.id: e for e in epics}
    seen: set[str] = set()
    todo = list(by_id[epic_id].depends_on) if epic_id in by_id else []
    while todo:
        up = todo.pop()
        if up not in seen and up in by_id:
            seen.add(up)
            todo.extend(by_id[up].depends_on)
    return seen


def _entry_epic(epics: Sequence[_Epic]) -> str | None:
    """The epic that owns the entrypoint ticket: the last one every other epic is
    upstream of, else the last epic (the coverage check then names the gap)."""
    if not epics:
        return None
    others = {e.id for e in epics}
    sinks = [e.id for e in epics if _upstream_epics(epics, e.id) == others - {e.id}]
    return sinks[-1] if sinks else epics[-1].id


def _link_entrypoints(
    deps: dict[str, list[str]], items: dict[str, dict[str, Any]], epics: Sequence[_Epic]
) -> None:
    """An epic's entrypoint ticket cannot name other epics' tickets, so it is made
    to depend on every feature ticket of the epics upstream of its own; those never
    depend on it (cross-epic order only points upstream), so no cycle. Mutates
    ``deps``."""
    for tid, item in items.items():
        if not _bool(item.get("entrypoint")) or _is_test(item):
            continue
        upstream = _upstream_epics(epics, _str(item.get(_EPIC_KEY)))
        deps[tid] = list(
            _uniq(
                (
                    *deps[tid],
                    *(
                        other
                        for other, it in items.items()
                        if it.get(_EPIC_KEY) in upstream and not _is_test(it)
                    ),
                )
            )
        )


def _is_test(item: dict[str, Any]) -> bool:
    return _enum(Specialty, item.get("specialty"), Specialty.BACKEND) is Specialty.TEST


def _tagged(epic_id: str, gaps: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"[{epic_id}] {g}" for g in gaps)


def _plan_brief(payload: dict[str, Any], intent: IntentBrief | None = None) -> str:
    """What every epic call needs of call 1's plan besides the epics and contracts."""
    prd, design = _section(payload, "prd"), _section(payload, "design")
    constraints = [f"- {_str(c.get('text'))}" for c in _objs(payload.get("pinned_constraints"))]
    stack = _section(payload, "stack")
    return (
        "PLAN (from the lead planner):\n"
        f"Stack: {_str(stack.get('language')) or '(unset)'}"
        + (f" / {_str(stack.get('framework'))}" if stack.get("framework") else "")
        + (
            f"; contracts file (written for you, never list it in files): "
            f"{_str(stack.get('contracts_file'))}"
            if stack.get("contracts_file")
            else ""
        )
        + "\n"
        f"Product: {_str(prd.get('title'))}\n{_str(prd.get('body'))[:_BRIEF_CAP]}\n"
        f"Architecture: {_str(design.get('title'))}\n"
        f"{_str(design.get('body'))[:_BRIEF_CAP]}\n"
        f"Components: {', '.join(_strs(design.get('components'))) or '(none)'}"
        + (
            "\nPinned constraints (apply to every ticket):\n" + "\n".join(constraints)
            if constraints
            else ""
        )
        + _goals_text(intent)
    )


def _interfaces_text(pairs: Iterable[tuple[str, str]]) -> str:
    return "\n".join(f"- {name}: {spec}" for name, spec in pairs) or "(none)"


def _epic_content(
    intake: str,
    brief: str,
    epics: Sequence[_Epic],
    epic: _Epic,
    interfaces: str,
    revision: str,
) -> str:
    entry = _entry_epic(epics)
    roster = "\n".join(
        f"- {e.id}: {e.title} — {e.summary}"
        + (f" (after {', '.join(e.depends_on)})" if e.depends_on else "")
        for e in epics
    )
    mine = {
        "id": epic.id,
        "title": epic.title,
        "summary": epic.summary,
        "specialty_mix": list(epic.specialty_mix),
        "depends_on": list(epic.depends_on),
        "provides": list(epic.provides),
        "consumes": list(epic.consumes),
    }
    return (
        f"{intake}\n\n{brief}\n\nEPICS (each planned in its own call):\n{roster}\n\n"
        f"YOUR EPIC: {epic.id}\n{as_text(mine)}\n"
        + (
            "YOUR EPIC OWNS THE ENTRYPOINT: mark exactly one feature ticket "
            "entrypoint=true that wires the whole app together end to end (main / server "
            "/ CLI / public API), depends_on every other feature ticket of your epic and "
            "carries an end-to-end scenario.\n\n"
            if epic.id == entry
            else "Your epic does NOT own the entrypoint: entrypoint=false on every ticket.\n\n"
        )
        + "INTERFACE CONTRACTS (shared by every epic; implements[] / consumes[] them "
        f"by name):\n{interfaces}" + revision
    )


_A = TypeVar("_A")
_R = TypeVar("_R")


def _run_parallel(fn: Callable[[_A], _R], args: Sequence[_A]) -> list[_R]:
    """``fn`` over ``args`` on a thread pool, results in ``args`` order. The first
    failure (e.g. ``TokenCapExceeded``) cancels the calls not yet started and
    propagates once the running ones return."""
    if len(args) <= 1:
        return [fn(a) for a in args]
    with ThreadPoolExecutor(
        max_workers=min(len(args), _EPIC_WORKERS), thread_name_prefix="planner-epic"
    ) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, a) for a in args]
        try:
            for done in as_completed(futures):
                done.result()
        except BaseException:
            for f in futures:
                f.cancel()
            raise
        return [f.result() for f in futures]


def _rehome_scenarios(
    kept: dict[str, dict[str, Any]], dropped: Sequence[tuple[str, dict[str, Any]]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The scenarios of tickets dropped at a cap move to the kept feature ticket that
    owns one of the dropped ticket's files; with none, they are dropped with a note."""
    out = dict(kept)
    concerns: list[str] = []
    for tid, item in dropped:
        scenarios = _objs(item.get("scenarios"))
        if not scenarios:
            continue
        ids = ", ".join(_str(s.get("id")).strip() or "?" for s in scenarios)
        files = set(_strs(item.get("files")))
        home = next(
            (
                k
                for k, it in out.items()
                if not _is_test(it) and files.intersection(_strs(it.get("files")))
            ),
            None,
        )
        if home is None:
            concerns.append(
                f"Dropped scenario(s) {ids} with ticket {tid}: no kept ticket owns its files."
            )
            continue
        out[home] = {**out[home], "scenarios": [*_objs(out[home].get("scenarios")), *scenarios]}
        concerns.append(
            f"Moved scenario(s) {ids} of dropped ticket {tid} to {home}, which owns its files."
        )
    return out, concerns


def _build_tickets(
    raw: Any,
    ticket_seq: Seq,
    criteria_seq: Seq,
    interface_names: set[str],
    *,
    cap: int = MAX_TICKETS,
    epics: Sequence[_Epic] = (),
    overflow: list[str] | None = None,
    reserved: frozenset[str] = frozenset(),
) -> tuple[list[Ticket], list[AcceptanceCriterion], list[str]]:
    """Tickets + their scenarios as criteria, made a valid DAG deterministically.

    Duplicate ticket / scenario ids: the later entry wins. Over ``cap`` the tail
    is dropped. Unknown dependencies and contract names are dropped and a cycle
    loses its closing edge. A ``test`` ticket covers its own scenarios plus those
    of the feature tickets it declares a dependency on. With ``epics`` (items tagged by
    :func:`_epic_items`), each epic's tickets are then ordered after the epics
    it depends on (:func:`_link_epics`), and each epic's entrypoint ticket after
    every feature ticket of the epics upstream of it. Tickets dropped at ``cap``
    are named in ``overflow``. ``reserved`` ids (a base tree's feature names) are
    renamed so a change never replaces an existing feature file."""
    concerns: list[str] = []
    raw_items: dict[str, dict[str, Any]] = {}
    dropped: list[tuple[str, dict[str, Any]]] = []
    for i, item in enumerate(_objs(raw)):
        tid = _str(item.get("id")).strip() or f"T-{i + 1:03d}"
        if item.get(_DROPPED_KEY):
            dropped.append((tid, item))
            continue
        if tid in raw_items:
            concerns.append(f"Duplicate ticket id {tid}: the later entry wins.")
        raw_items[tid] = item
    if len(raw_items) > cap:
        concerns.append(f"{len(raw_items)} tickets exceed the cap of {cap}; kept the first {cap}.")
        if overflow is not None:
            overflow.append(
                f"{len(raw_items)} tickets exceed the cap of {cap}; dropped "
                + _dropped_text(list(raw_items.items())[cap:])
                + ": consolidate tickets"
                + ("" if epics else " (or split a larger build into epics)")
                + " so no work is lost."
            )
        dropped += list(raw_items.items())[cap:]
        raw_items = dict(list(raw_items.items())[:cap])
    raw_items, rehome_concerns = _rehome_scenarios(raw_items, dropped)
    concerns += rehome_concerns
    alias = _safe_ticket_ids(list(raw_items), reserved)
    concerns += [
        f"Ticket id {old!r} renamed to {new!r} (ids name files)."
        for old, new in alias.items()
        if old != new
    ]
    items = {alias[tid]: item for tid, item in raw_items.items()}

    deps: dict[str, list[str]] = {}
    for tid, item in items.items():
        wanted = _uniq(alias.get(d, d) for d in _strs(item.get("depends_on")))
        unknown = [d for d in wanted if d not in items]
        if unknown:
            concerns.append(f"Ticket {tid}: dropped unknown dependencies {', '.join(unknown)}.")
        deps[tid] = [d for d in wanted if d in items]
    concerns += _break_cycles(deps)
    # Test coverage follows only the deps the plan declared: a cross-epic order
    # link makes no test ticket cover another epic's scenarios.
    declared = {tid: tuple(d) for tid, d in deps.items()}
    if epics:
        # After the cycle break, so every epic has roots to hang its implicit
        # deps on; they only point at upstream epics of a DAG, so add no cycle.
        _link_epics(deps, items, epics, interface_names)
        _link_entrypoints(deps, items, epics)

    criteria: dict[str, AcceptanceCriterion] = {}
    n = 0
    for tid, item in items.items():
        for scenario in _objs(item.get("scenarios")):
            n += 1
            c = _build_criterion(
                scenario, ticket_id=tid, default_id=f"AC-{n:03d}", seq=criteria_seq
            )
            if c.id in criteria:
                concerns.append(f"Duplicate scenario id {c.id}: the later entry wins.")
            criteria[c.id] = c
    ordered = [
        c.model_copy(update={"seq": criteria_seq._replace(local=i)})
        for i, c in enumerate(criteria.values())
    ]

    specialty = {
        tid: _enum(Specialty, item.get("specialty"), Specialty.BACKEND)
        for tid, item in items.items()
    }
    own = {tid: tuple(c.id for c in ordered if c.ticket_id == tid) for tid in items}
    files: dict[str, tuple[str, ...]] = {}
    for tid, item in items.items():
        planned = _uniq(_strs(item.get("files")))
        files[tid] = tuple(f for f in planned if not f.endswith(".feature"))
        if files[tid] != planned:
            concerns.append(
                f"Ticket {tid}: dropped feature file(s) "
                f"{', '.join(f for f in planned if f.endswith('.feature'))} from its files: "
                f"the gate writes {FEATURE_DIR}/<ticket>.feature from the scenarios."
            )
    tickets: list[Ticket] = []
    for i, (tid, item) in enumerate(items.items()):
        scenario_ids = own[tid]
        if specialty[tid] is Specialty.TEST:
            scenario_ids = _uniq(
                (
                    *scenario_ids,
                    *(
                        c
                        for d in declared[tid]
                        if specialty[d] is not Specialty.TEST
                        for c in own[d]
                    ),
                )
            )
        contracts: dict[str, tuple[str, ...]] = {}
        for key in ("implements", "consumes"):
            named = _uniq(_strs(item.get(key)))
            unknown = [x for x in named if x not in interface_names]
            if unknown:
                concerns.append(
                    f"Ticket {tid}: dropped unknown {key} contracts {', '.join(unknown)}."
                )
            contracts[key] = tuple(x for x in named if x in interface_names)
        tickets.append(
            Ticket(
                id=tid,
                title=_str(item.get("title")),
                description=_str(item.get("description")),
                specialty=specialty[tid],
                difficulty=_enum(Difficulty, item.get("difficulty"), Difficulty.STANDARD),
                depends_on=tuple(deps[tid]),
                files=files[tid],
                reads=_uniq(_strs(item.get("reads"))),
                scenario_ids=scenario_ids,
                implements=contracts["implements"],
                consumes=contracts["consumes"],
                entrypoint=_bool(item.get("entrypoint")),
                seq=ticket_seq._replace(local=i),
            )
        )

    tested = {d for t in tickets if t.specialty is Specialty.TEST for d in declared[t.id]}
    # A demo feature ticket may bind its own scenarios from a test file it writes.
    tested |= {t.id for t in tickets if any(_is_python_test(f) for f in t.files)}
    concerns += [
        f"Ticket {t.id} has scenarios but neither a test ticket nor a test file of its own."
        for t in tickets
        if t.specialty is not Specialty.TEST and own[t.id] and t.id not in tested
    ]
    return tickets, ordered, concerns


def _build_adrs(raw: Any, seq: Seq, *, store: ArtifactStore | None) -> list[ADR]:
    return [
        ADR(
            id=_str(item.get("id"), f"ADR-{i:03d}"),
            title=_str(item.get("title")),
            decision=_str(item.get("decision")),
            body_uri=persist_prose(_str(item.get("body")), store=store),
            rejected_alternatives=_strs(item.get("rejected_alternatives")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def _contract_kind(item: dict[str, Any]) -> str:
    kind = _str(item.get("kind")).strip().lower()
    return kind if kind in CONTRACT_KINDS else ""


def _contract_text(item: dict[str, Any]) -> str:
    """A contract's body: kind + signature, description, inputs/outputs/errors.
    A contract given only as free-text ``spec`` keeps that text unchanged."""
    signature = _str(item.get("signature")).strip()
    kind = _contract_kind(item)
    lines = [f"[{kind}] {signature}" if kind else signature] if signature else []
    description = _str(item.get("description")).strip() or _str(item.get("spec"))
    if description:
        lines.append(description)
    for key in ("inputs", "outputs", "errors"):
        if values := _steps(item.get(key)):
            lines.append(f"{key}: " + "; ".join(values))
    return "\n".join(lines)


def _build_interfaces(
    raw: Any, seq: Seq, *, store: ArtifactStore | None = None
) -> list[InterfaceContract]:
    return [
        InterfaceContract(
            name=_str(item.get("name"), f"iface-{i}"),
            # Same handle as cas_uri(body); persisted so a targeted re-plan can
            # hand the contract text to the epics it re-runs.
            spec_uri=persist_prose(_contract_text(item), store=store),
            kind=_contract_kind(item),
            signature=_str(item.get("signature")).strip(),
            consumers=_strs(item.get("consumers")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]
