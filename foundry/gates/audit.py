"""Dependency-manifest recognition for the trusted-side materializer.

:func:`recognized_manifests` tells the manifest scaffold whether a tree already
carries a recognized lockfile / dependency manifest at its root.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["recognized_manifests"]

#: Lockfiles / dependency manifests, in canonical order. Stack-agnostic so a
#: Go/Rust/JVM/Ruby/PHP tree is not scaffolded a Python requirements.txt.
#: ``requirements*.txt`` is matched by glob (see ``_REQUIREMENTS_GLOB``).
_LOCKFILE_NAMES: tuple[str, ...] = (
    # Python
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    # Node
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    # Go (go.mod always present; go.sum only once there are deps)
    "go.mod",
    "go.sum",
    # Rust
    "Cargo.lock",
    # JVM
    "pom.xml",
    "gradle.lockfile",
    # Ruby / PHP
    "Gemfile.lock",
    "composer.lock",
)
_REQUIREMENTS_GLOB = "requirements*.txt"


def recognized_manifests(workdir: Path) -> list[str]:
    """Names of the recognized dependency manifests present at ``workdir`` root.

    A fixed lockfile name (:data:`_LOCKFILE_NAMES`) or a ``requirements*.txt``
    glob match, ROOT ONLY (a vendored lockfile deep in the tree is not the
    project's manifest). The materializer's manifest scaffold writes one only
    when this returns empty.
    """
    names = [n for n in _LOCKFILE_NAMES if (workdir / n).is_file()]
    names += [p.name for p in workdir.glob(_REQUIREMENTS_GLOB) if p.is_file()]
    return names
