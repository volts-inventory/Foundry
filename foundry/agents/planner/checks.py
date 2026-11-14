"""In-node plan checks: what the Spec gate would reject, found before it runs."""

from __future__ import annotations

import builtins
import collections.abc
import re
import textwrap
import typing
from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from foundry.agents.plan_contracts import (
    CompileResult,
    check_contracts,
    top_symbols,
)
from foundry.agents.planner.readers import _str, _strs
from foundry.agents.planner.schema import _GENERATED_TICKETS, CODE_CONTRACT_KINDS
from foundry.agents.planner.sizing import Ceilings, over_ceiling
from foundry.agents.planner.spec_text import with_referenced
from foundry.base_tree import BaseView
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.implementation import BDD_REPORT_ENV, Specialty, StackDecision, Ticket
from foundry.schemas.product import AcceptanceCriterion, IntentBrief

# An indented member that takes arguments (`    def add(`, `  add(x: T): R;`); a
# constructor alone (an error class taking a message) is still a declaration.
_MEMBER_CALL = re.compile(
    r"^\s+(?:def\s+)?(?![\w<>\[\], ]*\b(?:__init__|constructor)\s*\()"
    r"[\w<>\[\], ]*\w\s*\(",
    re.MULTILINE,
)


def _declaration_only(contract: InterfaceContract) -> bool:
    """A contract the generated contracts ticket writes in full, as shipping code: a
    type, or a class with no methods besides ``__init__`` (an error, a plain record).
    A function, or a class with methods, is defined by the one ticket implementing
    it, at its own path: the contracts file never holds a stub of it."""
    if contract.kind == "type":
        return True
    return contract.kind == "class" and not _MEMBER_CALL.search(contract.signature)


def _contract_findings(
    tickets: Sequence[Ticket],
    criteria: Sequence[AcceptanceCriterion],
    declared: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """The plan-time contract fit check over the model's tickets: every consumed
    contract has exactly one implementing ticket and the consumer depends on it.
    ``declared`` names contracts the generated contracts ticket writes in full
    (``_declaration_only``): consuming one needs no implementing ticket."""
    work = [t for t in tickets if t.id not in _GENERATED_TICKETS]
    deps = {t.id: t.depends_on for t in tickets}
    producers: dict[str, list[str]] = {}
    for t in work:
        for name in t.implements:
            producers.setdefault(name, []).append(t.id)

    def ancestors(tid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(deps.get(tid, ()))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        return seen

    out = [
        f"Contract {name} is implemented by {len(ids)} tickets ({', '.join(ids)}); "
        "exactly one ticket may implement it."
        for name, ids in producers.items()
        if len(ids) > 1
    ]
    for t in work:
        for name in t.consumes:
            if name in t.implements:
                continue
            ids = producers.get(name, [])
            if not ids and name in declared:
                continue
            if not ids:
                out.append(
                    f"Ticket {t.id} consumes contract {name}, but no ticket implements it "
                    "(the contracts file holds only data types and errors: name the one "
                    "ticket that defines it in its own files)."
                )
            elif len(ids) == 1 and ids[0] not in ancestors(t.id):
                out.append(
                    f"Ticket {t.id} consumes contract {name} but does not depend on "
                    f"{ids[0]}, which implements it."
                )
    return tuple(out)


def _unused_contracts(
    tickets: Sequence[Ticket], criteria: Sequence[AcceptanceCriterion]
) -> list[str]:
    """Concerns, not findings: an implemented contract nothing consumes and no
    scenario on its implementer exercises is dead weight, not a gap."""
    producers: dict[str, list[str]] = {}
    consumed: set[str] = set()
    for t in tickets:
        if t.id in _GENERATED_TICKETS:
            continue
        for name in t.implements:
            producers.setdefault(name, []).append(t.id)
        consumed.update(n for n in t.consumes if n not in t.implements)
    with_scenarios = {c.ticket_id for c in criteria}
    return [
        f"Contract {name} (implemented by {', '.join(ids)}) has no consumer and no "
        "scenario on its implementing ticket."
        for name, ids in producers.items()
        if name not in consumed and not with_scenarios.intersection(ids)
    ]


# Builtin types a method name can shadow inside its class body, breaking every
# annotation there that uses the builtin (`def list(self) -> list[Book]`).
_BUILTIN_TYPES = frozenset(
    n for n in dir(builtins) if isinstance(getattr(builtins, n), type) and n.islower()
)


_PY_METHOD = re.compile(r"\bdef\s+([A-Za-z_]\w*)\s*\(")


def _builtin_method_findings(
    interfaces: Sequence[InterfaceContract], language: str
) -> tuple[str, ...]:
    """Python contract signatures that name a method after a builtin type the same
    signature also uses in an annotation: mypy then resolves the annotation to the
    method and rejects it. Re-planning renames the method (list_books, get_by_id)."""
    if language != "python":
        return ()
    out: list[str] = []
    for i in interfaces:
        if i.kind not in CODE_CONTRACT_KINDS:
            continue
        clashes = sorted(
            {
                name
                for name in _PY_METHOD.findall(i.signature)
                if name in _BUILTIN_TYPES
                and re.search(rf"(?:->|:)\s*(?:[\w.]+\[)*{name}\b", i.signature)
            }
        )
        if clashes:
            out.append(
                f"Contract {i.name} names method(s) {', '.join(clashes)} after a builtin "
                "its own annotations use, which the type checker rejects; rename them "
                "(e.g. list_books, get_by_id)."
            )
    return tuple(out)


# A constructor taking something besides the receiver.
_CTOR_ARGS = re.compile(r"\b(?:__init__|constructor)\s*\(\s*(?!(?:self\s*)?\))")

# A top-level (unindented) function: a factory beside the class.
_TOP_FUNCTION = re.compile(
    r"^(?:async\s+)?def\s+\w+\s*\(|^(?:export\s+)?(?:async\s+)?function\s+\w+",
    re.MULTILINE,
)
_UNINDENTED = re.compile(r"^\S", re.MULTILINE)
_RETURNS = re.compile(r"\)\s*(?:->|:)\s*([^\n{]*)")
_PY_BASES = re.compile(r"\bclass\s+\w+\s*\(([^)]*)\)")
_TS_BASES = re.compile(r"\b(?:extends|implements)\s+([\w.,\s]+?)\s*\{")


def _bases(signature: str) -> set[str]:
    found = _PY_BASES.findall(signature) + _TS_BASES.findall(signature)
    return {w for group in found for w in re.findall(r"\w+", group)}


def _top_functions(signature: str) -> list[str]:
    """Each top-level function of ``signature``, up to the next unindented line."""
    starts = [m.start() for m in _TOP_FUNCTION.finditer(signature)]
    out: list[str] = []
    for start in starts:
        nxt = _UNINDENTED.search(signature, start + 1)
        out.append(signature[start : nxt.start() if nxt else len(signature)])
    return out


def _builds(function: str, made: set[str], deps: Sequence[str]) -> bool:
    """A function is a factory for a class only if it returns the class (or a base
    of it) or takes one of the dependencies: a ``def main() -> None`` beside the
    class hands it nothing."""
    m = _RETURNS.search(function)
    params, returned = (function[: m.start()], m.group(1)) if m else (function, "")
    named = set(re.findall(r"\w+", returned))
    return bool(named & made) or any(re.search(rf"\b{re.escape(d)}\b", params) for d in deps)


def _wiring_findings(
    tickets: Sequence[Ticket], interfaces: Sequence[InterfaceContract]
) -> tuple[str, ...]:
    """An entrypoint ticket's class contract that composes other tickets' class
    contracts but declares no way to receive them: its stub is a bare class nothing
    can hand the store/services to, and the stub-signature guard rejects the
    factory the implementer reaches for. A constructor taking arguments, a factory
    function (in the signature or a function contract of the same ticket), or a
    signature naming the dependencies itself is enough."""
    by_name = {i.name: i for i in interfaces}

    def injected(name: str) -> bool:
        i = by_name.get(name) or by_name.get(name.partition(".")[0])
        return i is not None and i.kind == "class" and not _declaration_only(i)

    out: list[str] = []
    for t in tickets:
        if not t.entrypoint or t.id in _GENERATED_TICKETS:
            continue
        deps = sorted(
            {n.partition(".")[0] for n in t.consumes if n not in t.implements and injected(n)}
        )
        if not deps:
            continue
        ctor = any(n.endswith((".__init__", ".constructor")) for n in t.implements)
        functions = [
            f.signature
            for n in t.implements
            if (f := by_name.get(n)) is not None and f.kind == "function" and "." not in n
        ]
        for name in t.implements:
            i = by_name.get(name)
            if i is None or i.kind != "class" or _declaration_only(i) or ctor:
                continue
            sig = i.signature
            made = {name, *_bases(sig)}
            if (
                _CTOR_ARGS.search(sig)
                or any(_builds(f, made, deps) for f in (*functions, *_top_functions(sig)))
                or any(re.search(rf"\b{re.escape(d)}\b", sig) for d in deps)
            ):
                continue
            out.append(
                f"Contract {name} (entrypoint ticket {t.id}) composes {', '.join(deps)} "
                "but declares no way to receive them: give its signature an __init__ "
                "taking them."
            )
    return tuple(out)


_GOAL_ID = re.compile(r"(MH|SC)[-_ ]?0*(\d+)", re.IGNORECASE)


def _goals(raw: Any, prefix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(ids, texts)`` of the must-haves (``MH``) or success criteria (``SC``).

    An item is ``{id, text}`` or a bare string. A declared ``<prefix>-<n>`` id is
    normalised (``mh1`` -> ``MH-1``) and kept when unused; otherwise the item gets
    the first free positional ``<prefix>-<n>``, so ids are deterministic and the
    two lists never share one."""
    entries = raw if isinstance(raw, (list, tuple)) else _strs(raw)
    ids: list[str] = []
    texts: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            text = _str(item.get("text") or item.get("description")).strip()
            wanted = _str(item.get("id")).strip()
        else:
            text, wanted = ("" if item is None else str(item).strip()), ""
        if not text:
            continue
        m = _GOAL_ID.fullmatch(wanted)
        gid = f"{prefix}-{int(m.group(2))}" if m and m.group(1).upper() == prefix else ""
        if not gid or gid in ids:
            n = len(ids) + 1
            while f"{prefix}-{n}" in ids:
                n += 1
            gid = f"{prefix}-{n}"
        ids.append(gid)
        texts.append(text)
    return tuple(ids), tuple(texts)


def _goal_of(raw: str, known: dict[str, str]) -> str | None:
    """The goal id a scenario's ``covers`` entry names, or None."""
    key = raw.strip()
    m = _GOAL_ID.fullmatch(key)
    if m:
        key = f"{m.group(1).upper()}-{int(m.group(2))}"
    return known.get(key.lower())


def _goal_rows(brief: IntentBrief | None) -> list[tuple[str, str, str]]:
    """``(kind, id, text)`` of every must-have and success criterion."""
    if brief is None:
        return []
    return [
        *(("Must-have", g, t) for g, t in zip(brief.must_have_ids, brief.must_haves, strict=False)),
        *(
            ("Success criterion", g, t)
            for g, t in zip(brief.success_criterion_ids, brief.success_criteria, strict=False)
        ),
    ]


def _goals_text(brief: IntentBrief | None) -> str:
    if brief is None:
        return ""
    lines = [f"- {gid} ({kind.lower()}): {text}" for kind, gid, text in _goal_rows(brief)]
    return f"\nDeliverable: {brief.deliverable or 'app'}" + (
        "\nMUST-HAVES AND SUCCESS CRITERIA (cite these ids in scenario covers[]; each "
        "needs at least one covering scenario across the plan):\n" + "\n".join(lines)
        if lines
        else ""
    )


def _coverage_findings(
    tickets: Sequence[Ticket],
    criteria: Sequence[AcceptanceCriterion],
    brief: IntentBrief | None,
    *,
    run: str | None,
    change: bool = False,
) -> tuple[str, ...]:
    """The plan-time coverage check: every must-have and success criterion is
    covered by a scenario on a live ticket; every feature ticket has scenarios; a
    feature ticket marked ``entrypoint`` depends (transitively) on every other
    feature ticket; and an app-like deliverable has a ``run`` command (``run``
    None: not checked, e.g. an operator-pinned profile). A ``change`` build needs
    no entrypoint ticket: the base tree already wires the app."""
    goals = _goal_rows(brief)
    known = {g.lower(): g for _, g, _ in goals}
    live = {t.id for t in tickets}
    out: list[str] = []
    covered: set[str] = set()
    owned: set[str] = set()
    for c in criteria:
        if c.ticket_id not in live:
            out.append(
                f"Scenario {c.id} is not owned by a live ticket"
                + (f" ({c.ticket_id} is not in the plan)" if c.ticket_id else "")
                + "; put it on a ticket of the plan."
            )
            continue
        owned.add(c.ticket_id)
        for raw in c.covers:
            gid = _goal_of(raw, known)
            if gid is None:
                out.append(
                    f"Scenario {c.id} covers unknown id {raw!r}; cite must-have / "
                    "success-criterion ids ("
                    + (", ".join(g for _, g, _ in goals) or "none declared")
                    + ")."
                )
            else:
                covered.add(gid)
    out += [
        f"{kind} {gid} ({text[:80]!r}) has no covering scenario; add one whose covers lists {gid}."
        for kind, gid, text in goals
        if gid not in covered
    ]
    features = [
        t for t in tickets if t.id not in _GENERATED_TICKETS and t.specialty is not Specialty.TEST
    ]
    out += [
        f"Feature ticket {t.id} has no scenarios; give it at least one (or fold it "
        "into the ticket whose behaviour it serves)."
        for t in features
        if t.id not in owned
    ]
    entries = [t for t in features if t.entrypoint]
    if features and not entries and not change:
        out.append(
            "No entrypoint ticket: mark one feature ticket entrypoint=true that wires the "
            "app together end to end and depends_on every other feature ticket."
        )
    deps = {t.id: t.depends_on for t in tickets}
    gaps: dict[str, list[str]] = {}
    for e in entries:
        seen: set[str] = set()
        todo = list(deps[e.id])
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        gaps[e.id] = [t.id for t in features if t.id != e.id and t.id not in seen]
    if gaps and all(gaps.values()):
        out += [
            f"Entrypoint ticket {tid} does not depend (directly or transitively) on "
            f"feature ticket(s) {', '.join(missing)}; it must wire every feature in."
            for tid, missing in gaps.items()
        ]
    deliverable = brief.deliverable if brief is not None else ""
    if run is not None and not run.strip() and deliverable != "library":
        out.append(
            "stack.run is empty but the deliverable is "
            f"{deliverable or 'an app (undeclared)'}: give a run command that boots the "
            "real app headless and exits 0, or declare deliverable 'library' if nothing runs."
        )
    return tuple(out)


# "X or Y" naming alternatives; "one or more", "and/or" and "more or less" do not.
_BRANCHING_OR = re.compile(
    r"(?<!and/)(?<!more )\bor\b(?!\s+(?:more|less|fewer|greater|so|later|equal)\b)", re.I
)


def _branch_findings(
    criteria: Sequence[AcceptanceCriterion], brief: IntentBrief | None
) -> tuple[str, ...]:
    """A must-have or success criterion naming alternatives ("by date or author") that
    a single scenario covers: the other branch is never executed."""
    goals = _goal_rows(brief)
    known = {g.lower(): g for _, g, _ in goals}
    covering: dict[str, list[str]] = {}
    for c in criteria:
        for raw in c.covers:
            if (gid := _goal_of(raw, known)) is not None:
                covering.setdefault(gid, []).append(c.id)
    return tuple(
        f'{kind} {gid} ({text[:80]!r}) names alternatives ("or") but only scenario '
        f"{covering[gid][0]} covers it: add one scenario per branch, each citing {gid}."
        for kind, gid, text in goals
        if _BRANCHING_OR.search(text) and len(covering.get(gid, ())) == 1
    )


# An example input of a notation the request names: `notation like 2d6+3`,
# `expressions such as (1+2)*3`. Users type such notations with spaces and in
# either case, so a plan whose scenarios only ever feed the exact example accepts
# one spelling.
_NOTATION_EXAMPLE = re.compile(
    r"\b(?:notation|expressions?|syntax|formulas?|queries|query)\s+"
    r"(?:like|such as|e\.g\.,?|for example,?)\s+[`'\"]?([^\s`'\",;]+)"
)
_EXAMPLE_PARTS = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9\s]")


def notation_examples(request: str) -> list[tuple[str, list[str]]]:
    """(example, its parts) of each notation example input ``request`` shows that has
    letters and at least two parts, so spacing and letter case can vary it."""
    out: list[tuple[str, list[str]]] = []
    for example in dict.fromkeys(_NOTATION_EXAMPLE.findall(request)):
        example = example.rstrip(".")
        parts = _EXAMPLE_PARTS.findall(example)
        if len(parts) >= 2 and re.search(r"[A-Za-z]", example):
            out.append((example, parts))
    return out


def missing_variants(
    criteria: Sequence[AcceptanceCriterion], example: str, parts: Sequence[str]
) -> list[tuple[str, str]]:
    """("spaces" | "case", a variant of ``example``) for each variation no scenario
    feeds it in."""
    loose = re.compile(r"\s*".join(map(re.escape, parts)), re.I)
    seen = [m[0] for c in criteria for m in loose.finditer(c.text)]
    out: list[tuple[str, str]] = []
    if not any(re.search(r"\s", v) for v in seen):
        out.append(("spaces", " ".join(parts)))
    if not any(
        v.replace(" ", "") != example and v.lower().replace(" ", "") == example.lower()
        for v in seen
    ):
        out.append(("case", example.swapcase()))
    return out


_VARIANT_WHAT = {"spaces": "with spaces", "case": "in the other letter case"}


def _variant_findings(criteria: Sequence[AcceptanceCriterion], request: str) -> tuple[str, ...]:
    """A request showing a notation's example input whose scenarios never feed it
    with spaces around its symbols, or in the other letter case: the plan accepts
    only the spelling the request happened to show."""
    return tuple(
        f"No scenario feeds the example input {example!r} {_VARIANT_WHAT[kind]} "
        f"(e.g. {variant!r}): add one where it gets the same answer, per the "
        "CONVENTIONS rule."
        for example, parts in notation_examples(request)
        for kind, variant in missing_variants(criteria, example, parts)
    )


_ROLE_WORDS = re.compile(r"\b(admins?|administrators?|roles?|moderators?|superusers?)\b", re.I)
_ROLE_OWNER = re.compile(r"\b(roles?|admins?|administrators?|seed(?:s|ed|ing)?)\b", re.I)


def _role_findings(tickets: Sequence[Ticket], brief: IntentBrief | None) -> tuple[str, ...]:
    """A request with roles needs ONE ticket owning the roles, the admin account and
    the seed data: left to every feature, each invents its own, or none does."""
    if brief is None:
        return ()
    goals = " ".join((brief.intent, *brief.must_haves, *brief.success_criteria))
    if not _ROLE_WORDS.search(goals):
        return ()
    if any(
        _ROLE_OWNER.search(" ".join((t.id, t.title, t.description)))
        for t in tickets
        if t.id not in _GENERATED_TICKETS and t.specialty is not Specialty.TEST
    ):
        return ()
    return (
        "The request has user roles but no ticket owns the roles, the admin account and "
        "the seed data: give exactly one ticket that work and say so in its description.",
    )


def _entrypoint_findings(
    tickets: Sequence[Ticket], interfaces: Sequence[InterfaceContract]
) -> tuple[str, ...]:
    """An entrypoint ticket whose contract signatures declare a function or class a
    dependency's contract declares: it must import and wire those, never
    re-implement them (a second copy of the routes ships as dead code). A shared
    file is no finding: overlapping scopes run in separate waves (a split ticket's
    children share their parent's files); the post-build check reads the code."""
    by_name = {i.name: i for i in interfaces}
    by_id = {t.id: t for t in tickets}
    deps = {t.id: t.depends_on for t in tickets}

    def ancestors(tid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(deps.get(tid, ()))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        return seen

    def declared(t: Ticket) -> set[str]:
        return {
            n
            for name in t.implements
            if (i := by_name.get(name)) is not None and i.kind in CODE_CONTRACT_KINDS
            for n in top_symbols(textwrap.dedent(i.signature))
        }

    out: list[str] = []
    for e in tickets:
        if not e.entrypoint or e.id in _GENERATED_TICKETS:
            continue
        mine = declared(e)
        for did in sorted(ancestors(e.id) - _GENERATED_TICKETS):
            d = by_id.get(did)
            if d is None:
                continue
            if twice := sorted(mine & declared(d)):
                out.append(
                    f"Entrypoint ticket {e.id} redefines what ticket {did} owns ("
                    + ", ".join(twice)
                    + f"): import and wire {did}'s modules instead of re-implementing "
                    "them, and drop them from the entrypoint's contracts."
                )
    return tuple(out)


_INSTALL_ASSERTION = re.compile(
    r"\b(pip3?\s+install|npm\s+(install|ci)|yarn\s+install|pnpm\s+install|"
    r"uv\s+(pip\s+install|sync)|poetry\s+install|cargo\s+fetch|go\s+mod\s+download)\b"
    r"|\binstall(s|ed|ing|ation)?\s+(of\s+)?(\w+\s+){0,2}"
    r"(succeed|succeeds|work|works|complete|completes|without\s+error)\b",
    re.I,
)


def _offline_scenario_findings(criteria: Sequence[AcceptanceCriterion]) -> tuple[str, ...]:
    """A scenario whose When/Then asserts that installing dependencies works can
    never pass: every gate after install runs offline."""
    return tuple(
        f"Scenario {c.id} asserts that installing dependencies succeeds, but the gates "
        "run offline: check the manifest itself (it exists, every entry is pinned) "
        "instead."
        for c in criteria
        if any(_INSTALL_ASSERTION.search(s) for s in (*c.when, *c.then))
    )


_BARE_KEY = re.compile(r"^[\"']?[A-Za-z_][\w-]*[\"']?\s*:$")
# "placeholder" in an identifier or as the step's last word; an HTML input's
# placeholder text ("the box shows the placeholder 'Search'") is a real step.
_PLACEHOLDER = re.compile(r"(?i)[a-z0-9]_placeholder|placeholder_[a-z0-9]|\bplaceholder\W*$")


def _garbled(step: str) -> bool:
    s = step.strip()
    return (
        s.endswith((",", "[", "{"))
        or s.count("[") != s.count("]")
        or s.count("{") != s.count("}")
        or bool(_BARE_KEY.match(s))
        or bool(_PLACEHOLDER.search(s))
    )


def _garbled_step_findings(criteria: Sequence[AcceptanceCriterion]) -> tuple[str, ...]:
    """A step that is a fragment of the reply's JSON (ends in a comma or an open
    bracket, unbalanced brackets, a bare ``key:``) or names a placeholder: a local
    planner once emitted ``...'Hello world'],`` then
    ``criticaality_placeholder_ignore_me_note_2:`` as steps, and both shipped to the
    feature file."""
    out: list[str] = []
    for c in criteria:
        bad = [s for s in (*c.given, *c.when, *c.then) if _garbled(s)]
        if bad:
            out.append(
                f"Scenario {c.id} has step text that is not a step: "
                + "; ".join(repr(s) for s in bad)
                + ". Write each step as one plain phrase: no JSON punctuation, keys or "
                "placeholders."
            )
    return tuple(out)


def _size_findings(
    tickets: Sequence[Ticket], ceilings: Ceilings, protected: Collection[str] = ()
) -> tuple[str, ...]:
    """A ticket over the size ceiling of the tier its difficulty runs on: one
    session builds it, and a weaker (local) model loses the thread of a big one.
    The entrypoint is exempt: the wiring and templates must live there, and no
    split or fold can shrink it."""
    out: list[str] = []
    for t in tickets:
        if t.id in _GENERATED_TICKETS or t.entrypoint:
            continue
        ceiling = ceilings[t.difficulty]
        size = over_ceiling(t, ceiling, protected)
        if size is not None:
            out.append(
                f"Ticket {t.id} is over its size ceiling: {size[0]} work files, {size[1]} "
                f"files in all and {size[2]} scenarios, where a {t.difficulty.value} ticket "
                f"(built on a {ceiling.where} model) takes at most {ceiling.files} work "
                f"files, {ceiling.total} in all and {ceiling.scenarios} scenarios: split it "
                "into tickets each within the ceiling."
            )
    return tuple(out)


# Web frameworks whose app entry serves until killed.
_WEB_FRAMEWORK = re.compile(
    r"\b(express|fastify|koa|hapi|nest(js)?|next(\.js)?|hono|flask|fastapi|django|"
    r"starlette|aiohttp|sanic|bottle|tornado|quart|litestar)\b",
    re.I,
)


# Commands that always start a server.
_SERVER_COMMAND = re.compile(
    r"^((python3?\s+-m\s+)?(flask\s+run|uvicorn|gunicorn|hypercorn|fastapi\s+(run|dev))\b"
    r"|python3?\s+manage\.py\s+runserver\b)"
)


# A bare app entry: an interpreter on a server-named file, or the package's start.
_APP_ENTRY = re.compile(
    r"^((node|nodejs|bun|deno\s+run|npx\s+(tsx|ts-node)|tsx|ts-node|python3?)\s+"
    r"(\S+/)?(app|server|index|main|wsgi|asgi)\.(js|mjs|cjs|ts|py)"
    r"|npm\s+(start|run\s+(start|dev|serve))|yarn\s+(start|dev)|pnpm\s+(start|dev))$"
)


def _run_findings(stack: StackDecision) -> tuple[str, ...]:
    """A web stack whose run command is the bare server entry: the smoke gate waits
    for it to exit, and a server never does."""
    run = " ".join(stack.run.split())
    if not (
        _SERVER_COMMAND.match(run)
        or (_APP_ENTRY.match(run) and _WEB_FRAMEWORK.search(stack.framework))
    ):
        return ()
    return (
        f"stack.run `{stack.run}` starts the server, which never exits, so the smoke "
        "check hangs: make run a smoke script (e.g. `node smoke.js`, `python3 smoke.py`, "
        "in the entrypoint ticket's files) that starts the app on port 0, makes one "
        "request, stops it and exits 0.",
    )


def _web_stack(stack: StackDecision | None) -> bool:
    return stack is not None and (
        stack.kind == "http" or bool(_WEB_FRAMEWORK.search(stack.framework))
    )


# ONE database connection, not a pool, engine or factory: sqlite3.Connection,
# psycopg.Connection, java.sql.Connection, rusqlite::Connection, Go's *sql.Conn.
_DB_CONN = r"(?:&\s*(?:mut\s+)?|\*)?(?:\w+(?:\.|::))*(?:Connection|Conn)"
_ANNOTATED_CONN = re.compile(
    rf"^(?:(?:private|public|protected|readonly)\s+)*[\w$]+\??\s*:\s*(?:Optional\[\s*{_DB_CONN}\s*\]|{_DB_CONN})"
    r"\s*(?:\|\s*None\s*)?(?:=.*)?,?$"
)
# `Connection conn` (Java, C#) or `conn *sql.Conn` (Go).
_TYPED_CONN = re.compile(rf"^(?:final\s+)?{_DB_CONN}\s+\w+$|^\w+\s+{_DB_CONN}$")
_CTOR = re.compile(r"\b(?:__init__|constructor|fn\s+new|new)\s*\(")
_APP_FACTORY = re.compile(r"(?i)^(?:create|make|build|new)_?app$|^app_?factory$")
_FUNC_NAME = re.compile(r"\b(?:def|function|fn|func)\s+(\w+)\s*\(")
_FIELD = re.compile(r"^\s+([\w$]+\??\s*:[^(\n]*)$", re.MULTILINE)
_MODULE_VAR = re.compile(r"^(?:export\s+)?(?:(?:const|let|var|static)\s+)?([\w$]+\s*:[^(\n]*)$")


def _is_conn(param: str) -> bool:
    p = " ".join(param.split())
    return bool(_ANNOTATED_CONN.match(p) or _TYPED_CONN.match(p))


def _param_lists(signature: str, opener: re.Pattern[str]) -> list[list[str]]:
    """The parameters of each call ``opener`` matches (up to its open paren)."""
    out: list[list[str]] = []
    for m in opener.finditer(signature):
        depth = 0
        for i in range(m.end(), len(signature)):
            ch = signature[i]
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>" and depth:
                depth -= 1
            elif ch == ")":
                out.append([p for p in _split_top(signature[m.end() : i]) if p.strip()])
                break
    return out


def _held_connection_findings(
    stack: StackDecision | None,
    tickets: Sequence[Ticket],
    interfaces: Sequence[InterfaceContract],
) -> tuple[str, ...]:
    """A web app's contract that keeps ONE database connection for the app's life: a
    class or record constructed with (or holding) one, the app factory or an
    entrypoint function taking one, or a module-level one. Concurrent requests then
    share its one transaction (3 of 30 parallel POSTs failed: InterfaceError, 'no
    transaction active'). A function taking the connection per call is fine, as is
    a factory, pool, engine or database path: only a bare connection type counts."""
    if not _web_stack(stack):
        return ()
    entry = {n for t in tickets if t.entrypoint for n in t.implements}
    out: list[str] = []
    for c in interfaces:
        if c.kind not in CODE_CONTRACT_KINDS:
            continue
        sig = textwrap.dedent(c.signature)
        # A Java/C# constructor is named after its class.
        cls = re.escape(c.name.rpartition(".")[2])
        ctor = re.compile(rf"{_CTOR.pattern}|\b{cls}\s*\(") if c.kind == "class" else _CTOR
        candidates = [p for ps in _param_lists(sig, ctor) for p in ps]
        candidates += [m.group(1) for ln in sig.splitlines() if (m := _MODULE_VAR.match(ln))]
        if c.kind in ("class", "type"):
            candidates += _FIELD.findall(sig)
        names = {c.name, *_FUNC_NAME.findall(sig)}
        if c.kind == "function" and (c.name in entry or any(map(_APP_FACTORY.match, names))):
            candidates += _params(sig)
        if held := [h for h in candidates if _is_conn(h)]:
            shown = ", ".join(sorted({" ".join(h.split()).rstrip(",") for h in held}))
            out.append(
                f"Contract {c.name} holds one database connection for the web app's "
                f"lifetime (`{shown}`): concurrent requests then share its one "
                "transaction and fail. Take a connection per call, or a connection "
                "factory or database path, and open one connection per request (closed "
                "at request teardown) or per thread."
            )
    return tuple(out)


_BDD_REPORT_SINGLE_QUOTED = re.compile(
    r"(?<![^\s=:])'([^'\"]*\$\{?" + BDD_REPORT_ENV + r"\}?[^'\"]*)'"
)


_CUCUMBER_JS = re.compile(r"\bcucumber-js\b")


_TS_LOADER = re.compile(r"--(import|require|require-module|loader)[\s=]+(tsx|ts-node)\b")


def _bdd_quoting(bdd: str) -> str:
    """Single quotes keep the shell from expanding $FOUNDRY_BDD_REPORT: the runner
    then writes to a file literally named so and the gate finds no report."""
    return _BDD_REPORT_SINGLE_QUOTED.sub(r'"\1"', bdd)


def _bdd_findings(stack: StackDecision) -> tuple[str, ...]:
    """cucumber-js bdd commands the gate cannot read a report from."""
    if not _CUCUMBER_JS.search(stack.bdd):
        return ()
    out: list[str] = []
    if re.search(r"(--format|-f)[\s=]+[\"']?json:", stack.bdd):
        out.append(
            f"stack.bdd `{stack.bdd}` writes the legacy `json:` report; the gate reads "
            f'Cucumber Messages: use `--format "message:${BDD_REPORT_ENV}"`.'
        )
    if re.search(r"\.ts\b", stack.bdd) and not _TS_LOADER.search(stack.bdd):
        out.append(
            f"stack.bdd `{stack.bdd}` loads TypeScript step files without a loader: add "
            "`--import tsx` (tsx as a devDependency) or `--require-module "
            "ts-node/register`."
        )
    return tuple(out)


def _stack_command_findings(stack: StackDecision | None) -> tuple[str, ...]:
    """Plan-time checks of a stack the planner chose this pass (a pinned one cannot
    be re-chosen, so flagging it would never close)."""
    if stack is None or stack.profile:
        return ()
    return (*_run_findings(stack), *_bdd_findings(stack))


# Capitalised names a signature uses that no plan declares: builtins, the standard
# library and the usual framework types, across the languages. A name missing here
# that two tickets both use FAILs the plan, so the list leans long.
_KNOWN_TYPES = frozenset(
    {
        *(n for n in dir(builtins) if n[:1].isupper()),
        *(n for n in dir(typing) if n[:1].isupper()),
        *(n for n in dir(collections.abc) if n[:1].isupper()),
        # Python stdlib and frameworks
        "Random", "Path", "PurePath", "PurePosixPath", "Decimal", "Fraction", "UUID",
        "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ABC", "ABCMeta", "TextIO",
        "BinaryIO", "IO", "StringIO", "BytesIO", "Logger", "Thread", "Lock", "RLock",
        "Queue", "Counter", "OrderedDict", "ChainMap", "Namespace",
        "ArgumentParser", "Connection", "Cursor", "Engine", "Session",
        "BaseModel", "Field", "FastAPI", "APIRouter", "Depends", "HTTPException",
        "Request", "Response", "JSONResponse", "HTMLResponse", "Flask", "Blueprint",
        "BaseHTTPRequestHandler", "HTTPServer", "ThreadingHTTPServer", "Template",
        "Jinja2Templates", "TestClient", "Mock", "MagicMock", "Pattern", "Match",
        "Executor", "Future", "Popen", "CompletedProcess", "Dialect",
        "ElementTree", "Cookie", "SimpleCookie", "Headers",
        # TypeScript / JavaScript globals and frameworks
        "Promise", "Array", "ReadonlyArray", "Record", "Partial", "Required", "Readonly",
        "Pick", "Omit", "Exclude", "Extract", "NonNullable", "ReturnType", "Parameters",
        "InstanceType", "Awaited", "Map", "Set", "WeakMap", "WeakSet", "Date", "Error",
        "TypeError", "RangeError", "SyntaxError", "RegExp", "Function", "Object",
        "String", "Number", "Boolean", "Symbol", "BigInt", "JSON", "Math", "Uint8Array",
        "ArrayBuffer", "Buffer", "Blob", "File", "FormData", "URL", "URLSearchParams",
        "AbortSignal", "AbortController", "EventEmitter", "ReadableStream", "Readable",
        "Writable", "Express", "Router", "NextFunction", "RequestHandler", "Application",
        "Server", "IncomingMessage", "ServerResponse", "Database", "Statement",
        "HTMLElement", "HTMLInputElement", "Document", "Window",
        "AsyncIterable", "AsyncIterableIterator", "AsyncGenerator", "Generator",
        "PropertyKey", "Console", "Storage", "Body",
        # Java / Kotlin / C# / Rust
        "Integer", "Long", "Double", "Float", "Short", "Byte", "Character", "Void",
        "List", "ArrayList", "LinkedList", "HashMap", "TreeMap", "HashSet", "TreeSet",
        "Optional", "Stream", "Collection", "Iterable", "Iterator", "Comparable",
        "Exception", "RuntimeException", "IllegalArgumentException", "Throwable",
        "Vec", "Box", "Rc", "Arc", "RefCell", "Cell", "Mutex", "RwLock", "Option",
        "Result", "Ok", "Err", "Some", "None", "Self", "BTreeMap", "Cow",
        "Duration", "Instant", "SystemTime", "PathBuf", "Display", "Debug", "Clone",
        "Default", "Serialize", "Deserialize", "Send", "Sync", "Sized", "Into", "From",
        "IEnumerable", "IList", "Dictionary", "Guid", "DateTime", "TimeSpan", "Int32",
        "Int64", "Unit", "Any", "Nothing", "MutableList", "MutableMap", "Sequence",
    }
)  # fmt: skip
_TYPE_NAME = re.compile(r"(?<![\w.$])([A-Z][A-Za-z0-9_]*)\b(?!\s*\.)")
_STRING = re.compile(r"\"\"\".*?\"\"\"|'''.*?'''|\"[^\"\n]*\"|'[^'\n]*'|`[^`]*`", re.DOTALL)
_COMMENT = re.compile(r"#[^\n]*|//[^\n]*|/\*.*?\*/", re.DOTALL)
_LITERAL = re.compile(r"\bLiteral\[[^\]]*\]")
_FORWARD_REF = re.compile(r"""^["']([A-Za-z_]\w*)["']$""")
_IMPORTED = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+\(?([^)\n]+)|import\s+(?:type\s+)?\{([^}]+)\})",
    re.MULTILINE,
)
_PY_ALIAS = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*\w+\s*)?=", re.MULTILINE)


def _type_names(signature: str) -> set[str]:
    """The capitalised, non-builtin type names ``signature`` uses: never its strings
    (bar a quoted forward reference), comments, ``Literal`` values, an attribute or
    module-qualified head, an ALL-CAPS constant or a one-letter type parameter."""
    imported = {
        n.split(" as ")[-1].strip()
        for m in _IMPORTED.finditer(signature)
        for n in (m.group(1) or m.group(2)).split(",")
    }
    text = _COMMENT.sub(" ", _LITERAL.sub(" ", signature))
    text = _STRING.sub(
        lambda m: f" {ref.group(1)} " if (ref := _FORWARD_REF.match(m.group())) else " ", text
    )
    return {
        n
        for n in _TYPE_NAME.findall(text)
        if len(n) > 1 and not n.isupper() and n not in _KNOWN_TYPES and n not in imported
    }


def _defined_names(signature: str) -> set[str]:
    return top_symbols(signature) | set(_PY_ALIAS.findall(signature))


def _shared_type_users(
    tickets: Sequence[Ticket],
    interfaces: Sequence[InterfaceContract],
    declared: Collection[str],
) -> dict[str, list[str]]:
    return {
        name: ids
        for name, ids in _undeclared_type_users(tickets, interfaces, declared).items()
        if len(ids) > 1
    }


def _undeclared_type_users(
    tickets: Sequence[Ticket],
    interfaces: Sequence[InterfaceContract],
    declared: Collection[str],
) -> dict[str, list[str]]:
    """A data type tickets' contracts name that the contracts file does not
    declare -> those tickets, in plan order; with two (``_shared_type_users``) each
    would write its own copy (a parser's and an evaluator's ``Expression``) and the
    values of one are not the other. A name is accounted for when the contracts
    file declares it (``declared``) or any contract defines it: by name, or in its
    signature (a class one ticket implements, imported from there; a
    declaration-only signature's extra records)."""
    by_name = {i.name: i for i in interfaces}
    known = set(declared)
    for i in interfaces:
        known |= {i.name.partition(".")[0], *_defined_names(i.signature)}
    users: dict[str, list[str]] = {}
    for t in tickets:
        if t.id in _GENERATED_TICKETS:
            continue
        names: set[str] = set()
        for n in (*t.implements, *t.consumes):
            c = by_name.get(n)
            if c is not None and c.kind in CODE_CONTRACT_KINDS and not _declaration_only(c):
                names |= _type_names(c.signature)
        for name in sorted(names - known):
            users.setdefault(name, []).append(t.id)
    return users


def _shared_type_findings(
    tickets: Sequence[Ticket],
    interfaces: Sequence[InterfaceContract],
    declared: Collection[str],
    owned: Collection[str] = (),
) -> tuple[str, ...]:
    """The shared undeclared types not in ``owned``, the ones
    :func:`folds._assign_shared_types` gave an owner ticket."""
    return tuple(
        f"Type {name} is used by the contracts of tickets {', '.join(ids)} but is not "
        "declared in the contracts file: each ticket would define its own copy. Add a "
        f"type contract {name} (its fields) so every ticket imports the one definition."
        for name, ids in _shared_type_users(tickets, interfaces, declared).items()
        if name not in owned
    )


def _type_contract_findings(
    tickets: Sequence[Ticket],
    interfaces: Sequence[InterfaceContract],
    declared: Collection[str],
    types: Mapping[str, str],
    contracts_file: str,
    design: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(findings, concerns): the design names the contracts file and defines data
    types for it that tickets' contracts use, yet no contract declares a data type,
    so no contracts ticket writes them. Blocking when two or more tickets use them
    (each would invent its own fields); with one, the shared-type fold's field list
    covers it and this is a concern."""
    if (
        not contracts_file
        or not types
        or any(i.kind in CODE_CONTRACT_KINDS and _declaration_only(i) for i in interfaces)
        or (contracts_file not in design and PurePosixPath(contracts_file).name not in design)
    ):
        return (), ()
    users = _undeclared_type_users(tickets, interfaces, declared)
    names = with_referenced((n for n in types if n in users), types)
    if not names:
        return (), ()
    ids = {i for n in names for i in users.get(n, ())}
    text = (
        f"The design defines {', '.join(names)} for {contracts_file}, but no interface "
        "contract declares them: add a kind=type contract for each, with its fields, so "
        "the contracts ticket writes them once and every ticket imports them."
    )
    return ((text,), ()) if len(ids) > 1 else ((), (text,))


def _redeclared_findings(
    interfaces: Sequence[InterfaceContract], base: BaseView
) -> tuple[str, ...]:
    """A change consumes the base tree's contracts; redeclaring one forks it."""
    existing = base.contract_names
    return tuple(
        f"Contract {i.name} already exists in the base tree: drop it from interfaces "
        "and consume it by name instead of redeclaring it."
        for i in interfaces
        if i.name in existing
    )


def _compile(
    interfaces: Sequence[InterfaceContract],
    language: str,
    stack: StackDecision | None,
    base: BaseView | None = None,
) -> CompileResult:
    code = [(i.name, i.signature) for i in interfaces if i.kind in CODE_CONTRACT_KINDS]
    if base is not None:
        # New contracts may reference the base's: compile them together.
        new = {name for name, _ in code}
        code = [
            (c["name"], c["signature"])
            for c in base.contracts
            if c.get("kind") in CODE_CONTRACT_KINDS and c["name"] not in new
        ] + code
    strict = stack is not None and "--strict" in stack.typecheck.split()
    return check_contracts(code, language, strict=strict)


# Telemetry slugs for the finding templates above (and the Spec gate's own), most
# specific first; a finding none of them matches counts as "other".
_FINDING_KINDS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), kind)
    for p, kind in (
        (r"^Contract \S.* is implemented by \d+ tickets", "contract-multi-impl"),
        (r"consumes contract .*, but no ticket implements it", "contract-unimplemented"),
        (r"consumes contract .* but does not depend on", "contract-dep-missing"),
        (r"after a builtin its own annotations use", "contract-builtin-method"),
        (r"already exists in the base tree", "contract-redeclared"),
        (r"^Type \S+ is used by the contracts of tickets", "type-undeclared"),
        (r"^The design defines .* but no interface contract declares", "type-contract-missing"),
        (r"^Scenario \S+ uses .* but pinned constraint", "scenario-constraint-conflict"),
        (r"declares no way to receive them", "contract-unwired"),
        (r"^Contract \S.* does not compile", "contract-compile"),
        (r"more \S+ error\(s\)\.$", "contract-compile"),
        (r"^Scenario \S+ is not owned by a live ticket", "scenario-orphan"),
        (r"^Scenario \S+ covers unknown id", "scenario-unknown-cover"),
        (r"^Scenario \S+ asserts that installing dependencies", "scenario-offline-install"),
        (r"^Scenario \S+ has step text that is not a step", "scenario-garbled-step"),
        (r"has no covering scenario", "goal-uncovered"),
        (r"^Feature ticket \S+ has no scenarios", "ticket-no-scenarios"),
        (r"^Ticket \S+ is over its size ceiling", "ticket-oversized"),
        (r"^No entrypoint ticket", "entrypoint-missing"),
        (r"^Entrypoint ticket \S+ does not depend", "entrypoint-unwired"),
        (r"^Entrypoint ticket \S+ redefines what", "entrypoint-redefines"),
        (r"names alternatives .* but only scenario", "or-branch"),
        (r"^No scenario feeds the example input", "notation-variant"),
        (r"has user roles but no ticket owns", "roles-unowned"),
        (r"^stack\.run is empty", "run-missing"),
        (r"starts the server, which never exits", "run-server"),
        (r"holds one database connection for the web app", "db-shared-connection"),
        (r"^stack\.bdd ", "bdd-command"),
        (r"^Stack command file \S+ is written by no ticket", "stack-file-unowned"),
        (r"exceed the (per-epic )?cap of", "overflow"),
        (r"^criterion \S+: missing/empty", "criterion-incomplete"),
        (r"^criterion \S+: scenario has no", "scenario-steps-missing"),
        (r"but no contract in the plan has properties", "contract-no-properties"),
        (r"security/abuse|security_abuse", "security-gap"),
    )
)


def finding_kind(finding: str) -> str:
    """A short stable slug for ``finding``'s template, e.g. ``contract-unimplemented``.
    The Spec gate's ``contract fit:`` / ``coverage:`` prefixes are looked through."""
    text = re.sub(r"^(contract fit|coverage): ", "", finding.strip())
    return next((kind for pattern, kind in _FINDING_KINDS if pattern.search(text)), "other")


# A parameter of a parsed or computed-on kind: text, bytes, a number, a collection.
_DOMAIN_TYPE = re.compile(
    r"(?i)(?<![\w.])(?:&?str|string|bytes|bytearray|int|float|decimal|number|double|long"
    r"|bigint|[iu](?:8|16|32|64|128|size)|f32|f64|list|tuple|set|frozenset|dict|sequence"
    r"|iterable|mapping|vec|array|map|slice)\b|\[\]"
)
_SELF_PARAM = re.compile(r"^\s*(?:&\s*(?:mut\s+)?)?(?:self|cls|this)\b")


def _params(signature: str) -> list[str]:
    """Every parameter of every call signature in ``signature``: the depth-0
    comma-separated parts of a parenthesis opened right after a name (a return
    tuple, ``-> (A, B)``, is no parameter list)."""
    out: list[str] = []
    for m in re.finditer(r"\w\s*\(", signature):
        depth, start = 0, m.end()
        for i in range(start, len(signature)):
            ch = signature[i]
            if ch in "([{<":
                depth += 1
            elif ch in ")]}>" and depth:
                depth -= 1
            elif ch == ")":
                out += [p for p in _split_top(signature[start:i]) if p.strip()]
                break
    return out


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [*parts, cur]


def _property_findings(
    tickets: Sequence[Ticket], interfaces: Sequence[InterfaceContract]
) -> tuple[str, ...]:
    """A plan with no contract property at all while a free function some feature
    ticket implements takes text, bytes, a number or a collection (what a parser or
    calculator computes on): one finding per such function. Conservative on purpose,
    since it costs a repair call: a method (a store's CRUD), a class and the
    entrypoint's functions (main(argv), create_app(config)) are never judged, and
    any property in the plan silences it, so a repair adding one never loops."""
    if any(c.properties for c in interfaces):
        return ()
    implemented = {n for t in tickets if not t.entrypoint for n in t.implements}
    entry = {n for t in tickets if t.entrypoint for n in t.implements}
    out: list[str] = []
    for c in interfaces:
        if c.kind != "function" or "." in c.name or c.name not in implemented - entry:
            continue
        params = _params(c.signature)
        if any(_SELF_PARAM.match(p) for p in params):
            continue
        if args := sorted({p.strip() for p in params if _DOMAIN_TYPE.search(p)}):
            out.append(
                f"Contract {c.name} takes {', '.join(args)} but no contract in the plan "
                "has properties[]: give it 1-4 properties (PR1, ...) that hold for every "
                "valid generated input, over combinations of its input's features (several "
                "options, fields or operators together), not one case; its implementing ticket "
                "proves each with a property-based test."
            )
    return tuple(out)
