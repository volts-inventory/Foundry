"""File ownership: every source or test module in the tree belongs to a ticket.

A fixer session once created parser.py, roller.py, cli.py and top-level tests that
no ticket owned; the planned tickets then wrote dice_parser.py, dice_roller.py and
main.py, and the strays shipped as dead code with every gate green. BUILD runs
:func:`unowned_findings` over the tree (``BuildGate(owned=...)``): a code file no
ticket's ``files`` cover FAILs the build, named, so the fix loop deletes it or
moves its code where it belongs.

Language-agnostic: a file counts as code by its extension; markup, data, config
and docs are never judged. Owned without being listed: anything under an owned
directory entry (``app/``), the generated BDD material (``features/``), package
markers and pytest's ``conftest.py`` inside a directory the plan writes into, the
smoke script and build-tool config modules (``setup.py``, ``vite.config.ts``).
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path

from foundry.schemas.implementation import FEATURE_DIR

__all__ = [
    "CODE_SUFFIXES",
    "UNOWNED_FAILED",
    "is_code",
    "owned_dirs",
    "owns",
    "unowned_files",
    "unowned_findings",
]

UNOWNED_FAILED = (
    "the tree holds source or test files that no ticket owns (dead code the plan "
    "never asked for) — FAIL: delete each file below, or move its code into the "
    "owning ticket's file"
)

CODE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".cs", ".fs", ".swift", ".rb",
        ".php", ".ex", ".exs", ".c", ".cc", ".cpp", ".h", ".hpp", ".lua", ".dart",
    }
)  # fmt: skip

_MAX_FINDINGS = 25
_SKIP_DIRS = frozenset(
    {
        "node_modules", "venv", "env", "__pycache__", "dist", "build", "target",
        "vendor", "site-packages", "coverage", "htmlcov",
    }
)  # fmt: skip
# Package markers and pytest's fixture module: owned where the plan writes.
_MARKERS = frozenset({"__init__.py", "__main__.py", "conftest.py"})
_ROOT_SCRIPTS = re.compile(r"^(setup\.py|smoke\.\w+)$")
_TOOL_CONFIG = re.compile(r"^[\w.-]+\.config\.[cm]?[jt]s$")


def is_code(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in CODE_SUFFIXES


def _norm(path: str) -> str:
    return path.removeprefix("./")


def owned_dirs(owned: Collection[str]) -> frozenset[str]:
    """Every directory the owned paths write into ("" = the root)."""
    out: set[str] = {""}
    for f in owned:
        p = _norm(f).rstrip("/")
        parts = p.split("/")
        stop = len(parts) if _norm(f).endswith("/") else len(parts) - 1
        out.update("/".join(parts[:i]) for i in range(1, stop + 1))
    return frozenset(out)


def owns(path: str, owned: Collection[str], dirs: Collection[str] | None = None) -> bool:
    """Does the plan own ``path``? ``owned``: the ticket files (an entry ending in
    ``/`` owns its subtree; no ``./`` prefix) and any other file the run treats as
    given (a change build's base tree). ``dirs``: :func:`owned_dirs` of ``owned``."""
    path = _norm(path)
    if not is_code(path):
        return True
    if path in owned or any(f.endswith("/") and path.startswith(_norm(f)) for f in owned):
        return True
    parent, _, name = path.rpartition("/")
    if (
        path.split("/", 1)[0] == FEATURE_DIR
        or _TOOL_CONFIG.match(name)
        or (not parent and _ROOT_SCRIPTS.match(name))
    ):
        return True
    return name in _MARKERS and parent in (dirs if dirs is not None else owned_dirs(owned))


def _walk(root: Path) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        rel = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel == "." else rel + "/"
        for name in sorted(filenames):
            if not name.startswith("."):
                yield prefix + name


def unowned_files(workdir: Path, owned: Collection[str]) -> list[str]:
    """The code files in ``workdir`` the plan does not own (:func:`owns`)."""
    dirs = owned_dirs(owned)
    return [rel for rel in _walk(workdir) if not owns(rel, owned, dirs)]


def unowned_findings(stray: Sequence[str]) -> tuple[str, ...]:
    """The Build gate's findings for the unowned code files ``stray`` (none: empty).
    Each line reads ``path:1: ...`` so the fix loop hands the fixer that file."""
    if not stray:
        return ()
    lines = [
        f"{rel}:1: not owned by any ticket: delete it or move its code into the "
        "owning ticket's file"
        for rel in stray[:_MAX_FINDINGS]
    ]
    if len(stray) > _MAX_FINDINGS:
        lines.append(f"... and {len(stray) - _MAX_FINDINGS} more unowned file(s)")
    return (UNOWNED_FAILED, *lines)
