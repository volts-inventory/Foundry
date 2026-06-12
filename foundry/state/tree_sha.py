"""`tree_sha` / `state_sha` — deterministic logical-state hashing (architecture.md §4).

The integrated-gate keying and invalidation correctness depend on `tree_sha`
being a deterministic hash of *logical state that affects behavior* and nothing
volatile (DS4):

    tree_sha = H( normalized_source_tree   # TRACKED SOURCE only, canonical
                                            #   bytewise path ordering, no
                                            #   timestamps/permissions
                ⧺ resolved_lockfile_hash    # exact dependency versions
                ⧺ toolchain_digest )        # pinned builder image + tools

Invariants (DS4):

- **Excludes** build outputs, caches, and anything time/host-volatile, so the
  same logical state hashes identically across clean checkouts on different
  machines. File mtimes and permission bits never enter the digest; content
  and relative POSIX path do.
- **Includes** the resolved lockfile hash and toolchain digest, so a
  dependency or toolchain change *does* change the hash — two different
  logical states cannot collide into a stale "already verified" PASS.
- `state_sha` generalizes for data/ML domains:
  `H(source ⧺ lockfile ⧺ toolchain ⧺ data_refs ⧺ feature_defs ⧺ weight_refs)`
  where data/weight refs are *content-identity* manifest hashes (sorted,
  deduplicated, canonical). `tree_sha` is the source-only special case
  (all ref sets empty).
- Execution-dependent verdicts (Test/E2E/SAST) depend on inputs *not* in the
  tree: `execution_reuse_key` binds the monotonic `mutant_rotation_id` and the
  `scanner_rule_version` into the reuse key, so a cached class-2 verdict is
  invalidated across a rotation / rule-version boundary.

Tracked-set discovery prefers `git ls-files -z` when the root is a git repo
and git is available; otherwise a pure-Python deterministic walk is used. In
both modes the explicit exclude predicate is applied uniformly.

Blob-hash cache (pure optimization)
-----------------------------------
`normalized_source_tree`/`tree_sha` accept an optional mutable mapping keyed
on `(absolute_path, size_bytes, mtime_ns)` whose values are content sha256
hex digests. Repeated computations over a large, mostly-unchanged tree then
avoid re-reading every byte. The cache is a *pure* optimization: a cold cache
and a warm cache MUST (and do) produce identical hashes, because only the
cached *content digest* — never the cache key (and thus never size/mtime) —
flows into the tree digest. A stale entry is impossible to consult without a
matching (size, mtime_ns), and even a spurious hit only ever substitutes the
same content hash that a re-read would produce for unchanged bytes.

All digests are sha256 hex over canonical bytes (foundry.canonical).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from pathlib import Path

from foundry.canonical import content_digest

__all__ = [
    "DEFAULT_EXCLUDED_DIRS",
    "DEFAULT_EXCLUDED_GLOBS",
    "BlobCache",
    "ExcludePredicate",
    "default_exclude",
    "execution_reuse_key",
    "normalized_source_tree",
    "resolved_lockfile_hash",
    "state_sha",
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

# (absolute_path, size_bytes, mtime_ns) -> sha256 hex of file content.
BlobCache = MutableMapping[tuple[str, int, int], str]


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


def _blob_sha256(path: Path, cache: BlobCache | None) -> str:
    """sha256 hex of file content, via the (path, size, mtime_ns) cache.

    The cache key never enters any digest; only the content hash does, so a
    cold and a warm cache yield identical results (pure optimization).
    """
    if cache is None:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = cache.get(key)
    if hit is not None:
        return hit
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cache[key] = digest
    return digest


def _git_tracked_files(root: Path) -> list[str] | None:
    """Relative POSIX paths of git-tracked files, or None if unavailable.

    Returns None when the root is not a git work tree or git itself is
    missing/failing, signaling the caller to use the pure-Python walk.
    """
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--full-name"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    out = proc.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in out.split("\0") if p]


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
    prefer_git: bool = True,
    cache: BlobCache | None = None,
) -> str:
    """Hash of the tracked source tree: canonical, content-only, timestamp-free.

    The digest covers the bytewise-sorted sequence of
    ``(relative_posix_path, sha256(content))`` pairs. mtimes and permission
    bits never enter the digest. ``exclude`` (default: `default_exclude`)
    is applied uniformly in both git and pure-walk modes.

    When ``prefer_git`` and the root is a git repo with git available, the
    tracked set comes from ``git ls-files -z`` (files deleted from the
    worktree are skipped); otherwise a deterministic pure-Python walk is used.
    """
    root_path = Path(root).resolve()
    predicate = default_exclude if exclude is None else exclude

    rel_paths: list[str] | None = None
    if prefer_git:
        tracked = _git_tracked_files(root_path)
        if tracked is not None:
            rel_paths = [
                p for p in tracked if not predicate(p) and (root_path / p).is_file()
            ]
    if rel_paths is None:
        rel_paths = _walk_files(root_path, predicate)

    # Canonical bytewise path ordering (matches git tree ordering semantics).
    rel_paths.sort(key=lambda p: p.encode("utf-8"))
    files: list[list[str]] = [
        [rel, _blob_sha256(root_path / rel, cache)] for rel in rel_paths
    ]
    return content_digest({"kind": "normalized_source_tree", "files": files})


def resolved_lockfile_hash(
    lockfile_hashes: Mapping[str, str] | None = None,
    *,
    lockfile_paths: Iterable[Path | str] | None = None,
) -> str:
    """Hash of the exact resolved dependency set (all ecosystems).

    Accepts a precomputed ``{lockfile_name: sha256_hex}`` mapping and/or
    lockfile paths to hash here (keyed by file name). The combined mapping is
    canonicalized (sorted keys) before digesting, so entry order is irrelevant.
    """
    combined: dict[str, str] = dict(lockfile_hashes or {})
    for p in lockfile_paths or ():
        path = Path(p)
        combined[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return content_digest({"kind": "resolved_lockfile_hash", "lockfiles": combined})


def state_sha(
    *,
    source_tree: str,
    lockfile_hash: str,
    toolchain_digest: str,
    data_refs: Iterable[str] = (),
    feature_defs: Iterable[str] = (),
    weight_refs: Iterable[str] = (),
) -> str:
    """`H(source ⧺ lockfile ⧺ toolchain ⧺ data_refs ⧺ feature_defs ⧺ weight_refs)`.

    Data/weight/feature refs are content-identity manifest hashes; each set is
    sorted and deduplicated before digesting, so caller ordering (and
    duplication) cannot perturb the key. `tree_sha` is the source-only
    special case (all three ref sets empty). (architecture.md §4, DS4)
    """
    return content_digest(
        {
            "kind": "state_sha",
            "source": source_tree,
            "lockfile": lockfile_hash,
            "toolchain": toolchain_digest,
            "data_refs": sorted(set(data_refs)),
            "feature_defs": sorted(set(feature_defs)),
            "weight_refs": sorted(set(weight_refs)),
        }
    )


def tree_sha(
    root: Path | str,
    *,
    toolchain_digest: str,
    lockfile_hashes: Mapping[str, str] | None = None,
    lockfile_paths: Iterable[Path | str] | None = None,
    exclude: ExcludePredicate | None = None,
    prefer_git: bool = True,
    cache: BlobCache | None = None,
) -> str:
    """`H(normalized_source_tree ⧺ resolved_lockfile_hash ⧺ toolchain_digest)`.

    The source-only special case of `state_sha` (architecture.md §4). See the
    module docstring for the DS4 invariants and the blob-cache contract.
    """
    return state_sha(
        source_tree=normalized_source_tree(
            root, exclude=exclude, prefer_git=prefer_git, cache=cache
        ),
        lockfile_hash=resolved_lockfile_hash(lockfile_hashes, lockfile_paths=lockfile_paths),
        toolchain_digest=toolchain_digest,
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
