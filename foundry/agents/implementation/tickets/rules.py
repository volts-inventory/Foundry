"""Packet rules: self-verification commands, scenario rules, the CHECK rule, failure grouping."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from foundry.agents.implementation.tickets.tasks import task_id_for_ticket
from foundry.gates.bdd_scaffold import step_module
from foundry.gates.profiles import pytest_bdd_binds
from foundry.schemas.implementation import CONTRACTS_TICKET_ID, FEATURE_DIR, StackDecision, Ticket

_VERIFY_STEPS = ("install", "build", "typecheck", "test", "bdd")


def _python_test_files(files: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        f
        for f in files
        if f.endswith(".py")
        and (PurePosixPath(f).name.startswith("test_") or f.endswith("_test.py"))
    )


# A local model returned a Given's value and took it as a parameter of the When:
# pytest-bdd then looks for a fixture of that name and fails every scenario.
_PYTEST_BDD_ARGS = (
    "A step function's parameters come only from its step text, a pytest fixture, or "
    'an earlier step\'s target_fixture, e.g. `@given("a saved draft", '
    'target_fixture="draft_id")` returning the id, then `@when(parsers.parse('
    '"they publish it to {channel}"))` with `def step(draft_id, channel)`; a step\'s plain '
    "return value is passed to nothing. Scenarios that share a step line share ONE "
    "step definition: pytest-bdd runs only the last definition of a step text, so "
    "never define the same step text twice in a module."
)


def scenario_rules(
    files: Sequence[str],
    scenarios: Mapping[str, Sequence[str]],
    stack: StackDecision | None,
    *,
    test: bool = False,
    ticket_id: str = "",
    check: bool = False,
) -> list[str]:
    """The packet rules that tie a ticket to its gate-owned scenarios: the exact
    feature file(s) and scenario names, how to bind them on this stack, and never
    to author a feature file. ``scenarios`` maps feature path -> scenario names;
    ``check``: the session confirms them with the ``check`` tool.
    A ``test`` ticket is the binder itself, so it is never told another one binds.
    On pytest-bdd exactly one module binds them (:func:`step_module`): told to bind
    from each of four modules, a model wrote one step module four times and every
    scenario ran 4x."""
    if not scenarios:
        return []
    listing = "; ".join(f"{path} (Scenario: {', '.join(ids)})" for path, ids in scenarios.items())
    rules = [
        f"ACCEPTANCE SCENARIOS: this ticket's scenarios are in the gate-owned file(s) "
        f"{listing}. Never write, copy or regenerate a .feature file anywhere: the "
        f"gate writes {FEATURE_DIR}/<ticket>.feature, executes only those, and "
        "discards any other .feature file."
    ]
    paths = list(scenarios)
    if stack is not None and stack.bdd:
        rules.append(
            f"BIND THEM: the bdd command `{stack.bdd}` runs every {FEATURE_DIR}/*.feature "
            f"from the repo root; every Given/When/Then of {', '.join(paths)} needs a "
            "step definition the runner loads (an undefined or skipped step fails the "
            "gate). "
            + (
                "`check` runs them as the gate does: each of these scenarios must pass."
                if check
                else "Run the bdd command and confirm each of these scenarios passed."
            )
        )
    elif pytest_bdd_binds(stack):
        binder = step_module(files, ticket_id)
        if binder is not None:
            calls = " ".join(
                f'scenarios("{posixpath.relpath(p, posixpath.dirname(binder) or ".")}")'
                for p in paths
            )
            others = [f for f in _python_test_files(files) if f != binder]
            rules.append(
                f"BIND THEM with pytest-bdd in {binder} ONLY, the path relative to it: "
                f"{calls}. Binding any other file leaves these scenarios unexecuted, "
                "which fails the gate"
                + (
                    f"; {', '.join(others)} must not call scenarios() on them, or every "
                    "scenario runs again"
                    if others
                    else ""
                )
                + (
                    ". `check` runs them as the gate does"
                    if check
                    else ". The `scenarios` command in `verify` checks the binding and runs them"
                )
                + ": every scenario above must pass. "
                + _PYTEST_BDD_ARGS
            )
        elif files and not test:
            rules.append(
                "A paired test ticket binds these scenarios with pytest-bdd; the code "
                "you write must make them pass."
            )
        else:
            rules.append(
                "BIND THEM with pytest-bdd from a step module, the path relative to it "
                f'(from tests/test_x.py: scenarios("../{paths[0]}")), and implement '
                "every Given/When/Then step. " + _PYTEST_BDD_ARGS
            )
    return rules


def scenario_verify(
    files: Sequence[str],
    scenarios: Mapping[str, Sequence[str]],
    stack: StackDecision | None,
    ticket_id: str = "",
) -> dict[str, str]:
    """The ``verify`` entry that runs a pytest-bdd ticket's own scenarios: check its
    step module (:func:`step_module`) names its feature file(s), then run it."""
    binder = step_module(files, ticket_id)
    if not scenarios or binder is None or not pytest_bdd_binds(stack):
        return {}
    test = stack.test if stack is not None and "pytest" in stack.test else "python -m pytest -q"
    targets = shlex.quote(binder)
    checks = [f"grep -q {shlex.quote(p)} {targets}" for p in scenarios]
    return {"scenarios": " && ".join([*checks, f"{test} -rA {targets}"])}


def verify_commands(stack: StackDecision | None) -> dict[str, str]:
    """The stack's planned commands an agent runs on its own work (the gates run
    the same ones on the integrated tree)."""
    if stack is None:
        return {}
    return {k: v for k in _VERIFY_STEPS if (v := getattr(stack, k))}


# Type-check ONE file in isolation, for the contracts ticket (its siblings in wave
# 0 may not have written the project config yet).
_SINGLE_FILE_TYPECHECK: dict[str, str] = {
    "python": "python -m mypy {file}",
    "typescript": "npx tsc --noEmit --strict --skipLibCheck {file}",
}


def verify_rules(
    stack: StackDecision | None,
    *,
    ticket_id: str = "",
    contract_files: Sequence[str] = (),
    check: bool = False,
) -> list[str]:
    """The packet rules that make an agent check its own work before it finishes:
    run the stack's commands (``verify``) — or, in a session with the ``check``
    tool, the gates themselves — and fix what fails in its own files; the
    contracts ticket writes declarations only and type-checks its file alone; every
    other ticket uses the contract types instead of re-declaring them."""
    cmds = verify_commands(stack)
    rules = [
        CHECK_RULE
        if check
        else (
            "VERIFY BEFORE YOU FINISH: from this directory run "
            + (
                "the commands in `verify` (install only once its package manifest exists; "
                "then build and typecheck; then the tests that cover your files)"
                if cmds
                else "the project's build, type checker and the tests that cover your files"
            )
            + ". Fix every failure in the files you may write and re-run until they pass. "
            "Failures in other files, or caused by files later tickets have not written "
            "yet, are expected: leave them and name them in a TRIAGE line. If a command "
            "cannot run here (missing tool, no network), say so in a TRIAGE line."
        )
    ]
    if "bdd" in cmds and "FOUNDRY_BDD_REPORT" in cmds["bdd"]:
        rules.append(
            "The bdd command writes its report to $FOUNDRY_BDD_REPORT: set it "
            "to a scratch file outside this directory when you run it."
        )
    if ticket_id == task_id_for_ticket(CONTRACTS_TICKET_ID):
        files = ", ".join(contract_files) or "the contracts file"
        language = stack.language if stack else ""
        single = _SINGLE_FILE_TYPECHECK.get(language, "")
        alone = (
            single.format(file=" ".join(contract_files))
            if single and contract_files
            else cmds.get("typecheck", "the language's type checker")
        )
        rules.append(
            f"DATA TYPES AND ERRORS ONLY in {files}, as real code that ships — Python: "
            "dataclass, TypedDict and Enum classes with typed fields, and Exception "
            "subclasses with a real __init__; TypeScript: interface and type "
            "declarations (plus the enum/const values the contracts name) and Error "
            "subclasses with a real constructor. No functions, services, Protocols, "
            "ABCs, placeholder bodies, I/O or validation. Type-check this file on its "
            f"own before you finish (`{alone}`) and fix every error it reports."
        )
    elif contract_files:
        rules.append(
            "CONTRACT TYPES ARE THE ONLY TYPES: the data types and errors in "
            f"{', '.join(contract_files)} are the single definition of each. Import "
            "them from there; never declare a class or type with the same name in "
            "another file (no parallel models.X copy of a contract type) — a copy "
            "type-checks alone and breaks every caller that uses the contract's type. "
            "A contract function or class is defined by the ticket that implements it, "
            "in its own files: implement yours there, and import the ones you call "
            "from the module that defines them."
        )
    return rules


CHECK_RULE = (
    "VERIFY WITH `check` BEFORE YOU FINISH: `check` runs the gates themselves (the "
    "build and type checker, and the acceptance scenarios that can run yet) on a copy "
    "of this tree shaped as the gate sees it: offline, only the declared dependencies "
    "installed. Call it after your edits, fix every failure it lists as yours, and "
    "finish when it lists none; done is refused while it does. The commands in `verify` "
    "are for reading a failure's full output. Failures it lists as pre-existing, or "
    "caused by files later tickets have not written yet, are expected: leave them and "
    "name them in a TRIAGE line."
)


def check_rewrites(plain: Sequence[str], checked: Sequence[str]) -> dict[str, str]:
    """Rule -> its variant for a session with ``check`` (the two lists of one
    packet's rules, built without and with ``check``)."""
    return {a: b for a, b in zip(plain, checked, strict=True) if a != b}


_BDD_FINDING = re.compile(r"^BDD criterion (\S+): scenario (\S+?)::\S+ (.*)$")


def unwritten_ticket_files(
    findings: Sequence[str],
    tickets: Sequence[Ticket],
    built: set[str],
    tree: Mapping[str, str | None],
    stack: StackDecision | None,
) -> tuple[str, ...]:
    """One line per built ticket whose failing scenarios ``findings`` name while some
    of its own files are absent from ``tree``: its session ended before it wrote
    them (a turn cap), so the fix is to create them. A fixer told only that the
    scenarios "were not executed" read the whole project twice without writing the
    missing step module. A missing pytest-bdd test module gets its binding call."""
    failing: dict[str, dict[str, None]] = {}  # ticket id -> its failing feature paths
    carriers = {cid: t for t in tickets for cid in t.scenario_ids}
    for line in findings:
        m = _BDD_FINDING.match(line)
        if m is not None and (t := carriers.get(m.group(1))) is not None:
            failing.setdefault(t.id, {})[m.group(2)] = None
    out: list[str] = []
    for t in tickets:
        if t.id not in failing or task_id_for_ticket(t.id) not in built:
            continue
        missing = [f for f in t.files if not f.endswith("/") and tree.get(f) is None]
        if not missing:
            continue
        binder = step_module(t.files, t.id)
        binds = [
            f"in {f}: "
            + " ".join(
                f'scenarios("{posixpath.relpath(p, posixpath.dirname(f) or ".")}")'
                for p in failing[t.id]
            )
            for f in (
                (binder,)
                if binder is not None and pytest_bdd_binds(stack) and binder in missing
                else ()
            )
        ]
        out.append(
            f"Create these files first: {', '.join(missing)}. Ticket {t.id} owns them and "
            "its failing scenarios need them, but its session ended before it wrote them: "
            "writing them is this fix, not a later ticket's work"
            + (f" (bind the scenarios {'; '.join(binds)})" if binds else "")
        )
    return tuple(out)


def group_failures_by_ticket(failures: Sequence[str], tickets: Sequence[Ticket]) -> tuple[str, ...]:
    """``failures`` with the per-scenario BDD findings folded into one line per
    (tickets, feature file, reason): the tickets that carry those scenarios and the
    files they own, so a fixer facing dozens of unbound scenarios sees which step
    module to fix for which feature instead of one line per scenario. Other lines
    keep their order; the grouped block takes the first BDD line's place."""
    carriers: dict[str, list[Ticket]] = {}
    for t in tickets:
        for cid in t.scenario_ids:
            carriers.setdefault(cid, []).append(t)
    groups: dict[tuple[str, str, str], list[str]] = {}
    out: list[str] = []
    anchor = -1
    for line in failures:
        m = _BDD_FINDING.match(line)
        if m is None:
            out.append(line)
            continue
        cid, feature, why = m.groups()
        owners = (
            ", ".join(f"{t.id} ({', '.join(t.files) or 'any file'})" for t in carriers.get(cid, ()))
            or "no ticket"
        )
        groups.setdefault((owners, feature, why), []).append(cid)
        if anchor < 0:
            anchor = len(out)
    if anchor < 0:
        return tuple(failures)
    block = [
        f"[{feature}] scenarios {', '.join(ids)} {why} — ticket(s): {owners}"
        for (owners, feature, why), ids in sorted(groups.items(), key=lambda kv: kv[0][1])
    ]
    return (
        *out[:anchor],
        f"[BDD failures by feature file: {len(block)} group(s)]",
        *block,
        *out[anchor:],
    )
