"""The typed skeleton: deterministic Python stubs of the planned contracts.

Once the contracts ticket has written the contracts file, every module a later
ticket owns that implements a planned code contract is written as a stub — the
contract types imported from the contracts file, each planned function and method
with its exact planned signature and a ``raise NotImplementedError`` body — so the
wave-0 check type-checks the whole interface before any implementation exists, and
an implementation ticket fills in bodies rather than inventing signatures.

An interface-like class contract (``TodoStore``, or method contracts
``TodoStore.add``) is the contracts file's own declaration; its implementation is a
concrete subclass (``ProductService`` for ``ProductServiceInterface``/``IProductService``,
else ``<Class>Impl``), so the type checker holds every override to the contract and the
contract type is never redeclared. A data type (an Enum, dataclass, NamedTuple, a
``@final`` or ``type``-kind class) is defined in full by the contracts file: never
subclassed. Only signatures that parse as fully annotated
Python whose every name resolves (the contracts file, the standard prelude,
builtins) are stubbed; anything else is left to the ticket.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from foundry.agents.plan_contracts import _PY_PRELUDE, _py_normalize
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.implementation import CONTRACTS_TICKET_ID, Ticket

__all__ = [
    "IMPL_SUFFIX",
    "Skeleton",
    "impl_name",
    "is_stub",
    "python_skeleton",
    "signature_changes",
    "stub_signatures",
]

IMPL_SUFFIX = "Impl"
_KEPT_DECORATORS = frozenset({"staticmethod", "classmethod", "property"})
_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


_IFACE_AFFIX = re.compile(
    r"^(?:I(?=[A-Z][a-z])|Abstract(?=[A-Z]))|(?<=[a-z0-9])(?:Interface|Protocol|ABC)$"
)


def impl_name(cls: str, taken: Iterable[str] = ()) -> str:
    """The implementation class of contract ``cls``: its name without an interface
    prefix/suffix (``IProductService``, ``ProductServiceProtocol`` -> ``ProductService``)
    when that is free, else ``<cls>Impl``."""
    natural = _IFACE_AFFIX.sub("", cls)
    if natural != cls and natural.isidentifier() and natural not in set(taken):
        return natural
    return f"{cls}{IMPL_SUFFIX}"


# Bases and decorators of a class the contracts file defines in full: subclassing an
# Enum with members is a TypeError, a @final class cannot be subclassed, and moving
# a data type's method bodies into an unused subclass changes what the type does.
_DATA_BASES = frozenset(
    {
        "Enum",
        "IntEnum",
        "StrEnum",
        "Flag",
        "IntFlag",
        "ReprEnum",
        "NamedTuple",
        "TypedDict",
        "BaseModel",
        "Struct",
    }
)
_DATA_DECORATORS = frozenset(
    {"dataclass", "final", "define", "frozen", "mutable", "attrs", "s", "dataclass_json"}
)


def _tail(expr: ast.expr) -> str:
    """The last name of ``a.b.C``, ``C[...]`` or ``C(...)``."""
    while isinstance(expr, (ast.Call, ast.Subscript)):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return expr.id if isinstance(expr, ast.Name) else ""


def _data_classes(text: str) -> set[str]:
    """The classes of a contracts module that are data types, not interfaces."""
    try:
        nodes = [n for n in ast.parse(text).body if isinstance(n, ast.ClassDef)]
    except SyntaxError:
        return set()
    out: set[str] = set()
    grew = True
    while grew:
        grew = False
        for n in nodes:
            if n.name in out:
                continue
            if {_tail(d) for d in n.decorator_list} & _DATA_DECORATORS or {
                _tail(b) for b in n.bases
            } & (_DATA_BASES | out):
                out.add(n.name)
                grew = True
    return out


def is_stub(text: str) -> bool:
    """``text`` defines at least one function and every function body is still
    ``raise NotImplementedError`` (a generated stub, perhaps re-signed by a fixer)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    fns = [n for n in ast.walk(tree) if isinstance(n, _FUNCS)]

    def raises(fn: _FuncDef) -> bool:
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        return (
            len(body) == 1
            and isinstance(body[0], ast.Raise)
            and body[0].exc is not None
            and _tail(body[0].exc) == "NotImplementedError"
        )

    return bool(fns) and all(raises(f) for f in fns)


def _prelude_imports() -> dict[str, str]:
    """Name -> the prelude import statement that binds it."""
    out: dict[str, str] = {}
    for node in ast.parse(_PY_PRELUDE).body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name] = f"import {a.name}"
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for a in node.names:
                out[a.asname or a.name] = f"from {node.module} import {a.name}"
    return out


_PRELUDE = _prelude_imports()
_BUILTINS = frozenset(dir(builtins))


@dataclass
class Skeleton:
    files: dict[str, str] = field(default_factory=dict)  # path -> stub text
    owners: dict[str, str] = field(default_factory=dict)  # path -> implementing ticket id
    # contract name -> (implementation name, path), for the tickets that call it
    impls: dict[str, tuple[str, str]] = field(default_factory=dict)
    concerns: list[str] = field(default_factory=list)


def _module_of(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    return ".".join(parts)


def _top_names(text: str) -> tuple[set[str], set[str]]:
    """(every top-level name, the class names) a module declares."""
    try:
        body = ast.parse(text).body
    except SyntaxError:
        return set(), set()
    names: set[str] = set()
    classes: set[str] = set()
    for node in body:
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            classes.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names, classes


def _parse(signature: str) -> list[ast.stmt] | None:
    try:
        return ast.parse(_py_normalize(signature)).body
    except SyntaxError:
        return None


def _names_in(node: ast.AST) -> set[str]:
    """Every free name an annotation/default/decorator uses (string forward
    references included)."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            with contextlib.suppress(SyntaxError):
                out |= _names_in(ast.parse(n.value, mode="eval"))
    return out


def _signature_names(fn: _FuncDef) -> set[str]:
    a = fn.args
    parts: list[ast.AST] = [*a.defaults, *(d for d in a.kw_defaults if d is not None)]
    parts += [
        x.annotation
        for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, *(v for v in (a.vararg, a.kwarg) if v))
        if x.annotation
    ]
    if fn.returns is not None:
        parts.append(fn.returns)
    out: set[str] = set()
    for p in parts:
        out |= _names_in(p)
    return out


def _stub_def(fn: _FuncDef, *, method: bool) -> _FuncDef | None:
    """``fn`` with a ``raise NotImplementedError`` body, or None when a parameter
    or the return is unannotated (``__init__`` returns None)."""
    decorators: list[ast.expr] = [
        d
        for d in fn.decorator_list
        if method and isinstance(d, ast.Name) and d.id in _KEPT_DECORATORS
    ]
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in decorators)
    a = fn.args
    params = [*a.posonlyargs, *a.args]
    receiver = params[:1] if method and not static and params else []
    rest = [
        p for p in (*params[len(receiver) :], *a.kwonlyargs, *(v for v in (a.vararg, a.kwarg) if v))
    ]
    if any(p.annotation is None for p in rest):
        return None
    returns = fn.returns
    if returns is None:
        if fn.name != "__init__":
            return None
        returns = ast.Constant(None)
    out = copy.copy(fn)
    out.body = ast.parse("raise NotImplementedError").body
    out.decorator_list = decorators
    out.returns = returns
    return ast.fix_missing_locations(out)


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
        or "tests" in p.parts
    )


def _target_file(ticket: Ticket, key: str, contract_files: Iterable[str]) -> str | None:
    """The one module of ``ticket`` that implements ``key`` (a class or function
    name): its only source file, else the one whose name is a word of ``key``."""
    skip = set(contract_files)
    py = [f for f in ticket.files if f.endswith(".py") and f not in skip and not _is_test(f)]
    modules = [f for f in py if PurePosixPath(f).name != "__init__.py"] or py
    if len(modules) == 1:
        return modules[0]
    words = _snake(key).split("_")
    hits = [
        f for f in modules if PurePosixPath(f).stem in words or PurePosixPath(f).stem == _snake(key)
    ]
    return hits[0] if len(hits) == 1 else None


@dataclass
class _Module:
    owner: str
    functions: list[_FuncDef] = field(default_factory=list)
    classes: dict[str, list[_FuncDef]] = field(default_factory=dict)  # contract class -> methods
    names: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)  # import lines a signature carried


def _members(
    node: ast.stmt, owner: str, classes: set[str], data: set[str]
) -> tuple[str, list[_FuncDef]] | None:
    """(the contract class it implements or "" for a module function, its defs),
    or None when ``node`` is nothing to stub."""
    if isinstance(node, ast.ClassDef):
        defs = [m for m in node.body if isinstance(m, _FUNCS)]
        # A class the contracts file lacks has nothing to subclass; one with no
        # methods, or a data type, is a declaration the contracts file holds in full.
        return (node.name, defs) if node.name in classes - data and defs else None
    if not isinstance(node, _FUNCS) or owner in data:
        return None
    receiver = bool(node.args.args) and node.args.args[0].arg in ("self", "cls")
    if owner in classes:
        return owner, [node]
    return None if receiver else ("", [node])


def python_skeleton(
    contracts: Sequence[InterfaceContract],
    tickets: Sequence[Ticket],
    contract_texts: Mapping[str, str],
    exists: Iterable[str] = (),
) -> Skeleton:
    """The stub module of every file a later ticket owns that implements a planned
    Python code contract. ``contract_texts`` is the contracts file(s) as built
    (path -> text); ``exists`` are the paths already in the tree, never stubbed."""
    out = Skeleton()
    present = set(exists)
    declared: dict[str, str] = {}  # name -> its contracts module
    contract_classes: set[str] = set()
    data: set[str] = set()
    for path, text in contract_texts.items():
        if path.endswith(".py"):
            names, classes = _top_names(text)
            for n in names:
                declared.setdefault(n, _module_of(path))
            contract_classes |= classes
            data |= _data_classes(text)
    if not declared:
        return out
    by_name = {c.name: c for c in contracts}
    data |= {c.name for c in contracts if c.kind == "type"} & contract_classes
    impls: dict[str, str] = {}  # contract class -> its implementation's name

    def impl_of(cls: str) -> str:
        if cls not in impls:
            impls[cls] = impl_name(cls, {*declared, *impls.values()})
        return impls[cls]

    modules: dict[str, _Module] = {}
    for ticket in tickets:
        if ticket.id == CONTRACTS_TICKET_ID:
            continue
        for cname in ticket.implements:
            contract = by_name.get(cname)
            if contract is None or not contract.signature.strip():
                continue
            body = _parse(contract.signature)
            if body is None or not any(isinstance(n, (ast.ClassDef, *_FUNCS)) for n in body):
                out.concerns.append(
                    f"Contract {cname}: its signature is not Python code; "
                    f"left to ticket {ticket.id}."
                )
                continue
            carried = {
                (a.asname or a.name).split(".")[0]: ast.unparse(n)
                for n in body
                if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names
            }
            known = set(declared) | set(carried) | set(_PRELUDE) | _BUILTINS
            of_class = cname.rpartition(".")[0]
            for node in body:
                got = _members(node, of_class, contract_classes, data)
                if got is None:
                    held = node.name if isinstance(node, ast.ClassDef) else of_class
                    if held in data and (
                        isinstance(node, _FUNCS)
                        or (
                            isinstance(node, ast.ClassDef)
                            and any(isinstance(m, _FUNCS) for m in node.body)
                        )
                    ):
                        out.concerns.append(
                            f"Contract {cname}: {held} is a data type the contracts file "
                            f"defines in full; not stubbed."
                        )
                    continue
                cls, defs = got
                target_path = _target_file(ticket, cls or defs[0].name, contract_texts)
                if target_path is None or target_path in present:
                    why = "it already exists" if target_path else "no single module of the ticket"
                    out.concerns.append(
                        f"Contract {cname}: not stubbed ({why}); left to ticket {ticket.id}."
                    )
                    continue
                mod = modules.setdefault(target_path, _Module(owner=ticket.id))
                if mod.owner != ticket.id:
                    continue
                kept: list[_FuncDef] = []
                for d in defs:
                    stub = _stub_def(d, method=bool(cls))
                    names = (
                        set()
                        if stub is None
                        else _signature_names(stub).union(
                            *(_names_in(x) for x in stub.decorator_list)
                        )
                    )
                    if stub is None or not names <= known | ({impl_of(cls)} if cls else set()):
                        why = (
                            "is not fully annotated"
                            if stub is None
                            else "uses "
                            + ", ".join(sorted(names - known))
                            + ", which neither the contracts file nor the standard "
                            "library defines"
                        )
                        out.concerns.append(
                            f"Contract {cname}: {d.name} {why}; its stub "
                            f"is left to ticket {ticket.id}."
                        )
                        continue
                    kept.append(stub)
                    mod.names |= names
                    mod.imports |= {carried[n] for n in names if n in carried and n not in declared}
                target = mod.classes.setdefault(cls, []) if cls else mod.functions
                have = {s.name for s in target}
                target.extend(s for s in kept if s.name not in have)
                if cls:
                    mod.names.add(cls)
                    out.impls.setdefault(cname, (impl_of(cls), target_path))

    for path, mod in sorted(modules.items()):
        if not mod.functions and not any(mod.classes.values()):
            continue
        defined = {impl_of(c) for c in mod.classes} | {f.name for f in mod.functions}
        plain = set(mod.imports)
        froms: dict[str, list[str]] = {}
        for name in sorted(mod.names - defined):
            if name in declared:
                froms.setdefault(declared[name], []).append(name)
            elif name in _PRELUDE:
                plain.add(_PRELUDE[name])
        head = [
            "from __future__ import annotations",
            "",
            *sorted(plain),
            *(f"from {m} import {', '.join(ns)}" for m, ns in sorted(froms.items())),
        ]
        chunks: list[str] = []
        for cls, methods in mod.classes.items():
            node = ast.parse(f"class {impl_of(cls)}({cls}):\n    pass").body[0]
            assert isinstance(node, ast.ClassDef)
            node.body = list(methods) or node.body
            chunks.append(ast.unparse(node))
        chunks += [ast.unparse(f) for f in mod.functions]
        out.files[path] = "\n".join(head) + "\n\n\n" + "\n\n\n".join(chunks) + "\n"
        out.owners[path] = mod.owner
    for path in list(out.owners):
        for parent in PurePosixPath(path).parents:
            marker = f"{parent}/__init__.py"
            if str(parent) == "." or marker in present or marker in out.files:
                continue
            owner = next((t.id for t in tickets if marker in t.files), None)
            if owner is not None:
                out.files[marker] = ""
                out.owners[marker] = owner
    for cname, (_impl, p) in list(out.impls.items()):
        if p not in out.files:
            del out.impls[cname]
    return out


# ---- the public-signature check ------------------------------------------------


def _fingerprint(fn: _FuncDef) -> str:
    head = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"{head} {fn.name}({ast.unparse(fn.args)}){ret}"


def stub_signatures(text: str) -> dict[str, str] | None:
    """Qualified name -> signature of every top-level function and class method in
    ``text`` (a class maps to its base list); None when it does not parse."""
    try:
        body = ast.parse(text).body
    except SyntaxError:
        return None
    out: dict[str, str] = {}
    for node in body:
        if isinstance(node, _FUNCS):
            out[node.name] = _fingerprint(node)
        elif isinstance(node, ast.ClassDef):
            out[node.name] = "class " + ", ".join(ast.unparse(b) for b in node.bases)
            for m in node.body:
                if isinstance(m, _FUNCS):
                    out[f"{node.name}.{m.name}"] = _fingerprint(m)
    return out


def signature_changes(stub: str, written: str | None) -> tuple[str, ...]:
    """One finding per stubbed name ``written`` (the ticket's final file; None if
    deleted) removes, or whose parameter list or return annotation it changes. A
    class keeps every base the stub gave it; anything added is free."""
    planned = stub_signatures(stub) or {}
    if written is None:
        return tuple(f"{n} was removed (the file is gone)" for n in planned)
    now = stub_signatures(written)
    if now is None:
        return ()  # a syntax error is the syntax check's finding
    out: list[str] = []
    for name, sig in planned.items():
        got = now.get(name)
        if got is None:
            out.append(f"{name} was removed or renamed (planned: `{sig}`)")
        elif sig.startswith("class "):
            want = {b.strip() for b in sig.removeprefix("class ").split(",") if b.strip()}
            have = {b.strip() for b in got.removeprefix("class ").split(",")}
            if not want <= have:
                out.append(f"class {name} dropped its base {', '.join(sorted(want - have))}")
        elif got != sig:
            out.append(f"{name} changed from `{sig}` to `{got}`")
    return tuple(out)
