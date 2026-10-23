"""Language toolchains available on this machine, offered to the planner.

Each language needs a set of binaries on ``PATH``; java takes either build tool.
``--version`` is probed with a short timeout and is fail-soft: a binary that is
present but whose version check hangs or errors still counts, its version left
blank. Python is always available (Foundry runs on it), so ``sys.executable``
stands in when no ``python3`` is on ``PATH``.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from collections.abc import Callable

from foundry.schemas.implementation import Toolchain

__all__ = ["available_languages", "available_toolchains", "detect_toolchains"]

_VERSION_TIMEOUT_S = 5.0

# language -> groups of binaries; every group needs one present binary (first wins).
_LANGUAGES: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("python", (("python3", "python"),)),
    ("typescript", (("node",), ("npm",))),
    ("javascript", (("node",), ("npm",))),
    ("go", (("go",),)),
    ("rust", (("cargo",),)),
    ("java", (("java",), ("mvn", "gradle"))),
)

# Binaries whose version flag is not ``--version``.
_VERSION_ARGS: dict[str, tuple[str, ...]] = {"go": ("version",), "java": ("-version",)}


def _version(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    try:
        proc = subprocess.run(
            (path, *_VERSION_ARGS.get(name, ("--version",))),
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [
        ln.strip() for ln in (proc.stdout.strip() or proc.stderr.strip()).splitlines()
        # The JVM prints JAVA_TOOL_OPTIONS ahead of its version.
        if ln.strip() and not ln.startswith("Picked up ")
    ]
    return lines[0][:120] if lines else ""


def detect_toolchains(
    which: Callable[[str], str | None] | None = None,
    version: Callable[[str], str] = _version,
) -> tuple[Toolchain, ...]:
    """Every language whose binaries are found, in a fixed order."""
    import shutil

    find = which or shutil.which
    out: list[Toolchain] = []
    for language, groups in _LANGUAGES:
        found = [next(((b, p) for b in group if (p := find(b))), None) for group in groups]
        hits = [h for h in found if h is not None]
        if len(hits) == len(groups):
            out.append(Toolchain(
                language=language,
                binaries=tuple(b for b, _ in hits),
                version=version(hits[0][1]),
            ))
        elif language == "python" and which is None:
            out.append(Toolchain(
                language="python", binaries=("python3",), version=sys.version.split()[0],
            ))
    return tuple(out)


@functools.cache
def available_toolchains() -> tuple[Toolchain, ...]:
    """This host's toolchains, probed once per process."""
    return detect_toolchains()


def available_languages() -> frozenset[str]:
    return frozenset(t.language for t in available_toolchains())
