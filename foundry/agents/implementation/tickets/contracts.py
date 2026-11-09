"""Contract checks on a ticket's writes: drift, stub signatures, duplicate types."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.agents.implementation.stubs import signature_findings
from foundry.agents.implementation.tickets.constants import (
    CONTRACT_DRIFT_TRIAGE,
    CONTRACT_DUP_TRIAGE,
    NODE_ID,
    SIGNATURE_CHANGE_TRIAGE,
)
from foundry.agents.implementation.tickets.tasks import task_id_for_ticket
from foundry.execution.adapter_contract import AdapterResult
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import CONTRACTS_TICKET_ID, FeatureDiff
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.execution.sandbox import Sandbox


class ContractDriftError(RuntimeError):
    """The contracts ticket's files leave out names its planned contracts declare."""


# A declared name in a contract signature: a capitalized type, or a function/method
# followed by its parameter list (a Go method after its receiver). The shapes are
# code-only so a prose signature ("the type of an order") yields no names.
_MEMBER_DECL = re.compile(
    r"\b(?:(?:class|interface|type|struct|enum|trait|record)\s+([A-Z]\w*)"
    r"|(?:def|function|fn|func(?:\s*\([^)]*\))?)\s+([A-Za-z_]\w*)\s*[(<])"
)


def contract_members(signature: str) -> tuple[str, ...]:
    """The types and functions/methods a planned contract signature declares."""
    out: list[str] = []
    for groups in _MEMBER_DECL.findall(signature):
        name = groups[0] or groups[1]
        if not name.startswith("__") and name not in out:
            out.append(name)
    return tuple(out)


def contract_drift(
    files: Mapping[str, str | None], required: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    """One finding per planned contract (name -> its declared names) whose names
    ``files`` (the written contract files) never mention."""
    text = "\n".join(t for t in files.values() if t)
    out: list[str] = []
    for contract, names in sorted(required.items()):
        missing = [n for n in names if not re.search(rf"\b{re.escape(n)}\b", text)]
        if missing:
            out.append(
                f"contract {contract} declares {', '.join(missing)}, which "
                f"{', '.join(sorted(files)) or 'the contract files'} never "
                "declare(s)"
            )
    return tuple(out)


def _check_contract_names(
    result: AdapterResult,
    sandbox: Sandbox,
    files: Sequence[str],
    task_id: str,
    required: Mapping[str, Sequence[str]],
    *,
    strict: bool,
) -> AdapterResult:
    """Every later ticket is handed the planned contract text and told to match it,
    while it imports the contract files: a name the contracts ticket renamed or
    dropped splits the tickets between the two. ``strict`` fails the attempt (the
    retry sees the findings); on the last attempt the files are kept and noted."""
    worktree = Path(sandbox.worktree)
    diff = _written_sources(result, sandbox, task_id)
    written: dict[str, str | None] = {}
    for f in files:
        if f in diff:
            written[f] = diff[f]
            continue
        path = worktree / f
        try:
            written[f] = path.read_text(encoding="utf-8") if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            written[f] = None
    findings = contract_drift(written, required)
    if not findings:
        return result
    why = (
        "; ".join(findings) + ". Every other ticket codes against these exact names: "
        "declare each one exactly as its contract gives it (never rename one)"
    )
    if strict:
        raise ContractDriftError(why)
    note = GateTriage(
        name=CONTRACT_DRIFT_TRIAGE,
        run_against=f"task/{task_id}",
        explanation=why[:2000],
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(update={"triage": (*result.triage, note)})


class SignatureChangeError(ContractDriftError):
    """An implementation ticket changed a public signature its typed stub planned."""


def _check_stub_signatures(
    result: AdapterResult,
    sandbox: Sandbox,
    task_id: str,
    stubs: Mapping[str, str],
    *,
    strict: bool,
    shared: frozenset[str] | None = None,
) -> AdapterResult:
    """The ticket's stub files must keep every stubbed name, parameter list and
    return annotation: the other tickets code against them. ``strict`` fails the
    attempt (the retry sees the findings); on the last attempt the files are kept
    and noted, as for contract drift. ``shared`` are the top-level names another
    ticket depends on (None: all of them); a change to only unshared ones is noted,
    never failed."""
    worktree = Path(sandbox.worktree)
    diff = _written_sources(result, sandbox, task_id)
    findings: list[str] = []
    pinned = False
    for path, stub in sorted(stubs.items()):
        if path in diff:
            text = diff[path]
        else:
            try:
                target = worktree / path
                text = target.read_text(encoding="utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                continue
        for name, finding in signature_findings(stub, text):
            findings.append(f"{path}: {finding}")
            pinned = pinned or shared is None or name.partition(".")[0] in shared
    if not findings:
        return result
    why = (
        "; ".join(findings) + ". These are the planned public signatures the other "
        "tickets code against: keep each planned top-level name and signature exactly "
        "as the typed stub declares it and fill in the bodies. Adding `__init__`, "
        "private helpers, extra methods or extra functions is allowed"
    )
    if strict and pinned:
        raise SignatureChangeError(why)
    note = GateTriage(
        name=SIGNATURE_CHANGE_TRIAGE,
        run_against=f"task/{task_id}",
        explanation=why[:2000],
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(update={"triage": (*result.triage, note)})


# A top-level (column-0) type declaration in the common languages: Python/TS/JS
# classes, TS interfaces and type aliases, Go types, Rust structs/enums/traits,
# Java/Kotlin classes, interfaces and records.
_TYPE_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:public\s+)?(?:(?:abstract|final|sealed|data)\s+)*"
    r"(?:class|interface|type|struct|enum|trait|record)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_SOURCE_SUFFIXES = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".swift",
)


def _declared_types(text: str) -> set[str]:
    return set(_TYPE_DECL.findall(text))


def contract_duplicates(
    files: Mapping[str, str | None], contract_files: Sequence[str]
) -> tuple[str, ...]:
    """One finding per source file outside ``contract_files`` that declares a
    top-level type with the name of a type the contract files declare: a parallel
    copy (``models.Category`` beside ``contracts.Category``) type-checks on its own
    and then fails to fit every caller that uses the contract's type."""
    names: set[str] = set()
    for f in contract_files:
        names |= _declared_types(files.get(f) or "")
    if not names:
        return ()
    out: list[str] = []
    for path, text in sorted(files.items()):
        if path in contract_files or not text or not path.endswith(_SOURCE_SUFFIXES):
            continue
        dup = sorted(_declared_types(text) & names)
        if dup:
            out.append(
                f"{path} redeclares contract type(s) {', '.join(dup)} already defined in "
                f"{', '.join(contract_files)}: delete the copy and import the contract's "
                "type instead (a class implementing a contract interface takes its own "
                "name and subclasses or implements the interface, e.g. "
                "`class SqlStore(Store)`, so the type checker holds it to the contract's "
                "methods)"
            )
    return tuple(out)


def _written_sources(
    result: AdapterResult, sandbox: Sandbox, task_id: str
) -> dict[str, str | None]:
    """The final text of every file the session's diff writes (a patched file is
    read off the still-live worktree); empty when the body cannot be parsed."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops

    probe = FeatureDiff(
        diff_uri=result.diff_uri,
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="",
        seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        ops = _diff_ops(probe, sandbox.get_blob)
    except MaterializationError:
        return {}
    out: dict[str, str | None] = dict(ops.writes)
    for rel in sorted(set(ops.patches) - set(ops.writes)):
        path = Path(sandbox.worktree) / rel
        try:
            out[rel] = path.read_text(encoding="utf-8") if path.is_file() else None
        except UnicodeDecodeError:
            out[rel] = None
    return out


def _flag_contract_duplicates(
    result: AdapterResult, sandbox: Sandbox, contracts: Mapping[str, str], task_id: str
) -> AdapterResult:
    if not contracts or task_id == task_id_for_ticket(CONTRACTS_TICKET_ID):
        return result
    written = _written_sources(result, sandbox, task_id)
    findings = contract_duplicates({**written, **contracts}, tuple(contracts))
    if not findings:
        return result
    note = GateTriage(
        name=CONTRACT_DUP_TRIAGE,
        run_against=f"task/{task_id}",
        explanation="; ".join(findings)[:2000],
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(update={"triage": (*result.triage, note)})
