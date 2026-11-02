"""The Workshop reads a finished build's repository straight from the durable store.

``webapp.repo.read_repo`` reconstructs the integrated tree from the durable CAS +
checkpoint — NO output directory. This is the source of truth behind the
file viewer and zip export: the code and the gate verdicts (as checks) come
back out of the database.
"""

from __future__ import annotations

import json
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
    # file_bytes returns the exact materialized content.
    first = next(iter(paths))
    assert repo.file_bytes(first) is not None
    assert repo.tree_sha  # the integrated tree_sha the gates ran against


def test_a_mock_builds_repo_is_the_tree_its_gates_judged(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    out = tmp_path / "out"
    result = run("build a todo app", thread_id="repo-3", state_dir=state_dir,
                 output_dir=str(out))
    assert result.passed is True

    repo = read_repo("repo-3", state_dir, backend="mock")
    exported = {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*")
                if p.is_file() and not p.name.startswith("BUILD-MANIFEST")}
    assert {f.path: repo.file_bytes(f.path) for f in repo.files} == exported
    manifest = json.loads((out / "BUILD-MANIFEST.json").read_text())
    for key in ("request", "thread_id", "stack", "contracts", "contracts_files"):
        assert repo.manifest[key] == manifest[key], key


def test_read_repo_unavailable_without_a_checkpoint(tmp_path: Path) -> None:
    state_dir = str(tmp_path / "state")
    run("build a tiny URL-shortener service", thread_id="repo-2", state_dir=state_dir)
    with pytest.raises(RepoUnavailable):
        read_repo("no-such-build", state_dir)
