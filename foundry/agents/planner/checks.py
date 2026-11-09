"""In-node plan checks: what the Spec gate would reject, found before it runs."""

from __future__ import annotations

import builtins
import re
from collections.abc import Sequence
from typing import Any

from foundry.agents.plan_contracts import CompileResult, check_contracts
from foundry.agents.planner.readers import _str, _strs
from foundry.agents.planner.schema import _GENERATED_TICKETS, CODE_CONTRACT_KINDS
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
    """A contract the generated contracts ticket writes in full: a type, or a class
    with no methods (an error hierarchy, a plain record). One with methods still
    needs a ticket to implement its behaviour."""
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
                out.append(f"Ticket {t.id} consumes contract {name}, but no ticket implements it.")
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
    strict = stack is not None and "--strict" in stack.typecheck
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
        (r"^Contract \S.* does not compile", "contract-compile"),
        (r"more \S+ error\(s\)\.$", "contract-compile"),
        (r"^Scenario \S+ is not owned by a live ticket", "scenario-orphan"),
        (r"^Scenario \S+ covers unknown id", "scenario-unknown-cover"),
        (r"^Scenario \S+ asserts that installing dependencies", "scenario-offline-install"),
        (r"has no covering scenario", "goal-uncovered"),
        (r"^Feature ticket \S+ has no scenarios", "ticket-no-scenarios"),
        (r"^No entrypoint ticket", "entrypoint-missing"),
        (r"^Entrypoint ticket \S+ does not depend", "entrypoint-unwired"),
        (r"^stack\.run is empty", "run-missing"),
        (r"starts the server, which never exits", "run-server"),
        (r"^stack\.bdd ", "bdd-command"),
        (r"^Stack command file \S+ is written by no ticket", "stack-file-unowned"),
        (r"exceed the (per-epic )?cap of", "overflow"),
        (r"^criterion \S+: missing/empty", "criterion-incomplete"),
        (r"^criterion \S+: scenario has no", "scenario-steps-missing"),
        (r"security/abuse|security_abuse", "security-gap"),
    )
)


def finding_kind(finding: str) -> str:
    """A short stable slug for ``finding``'s template, e.g. ``contract-unimplemented``.
    The Spec gate's ``contract fit:`` / ``coverage:`` prefixes are looked through."""
    text = re.sub(r"^(contract fit|coverage): ", "", finding.strip())
    return next((kind for pattern, kind in _FINDING_KINDS if pattern.search(text)), "other")
