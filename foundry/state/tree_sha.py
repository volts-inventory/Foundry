"""`tree_sha` — deterministic logical-state hashing (architecture.md §4).

The integrated-gate keying and invalidation correctness depend on `tree_sha`
being a deterministic hash of *logical state that affects behavior* and nothing
volatile (DS4):

    tree_sha = H( normalized_source_tree   # source only, canonical bytewise
                                            #   path ordering, no
                                            #   timestamps/permissions
                ⧺ toolchain_digest )        # pinned builder image + tools

Invariants (DS4):

- **Excludes** build outputs, caches, and anything time/host-volatile, so the
  same logical state hashes identically across clean checkouts on different
  machines. File mtimes and permission bits never enter the digest; content
  and relative POSIX path do.
- **Includes** the toolchain digest, so a toolchain change *does* change the
  hash — two different logical states cannot collide into a stale "already
  verified" PASS.
- Execution-dependent verdicts (Test/E2E/SAST) depend on inputs *not* in the
  tree: `execution_reuse_key` binds the monotonic `mutant_rotation_id` and the
  `scanner_rule_version` into the reuse key, so a cached class-2 verdict is
  invalidated across a rotation / rule-version boundary.

The file set comes from a deterministic pure-Python walk filtered by the
exclude predicate.

All digests are sha256 hex over canonical bytes (foundry.canonical).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from foundry.canonical import content_digest

__all__ = [
    "DEFAULT_EXCLUDED_DIRS",
    "DEFAULT_EXCLUDED_GLOBS",
    "ExcludePredicate",
    "default_exclude",
    "execution_reuse_key",
    "normalized_source_tree",
    "tree_sha",
]

# Path segments excluded by default (build outputs, caches, vendored envs).
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".pytest_cache",
        ".hypothesis",
    }
)

# Basename glob patterns excluded by default.
DEFAULT_EXCLUDED_GLOBS: tuple[str, ...] = ("*.pyc",)

# exclude predicate: relative POSIX path -> True if the path must be skipped.
ExcludePredicate = Callable[[str], bool]

# The digest payload keeps the empty lockfile / ref slots it had when tree_sha
# also took them, so tree_sha values (persisted stable-FAIL latches, checkpoints)
# stay byte-identical across the simplification.
_EMPTY_LOCKFILE_HASH = content_digest({"kind": "resolved_lockfile_hash", "lockfiles": {}})


def default_exclude(rel_posix: str) -> bool:
    """Default exclude predicate over a relative POSIX path.

    Excludes any path containing a default-excluded segment (``.git``,
    ``.venv``, ``__pycache__``, ``node_modules``, ``dist``, ``build``,
    ``.pytest_cache``, ``.hypothesis``) and any basename matching a
    default-excluded glob (``*.pyc``).
    """
    parts = rel_posix.split("/")
    if any(part in DEFAULT_EXCLUDED_DIRS for part in parts):
        return True
    basename = parts[-1]
    return any(fnmatch.fnmatch(basename, pat) for pat in DEFAULT_EXCLUDED_GLOBS)


def _walk_files(root: Path, exclude: ExcludePredicate) -> list[str]:
    """Deterministic pure-Python walk: relative POSIX paths of regular files."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        # Prune excluded directories so we never descend into them.
        dirnames[:] = [d for d in dirnames if not exclude(prefix + d)]
        dirnames.sort()  # determinism of traversal (output is re-sorted anyway)
        for name in filenames:
            rel = prefix + name
            if not exclude(rel):
                found.append(rel)
    return found


def normalized_source_tree(
    root: Path | str,
    *,
    exclude: ExcludePredicate | None = None,
) -> str:
    """Hash of the source tree: canonical, content-only, timestamp-free.

    The digest covers the bytewise-sorted sequence of
    ``(relative_posix_path, sha256(content))`` pairs. mtimes and permission
    bits never enter the digest. ``exclude`` defaults to `default_exclude`.
    """
    root_path = Path(root).resolve()
    predicate = default_exclude if exclude is None else exclude
    rel_paths = _walk_files(root_path, predicate)

    # Canonical bytewise path ordering (matches git tree ordering semantics).
    rel_paths.sort(key=lambda p: p.encode("utf-8"))
    files: list[list[str]] = [
        [rel, hashlib.sha256((root_path / rel).read_bytes()).hexdigest()] for rel in rel_paths
    ]
    return content_digest({"kind": "normalized_source_tree", "files": files})


def tree_sha(
    root: Path | str,
    *,
    toolchain_digest: str,
    exclude: ExcludePredicate | None = None,
) -> str:
    """`H(normalized_source_tree ⧺ toolchain_digest)` (architecture.md §4)."""
    return content_digest(
        {
            "kind": "state_sha",
            "source": normalized_source_tree(root, exclude=exclude),
            "lockfile": _EMPTY_LOCKFILE_HASH,
            "toolchain": toolchain_digest,
            "data_refs": [],
            "feature_defs": [],
            "weight_refs": [],
        }
    )


def execution_reuse_key(
    state_sha: str,
    mutant_rotation_id: int,
    scanner_rule_version: str,
) -> str:
    """Reuse key for execution-dependent (class-2) verdicts: Test/E2E/SAST.

    Cached runner-attested verdicts also depend on the rotating held-out
    mutant family and the scanner rule-set version — neither lives in the
    source tree — so both are bound into the reuse key. A rotation or
    rule-version boundary therefore invalidates the cached verdict instead of
    laundering it forward. (architecture.md §4, ties to §SEC1)
    """
    return content_digest(
        {
            "kind": "execution_reuse_key",
            "state_sha": state_sha,
            "mutant_rotation_id": mutant_rotation_id,
            "scanner_rule_version": scanner_rule_version,
        }
    )
