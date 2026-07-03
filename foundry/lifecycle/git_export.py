"""Git export — a DETERMINISTIC projection of a release lineage into one repo.

The CAS stays the source of truth (verdicts bind to normalized tree digests;
records are append-only); git is the HUMAN interface: one repository where
each released increment is a tagged commit — `git diff m01..m02`, checkout any
increment, push to a remote. This module is a pure projection: it never feeds
back into trust, and re-exporting the same lineage reproduces IDENTICAL commit
SHAs (pinned author/committer identity + deterministic timestamps derived from
the sequence, never the wall clock) — the same L7 discipline as the rest of
the factory, so a resumed/re-run export is idempotent.

Fail-closed: a missing `git` binary or a failing git command raises
:class:`GitExportError` — the caller reports it; the released trees stand.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["GitExportError", "ReleaseExport", "export_lineage"]

_IDENTITY = "Foundry <foundry@localhost>"
# Deterministic timebase: commit i gets EPOCH + i seconds. An arbitrary fixed
# constant (not the wall clock) so the same lineage always yields the same SHAs.
_EPOCH = 1_700_000_000


class GitExportError(RuntimeError):
    """git is missing or a git command failed — the export (a VIEW) aborts;
    the released trees / CAS records are untouched."""


@dataclass(frozen=True, slots=True)
class ReleaseExport:
    tag: str  # e.g. "m01" / "r03" — becomes the commit's tag
    tree_dir: Path  # the FULL released tree for this increment
    message: str  # commit subject/body (request + gate summary + ballot)


def _git(repo: Path, *args: str, index: int = 0) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Foundry", "GIT_AUTHOR_EMAIL": "foundry@localhost",
        "GIT_COMMITTER_NAME": "Foundry", "GIT_COMMITTER_EMAIL": "foundry@localhost",
        "GIT_AUTHOR_DATE": f"{_EPOCH + index} +0000",
        "GIT_COMMITTER_DATE": f"{_EPOCH + index} +0000",
        "HOME": str(repo),  # no user gitconfig bleed: identical SHAs on any machine
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GitExportError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def export_lineage(dest: Path | str, releases: Sequence[ReleaseExport]) -> str:
    """Project ``releases`` (oldest→newest) into ONE git repo at ``dest``.

    Each release becomes a commit whose tree is exactly that increment's
    released tree (the worktree is synced to it — files a later release
    deleted disappear from history going forward, exactly like the overlay),
    tagged with its ``tag``. Returns the HEAD commit sha. Deterministic:
    re-export of the same lineage into a fresh dest yields byte-identical SHAs.
    """
    if shutil.which("git") is None:
        raise GitExportError("the `git` binary is not on PATH — install git or skip --git")
    if not releases:
        raise GitExportError("nothing to export: no released increments")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(p.name != ".git" for p in dest.iterdir()):
        raise GitExportError(f"refusing to export into non-empty directory {dest}")
    _git(dest, "init", "-q", "-b", "main")
    head = ""
    for i, rel in enumerate(releases):
        if not rel.tree_dir.is_dir():
            raise GitExportError(f"release {rel.tag!r}: tree dir {rel.tree_dir} missing")
        # Sync the worktree to EXACTLY this release's tree (deletions included).
        for existing in dest.iterdir():
            if existing.name == ".git":
                continue
            shutil.rmtree(existing) if existing.is_dir() else existing.unlink()
        for src in sorted(rel.tree_dir.rglob("*")):
            if src.is_file():
                target = dest / src.relative_to(rel.tree_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
        _git(dest, "add", "-A", index=i)
        _git(dest, "commit", "-q", "--allow-empty", "-m", rel.message, index=i)
        _git(dest, "tag", rel.tag, index=i)
        head = _git(dest, "rev-parse", "HEAD", index=i)
    return head
