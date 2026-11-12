"""Scenario/test-binding folds and dependency wiring over the built tickets."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from foundry.agents.planner.checks import _goal_of, _goal_rows
from foundry.agents.planner.readers import _uniq
from foundry.agents.planner.schema import _EPIC_OF_TICKET, _GENERATED_TICKETS
from foundry.agents.planner.sizing import Ceilings, over_ceiling
from foundry.gates.bdd_scaffold import is_helper_module, step_module, ticket_step_module
from foundry.gates.profiles import pytest_bdd_binds
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    Difficulty,
    Specialty,
    StackDecision,
    Ticket,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion, Criticality, IntentBrief, render_scenario

if TYPE_CHECKING:
    from foundry.state.seq import Seq

# Script files a stack command can name as its own repo file (`python3 smoke.py`).
_SCRIPT_SUFFIXES = (
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".mts",
    ".sh",
    ".rb",
    ".lua",
    ".pl",
    ".php",
)


def stack_files(stack: StackDecision) -> tuple[str, ...]:
    """Repo-relative script files the stack's run / test / bdd commands execute."""
    out: list[str] = []
    for cmd in (stack.run, stack.test, stack.bdd):
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        for tok in tokens:
            path = tok.removeprefix("./")
            if (
                path.endswith(_SCRIPT_SUFFIXES)
                and not path.startswith(("-", "/"))
                and not any(ch in path for ch in "$=*:")
                and ".." not in path.split("/")
                and path not in out
            ):
                out.append(path)
    return tuple(out)


def _own_stack_files(
    tickets: Sequence[Ticket],
    stack: StackDecision | None,
    existing: frozenset[str] = frozenset(),
) -> tuple[list[Ticket], list[str], tuple[str, ...]]:
    """Give every script a stack command runs (``smoke.py`` for ``python3 smoke.py``)
    an owner: one no ticket writes is added to the entrypoint ticket, which wires the
    app end to end — else the run command fails on a file nothing builds. With no
    entrypoint ticket it is a coverage finding (a re-plan). Returns (tickets,
    concerns, findings). ``existing`` files (a change build's base tree) need no owner."""
    if stack is None:
        return list(tickets), [], ()
    tickets, init_concerns = _own_package_inits(tickets, stack, existing)

    def owned(path: str) -> bool:
        return path in existing or any(
            f == path or (f.endswith("/") and path.startswith(f)) for t in tickets for f in t.files
        )

    orphans = [f for f in stack_files(stack) if not owned(f)]
    if not orphans:
        return list(tickets), init_concerns, ()
    entry = next((t for t in tickets if t.entrypoint and t.id not in _GENERATED_TICKETS), None)
    if entry is None:
        return (
            list(tickets),
            init_concerns,
            tuple(
                f"Stack command file {f} is written by no ticket: put it in the files[] of "
                "the ticket that wires the app (the entrypoint ticket)."
                for f in orphans
            ),
        )
    note = (
        " Also write "
        + ", ".join(orphans)
        + ": the stack's commands run "
        + ("it" if len(orphans) == 1 else "them")
        + (
            f" (run: `{stack.run}`), which must boot the real app headless and exit 0"
            if stack.run
            else ""
        )
        + "."
    )
    updated = entry.model_copy(
        update={
            "files": (*entry.files, *orphans),
            "description": entry.description + note,
        }
    )
    return (
        [updated if t.id == entry.id else t for t in tickets],
        [
            *init_concerns,
            f"Ticket {entry.id}: added stack command file(s) {', '.join(orphans)} that no "
            "ticket wrote.",
        ],
        (),
    )


def _is_python_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _own_scenario_bindings(
    tickets: Sequence[Ticket],
    stack: StackDecision | None,
) -> tuple[list[Ticket], list[str]]:
    """On a pytest-bdd stack, give a feature ticket that carries scenarios but has no
    test ticket after it (the demo shape) a step module of its own to bind them from.
    Without one its packet says a paired test ticket binds them, nobody does, and the
    Test gate fails every scenario as not executed. A test ticket carrying scenarios
    but no test module (e.g. the plan's own ``bdd-harness``, renamed) gets one too:
    the feature tickets count it as their binder, so without one nobody binds."""
    if not pytest_bdd_binds(stack):
        return list(tickets), []
    bound = {c for t in tickets if t.specialty is Specialty.TEST for c in t.scenario_ids}
    out: list[Ticket] = []
    concerns: list[str] = []
    for t in tickets:
        test = t.specialty is Specialty.TEST
        # A file-less feature ticket is an acceptance-only one: a file would make it
        # build work.
        if (
            not t.scenario_ids
            or step_module(t.files, t.id) is not None
            or (not test and (not t.files or set(t.scenario_ids) <= bound))
        ):
            out.append(t)
            continue
        path = ticket_step_module(t.id)
        out.append(t.model_copy(update={"files": (*t.files, path)}))
        concerns.append(
            f"Ticket {t.id}: added {path} to bind its own scenarios "
            + (
                "(a test ticket with no test module)."
                if test
                else "(no test ticket depends on it)."
            )
        )
    return out, concerns


def _fold_test_bindings(
    tickets: Sequence[Ticket],
    stack: StackDecision | None,
) -> tuple[list[Ticket], list[str]]:
    """On a pytest-bdd stack, move a scenario's binding off a test ticket that writes
    only step modules onto the feature ticket that builds it, when that changes
    nothing about when it is judged. ``wave_scenarios`` judges a scenario once its
    every carrier is built, and a trailing test ticket binding all of an epic's
    tickets made it the last carrier of every scenario: no wave check ran their code
    until the last wave, and bugs piled up.

    A scenario of feature ticket F moves only when every other feature ticket the
    test ticket follows is already upstream of F: a models ticket's "When I GET
    /products" stays late, on the test ticket after the routes ticket (run after
    :func:`_wire_entrypoint`, so the entrypoint's order counts). A scenario F
    already binds from a step module of its own moves anyway: binding it twice
    runs it twice. Left alone: a test ticket that writes fixtures or helpers
    (conftest.py, tests/steps/*.py, a stack-command file) — the step modules lean
    on those, and moving them before their users would import app code not yet
    built — one that implements a contract (flagged), its own scenarios (a
    criterion no code ticket owns) and a file-less acceptance-only ticket's. A test
    ticket left with no scenarios is dropped, its dependents rewired to its
    dependencies."""
    if not pytest_bdd_binds(stack):
        return list(tickets), []
    protected = set(stack_files(stack)) if stack is not None else set()
    deps = {t.id: t.depends_on for t in tickets}
    by_id = {t.id: t for t in tickets}
    owner = {
        c: t.id
        for t in tickets
        if t.specialty is not Specialty.TEST and t.files
        for c in t.scenario_ids
    }
    # A file-less acceptance-only ticket builds nothing a scenario could wait for.
    features = {t.id for t in _features(tickets) if t.files}
    out: list[Ticket] = []
    dropped: dict[str, tuple[str, ...]] = {}
    concerns: list[str] = []
    for t in tickets:
        borrowed = [c for c in t.scenario_ids if c in owner]
        if t.specialty is not Specialty.TEST or t.id in _GENERATED_TICKETS or not borrowed:
            out.append(t)
            continue
        steps_only = all(
            _is_python_test(f) and not is_helper_module(f) and f not in protected for f in t.files
        )
        if t.implements:
            concerns.append(
                f"Test ticket {t.id} implements contract(s) "
                f"{', '.join(t.implements)}: left it binding its scenarios."
            )
        if t.implements or not t.files:
            out.append(t)
            continue
        upstream = _ancestors(deps, t.id) & features
        moved = [
            c
            for c in borrowed
            if step_module(by_id[owner[c]].files, owner[c]) is not None
            or (steps_only and upstream - {owner[c]} <= _ancestors(deps, owner[c]))
        ]
        if not moved:
            out.append(t)
            continue
        kept = tuple(c for c in t.scenario_ids if c not in moved)
        note = (
            f"Test ticket {t.id}: moved the binding of scenario(s) {', '.join(moved)} "
            f"to the ticket(s) that build them "
            f"({', '.join(_uniq(owner[c] for c in moved))})"
        )
        if not kept and steps_only:
            dropped[t.id] = t.depends_on
            concerns.append(note + "; dropped the ticket, which had nothing else to write.")
            continue
        out.append(t.model_copy(update={"scenario_ids": kept}))
        concerns.append(note + ".")
    if not dropped:
        return out, concerns

    def resolve(dep: str) -> tuple[str, ...]:
        return tuple(d for up in dropped[dep] for d in resolve(up)) if dep in dropped else (dep,)

    return [
        t.model_copy(
            update={
                "depends_on": _uniq(d for dep in t.depends_on for d in resolve(dep) if d != t.id)
            }
        )
        if set(t.depends_on) & set(dropped)
        else t
        for t in out
    ], concerns


_DIFFICULTY_RANK = {Difficulty.TRIVIAL: 0, Difficulty.STANDARD: 1, Difficulty.COMPLEX: 2}


def _merged(a: Ticket, b: Ticket) -> Ticket:
    """``a`` folded into its sole dependent ``b``: ``b``'s id, title, specialty and
    entrypoint; the union of both scopes; the harder difficulty (the tier follows it)."""
    files = _uniq((*b.files, *a.files))
    implements = _uniq((*b.implements, *a.implements))
    return b.model_copy(
        update={
            "description": f"{b.description} Also build what {a.id} ({a.title}) planned"
            + (f": {a.description}" if a.description else "."),
            "difficulty": max(a.difficulty, b.difficulty, key=_DIFFICULTY_RANK.__getitem__),
            "depends_on": _uniq(d for d in (*b.depends_on, *a.depends_on) if d != a.id),
            "files": files,
            "reads": _uniq(f for f in (*b.reads, *a.reads) if f not in files),
            "scenario_ids": _uniq((*b.scenario_ids, *a.scenario_ids)),
            "implements": implements,
            "consumes": _uniq(c for c in (*b.consumes, *a.consumes) if c not in implements),
        }
    )


def _merge_sole_dependents(
    tickets: Sequence[Ticket],
    criteria: Sequence[AcceptanceCriterion],
    stack: StackDecision | None,
    ceilings: Ceilings,
    wired: frozenset[tuple[str, str]] = frozenset(),
    keep: frozenset[str] = frozenset(),
) -> tuple[list[Ticket], list[AcceptanceCriterion], list[str]]:
    """Fold a ticket into the ONE ticket that depends on it when both are feature
    tickets of the same epic and the merge stays within the ceiling of both
    tickets' tiers: work only one other ticket uses is a private hand-off that
    costs a session, a contract and a wave check, and cross-ticket hand-offs are
    where builds fail. Its scenarios move with it (their records re-pointed, so
    each keeps exactly one owner). Repeats until no pair fits, in plan order.

    The entrypoint takes part neither way: wiring every feature in is its job, not
    a hand-off, and folding into it would collapse a plan into one ticket. So the
    ``wired`` edges (ticket, dependency) :func:`_wire_entrypoint` added do not
    count as dependents; an edge left pointing at a folded ticket moves to the
    ticket it folded into. Test tickets and file-less acceptance-only tickets
    (which build nothing to hand off) take no part either, nor do ``keep`` ids
    (an earlier plan's tickets, built work): their diffs, notes and restart memos
    are keyed by id, and a fold would orphan them."""
    protected = set(stack_files(stack)) if stack is not None else set()
    out = list(tickets)
    moved: dict[str, str] = {}
    concerns: list[str] = []
    ignored = set(wired)

    def epic(tid: str) -> str | None:
        m = _EPIC_OF_TICKET.match(tid)
        return m.group(1) if m else None

    def foldable(t: Ticket) -> bool:
        return (
            t.id not in _GENERATED_TICKETS
            and t.id not in keep
            and t.specialty is not Specialty.TEST
            and bool(t.files)
        )

    while True:
        dependents: dict[str, set[str]] = {}
        for t in out:
            for d in t.depends_on:
                if (t.id, d) not in ignored:
                    dependents.setdefault(d, set()).add(t.id)
        by_id = {t.id: t for t in out}
        pick: tuple[Ticket, Ticket] | None = None
        for a in out:
            ds = dependents.get(a.id, set())
            if len(ds) != 1 or not foldable(a) or a.entrypoint:
                continue
            b = by_id[next(iter(ds))]
            if not foldable(b) or b.entrypoint or epic(a.id) != epic(b.id):
                continue
            merged = _merged(a, b)
            if any(
                over_ceiling(merged, ceilings[d], protected)
                for d in (b.difficulty, merged.difficulty)
            ):
                continue
            pick = a, merged
            break
        if pick is None:
            break
        a, merged = pick
        rewired: list[Ticket] = []
        for t in out:
            if t.id == a.id:
                continue
            if t.id == merged.id:
                rewired.append(merged)
            elif a.id in t.depends_on:
                # Only an ignored (wired) edge can still point at a.
                ignored.discard((t.id, a.id))
                if merged.id not in t.depends_on:
                    ignored.add((t.id, merged.id))
                deps = _uniq(merged.id if d == a.id else d for d in t.depends_on)
                rewired.append(t.model_copy(update={"depends_on": deps}))
            else:
                rewired.append(t)
        out = rewired
        moved = {k: merged.id if v == a.id else v for k, v in moved.items()}
        moved[a.id] = merged.id
        concerns.append(
            f"Ticket {a.id}: merged into {merged.id}, the only ticket that depends on it "
            "(the merge stays within its size ceiling)."
        )
    if not moved:
        return out, list(criteria), concerns
    return (
        [t.model_copy(update={"seq": t.seq._replace(local=i)}) for i, t in enumerate(out)],
        [
            c.model_copy(
                update={
                    "ticket_id": moved[c.ticket_id],
                    "oracle_hint": scenario_ref(moved[c.ticket_id], c.id),
                }
            )
            if c.ticket_id in moved
            else c
            for c in criteria
        ],
        concerns,
    )


def _one_step_module_per_ticket(
    tickets: Sequence[Ticket],
    stack: StackDecision | None,
) -> tuple[list[Ticket], list[str]]:
    """On a pytest-bdd stack, a ticket carrying scenarios writes ONE step module, the
    one its packet binds them from (:func:`step_module`): four modules split by layer
    (test_cart_routes.py, test_cart_service.py, ...) were each written as the same
    29k-char step module, every scenario run 4x. Helper modules a step module
    imports (test_helpers.py, ...) and stack-command files stay."""
    if not pytest_bdd_binds(stack):
        return list(tickets), []
    protected = set(stack_files(stack)) if stack is not None else set()
    out: list[Ticket] = []
    concerns: list[str] = []
    for t in tickets:
        binder = step_module(t.files, t.id)
        extra = [
            f
            for f in t.files
            if _is_python_test(f) and f != binder and not is_helper_module(f) and f not in protected
        ]
        if not t.scenario_ids or not extra:
            out.append(t)
            continue
        out.append(t.model_copy(update={"files": tuple(f for f in t.files if f not in extra)}))
        concerns.append(
            f"Ticket {t.id}: dropped test module(s) {', '.join(extra)}: {binder} binds its "
            "scenarios, and every further module would bind and run them again."
        )
    return out, concerns


def _own_package_inits(
    tickets: Sequence[Ticket],
    stack: StackDecision,
    existing: frozenset[str] = frozenset(),
) -> tuple[list[Ticket], list[str]]:
    """Give every Python source directory's ``__init__.py`` an owner. Without one
    mypy maps ``finance/contracts.py`` to both ``contracts`` and ``finance.contracts``
    and fails, and no ticket may write it outside its files. The contracts ticket
    (wave 0) takes them, else the first ticket writing into that directory. Test
    directories are left to pytest's own rootdir handling."""
    if stack.language != "python":
        return list(tickets), []
    tickets, concerns = _merge_shadowed_modules(tickets, existing)
    owned = {f for t in tickets for f in t.files} | existing
    dirs: dict[str, str] = {}  # missing __init__.py -> first ticket writing below it
    for t in tickets:
        for f in t.files:
            if not f.endswith(".py"):
                continue
            parts = f.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                if any(p.startswith("test") for p in parts[:i]):
                    break
                init = "/".join((*parts[:i], "__init__.py"))
                if init not in owned:
                    dirs.setdefault(init, t.id)
    if not dirs:
        return list(tickets), concerns
    holder = CONTRACTS_TICKET_ID if any(t.id == CONTRACTS_TICKET_ID for t in tickets) else None
    added: dict[str, list[str]] = {}
    for init, first in dirs.items():
        added.setdefault(holder or first, []).append(init)
    out = [
        t.model_copy(
            update={
                "files": (*t.files, *added[t.id]),
                "description": t.description
                + " Also create the package marker(s) "
                + ", ".join(added[t.id])
                + " (empty unless this ticket needs exports there).",
            }
        )
        if t.id in added
        else t
        for t in tickets
    ]
    return out, [
        *concerns,
        *(
            f"Ticket {tid}: added package marker(s) {', '.join(inits)} that no ticket wrote."
            for tid, inits in added.items()
        ),
    ]


def _merge_shadowed_modules(
    tickets: Sequence[Ticket],
    existing: frozenset[str] = frozenset(),
) -> tuple[list[Ticket], list[str]]:
    """Plan a module ``x.py`` as ``x/__init__.py`` when the plan (or the base tree)
    also writes files under ``x/``: a package directory shadows the sibling module,
    so ``import x`` loads the package — an empty marker — and every ``from x import``
    of the module's names fails. Epic planners split the layout this way (one writes
    models.py, another models/cart.py). ``import x`` and ``x.y`` stay as planned.
    A base-tree module or a generated ticket's file is left as it is."""
    planned = {f for t in tickets for f in t.files}
    dirs = {f.rsplit("/", 1)[0] for f in planned | existing if "/" in f}
    dirs |= {"/".join(d.split("/")[:i]) for d in tuple(dirs) for i in range(1, d.count("/") + 1)}
    frozen = {f for t in tickets if t.id in _GENERATED_TICKETS for f in t.files}
    moves = {
        f: f.removesuffix(".py") + "/__init__.py"
        for f in planned
        if f.endswith(".py")
        and f.removesuffix(".py") in dirs
        and f not in existing
        and f not in frozen
        and not any(p.startswith("test") for p in f.split("/"))
    }
    if not moves:
        return list(tickets), []
    out: list[Ticket] = []
    concerns: list[str] = []
    for t in tickets:
        mine = [f for f in t.files if f in moves]
        files = _uniq(tuple(moves.get(f, f) for f in t.files))
        reads = _uniq(tuple(moves.get(f, f) for f in t.reads))
        if files == t.files and reads == t.reads:
            out.append(t)
            continue
        note = "".join(
            f" Write {f}'s code as {moves[f]}: the plan also has a {f.removesuffix('.py')}/ "
            f"package, which would shadow a {f} module (`import "
            f"{f.removesuffix('.py').replace('/', '.')}` is unchanged)."
            for f in mine
        )
        out.append(
            t.model_copy(
                update={"files": files, "reads": reads, "description": t.description + note}
            )
        )
        if mine:
            concerns.append(
                f"Ticket {t.id}: planned {', '.join(mine)} as "
                f"{', '.join(moves[f] for f in mine)}: a package of that "
                "name shadows the module."
            )
    return out, concerns


def _add_contract_deps(tickets: Sequence[Ticket]) -> tuple[list[Ticket], list[str]]:
    """Order each consumer after the one ticket implementing a contract it consumes
    when the plan forgot the edge: the fix is mechanical, and leaving it to a
    re-plan cost a full planner pass per missing edge. An edge that would close a
    cycle is left for the fit check to report."""
    out = {t.id: t for t in tickets}
    producers: dict[str, list[str]] = {}
    for t in tickets:
        if t.id not in _GENERATED_TICKETS:
            for name in t.implements:
                producers.setdefault(name, []).append(t.id)

    def ancestors(tid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(out[tid].depends_on) if tid in out else []
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(out[d].depends_on if d in out else ())
        return seen

    concerns: list[str] = []
    for t in tickets:
        for name in t.consumes:
            ids = producers.get(name, [])
            if len(ids) != 1 or ids[0] == t.id:
                continue
            dep = ids[0]
            if dep in ancestors(t.id) or t.id in ancestors(dep):
                continue
            cur = out[t.id]
            out[t.id] = cur.model_copy(update={"depends_on": (*cur.depends_on, dep)})
            concerns.append(
                f"Ticket {t.id}: added a dependency on {dep}, which "
                f"implements the contract {name} it consumes."
            )
    return [out[t.id] for t in tickets], concerns


def _add_read_deps(tickets: Sequence[Ticket]) -> tuple[list[Ticket], list[str]]:
    """Order each ticket after the one ticket that writes a file it reads[] when the
    plan forgot the edge: built in the same wave, the reader never sees the file and
    codes against names it guesses (a repository importing a model class the models
    ticket, running beside it, named differently). Skips files with several writers
    or a generated writer (every ticket already follows the contracts ticket), and
    an edge that would close a cycle."""
    deps = {t.id: t.depends_on for t in tickets}
    writers: dict[str, list[str]] = {}
    for t in tickets:
        for path in t.files:
            writers.setdefault(path, []).append(t.id)
    concerns: list[str] = []
    for t in tickets:
        for path in t.reads:
            ids = writers.get(path, [])
            if len(ids) != 1 or ids[0] == t.id or ids[0] in _GENERATED_TICKETS:
                continue
            dep = ids[0]
            if dep in _ancestors(deps, t.id) or t.id in _ancestors(deps, dep):
                continue
            deps[t.id] = (*deps[t.id], dep)
            concerns.append(
                f"Ticket {t.id}: added a dependency on {dep}, which writes {path} it reads."
            )
    return [
        t.model_copy(update={"depends_on": deps[t.id]}) if deps[t.id] != t.depends_on else t
        for t in tickets
    ], concerns


def _ancestors(deps: dict[str, tuple[str, ...]], tid: str) -> set[str]:
    seen: set[str] = set()
    todo = list(deps.get(tid, ()))
    while todo:
        d = todo.pop()
        if d not in seen:
            seen.add(d)
            todo.extend(deps.get(d, ()))
    return seen


def _drop_self_consumes(tickets: Sequence[Ticket]) -> tuple[list[Ticket], list[str]]:
    """A ticket consuming a contract it implements itself consumes nothing: drop the
    entry so the fit check and the dependency repair never see a self-edge."""
    out: list[Ticket] = []
    concerns: list[str] = []
    for t in tickets:
        own = [n for n in t.consumes if n in t.implements]
        if own:
            t = t.model_copy(update={"consumes": tuple(n for n in t.consumes if n not in own)})
            concerns.append(
                f"Ticket {t.id}: dropped consumes {', '.join(own)}, which it implements itself."
            )
        out.append(t)
    return out, concerns


def _features(tickets: Sequence[Ticket]) -> list[Ticket]:
    return [
        t for t in tickets if t.id not in _GENERATED_TICKETS and t.specialty is not Specialty.TEST
    ]


def _wire_entrypoint(tickets: Sequence[Ticket]) -> tuple[list[Ticket], list[str]]:
    """Make the entrypoint ticket depend on every feature ticket it does not already
    reach: wiring every feature in is its job, and a forgotten edge cost a re-plan.
    With several entrypoints the one missing the fewest is wired (the coverage
    check then passes on it); an edge that would close a cycle is left for the
    coverage check to report."""
    features = _features(tickets)
    entries = [t for t in features if t.entrypoint]
    if not entries:
        return list(tickets), []
    deps = {t.id: t.depends_on for t in tickets}

    def missing(entry: Ticket) -> list[str]:
        seen = _ancestors(deps, entry.id)
        return [f.id for f in features if f.id != entry.id and f.id not in seen]

    entry = min(reversed(entries), key=lambda e: len(missing(e)))
    reached = _ancestors(deps, entry.id)
    added: list[str] = []
    for fid in missing(entry):
        if fid in reached or entry.id in _ancestors(deps, fid):
            continue
        added.append(fid)
        reached |= {fid, *_ancestors(deps, fid)}
    if not added:
        return list(tickets), []
    wired = entry.model_copy(update={"depends_on": (*entry.depends_on, *added)})
    return [wired if t.id == entry.id else t for t in tickets], [
        f"Ticket {entry.id}: added dependencies on feature ticket(s) {', '.join(added)} "
        "so the entrypoint wires every feature in."
    ]


def _drop_orphan_scenarios(
    tickets: Sequence[Ticket], criteria: Sequence[AcceptanceCriterion]
) -> tuple[list[AcceptanceCriterion], list[str]]:
    """A scenario whose ticket is not in the plan has nowhere to run: drop it with a
    note (scenarios of tickets dropped at a cap were already rehomed by file)."""
    live = {t.id for t in tickets}
    orphans = [c for c in criteria if c.ticket_id not in live]
    return [c for c in criteria if c.ticket_id in live], [
        f"Dropped scenario {c.id}: its ticket {c.ticket_id or '(none)'} is not in the plan."
        for c in orphans
    ]


def _fix_covers(
    criteria: Sequence[AcceptanceCriterion], brief: IntentBrief | None
) -> tuple[list[AcceptanceCriterion], list[str]]:
    """Normalise every scenario's ``covers`` to the declared goal ids: an entry naming
    a goal by id (``mh1``) or by its exact text maps to that id; anything else is
    dropped with a note. Coverage of every goal is still checked after."""
    goals = _goal_rows(brief)
    known = {g.lower(): g for _, g, _ in goals}
    by_text = {" ".join(text.lower().split()): g for _, g, text in goals}
    hint = ", ".join(g for _, g, _ in goals) or "none declared"
    out: list[AcceptanceCriterion] = []
    concerns: list[str] = []
    for c in criteria:
        kept: list[str] = []
        bad: list[str] = []
        for raw in c.covers:
            gid = _goal_of(raw, known) or by_text.get(" ".join(raw.lower().split()))
            if gid is None:
                bad.append(raw)
            else:
                kept.append(gid)
        if bad:
            concerns.append(
                f"Scenario {c.id}: dropped unknown covers id(s) "
                f"{', '.join(repr(b) for b in bad)} (declared ids: {hint})."
            )
        covers = _uniq(kept)
        out.append(c if covers == c.covers else c.model_copy(update={"covers": covers}))
    return out, concerns


# A misuse scenario's When/Then name bad input or access, and its Then refuses it.
# Words a happy path uses too (empty, missing, unknown, limit, duplicate) are not
# enough: flagging such a scenario would let it stand in for the Spec gate's abuse case.
_MISUSE = re.compile(
    r"\b(invalid|malformed|blank|negative|non-numeric|wrong type|"
    r"reject(?:s|ed)?|refuse[sd]?|unauthori[sz]ed|unauthenticated|forbidden|denied|"
    r"not allowed|too (?:long|large|big)|oversized|injection|traversal|4\d\d)\b",
    re.I,
)
_REFUSED = re.compile(
    r"\b(reject(?:s|ed|ion)?|refuse[sd]?|errors?|denied|forbidden|unauthori[sz]ed|"
    r"not allowed|4\d\d)\b",
    re.I,
)


def _is_misuse(c: AcceptanceCriterion) -> bool:
    acts = " ".join((*c.when, *c.then))
    outcome = " ".join(c.then)
    if not acts:
        acts = outcome = c.text
    return bool(_MISUSE.search(acts) and _REFUSED.search(outcome))


def _ensure_security_scenario(
    tickets: Sequence[Ticket], criteria: Sequence[AcceptanceCriterion], seq: Seq
) -> tuple[list[Ticket], list[AcceptanceCriterion], list[str]]:
    """The Spec gate requires a security/abuse criterion. When the plan flags none,
    flag the first scenario that already exercises misuse (a high-risk one first);
    with none, add a malformed-input scenario to the entrypoint ticket (else the
    last feature ticket) and to a test ticket binding its scenarios. No repair
    call: the plan's abuse clause asks for exactly this case."""
    if not criteria or any(c.security_abuse for c in criteria):
        return list(tickets), list(criteria), []
    misuse = sorted(
        (c for c in criteria if _is_misuse(c)),
        key=lambda c: c.criticality is not Criticality.HIGH_RISK,
    )
    if misuse:
        pick = misuse[0]
        return (
            list(tickets),
            [c.model_copy(update={"security_abuse": True}) if c is pick else c for c in criteria],
            [f"Scenario {pick.id} flagged security_abuse=true: none was, and it covers misuse."],
        )
    features = _features(tickets)
    owner = next((t for t in features if t.entrypoint), features[-1] if features else None)
    if owner is None:
        return list(tickets), list(criteria), []
    taken = {c.id for c in criteria}
    cid = next(f"AC-ABUSE-{n}" for n in range(1, len(taken) + 2) if f"AC-ABUSE-{n}" not in taken)
    given, when, then = (
        ("the app is running",),
        ("a request sends malformed input",),
        ("the input is rejected with an error message", "the app keeps serving requests"),
    )
    added = AcceptanceCriterion(
        id=cid,
        text=render_scenario(given, when, then),
        criticality=Criticality.HIGH_RISK,
        oracle_hint=scenario_ref(owner.id, cid),
        security_abuse=True,
        given=given,
        when=when,
        then=then,
        ticket_id=owner.id,
        seq=seq._replace(local=len(criteria)),
    )
    own = set(owner.scenario_ids)
    out = [
        t.model_copy(update={"scenario_ids": (*t.scenario_ids, cid)})
        if t.id == owner.id
        or (t.specialty is Specialty.TEST and own and own <= set(t.scenario_ids))
        else t
        for t in tickets
    ]
    return (
        out,
        [*criteria, added],
        [f"Added security scenario {cid} to ticket {owner.id}: no scenario covered misuse."],
    )
