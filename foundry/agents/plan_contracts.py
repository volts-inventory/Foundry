"""Plan-time compile of the planned code contracts.

The contract signatures become the contracts file the wave-0 ticket writes, so a
signature the language's own checker rejects (a method named after a builtin its
annotations use, an undefined type) otherwise surfaces only at the first BUILD
gate, after agents already ran. Here the signatures are assembled into one file
and checked offline in the gate sandbox (``ground_untrusted``), when that
toolchain is installed. Only checker errors become findings: a missing
toolchain, a signature that is not code, a crash or a timeout is a concern.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ENV", "CompileResult", "assemble_python", "assemble_typescript", "check_contracts"]

# Operator / test switch: "0" (or off/false/no) skips the check entirely.
ENV = "FOUNDRY_PLAN_COMPILE"
_OFF = frozenset({"0", "off", "false", "no"})
_TIMEOUT_S = 60.0
_MAX_FINDINGS = 12

# Names a signature commonly uses without importing them.
_PY_PRELUDE = """\
from __future__ import annotations
import abc
import decimal
import enum
import typing
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence,
)
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import (
    Any, ClassVar, Final, Generic, Literal, NamedTuple, NewType, Optional, Protocol,
    TypedDict, TypeVar, Union,
)
from uuid import UUID
"""

# mypy codes that are artefacts of checking bare declarations, not contract errors:
# a stub body has no return; two contracts may each restate a shared type.
_PY_IGNORED_CODES = ("empty-body", "no-redef")
_PY_ERROR = re.compile(r"^contracts\.py:(\d+): error: (.*?)(?:\s+\[([\w-]+)\])?$")
# tsc: syntax errors (TS1xxx) mean the fragment is not assemblable code; duplicate
# identifiers, a missing implementation and unresolved modules are assembly artefacts.
_TS_IGNORED = frozenset({"TS2300", "TS2391", "TS2307", "TS2792"})
_TS_ERROR = re.compile(r"^contracts\.ts\((\d+),\d+\): error (TS\d+): (.*)$")
_TS_EXPORT = re.compile(r"^(?:export\s+)?(?:declare\s+)?")
_TS_DECL = re.compile(
    r"^(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(interface|type|class|function|enum|const|let|var|namespace)\b"
)


@dataclass(frozen=True)
class CompileResult:
    findings: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    ran: bool = False  # the checker ran and its verdict is in ``findings``


@dataclass
class _Source:
    lines: list[str]
    ranges: list[tuple[int, int, str]]  # (first line, last line, contract name)
    concerns: list[str]

    def emit(self, text: str, name: str) -> None:
        start = len(self.lines) + 1
        self.lines.extend(text.splitlines())
        self.ranges.append((start, len(self.lines), name))

    def owner_of(self, line: int) -> str | None:
        return next((n for a, b, n in self.ranges if a <= line <= b), None)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---- python ----------------------------------------------------------------------

# A one-line ``def`` header with no colon after its parameters / return type.
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s.*\)\s*(?:->[^:]*)?$")
_PY_BLOCK = re.compile(r"^\s*(?:@.*|(?:async\s+)?def\s.*|class\s.*):\s*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _py_normalize(signature: str) -> str:
    """A signature as parseable Python: a bare ``def f(x) -> T`` gains ``: ...``
    and a ``def``/``class`` header with no body gains ``...``."""
    lines = textwrap.dedent(signature.expandtabs()).strip("\n").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if _PY_DEF.match(line.rstrip()):
            line = line.rstrip() + ": ..."
        elif _PY_BLOCK.match(line) and not line.lstrip().startswith("@"):
            rest = [ln for ln in lines[i + 1:] if ln.strip()]
            if not rest or _indent(rest[0]) <= _indent(line):
                line = line.rstrip() + " ..."
        out.append(line)
    return "\n".join(out)


# Statements a contract may consist of; anything else (prose that happens to parse,
# `this is prose`) is not a declaration.
_PY_DECLARATIONS = (
    ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Import, ast.ImportFrom,
    ast.Assign, ast.AnnAssign,
)


def assemble_python(contracts: Sequence[tuple[str, str]]) -> _Source:
    """One module from the signatures, behind a prelude of the standard imports. A
    method contract (``Store.add`` whose signature is only ``def``s) joins the class
    another contract declares, else a ``Protocol`` of that name."""
    src = _Source(lines=_PY_PRELUDE.splitlines(), ranges=[], concerns=[])
    top: list[tuple[str, list[ast.stmt]]] = []
    methods: dict[str, list[tuple[str, ast.stmt]]] = {}
    for name, signature in contracts:
        try:
            body = ast.parse(_py_normalize(signature)).body
        except SyntaxError:
            body = []
        if not body or not all(isinstance(n, _PY_DECLARATIONS) for n in body):
            src.concerns.append(
                f"Contract {name}: its signature is not valid Python, so it was not "
                "compiled at plan time."
            )
            continue
        owner = name.rpartition(".")[0]
        if owner and body and all(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in body
        ):
            methods.setdefault(owner, []).extend((name, n) for n in body)
        else:
            top.append((name, body))

    def member_names(cls: ast.ClassDef) -> set[str]:
        return {
            n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    for name, body in top:
        for node in body:
            src.emit(ast.unparse(node), name)
            if isinstance(node, ast.ClassDef) and node.name in methods:
                have = member_names(node)
                for mname, m in methods.pop(node.name):
                    if getattr(m, "name", "") not in have:
                        src.emit(textwrap.indent(ast.unparse(m), "    "), mname)
    for owner, members in methods.items():
        if not owner.isidentifier():
            continue
        src.emit(f"class {owner}(Protocol):", members[0][0])
        for mname, m in members:
            src.emit(textwrap.indent(ast.unparse(m), "    "), mname)
    return src


# ---- typescript ------------------------------------------------------------------


def assemble_typescript(contracts: Sequence[tuple[str, str]]) -> _Source:
    """One module from the signatures. Declarations are kept (a class or function
    made ``declare``, so a bodiless member is legal); a bare member signature of a
    dotted contract (``Store.add`` -> ``add(x: T): R``) joins ``interface Store``,
    which TypeScript merges with any other declaration of it."""
    src = _Source(lines=[], ranges=[], concerns=[])
    members: dict[str, list[tuple[str, str]]] = {}
    for name, signature in contracts:
        sig = textwrap.dedent(signature).strip()
        owner = name.rpartition(".")[0]
        kind = _TS_DECL.match(sig)
        if kind is None and owner:
            members.setdefault(owner, []).append((name, sig.rstrip(";") + ";"))
            continue
        if kind is not None:
            ambient = kind.group(1) not in ("interface", "type", "namespace")
            sig = ("export declare " if ambient else "export ") + _TS_EXPORT.sub("", sig, 1)
        src.emit(sig, name)
    for owner, items in members.items():
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", owner):
            continue
        src.emit(f"export interface {owner} {{", items[0][0])
        for mname, sig in items:
            src.emit(textwrap.indent(sig, "  "), mname)
        src.emit("}", items[-1][0])
    return src


# ---- running the checker ---------------------------------------------------------


def _python_cmd(strict: bool) -> list[str] | None:
    if shutil.which("python3") is None:
        return None
    return [
        "python3", "-m", "mypy", "--no-error-summary", "--show-error-codes",
        "--hide-error-context", "--no-color-output", "--no-incremental",
        "--cache-dir", os.devnull, "--ignore-missing-imports",
        *(f"--disable-error-code={c}" for c in _PY_IGNORED_CODES),
        *(["--strict"] if strict else []), "contracts.py",
    ]


def _typescript_cmd() -> list[str] | None:
    flags = ["--noEmit", "--strict", "--skipLibCheck", "--pretty", "false",
             "--target", "es2022", "--module", "esnext", "contracts.ts"]
    if shutil.which("tsc"):
        return ["tsc", *flags]
    if shutil.which("npx"):  # offline: only a tsc already installed resolves
        return ["npx", "--no-install", "tsc", *flags]
    return None


def check_contracts(
    contracts: Sequence[tuple[str, str]], language: str, *, strict: bool = False,
) -> CompileResult:
    """``contracts`` are ``(name, signature)`` of the code-kind contracts. Python
    runs ``python3 -m mypy`` (``--strict`` when the stack's own typecheck is
    strict); TypeScript runs ``tsc --noEmit``; other languages are not checked."""
    if os.environ.get(ENV, "").strip().lower() in _OFF:
        return CompileResult()
    pairs = [(n, s) for n, s in contracts if s.strip()]
    if not pairs or language not in ("python", "typescript"):
        return CompileResult()
    python = language == "python"
    tool = "mypy" if python else "tsc"
    cmd = _python_cmd(strict) if python else _typescript_cmd()
    if cmd is None:
        return CompileResult(
            concerns=(f"Contracts not compiled at plan time: no {tool} toolchain here.",)
        )
    concerns: list[str] = []
    for _ in range(2):
        src = assemble_python(pairs) if python else assemble_typescript(pairs)
        concerns += src.concerns
        if not src.ranges:
            return CompileResult(concerns=tuple(concerns))
        try:
            rc, out = _run(cmd, "contracts.py" if python else "contracts.ts", src.text())
        except Exception as exc:  # unavailable sandbox/tool, timeout: never a finding
            return CompileResult(concerns=(
                *concerns, f"Contracts not compiled at plan time: {tool} did not run ({exc}).",
            ))
        errors, unparsed = (_parse_mypy if python else _parse_tsc)(out, src)
        if unparsed and not errors:
            # tsc stops at syntax errors: check the rest without those signatures.
            concerns += [
                f"Contract {name}: {tool} could not parse its signature, so it was not "
                "compiled at plan time." for name in unparsed
            ]
            pairs = [(n, s) for n, s in pairs if n not in unparsed]
            continue
        break
    if rc == 0 and not errors:
        return CompileResult(concerns=tuple(concerns), ran=True)
    if not errors:
        detail = out.strip().splitlines()
        return CompileResult(concerns=(
            *concerns, f"Contracts not compiled at plan time: {tool} exited {rc}"
            + (f" ({detail[-1][:200]})" if detail else "") + ".",
        ))
    findings = [f"Contract {name} does not compile ({tool}): {msg}" for name, msg in errors]
    if len(findings) > _MAX_FINDINGS:
        findings = [*findings[:_MAX_FINDINGS],
                    f"... and {len(findings) - _MAX_FINDINGS} more {tool} error(s)."]
    return CompileResult(findings=tuple(findings), concerns=tuple(concerns), ran=True)


def _run(cmd: list[str], filename: str, text: str) -> tuple[int, str]:
    from foundry.gates.sandbox import ground_untrusted

    with tempfile.TemporaryDirectory(prefix="foundry-contracts-") as tmp:
        (Path(tmp) / filename).write_text(text, encoding="utf-8")
        rc, out, err = ground_untrusted(cmd, Path(tmp), timeout_s=_TIMEOUT_S)
    return rc, out + "\n" + err


def _parse_mypy(output: str, src: _Source) -> tuple[list[tuple[str, str]], list[str]]:
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        m = _PY_ERROR.match(line.strip())
        if m is None:
            continue
        name = src.owner_of(int(m.group(1)))
        if name is not None:
            out.append((name, m.group(2) + (f" [{m.group(3)}]" if m.group(3) else "")))
    return _uniq(out), []


def _parse_tsc(output: str, src: _Source) -> tuple[list[tuple[str, str]], list[str]]:
    out: list[tuple[str, str]] = []
    unparsed: list[str] = []
    for line in output.splitlines():
        m = _TS_ERROR.match(line.strip())
        if m is None:
            continue
        code, name = m.group(2), src.owner_of(int(m.group(1)))
        if name is None or code in _TS_IGNORED:
            continue
        if code.startswith("TS1"):
            unparsed.append(name)
        else:
            out.append((name, f"{m.group(3)} [{code}]"))
    return _uniq(out), list(dict.fromkeys(unparsed))


def _uniq(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(pairs))
