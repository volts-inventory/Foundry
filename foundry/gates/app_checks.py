"""Static app checks: per-language regex rules for app-safety footguns the tests
rarely catch (a hardcoded secret, a SQLite connection shared across server threads,
an in-memory database for data that must persist, a fast digest on passwords, SQL
built by string formatting, a form POST without CSRF, one data type defined twice).

BUILD runs them over the tree after its commands pass (``BuildGate``): a hit FAILs
the build with one ``path:line`` finding per hit, each naming its fix, so the fix
loop can act on it. The same rules, as prose, are the stack constraint
:func:`app_check_constraints` gives the planner, so tickets avoid them up front.

Test code is never scanned: an in-memory database or a literal key is right there.
"""

from __future__ import annotations

import ast
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .profiles import SOURCE_SUFFIXES

if TYPE_CHECKING:
    from .profiles import VerificationProfile

__all__ = [
    "APP_CHECKS_FAILED",
    "app_check_constraints",
    "app_check_findings",
    "app_check_language",
    "has_app_source",
    "is_app_source",
]

APP_CHECKS_FAILED = (
    "the build commands passed, but the static app checks found unsafe patterns — "
    "FAIL: fix each line below"
)

_MAX_FILE_BYTES = 512 * 1024
_MAX_FINDINGS = 25
_SKIP_DIRS = frozenset(
    {
        ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
        "vendor", "site-packages", ".mypy_cache", ".pytest_cache", "target",
        "tests", "test", "__tests__", "spec", "features", "steps", "testdata",
    }
)  # fmt: skip
_TEST_FILE = re.compile(
    r"^(test_.*\.py|.*_test\.py|conftest\.py|.*_test\.go|.*\.(test|spec)\.[cm]?[jt]sx?)$"
)
_SUFFIXES: dict[str, tuple[str, ...]] = {
    lang: SOURCE_SUFFIXES[lang] for lang in ("python", "javascript", "go")
}
_LANGUAGE_ALIASES = {"typescript": "javascript", "js": "javascript", "ts": "javascript"}


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    fix: str


# ---- the rules, as the findings and the planner constraint name them --------------

_SECRET = _Rule(
    "hardcoded-secret",
    "a secret key / password literal, or a literal fallback for one: read it from "
    "the environment and fail at startup when it is unset (or generate one with "
    "secrets.token_hex / crypto.randomBytes for a dev run), never ship a default",
)
_SQLITE_THREADS = _Rule(
    "sqlite-threads",
    "a sqlite3 connection opened at module level (or as a class attribute or "
    "global singleton) without check_same_thread in a web app: a threaded server "
    "uses it from other request threads and every request after the first 500s; "
    "open the connection per request inside a function (e.g. Flask's g in "
    "get_db()) and close it at request teardown; check_same_thread=False on a "
    "shared connection still races under concurrent requests",
)
_MEMORY_DB = _Rule(
    "memory-db",
    "an in-memory database outside the tests: its data is gone when the process "
    "exits; use a file database whose path comes from the environment (default "
    "e.g. app.db) and keep ':memory:' for the tests",
)
_WEAK_HASH = _Rule(
    "weak-password-hash",
    "a password hashed with a fast digest (md5/sha*): use a salted password KDF "
    "(werkzeug.security.generate_password_hash, hashlib.scrypt/pbkdf2_hmac, bcrypt; "
    "Node crypto.scrypt; Go bcrypt) and a constant-time compare",
)
_SQL_FORMAT = _Rule(
    "sql-format",
    "SQL built by string formatting: pass every value as a bound parameter "
    "(? / %s / $1) and never interpolate user input into the statement",
)
_LIKE_ESCAPE = _Rule(
    "like-unescaped",
    "a LIKE pattern built from user input without escaping its % and _: escape "
    "them (replace \\ then % and _ with a \\ prefix) and add ESCAPE '\\' to the LIKE",
)
_CSRF = _Rule(
    "csrf-missing",
    "a form POST handler with no CSRF protection: enable it app-wide (Flask-WTF "
    "CSRFProtect, csurf / a same-site token check) and render the token in every form",
)

# ---- Python -------------------------------------------------------------------

_PY_WEB = re.compile(
    r"^\s*(?:from|import)\s+(flask|fastapi|django|bottle|starlette|aiohttp|tornado|"
    r"falcon|quart|sanic|http\.server|wsgiref)\b",
    re.MULTILINE,
)
_PY_FLASK = re.compile(r"^\s*(?:from|import)\s+flask\b", re.MULTILINE)
_SECRET_NAME = r"(?:\w+_)?(?:secret(?:_key)?|password|passwd)"
# `SECRET_KEY = "dev"`, `app.secret_key = 'x'`, `app.config["SECRET_KEY"] = "x"`,
# `{"SECRET_KEY": "x"}`, `SECRET_KEY: str = "x"`.
_PY_SECRET_LITERAL = re.compile(
    rf"(?i)\b{_SECRET_NAME}\b['\"]?\]?\s*(?::\s*[\w\[\], |]+\s*)?[:=]\s*[rbu]?['\"][^'\"\n]+['\"]"
)
_PY_SECRET_FALLBACK = re.compile(
    r"(?i)(?:getenv|environ\.get|environ\.setdefault)\(\s*['\"][^'\"]*"
    r"(?:secret|key|token|password|passwd)[^'\"]*['\"]\s*"
    r"(?:,\s*[rbu]?['\"][^'\"\n]+['\"]|\)\s*or\s*[rbu]?['\"][^'\"\n]+['\"])"
)
_MEMORY = re.compile(r"""['"](?::memory:|sqlite://|sqlite:///:memory:)['"]|mode=memory""")
_PY_FAST_DIGEST = re.compile(r"\bhashlib\.(?:md5|sha1|sha224|sha256|sha384|sha512)\s*\(")
_PY_PASSWORD_WORD = re.compile(r"(?i)pass(?:word|wd)?|pwd")
_PY_DEF = re.compile(r"^(?P<indent>\s*)def\s+(?P<name>\w+)\s*\(", re.MULTILINE)
_SQL_SHAPE = re.compile(
    r"(?i)\b(?:select\b.*\bfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)\b"
)
_SQL_VALUE_CLAUSE = re.compile(r"(?i)\b(?:where|like|values|set)\b")
# A formatted fragment that is not a value: a placeholder list or a column list.
_SQL_SAFE_HOLE = re.compile(
    r"(?i)placeholder|marks|qmarks|clause|columns?|cols|fields|join|table|order|sort|limit"
)
_PY_FSTRINGS = (
    re.compile(r"""\b[rR]?[fF][rR]?(?P<q>'''|\"\"\")(?P<body>.*?)(?P=q)""", re.DOTALL),
    re.compile(r"""\b[rR]?[fF][rR]?(?P<q>['"])(?P<body>(?:\\.|(?!(?P=q))[^\n])*)(?P=q)"""),
)
_PY_FORMAT_SQL = re.compile(
    r"""(?P<q>['"])(?P<body>[^'"\n]*)(?P=q)\s*(?:%\s*[\w(]|\.format\()""",
)
# A parametrized SQL LIKE (the pattern is built in code), and the SQL ESCAPE clause
# that makes escaping the user's % and _ possible at all.
_SQL_LIKE = re.compile(r"(?i)\bLIKE\s+(?:\?|%s|:\w+|\$\d+|'%'\s*\|\|)")
_SQL_LIKE_CONCAT = re.compile(r"(?i)\bLIKE\s+'%'\s*\|\|")
_ESCAPE = re.compile(r"(?i)\bESCAPE\s+['\"]")
_PY_WILDCARD = re.compile(r"""[fF](['"])%?\{[^}]+\}%\1|[fF](['"])%\{[^}]+\}\2|(['"])%\3\s*\+""")
_PY_FORM_POST = re.compile(r"\brequest\.form\b")
_CSRF_WORD = re.compile(r"(?i)csrf")


def _hole_names(body: str, open_: str = "{", close: str = "}") -> list[str]:
    out, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        if ch == open_:
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == close and depth:
            depth -= 1
            if depth == 0:
                out.append(body[start:i])
    return [h for h in out if h and not h.startswith("{")]


def _value_holes(sql: str, holes: list[str]) -> bool:
    """Whether a formatted SQL string interpolates a value: a hole after a WHERE /
    VALUES / SET / LIKE clause that is not a placeholder or column list."""
    clause = _SQL_VALUE_CLAUSE.search(sql)
    if clause is None:
        return False
    after = sql[clause.start() :]
    return any(h in after and not _SQL_SAFE_HOLE.search(h) for h in holes)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _like_hits(text: str, wildcard: re.Pattern[str]) -> Iterator[tuple[int, _Rule]]:
    """Each place a file with a parametrized LIKE and no ESCAPE clause wraps a value
    in % (or concatenates it in SQL)."""
    if not _SQL_LIKE.search(text) or _ESCAPE.search(text):
        return
    for pattern in (wildcard, _SQL_LIKE_CONCAT):
        for m in pattern.finditer(text):
            yield _line_of(text, m.start()), _LIKE_ESCAPE


_TEST_WORDS = frozenset({"test", "tests", "testing"})
_PY_HEADER = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)")
_JS_GO_HEADER = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\*?|class|func(?:\s*\([^)]*\))?)\s+(\w+)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function)"
)
_IF_HEADER = re.compile(r"^\s*(?:\}\s*else\s+)?(?:el)?if\b(.*)")


def _testy(text: str) -> bool:
    """Whether an identifier or condition names testing as a word (``TestingConfig``,
    ``test_db``, ``app.testing``), not as a substring (``latest``, ``contest``)."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", text)
    return any(w.lower() in _TEST_WORDS for w in words)


def _strip_comment(line: str, marker: str) -> str:
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif line.startswith(marker, i):
            return line[:i]
        i += 1
    return line


def _test_context(lines: list[str], n: int, header: re.Pattern[str]) -> bool:
    """Whether 1-based line ``n`` sits in test-only code: a function/class named for
    testing, an ``if`` on a testing flag (enclosing it by indentation), or a
    conditional expression on one on the line itself."""
    line = lines[n - 1]
    tail = line.split(" if ", 1)
    if (len(tail) == 2 and _testy(tail[1])) or ("?" in line and _testy(line.split("?", 1)[0])):
        return True
    indent = len(line) - len(line.lstrip())
    for prev in reversed(lines[: n - 1]):
        if not prev.strip():
            continue
        level = len(prev) - len(prev.lstrip())
        if level >= indent:
            continue
        indent = level
        if (m := header.match(prev)) and _testy(m.group(m.lastindex or 1) or ""):
            return True
        if (m := _IF_HEADER.match(prev)) and _testy(m.group(1)):
            return True
        if level == 0:
            break
    return False


def _memory_hits(text: str, comment: str, header: re.Pattern[str]) -> Iterator[tuple[int, _Rule]]:
    lines = text.splitlines()
    for n, line in _code_lines(text, comment):
        if _MEMORY.search(_strip_comment(line, comment)) and not _test_context(lines, n, header):
            yield n, _MEMORY_DB


def _py_shared_connects(text: str) -> Iterator[int]:
    """Lines of each ``sqlite3.connect`` without check_same_thread whose connection
    outlives a request: at module level, in a class body, or assigned to a name a
    function declares ``global``/``nonlocal``."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return

    def bare(call: ast.Call) -> bool:
        f = call.func
        return (
            isinstance(f, ast.Attribute)
            and f.attr == "connect"
            and isinstance(f.value, ast.Name)
            and f.value.id == "sqlite3"
            and not any(k.arg == "check_same_thread" for k in call.keywords)
        )

    def scan(body: list[ast.stmt], shared: bool, globals_: set[str]) -> Iterator[int]:
        todo: list[ast.AST] = list(body)
        while todo:
            node = todo.pop(0)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = {
                    name
                    for n in ast.walk(node)
                    if isinstance(n, (ast.Global, ast.Nonlocal))
                    for name in n.names
                }
                yield from scan(node.body, False, names)
            elif isinstance(node, ast.ClassDef):
                yield from scan(node.body, True, set())
            elif isinstance(node, ast.Lambda):
                continue
            elif isinstance(node, ast.Call) and bare(node) and shared:
                yield node.lineno
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not shared:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is not None and any(
                    isinstance(t, ast.Name) and t.id in globals_ for t in targets
                ):
                    yield from (
                        c.lineno
                        for c in ast.walk(node.value)
                        if isinstance(c, ast.Call) and bare(c)
                    )
                todo.extend(ast.iter_child_nodes(node))
            else:
                todo.extend(ast.iter_child_nodes(node))

    yield from scan(tree.body, True, set())


def _code_lines(text: str, comment: str) -> Iterator[tuple[int, str]]:
    for n, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith(comment):
            yield n, line


def _python(rel: str, text: str, tree: _Tree) -> Iterator[tuple[int, _Rule]]:
    for n, line in _code_lines(text, "#"):
        if _PY_SECRET_LITERAL.search(line) or _PY_SECRET_FALLBACK.search(line):
            yield n, _SECRET
        if _PY_FAST_DIGEST.search(line) and _PY_PASSWORD_WORD.search(line):
            yield n, _WEAK_HASH
    yield from _memory_hits(text, "#", _PY_HEADER)
    if tree.web:
        for at in _py_shared_connects(text):
            yield at, _SQLITE_THREADS
    yield from _py_password_functions(text)
    for pattern in _PY_FSTRINGS:
        for m in pattern.finditer(text):
            body = m.group("body")
            if _SQL_SHAPE.search(body) and _value_holes(body, _hole_names(body)):
                yield _line_of(text, m.start()), _SQL_FORMAT
    for m in _PY_FORMAT_SQL.finditer(text):
        body = m.group("body")
        if _SQL_SHAPE.search(body) and _SQL_VALUE_CLAUSE.search(body):
            yield _line_of(text, m.start()), _SQL_FORMAT
    yield from _like_hits(text, _PY_WILDCARD)
    if tree.flask and not tree.csrf:
        form = _PY_FORM_POST.search(text)
        if form is not None:
            yield _line_of(text, form.start()), _CSRF


def _py_password_functions(text: str) -> Iterator[tuple[int, _Rule]]:
    """A fast digest inside a function whose name says it handles passwords."""
    lines = text.splitlines()
    for m in _PY_DEF.finditer(text):
        if not _PY_PASSWORD_WORD.search(m.group("name")):
            continue
        indent = len(m.group("indent"))
        start = _line_of(text, m.start())
        for n in range(start, len(lines)):
            line = lines[n]
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            if _PY_FAST_DIGEST.search(line) and not _PY_PASSWORD_WORD.search(line):
                yield n + 1, _WEAK_HASH


# ---- JavaScript / TypeScript ----------------------------------------------------

_JS_SECRET_FALLBACK = re.compile(
    r"(?i)process\.env\.\w*(?:secret|key|token|password)\w*\s*(?:\|\||\?\?)\s*['\"`][^'\"`\n]+"
)
_JS_SECRET_LITERAL = re.compile(
    r"(?i)\b(?:secret|\w+_?secret(?:_?key)?|password)\s*[:=]\s*['\"`][^'\"`\n$]+['\"`]"
)
_JS_FAST_DIGEST = re.compile(r"createHash\(\s*['\"](?:md5|sha1|sha256|sha512)['\"]")
_JS_TEMPLATE = re.compile(r"`(?P<body>[^`]*)`")
_JS_WILDCARD = re.compile(r"`%\$\{|(['\"])%\1\s*\+|\+\s*(['\"])%\2")
_JS_FORM = re.compile(r"\b(?:express|bodyParser)\.urlencoded\s*\(")


def _javascript(rel: str, text: str, tree: _Tree) -> Iterator[tuple[int, _Rule]]:
    lines = text.splitlines()
    yield from _memory_hits(text, "//", _JS_GO_HEADER)
    for n, line in _code_lines(text, "//"):
        if _JS_SECRET_FALLBACK.search(line) or _JS_SECRET_LITERAL.search(line):
            yield n, _SECRET
        if _JS_FAST_DIGEST.search(line):
            window = " ".join(lines[max(0, n - 3) : n + 2])
            if _PY_PASSWORD_WORD.search(window):
                yield n, _WEAK_HASH
    for m in _JS_TEMPLATE.finditer(text):
        body = m.group("body")
        holes = re.findall(r"\$\{([^}]*)\}", body)
        if _SQL_SHAPE.search(body) and _value_holes(body, holes):
            yield _line_of(text, m.start()), _SQL_FORMAT
    yield from _like_hits(text, _JS_WILDCARD)
    if not tree.csrf:
        form = _JS_FORM.search(text)
        if form is not None and re.search(r"\.post\s*\(", text):
            yield _line_of(text, form.start()), _CSRF


# ---- Go ---------------------------------------------------------------------------

_GO_SECRET_LITERAL = re.compile(
    r"(?i)\b(?:\w*secret\w*|\w*(?:jwt|session|signing)key|(?:admin|default)?password)\s*"
    r"(?::=|=)\s*(?:\[\]byte\()?\"[^\"\n]+\""
)
_GO_FAST_DIGEST = re.compile(r"\b(?:md5|sha1|sha256|sha512)\.(?:Sum\w*|New)\s*\(")
_GO_SPRINTF_SQL = re.compile(r"fmt\.Sprintf\(\s*[\"`](?P<body>[^\"`]*)[\"`]")
_GO_WILDCARD = re.compile(r"\"%\"\s*\+|\+\s*\"%\"")


def _go(rel: str, text: str, tree: _Tree) -> Iterator[tuple[int, _Rule]]:
    yield from _memory_hits(text, "//", _JS_GO_HEADER)
    for n, line in _code_lines(text, "//"):
        if _GO_SECRET_LITERAL.search(line):
            yield n, _SECRET
        if _GO_FAST_DIGEST.search(line) and _PY_PASSWORD_WORD.search(line):
            yield n, _WEAK_HASH
    for m in _GO_SPRINTF_SQL.finditer(text):
        body = m.group("body")
        if _SQL_SHAPE.search(body) and re.search(r"%[sdvq]", body):
            yield _line_of(text, m.start()), _SQL_FORMAT
    yield from _like_hits(text, _GO_WILDCARD)


# ---- duplicate types ------------------------------------------------------------

_DUPLICATE_TYPE = _Rule(
    "duplicate-type",
    "the same data type (same name, same fields) defined in two source files: two "
    "tickets each wrote their own copy, so values of one are not the other; keep one "
    "definition (the contracts file's, when either is it) and import it everywhere",
)

# A type whose fields we can name: `(name, line, fields)`. A type with no fields
# (a bare error class, a marker) is never compared: same-named ones are too common.
_TypeDef = tuple[str, int, frozenset[str]]


def _py_types(text: str) -> Iterator[_TypeDef]:
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.add(stmt.target.id)
            elif isinstance(stmt, ast.Assign):
                fields.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
            elif isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__":
                for sub in ast.walk(stmt):
                    targets = (
                        sub.targets
                        if isinstance(sub, ast.Assign)
                        else [sub.target]
                        if isinstance(sub, ast.AnnAssign)
                        else []
                    )
                    fields.update(
                        t.attr
                        for t in targets
                        if isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                    )
        fields.discard("__slots__")
        if fields:
            yield node.name, node.lineno, frozenset(fields)


def _brace_body(text: str, start: int) -> str:
    """The text between the ``{`` at/after ``start`` and its matching ``}``."""
    open_at = text.find("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    return text[open_at + 1 :]


def _top_level_lines(body: str) -> Iterator[str]:
    """``body``'s lines at brace depth 0 (a member, not a method's statements)."""
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            yield line
        depth += line.count("{") - line.count("}")


_JS_TYPE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:class|interface)\s+([A-Za-z_$][\w$]*)[^{\n]*\{",
    re.MULTILINE,
)
_JS_FIELD = re.compile(
    r"^\s*(?:(?:public|private|protected|readonly|static|declare)\s+)*#?([A-Za-z_$][\w$]*)"
    r"[?!]?\s*(?::|=(?!=|>)|;|,|$)"
)
_JS_THIS_FIELD = re.compile(r"\bthis\.([A-Za-z_$][\w$]*)\s*=(?!=)")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+(struct|interface)\s*\{", re.MULTILINE)
_GO_FIELD = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+[^\s/(]")
_GO_METHOD = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(")


def _js_types(text: str) -> Iterator[_TypeDef]:
    for m in _JS_TYPE.finditer(text):
        body = _brace_body(text, m.end() - 1)
        fields = {
            f.group(1)
            for line in _top_level_lines(body)
            if (f := _JS_FIELD.match(_strip_comment(line, "//"))) is not None
        }
        fields.update(_JS_THIS_FIELD.findall(body))
        fields.discard("constructor")
        if fields:
            yield m.group(1), _line_of(text, m.start(1)), frozenset(fields)


def _go_types(text: str) -> Iterator[_TypeDef]:
    for m in _GO_TYPE.finditer(text):
        body = _brace_body(text, m.end() - 1)
        fields: set[str] = set()
        for line in _top_level_lines(body):
            line = _strip_comment(line, "//")
            if m.group(2) == "interface":
                if (g := _GO_METHOD.match(line)) is not None:
                    fields.add(g.group(1))
            elif (g := _GO_FIELD.match(line)) is not None:
                fields.update(n.strip() for n in g.group(1).split(","))
        if fields:
            yield m.group(1), _line_of(text, m.start(1)), frozenset(fields)


_TYPE_SCANNERS: dict[str, Callable[[str], Iterator[_TypeDef]]] = {
    "python": _py_types,
    "javascript": _js_types,
    "go": _go_types,
}


def _duplicate_types(lang: str, sources: list[tuple[str, str]]) -> list[str]:
    """One finding per source file redefining a type (same name and field set) an
    earlier file already defines. A re-export (an import) defines nothing, and test
    code is never among ``sources``."""
    first: dict[tuple[str, frozenset[str]], str] = {}
    out: list[str] = []
    for rel, text in sources:
        for name, line, fields in _TYPE_SCANNERS[lang](text):
            other = first.setdefault((name, fields), rel)
            if other != rel:
                out.append(
                    f"app check {_DUPLICATE_TYPE.id}: {rel}:{line}: duplicate type {name} "
                    f"in {other} and {rel}: import one from the contracts file instead "
                    "of redefining it"
                )
    return out


# ---- the scan -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tree:
    web: bool
    flask: bool
    csrf: bool


_SCANNERS: dict[str, Callable[[str, str, _Tree], Iterator[tuple[int, _Rule]]]] = {
    "python": _python,
    "javascript": _javascript,
    "go": _go,
}


def _language(language: str) -> str:
    key = language.strip().lower()
    return _LANGUAGE_ALIASES.get(key, key)


def app_check_language(profile: VerificationProfile | None) -> str:
    """The language whose rules BUILD runs for ``profile``: its ``app_checks`` hook
    when set, else the language it builds ("" = no rules)."""
    if profile is None:
        return "python"
    if profile.app_checks is not None:
        return profile.app_checks
    from .profiles import profile_language

    lang = profile_language(profile)
    return _language(lang) if _language(lang) in _SCANNERS else ""


def _read(path: Path) -> str | None:
    """A regular file's text, bounded; never through a symlink or from a FIFO the
    tree planted (this scan runs host-side, outside the sandbox)."""
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            yield Path(dirpath) / name


def is_app_source(rel: str, suffixes: tuple[str, ...]) -> bool:
    """True iff the tree path ``rel`` is a non-test source file with one of
    ``suffixes``, outside the test and harness directories this scan skips."""
    parts = PurePosixPath(rel.removeprefix("./")).parts
    return (
        bool(parts)
        and parts[-1].endswith(suffixes)
        and not _TEST_FILE.match(parts[-1])
        and not any(d in _SKIP_DIRS or d.startswith(".") for d in parts[:-1])
    )


def has_app_source(root: Path, suffixes: tuple[str, ...]) -> bool:
    return any(is_app_source(p.relative_to(root).as_posix(), suffixes) for p in _walk(Path(root)))


def _sources(root: Path, suffixes: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    for path in _walk(root):
        if path.name.endswith(suffixes) and not _TEST_FILE.match(path.name):
            text = _read(path)
            if text is not None:
                yield path.relative_to(root).as_posix(), text


def _templates_mention_csrf(root: Path) -> bool:
    return any(
        path.name.endswith((".html", ".jinja", ".jinja2", ".j2", ".ejs", ".hbs", ".pug"))
        and _CSRF_WORD.search(_read(path) or "")
        for path in _walk(root)
    )


def app_check_findings(workdir: Path, language: str) -> tuple[str, ...]:
    """One ``app check <rule>: path:line: fix`` line per hit of ``language``'s rules
    in the tree's non-test sources (bounded), or ``()`` when nothing hits."""
    lang = _language(language)
    scanner = _SCANNERS.get(lang)
    if scanner is None:
        return ()
    root = Path(workdir)
    sources = list(_sources(root, _SUFFIXES[lang]))
    joined = "\n".join(text for _, text in sources)
    tree = _Tree(
        web=bool(_PY_WEB.search(joined)) if lang == "python" else True,
        flask=bool(_PY_FLASK.search(joined)),
        csrf=bool(_CSRF_WORD.search(joined)) or _templates_mention_csrf(root),
    )
    out: list[str] = []
    seen: set[tuple[str, int, str]] = set()
    for rel, text in sources:
        for line, rule in scanner(rel, text, tree):
            if (rel, line, rule.id) in seen:
                continue
            seen.add((rel, line, rule.id))
            out.append(f"app check {rule.id}: {rel}:{line}: {rule.fix}")
    out += _duplicate_types(lang, sources)
    if len(out) > _MAX_FINDINGS:
        out = [*out[:_MAX_FINDINGS], f"... {len(out) - _MAX_FINDINGS} more app-check hit(s)"]
    return tuple(out)


_CONSTRAINT_RULES: dict[str, tuple[_Rule, ...]] = {
    "python": (
        _SECRET,
        _SQLITE_THREADS,
        _MEMORY_DB,
        _WEAK_HASH,
        _SQL_FORMAT,
        _LIKE_ESCAPE,
        _CSRF,
        _DUPLICATE_TYPE,
    ),
    "javascript": (
        _SECRET,
        _MEMORY_DB,
        _WEAK_HASH,
        _SQL_FORMAT,
        _LIKE_ESCAPE,
        _CSRF,
        _DUPLICATE_TYPE,
    ),
    "go": (_SECRET, _MEMORY_DB, _WEAK_HASH, _SQL_FORMAT, _LIKE_ESCAPE, _DUPLICATE_TYPE),
}


def app_check_constraints(language: str, source: str = "planner") -> list[dict[str, Any]]:
    """The pinned-constraint record (the planner's PC-* shape) that tells tickets
    what BUILD's static app checks FAIL for ``language``; ``[]`` without rules."""
    lang = _language(language)
    rules = _CONSTRAINT_RULES.get(lang)
    if not rules:
        return []
    text = (
        "App safety (the BUILD gate FAILs on each, outside test code): "
        + "; ".join(f"[{r.id}] {r.fix}" for r in rules)
        + "."
    )
    return [{"id": f"PC-{lang}-app-safety", "text": text, "scope": "global", "source": source}]
