"""The Workshop reads a finished build's repository straight from the durable store.

``webapp.repo.read_repo`` reconstructs the integrated tree from the durable CAS +
checkpoint — NO output directory. This is the source of truth behind the
self-hosted "GitHub" tab: the code, the gate verdicts (as checks), and each
FeatureDiff (as a 'pull request') all come back out of the database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.cli import run
from webapp.repo import RepoUnavailable, read_repo


def test_read_repo_materializes_tree_from_the_store(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    # A real (mock) build, durably checkpointed — but written to NO output dir.
    result = run(
        "build a tiny URL-shortener service",
        thread_id="repo-1",
        state_dir=state_dir,
    )
    assert result.passed is True
    assert result.output_root is None  # nothing on disk — it's all in the store

    repo = read_repo("repo-1", state_dir)
    # The integrated tree comes back from the CAS (the stub build's one file),
    # and the Spec gate's criteria.json INPUT is excluded (it's not a source file).
    paths = {f.path for f in repo.files}
    assert paths and "criteria.json" not in paths
    # Every gate verdict is surfaced as a status check, all green for a passing build.
    checks = {c.name: c for c in repo.checks}
    assert checks and all(c.ok for c in checks.values())
    assert "build" in checks and "test" in checks
    # The FeatureDiff is surfaced as a 'pull request' with its unified-diff body.
    assert repo.diffs and all(d.text for d in repo.diffs)
    # Sign-offs are surfaced too — a mock assistant build records none, so the
    # channel is present but empty (gated/autonomous builds populate it).
    assert repo.sign_offs == ()
    # file_bytes returns the exact materialized content.
    first = next(iter(paths))
    assert repo.file_bytes(first) is not None
    assert repo.tree_sha  # the integrated tree_sha the gates ran against


def test_read_repo_unavailable_without_a_checkpoint(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    run("build a tiny URL-shortener service", thread_id="repo-2", state_dir=state_dir)
    with pytest.raises(RepoUnavailable):
        read_repo("no-such-build", state_dir)
