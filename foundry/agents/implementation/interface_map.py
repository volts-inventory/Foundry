"""A compact signature map of the integrated tree (aider's repo-map idea): the
public declarations of the files a ticket builds on, with no bodies, so an agent
imports what exists instead of reading every file to learn it.

Python is read with ``ast`` (exact). TypeScript/JavaScript, Go and Rust are read
with a line scanner over exported top-level declarations and the members of
their class/interface/struct/trait/impl blocks; braces inside strings and
comments are stripped before counting, and multi-line signatures are joined."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import PurePosixPath

from foundry.canonical import content_digest

DEFAULT_BUDGET = 6000
MAP_LABEL = "INTERFACES ALREADY BUILT — import these exactly; read a file only if you need its body"

_SHORT = 80


def _short(node: ast.AST | None) -> str:
    if node is None:
        return ""
    text = ast.unparse(node)
    return text if len(text) <= _SHORT and "\n" not in text else "..."


def _public(name: str) -> bool:
    return not name.startswith("_")


def _decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, indent: str
) -> list[str]:
    out = []
    for d in node.decorator_list:
        text = ast.unparse(d)
        if len(text) <= 40:
            out.append(f"{indent}@{text}")
    return out


def _def(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> str:
    head = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{indent}{head} {node.name}({ast.unparse(node.args)}){ret}"


def _assign(node: ast.stmt, indent: str) -> list[str]:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if not _public(node.target.id):
            return []
        value = f" = {_short(node.value)}" if node.value is not None else ""
        return [f"{indent}{node.target.id}: {ast.unparse(node.annotation)}{value}"]
    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name) and _public(t.id)]
        return [f"{indent}{n} = {_short(node.value)}" for n in names]
    if type(node).__name__ == "TypeAlias":  # `type X = ...` (3.12+)
        return [f"{indent}{ast.unparse(node)}"]
    return []


def python_outline(text: str) -> list[str]:
    """Top-level classes (bases, public fields and method signatures), functions
    and module constants/type aliases of a Python module; ``[]`` when it does not
    parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            out += [*_decorators(node, ""), _def(node, "")]
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            bases = [ast.unparse(b) for b in node.bases] + [
                f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords if k.arg
            ]
            out += _decorators(node, "")
            out.append(
                f"class {node.name}({', '.join(bases)}):" if bases else f"class {node.name}:"
            )
            members: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _public(item.name) or item.name in ("__init__", "__call__"):
                        members += [*_decorators(item, "    "), _def(item, "    ")]
                elif isinstance(item, ast.ClassDef) and _public(item.name):
                    members.append(f"    class {item.name}: ...")
                else:
                    members += _assign(item, "    ")
            out += members or ["    ..."]
        else:
            out += _assign(node, "")
    return out


# ---- brace languages (regex line scanner) -------------------------------------

_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code(line: str) -> str:
    """``line`` without string literals and its ``//`` comment (for counting)."""
    return _STRING.sub('""', line).split("//", 1)[0]


_TS_TOP = re.compile(
    r"^export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\b|class\b|interface\b|type\b|enum\b|const\b|let\b|var\b|namespace\b)"
    r"|^export\s*(?:\{|\*)"
)
_TS_CONTAINER = re.compile(r"\b(?:class|interface|enum)\b|\btype\s+\w+.*=\s*\{")
_TS_MEMBER = re.compile(
    r"^(?:(?:public|readonly|static|async|abstract|get|set|override|declare)\s+)*"
    r"(?:constructor\b|\*?[A-Za-z$][\w$]*\??\s*[(:<=,]|[A-Za-z$][\w$]*\s*$)"
)
_GO_TOP = re.compile(r"^func\s+(?:\([^)]*\)\s*)?[A-Z]\w*|^type\s+[A-Z]\w*|^(?:const|var)\s+[A-Z]")
_GO_CONTAINER = re.compile(r"^type\s+\w+\s+(?:struct|interface)\s*\{")
_GO_MEMBER = re.compile(r"^[A-Z]\w*")
_RUST_TOP = re.compile(
    r"^pub(?:\([^)]*\))?\s+(?:(?:async|const|unsafe|extern\s+\"\w+\")\s+)*"
    r"(?:fn|struct|enum|trait|type|const|static|use)\b|^impl\b"
)
_RUST_CONTAINER = re.compile(r"\b(?:struct|enum|trait|impl)\b")


def _rust_member(line: str, header: str) -> bool:
    if header.startswith("impl"):
        return line.startswith("pub ") or line.startswith("pub(")
    if " trait " in f" {header} ":
        return bool(re.match(r"(?:async\s+|unsafe\s+)*fn\b|type\b|const\b", line))
    return bool(re.match(r"pub\b|[A-Z]\w*", line))


_TYPE_WORD = re.compile(r"\b(?:type|interface|struct|enum)\b")


def _head(stmt: str) -> str:
    """``stmt`` up to the ``{`` that opens its body (outside strings and parens),
    or whole when its braces close on the line (a one-line type or struct)."""
    code = _code(stmt)
    lead = code.split("{", 1)[0]
    if (
        "{" in code
        and code.count("{") == code.count("}")
        and len(stmt) <= 160
        and _TYPE_WORD.search(lead)
        and "(" not in lead
    ):
        return " ".join(stmt.split()).rstrip(";,").rstrip()
    depth, quote, cut = 0, "", len(stmt)
    for i, ch in enumerate(stmt):
        if quote:
            quote = "" if ch == quote and stmt[i - 1] != "\\" else quote
        elif ch in "\"'`":
            quote = ch
        elif ch in "(<[":
            depth += 1
        elif ch in ")>]" and not (ch == ">" and stmt[i - 1] == "="):
            depth = max(0, depth - 1)
        elif ch == "{" and depth == 0:
            cut = i
            break
    return " ".join(stmt[:cut].split()).rstrip(";,").rstrip()


def _brace_outline(
    text: str,
    top: re.Pattern[str],
    container: Callable[[str], bool],
    member: Callable[[str, str], bool],
) -> list[str]:
    lines = _BLOCK_COMMENT.sub("", text).splitlines()
    out: list[str] = []
    depth = 0
    header: str | None = None  # the open container's head, at depth 1
    shown = False
    i = 0
    while i < len(lines):
        stmt = lines[i].strip()
        # Join a multi-line signature until its parentheses balance.
        while (
            _code(stmt).count("(") > _code(stmt).count(")")
            and i + 1 < len(lines)
            and len(stmt) < 400
        ):
            i += 1
            stmt += " " + lines[i].strip()
        code = _code(stmt)
        if depth == 0 and stmt and top.match(stmt):
            head = _head(stmt)
            header = head if "{" in code and container(head) else None
            shown = not head.startswith("impl")
            if shown:
                out.append(head)
        elif depth == 1 and header is not None and stmt and member(stmt, header):
            if not shown:
                out.append(header)
                shown = True
            out.append("  " + _head(stmt))
        depth = max(0, depth + code.count("{") - code.count("}"))
        if depth == 0:
            header = None
        i += 1
    return out


def ts_outline(text: str) -> list[str]:
    return _brace_outline(
        text,
        _TS_TOP,
        lambda h: bool(_TS_CONTAINER.search(h)),
        lambda line, _h: (
            bool(_TS_MEMBER.match(line))
            and not line.startswith(("private ", "protected ", "#", "_", "return "))
        ),
    )


def go_outline(text: str) -> list[str]:
    return _brace_outline(
        text,
        _GO_TOP,
        lambda h: bool(_GO_CONTAINER.match(h + " {")),
        lambda line, _h: bool(_GO_MEMBER.match(line)),
    )


def rust_outline(text: str) -> list[str]:
    return _brace_outline(text, _RUST_TOP, lambda h: bool(_RUST_CONTAINER.search(h)), _rust_member)


_OUTLINERS: dict[str, Callable[[str], list[str]]] = {
    ".py": python_outline,
    ".pyi": python_outline,
    **dict.fromkeys((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"), ts_outline),
    ".go": go_outline,
    ".rs": rust_outline,
}


def _is_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name.startswith("test_")
        or name == "conftest.py"
        or name.endswith(
            (
                "_test.py",
                "_test.go",
                ".test.ts",
                ".test.js",
                ".spec.ts",
                ".spec.js",
                ".test.tsx",
                ".spec.tsx",
            )
        )
        or path.startswith(("tests/", "features/"))
    )


def outline(path: str, text: str) -> list[str]:
    fn = _OUTLINERS.get(PurePosixPath(path).suffix)
    return fn(text) if fn is not None else []


# ---- the map ------------------------------------------------------------------

_DECLARED = re.compile(
    r"\b(?:class|def|function|interface|type|struct|trait|enum|fn|func|const)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)"
)


def _expand(entries: Iterable[str], tree: Mapping[str, str | None]) -> list[str]:
    """Paths in ``tree`` named by ``entries`` (a file, or a directory prefix)."""
    out: list[str] = []
    for e in entries:
        if e.endswith("/") or (e not in tree and any(p.startswith(e + "/") for p in tree)):
            prefix = e.rstrip("/") + "/"
            out += sorted(p for p in tree if p.startswith(prefix))
        else:
            out.append(e)
    return out


def _mentions(path: str, lines: Sequence[str], text: str) -> int:
    stem = PurePosixPath(path).stem
    names = {stem, *(m.group(1) for line in lines for m in _DECLARED.finditer(line))}
    return sum(len(re.findall(rf"\b{re.escape(n)}\b", text)) for n in names if len(n) > 1)


def signature_map(
    tree: Mapping[str, str | None],
    *,
    contract_files: Sequence[str] = (),
    reads: Sequence[str] = (),
    dependency_files: Sequence[str] = (),
    exclude: Sequence[str] = (),
    mention_text: str = "",
    budget: int = DEFAULT_BUDGET,
) -> str:
    """The labelled signature map of ``tree`` (path -> text, the integrated tree),
    within ``budget`` characters: the contracts files first, then the files in
    ``reads``, then the dependency tickets' files — each group ranked by how often
    ``mention_text`` names the file's stem or its declarations. ``exclude`` (the
    ticket's own files) and test/feature files are left out. ``""`` when no file
    has a public declaration. The header carries a digest of the whole tree, so
    the prompt differs whenever the tree does (the call cache keys on it)."""
    skip = set(_expand(exclude, tree))
    seen: set[str] = set()
    picked: list[tuple[str, list[str]]] = []
    for group, ranked in ((contract_files, False), (reads, True), (dependency_files, True)):
        entries: list[tuple[int, int, str, list[str]]] = []
        for i, path in enumerate(_expand(group, tree)):
            text = tree.get(path)
            if (
                path in seen
                or path in skip
                or not isinstance(text, str)
                or (_is_test(path) and path not in contract_files)
            ):
                continue
            seen.add(path)
            lines = outline(path, text)
            if lines:
                score = _mentions(path, lines, mention_text) if ranked else 0
                entries.append((-score, i, path, lines))
        picked += [(p, ls) for _s, _i, p, ls in sorted(entries)]
    if not picked:
        return ""
    digest = content_digest({k: v for k, v in sorted(tree.items()) if v is not None})[:12]
    parts = [f"{MAP_LABEL} (integrated tree {digest}):"]
    used = len(parts[0])
    for path, lines in picked:
        block = "\n".join([f"## {path}", *lines])
        if used + len(block) + 1 <= budget:
            parts.append(block)
            used += len(block) + 1
            continue
        room = budget - used - len(f"## {path}") - 30
        kept: list[str] = []
        for line in lines:
            if room - len(line) - 1 < 0:
                break
            kept.append(line)
            room -= len(line) + 1
        if kept:
            parts.append("\n".join([f"## {path}", *kept, "... (truncated)"]))
        break
    return "\n".join(parts)
