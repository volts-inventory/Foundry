"""Git export — deterministic projection of a release lineage into one repo.

The CAS stays the source of truth; the export is a VIEW: one tagged commit per
released increment, byte-identical SHAs on re-export (pinned identity +
sequence-derived timestamps), deletions honored, fail-closed on a dirty dest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foundry.lifecycle.git_export import GitExportError, ReleaseExport, export_lineage

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def make_lineage(tmp_path: Path) -> list[ReleaseExport]:
    m1 = tmp_path / "m01"
    (m1 / "src").mkdir(parents=True)
    (m1 / "src" / "engine.py").write_text("TICK = 1\n")
    (m1 / "scratch.py").write_text("TEMP = 1\n")
    m2 = tmp_path / "m02"
    (m2 / "src").mkdir(parents=True)
    (m2 / "src" / "engine.py").write_text("TICK = 2\n")  # modified
    (m2 / "src" / "farm.py").write_text("CROPS = 6\n")  # added
    # scratch.py deleted in m02
    return [
        ReleaseExport(tag="m01", tree_dir=m1, message="engine loop"),
        ReleaseExport(tag="m02", tree_dir=m2, message="farming"),
    ]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_one_repo_tagged_commits_deletions_honored(tmp_path: Path) -> None:
    head = export_lineage(tmp_path / "repo", make_lineage(tmp_path))
    repo = tmp_path / "repo"
    assert git(repo, "tag") == "m01\nm02"
    assert git(repo, "rev-parse", "HEAD") == head
    # HEAD tree = exactly m02: modified engine, added farm, scratch GONE.
    assert (repo / "src" / "farm.py").read_text() == "CROPS = 6\n"
    assert (repo / "src" / "engine.py").read_text() == "TICK = 2\n"
    assert not (repo / "scratch.py").exists()
    # History answers "what changed in the farming milestone".
    diff = git(repo, "diff", "m01..m02", "--name-status")
    assert "M\tsrc/engine.py" in diff and "A\tsrc/farm.py" in diff
    assert "D\tscratch.py" in diff


def test_reexport_is_deterministic(tmp_path: Path) -> None:
    lineage = make_lineage(tmp_path)
    sha_a = export_lineage(tmp_path / "a", lineage)
    sha_b = export_lineage(tmp_path / "b", lineage)
    assert sha_a == sha_b  # same lineage => identical SHAs (idempotent projection)


def test_refuses_dirty_dest_and_empty_lineage(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("x")
    with pytest.raises(GitExportError, match="non-empty"):
        export_lineage(dirty, make_lineage(tmp_path))
    with pytest.raises(GitExportError, match="nothing to export"):
        export_lineage(tmp_path / "empty-out", [])

