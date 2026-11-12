"""The stack decision and the generated tickets (contracts, harness, deps)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents.planner.checks import _bdd_quoting, _declaration_only
from foundry.agents.planner.readers import _section, _str, _strs, _uniq
from foundry.agents.planner.schema import (
    _DEFAULT_CONTRACTS_FILE,
    _DEFAULT_HARNESS_FILES,
    CODE_CONTRACT_KINDS,
)
from foundry.base_tree import BaseView
from foundry.gates.profiles import bdd_runner_hint, property_testing, stack_install
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.implementation import (
    BDD_REPORT_ENV,
    CONTRACTS_TICKET_ID,
    FEATURE_DIR,
    HARNESS_TICKET_ID,
    Difficulty,
    Specialty,
    StackDecision,
    Ticket,
)

if TYPE_CHECKING:
    from foundry.state.seq import Seq


def _stack_decision(
    payload: dict[str, Any], languages: Iterable[str]
) -> tuple[StackDecision | None, list[str]]:
    """The plan's stack, or None (the gates then infer a profile from the request)
    when it names no language or one this host lacks."""
    raw = _section(payload, "stack")
    language = _str(raw.get("language")).strip().lower()
    available = sorted(set(languages))
    if language not in available:
        return None, [
            f"Stack language {language or '(none)'!r} is not available here "
            f"({', '.join(available)}); the gates infer a profile from the request."
        ]
    stack = StackDecision(
        language=language,
        **{
            k: _str(raw.get(k)).strip()
            for k in (
                "framework",
                "package_manager",
                "install",
                "build",
                "typecheck",
                "test",
                "bdd",
                "run",
                "kind",
                "launch",
                "notes",
            )
        },
    )
    if language != "python" and not stack.test and not stack.bdd:
        # The pytest harness cannot test another language: without a command of its
        # own the stack has no test gate at all.
        return None, [
            f"The {language} stack declares neither a test nor a bdd command; it was "
            "dropped and the gates infer a profile from the request."
        ]
    concerns: list[str] = []
    if (install := stack_install(stack)) != stack.install:
        concerns.append(
            f"The {language} stack's install `{stack.install or '(none)'}` "
            f"downloads no dependencies; it runs `{install}` so the offline "
            "steps have them."
        )
        stack = stack.model_copy(update={"install": install})
    if (bdd := _bdd_quoting(stack.bdd)) != stack.bdd:
        concerns.append(
            f"The bdd command `{stack.bdd}` single-quotes ${BDD_REPORT_ENV}, "
            f"which stops its expansion; it runs `{bdd}`."
        )
        stack = stack.model_copy(update={"bdd": bdd})
    if language != "python" and not stack.bdd:
        concerns.append(f"The {language} stack has no bdd command: its scenarios are not executed.")
    if language != "python" and not stack.test:
        concerns.append(f"The {language} stack has no test command.")
    return stack, concerns


def _plan_paths(raw: Any, what: str, concerns: list[str]) -> tuple[str, ...]:
    """Repo-relative file paths from the plan; gate inputs, directories, absolute
    paths and ``..`` are dropped with a concern."""
    kept: list[str] = []
    for path in _uniq(_strs(raw)):
        parts = path.split("/")
        if (
            path.startswith("/")
            or path.endswith("/")
            or ".." in parts
            or path == "criteria.json"
            or parts[0] == FEATURE_DIR
        ):
            concerns.append(f"Dropped {what} path {path!r}: not a project file.")
        else:
            kept.append(path)
    return tuple(kept)


# How to write the contract data types as real, shipping code.
_CONTRACT_STUB_RULES: dict[str, str] = {
    "python": (
        "data as @dataclass (or Enum / TypedDict) classes with typed fields, errors as "
        "Exception subclasses whose __init__ takes the fields it reports and calls "
        "super().__init__ with a message; no `...` bodies, no Protocol or ABC. Never "
        "name a field or method after a builtin used in annotations (`list`, `type`, "
        "`id`, `dict`, ...) — inside the class body it shadows the builtin, so "
        "`-> list[X]` breaks."
    ),
    "typescript": (
        "data as `interface` / `type` declarations (plus the `enum`/`const` values the "
        "contracts name), errors as classes extending Error with a real constructor; "
        "no function or service declarations."
    ),
    "": "data types and error types only, written as real code that compiles and "
    "ships: no function or service declarations and no empty bodies.",
}


def _python_deps_rule(stack: StackDecision, where: str) -> str:
    framework = f" ({stack.framework})" if stack.framework else ""
    return (
        f"Declare in {where}, one per line and pinned with ==, every third-party "
        f"package the project's code and tests import: the stack's runtime "
        f"framework{framework} and libraries, and the test packages. The per-wave "
        "checks install exactly what is declared there before running anything, so "
        "an undeclared import fails every later check with ModuleNotFoundError. "
        "Never list a standard-library module (sqlite3, json, os, ...): pip cannot "
        "install it."
    )


def _generated_tickets(
    interfaces: Sequence[InterfaceContract],
    raw_stack: dict[str, Any],
    stack: StackDecision | None,
    seq: Seq,
    base: BaseView | None = None,
) -> tuple[list[Ticket], list[str]]:
    """The planner's own tickets: ``contracts`` writes the contract data types and
    errors (``_declaration_only``) as real, shipping code when there are any;
    ``bdd-harness`` wires the stack's Cucumber runner when it has a ``bdd`` command.
    Both have no dependencies (wave 0), so the first BUILD check already has the
    manifest, the config and the contracts to compile. A change build (``base``)
    extends the base's contracts file and keeps its already-wired runner."""
    concerns: list[str] = []
    language = stack.language if stack else _str(raw_stack.get("language")).strip().lower()
    out: list[Ticket] = []
    # Only data types and errors: a function or class with behaviour is defined by
    # the ticket implementing it, at its own path, so the shipped tree holds no stub.
    code = [i for i in interfaces if i.kind in CODE_CONTRACT_KINDS and _declaration_only(i)]
    owned: tuple[str, ...] = ()
    if code:
        owned = _plan_paths(raw_stack.get("contracts_file"), "contracts", concerns)
        if not owned and base is not None and base.contracts_files:
            owned = (next(iter(base.contracts_files)),)
        if not owned and language in _DEFAULT_CONTRACTS_FILE:
            owned = (_DEFAULT_CONTRACTS_FILE[language],)
        if not owned:
            concerns.append(
                f"{len(code)} contract data type(s) but no contracts file for language "
                f"{language or '(none)'!r}: they are not written as code."
            )
        else:
            out.append(
                Ticket(
                    id=CONTRACTS_TICKET_ID,
                    title="Define the contract data types",
                    description=(
                        "Write the data types and errors this ticket implements as real "
                        f"{language or 'project-language'} code in {', '.join(owned)}: "
                        "records with their typed fields, enums, and error classes with a "
                        "real constructor. This code ships: no functions, services, "
                        "interfaces, I/O or validation, and no placeholder bodies. Every "
                        "other ticket imports these types and none may change or "
                        "redeclare them, so match each contract exactly. The file must pass "
                        "the stack's build and typecheck on its own: "
                        + _CONTRACT_STUB_RULES.get(language, _CONTRACT_STUB_RULES[""])
                        + (
                            " "
                            + ", ".join(f for f in owned if f in base.files)
                            + " already exists: keep every declaration in it exactly as it "
                            "is and add the new ones."
                            if base is not None and any(f in base.files for f in owned)
                            else ""
                        )
                    ),
                    specialty=Specialty.BACKEND,
                    difficulty=Difficulty.TRIVIAL,
                    files=owned,
                    implements=tuple(i.name for i in code),
                    seq=seq,
                )
            )
    if stack is not None and stack.bdd and base is not None:
        concerns.append("Change build: the base tree's BDD runner is kept as wired.")
    elif stack is not None and stack.bdd:
        files = _uniq(
            (
                *_plan_paths(raw_stack.get("harness_files"), "harness", concerns),
                *_DEFAULT_HARNESS_FILES.get(language, ()),
            )
        )
        commands = "; ".join(
            f"{k} `{v}`"
            for k, v in (
                ("install", stack.install),
                ("build", stack.build),
                ("typecheck", stack.typecheck),
                ("test", stack.test),
                ("run", stack.run),
            )
            if v
        )
        out.append(
            Ticket(
                id=HARNESS_TICKET_ID,
                title="Wire the BDD runner",
                description=(
                    f"Add the official Cucumber runner for {language} as a dev dependency "
                    "in the package manifest and write its config so the BDD command "
                    f"`{stack.bdd}` runs every {FEATURE_DIR}/*.feature file and writes "
                    f"its report to the file named by ${BDD_REPORT_ENV}. "
                    + (
                        f"The stack's commands, which must pass once every ticket is "
                        f"built: {commands}. "
                        if commands
                        else ""
                    )
                    + "Write ONLY your files: the feature tickets write the source after "
                    "you, and this wave's build check runs on your files alone, so "
                    "declare no build target at a source file you do not write; any "
                    "file you create outside your files is discarded."
                    + " Write no step definitions: the test tickets do."
                    + (
                        f" The contracts' property-based tests use {lib.library}: declare "
                        f"{lib.dependency}."
                        if any(i.properties for i in interfaces)
                        and (lib := property_testing(language)) is not None
                        and lib.dependency
                        else ""
                    )
                    + (f" {hint}" if (hint := bdd_runner_hint(language)) else "")
                    + (
                        " "
                        + _python_deps_rule(
                            stack, "requirements.txt or pyproject.toml [project].dependencies"
                        )
                        if language == "python"
                        else ""
                    )
                ),
                specialty=Specialty.TEST,
                difficulty=Difficulty.STANDARD,
                files=tuple(f for f in files if f not in owned),
                seq=seq,
            )
        )
    return out, concerns


_PY_MANIFESTS = ("requirements.txt", "pyproject.toml")


def _python_deps_ticket(
    tickets: Sequence[Ticket],
    generated: Sequence[Ticket],
    stack: StackDecision | None,
    base: BaseView | None,
    seq: Seq,
) -> list[Ticket]:
    """A wave-0 ticket declaring a new Python build's dependencies in the manifest a
    planned ticket owns (requirements.txt when none does and the stack names a
    framework), when no generated ticket writes one. The wave checks install
    only what a manifest declares: left to a late ticket (often the entrypoint), every
    earlier check runs without the framework and fails on ModuleNotFoundError, which
    the fixer may not repair in a later wave's file. The planned owner keeps the file
    too, so it can still extend it (overlapping scopes run in separate waves)."""
    if (
        stack is None
        or stack.language != "python"
        or stack.bdd
        or base is not None
        or any(t.id == HARNESS_TICKET_ID for t in generated)
    ):
        return []
    manifests = tuple(m for m in _PY_MANIFESTS if any(m in t.files for t in tickets))
    if not manifests and stack.framework.strip():
        # No ticket owns a manifest, yet the framework is third-party: unowned, the
        # only manifest is the import-derived scaffold, which is never installed.
        manifests = ("requirements.txt",)
    planned = {t.id for t in tickets}
    if not manifests or any(
        not planned.intersection(t.depends_on)
        for t in tickets
        if any(m in t.files for m in manifests)
    ):
        return []  # none, or a first-wave ticket already writes it
    return [
        Ticket(
            id=HARNESS_TICKET_ID,
            title="Declare the Python dependencies",
            description=(
                _python_deps_rule(stack, " / ".join(manifests))
                + " Write ONLY your files: the feature tickets write the source after you; "
                "any file you create outside your files is discarded. Write no application "
                "code and no step definitions."
            ),
            specialty=Specialty.BACKEND,
            difficulty=Difficulty.TRIVIAL,
            files=manifests,
            seq=seq,
        )
    ]


def _attach_generated(
    tickets: Sequence[Ticket], generated: Sequence[Ticket], seq: Seq
) -> tuple[list[Ticket], list[str]]:
    """Put the generated tickets first, make every other ticket depend on them, and
    take the contract files out of every other ticket's write scope (they are
    gate-owned once the contracts ticket is built)."""
    concerns: list[str] = []
    gen_ids = tuple(t.id for t in generated)
    owned = {f for t in generated if t.id == CONTRACTS_TICKET_ID for f in t.files}
    written = {n for t in generated if t.id == CONTRACTS_TICKET_ID for n in t.implements}
    ordered = list(generated)
    for t in tickets:
        files = tuple(f for f in t.files if f not in owned)
        if files != t.files:
            concerns.append(
                f"Ticket {t.id}: dropped the gate-owned contract file(s) "
                f"{', '.join(sorted(set(t.files) - set(files)))} from its files."
            )
        # A data type the contracts file defines: implementing it again redeclares it.
        moved = [n for n in t.implements if n in written]
        if moved:
            concerns.append(
                f"Ticket {t.id}: {', '.join(moved)} moved from implements to consumes: "
                "the contracts file defines the data types."
            )
        ordered.append(
            t.model_copy(
                update={
                    "files": files,
                    "depends_on": _uniq((*gen_ids, *t.depends_on)),
                    "implements": tuple(n for n in t.implements if n not in written),
                    "consumes": _uniq((*t.consumes, *moved)),
                }
            )
        )
    return [
        t.model_copy(update={"seq": seq._replace(local=i)}) for i, t in enumerate(ordered)
    ], concerns
