"""Static app checks: per-language regex rules for app-safety footguns the tests
rarely catch (a hardcoded secret, a SQLite connection shared across server threads,
an in-memory database for data that must persist, a fast digest on passwords, SQL
built by string formatting, a form POST without CSRF).

BUILD runs them over the tree after its commands pass (``BuildGate``): a hit FAILs
the build with one ``path:line`` finding per hit, each naming its fix, so the fix
loop can act on it. The same rules, as prose, are the stack constraint
:func:`app_check_constraints` gives the planner, so tickets avoid them up front.

Test code is never scanned: an in-memory database or a literal key is right there.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profiles import VerificationProfile

__all__ = [
    "APP_CHECKS_FAILED",
    "app_check_constraints",
    "app_check_findings",
    "app_check_language",
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
    "python": (".py",),
    "javascript": (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"),
    "go": (".go",),
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
    "sqlite3.connect without check_same_thread in a web app: a threaded server "
    "uses the connection from other threads and every request after the first "
    "500s; open one connection per request, or pass check_same_thread=False and "
    "guard the shared connection with a lock",
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
_PY_SQLITE_CONNECT = re.compile(r"\bsqlite3\.connect\s*\(")
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


def _call_args(text: str, open_paren: int) -> str:
    depth = 0
    for i in range(open_paren, min(len(text), open_paren + 2000)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren : i + 1]
    return text[open_paren : open_paren + 2000]


def _like_hits(text: str, wildcard: re.Pattern[str]) -> Iterator[tuple[int, _Rule]]:
    """Each place a file with a parametrized LIKE and no ESCAPE clause wraps a value
    in % (or concatenates it in SQL)."""
    if not _SQL_LIKE.search(text) or _ESCAPE.search(text):
        return
    for pattern in (wildcard, _SQL_LIKE_CONCAT):
        for m in pattern.finditer(text):
            yield _line_of(text, m.start()), _LIKE_ESCAPE


def _test_line(line: str) -> bool:
    return bool(re.search(r"(?i)test", line))


def _code_lines(text: str, comment: str) -> Iterator[tuple[int, str]]:
    for n, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith(comment):
            yield n, line


def _python(rel: str, text: str, tree: _Tree) -> Iterator[tuple[int, _Rule]]:
    for n, line in _code_lines(text, "#"):
        if _PY_SECRET_LITERAL.search(line) or _PY_SECRET_FALLBACK.search(line):
            yield n, _SECRET
        if _MEMORY.search(line) and not _test_line(line):
            yield n, _MEMORY_DB
        if _PY_FAST_DIGEST.search(line) and _PY_PASSWORD_WORD.search(line):
            yield n, _WEAK_HASH
    if tree.web:
        for m in _PY_SQLITE_CONNECT.finditer(text):
            if "check_same_thread" not in _call_args(text, m.end() - 1):
                yield _line_of(text, m.start()), _SQLITE_THREADS
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
    for n, line in _code_lines(text, "//"):
        if _JS_SECRET_FALLBACK.search(line) or _JS_SECRET_LITERAL.search(line):
            yield n, _SECRET
        if _MEMORY.search(line) and not _test_line(line):
            yield n, _MEMORY_DB
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
    for n, line in _code_lines(text, "//"):
        if _GO_SECRET_LITERAL.search(line):
            yield n, _SECRET
        if _MEMORY.search(line) and not _test_line(line):
            yield n, _MEMORY_DB
        if _GO_FAST_DIGEST.search(line) and _PY_PASSWORD_WORD.search(line):
            yield n, _WEAK_HASH
    for m in _GO_SPRINTF_SQL.finditer(text):
        body = m.group("body")
        if _SQL_SHAPE.search(body) and re.search(r"%[sdvq]", body):
            yield _line_of(text, m.start()), _SQL_FORMAT
    yield from _like_hits(text, _GO_WILDCARD)


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
    from .profiles import _PROFILE_LANGUAGE

    name = profile.name
    lang = name.split(":", 1)[1] if name.startswith("stack:") else _PROFILE_LANGUAGE.get(name, "")
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
    if len(out) > _MAX_FINDINGS:
        out = [*out[:_MAX_FINDINGS], f"... {len(out) - _MAX_FINDINGS} more app-check hit(s)"]
    return tuple(out)


_CONSTRAINT_RULES: dict[str, tuple[_Rule, ...]] = {
    "python": (_SECRET, _SQLITE_THREADS, _MEMORY_DB, _WEAK_HASH, _SQL_FORMAT, _LIKE_ESCAPE, _CSRF),
    "javascript": (_SECRET, _MEMORY_DB, _WEAK_HASH, _SQL_FORMAT, _LIKE_ESCAPE, _CSRF),
    "go": (_SECRET, _MEMORY_DB, _WEAK_HASH, _SQL_FORMAT, _LIKE_ESCAPE),
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
