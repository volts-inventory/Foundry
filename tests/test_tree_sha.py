"""Tests for foundry.state.tree_sha (architecture.md §4, DS4 invariants).

All tree tests use the pure-Python walk path (tmp_path is not a git repo);
the git-mode test skips gracefully when git is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from foundry.state.tree_sha import (
    execution_reuse_key,
    normalized_source_tree,
    resolved_lockfile_hash,
    state_sha,
    tree_sha,
)

TOOLCHAIN = "builder-image@sha256:abc123/python3.12.6"
LOCKS = {"uv.lock": "a" * 64}


def write_tree(root: Path, files: Mapping[str, bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


SOURCE = {
    "src/pkg/__init__.py": b"",
    "src/pkg/main.py": b"def main() -> int:\n    return 0\n",
    "README.md": b"# demo\n",
}


def make_tree_sha(root: Path, **kw: object) -> str:
    return tree_sha(root, toolchain_digest=TOOLCHAIN, lockfile_hashes=LOCKS, **kw)  # type: ignore[arg-type]


# --- stability ---------------------------------------------------------------


def test_identical_content_in_different_dirs_hashes_identically(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_tree(a, SOURCE)
    write_tree(b, SOURCE)
    assert make_tree_sha(a) == make_tree_sha(b)


def test_mtime_and_permission_changes_do_not_affect_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    before = make_tree_sha(tmp_path)
    target = tmp_path / "src/pkg/main.py"
    os.utime(target, ns=(1, 1))  # ancient mtime
    target.chmod(0o755)
    assert make_tree_sha(tmp_path) == before


def test_excluded_build_artifacts_do_not_affect_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    before = make_tree_sha(tmp_path)
    write_tree(
        tmp_path,
        {
            "build/out.bin": b"\x00\x01binary",
            "x.pyc": b"compiled",
            "src/pkg/__pycache__/main.cpython-312.pyc": b"cachedump",
            "dist/pkg-0.1.0.whl": b"wheel",
            ".pytest_cache/v/cache/lastfailed": b"{}",
        },
    )
    assert make_tree_sha(tmp_path) == before


# --- sensitivity --------------------------------------------------------------


def test_single_source_byte_edit_changes_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    before = make_tree_sha(tmp_path)
    (tmp_path / "src/pkg/main.py").write_bytes(b"def main() -> int:\n    return 1\n")
    assert make_tree_sha(tmp_path) != before


def test_lockfile_hash_change_changes_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    a = tree_sha(tmp_path, toolchain_digest=TOOLCHAIN, lockfile_hashes={"uv.lock": "a" * 64})
    b = tree_sha(tmp_path, toolchain_digest=TOOLCHAIN, lockfile_hashes={"uv.lock": "b" * 64})
    assert a != b


def test_toolchain_digest_change_changes_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    a = tree_sha(tmp_path, toolchain_digest="toolchain-v1", lockfile_hashes=LOCKS)
    b = tree_sha(tmp_path, toolchain_digest="toolchain-v2", lockfile_hashes=LOCKS)
    assert a != b


def test_lockfile_paths_are_hashed_by_content(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"resolved==1.0\n")
    a = resolved_lockfile_hash(lockfile_paths=[lock])
    lock.write_bytes(b"resolved==2.0\n")
    assert resolved_lockfile_hash(lockfile_paths=[lock]) != a


# --- state_sha generalization --------------------------------------------------


def test_data_ref_changes_state_sha_but_not_tree_sha(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    src = normalized_source_tree(tmp_path)
    lock = resolved_lockfile_hash(LOCKS)
    base = dict(source_tree=src, lockfile_hash=lock, toolchain_digest=TOOLCHAIN)
    s1 = state_sha(**base, data_refs=["d1" * 32])
    s2 = state_sha(**base, data_refs=["d2" * 32])
    assert s1 != s2
    assert make_tree_sha(tmp_path) == make_tree_sha(tmp_path)  # tree_sha unaffected


def test_tree_sha_is_state_sha_with_empty_refs(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    expected = state_sha(
        source_tree=normalized_source_tree(tmp_path),
        lockfile_hash=resolved_lockfile_hash(LOCKS),
        toolchain_digest=TOOLCHAIN,
    )
    assert make_tree_sha(tmp_path) == expected


def test_ref_ordering_does_not_matter() -> None:
    base = dict(source_tree="s" * 64, lockfile_hash="l" * 64, toolchain_digest=TOOLCHAIN)
    assert state_sha(**base, data_refs=["b", "a"], weight_refs=["w2", "w1"]) == state_sha(
        **base, data_refs=["a", "b"], weight_refs=["w1", "w2"]
    )


def test_each_ref_kind_perturbs_state_sha() -> None:
    base = dict(source_tree="s" * 64, lockfile_hash="l" * 64, toolchain_digest=TOOLCHAIN)
    plain = state_sha(**base)
    assert state_sha(**base, data_refs=["x"]) != plain
    assert state_sha(**base, feature_defs=["x"]) != plain
    assert state_sha(**base, weight_refs=["x"]) != plain
    # ref kinds are domain-separated: same ref in a different slot != same key
    assert state_sha(**base, data_refs=["x"]) != state_sha(**base, weight_refs=["x"])


# --- execution_reuse_key --------------------------------------------------------


def test_execution_reuse_key_binds_rotation_and_rule_version() -> None:
    s = "f" * 64
    k = execution_reuse_key(s, 7, "semgrep-rules-2026.06")
    assert k == execution_reuse_key(s, 7, "semgrep-rules-2026.06")  # deterministic
    assert k != execution_reuse_key(s, 8, "semgrep-rules-2026.06")  # rotation boundary
    assert k != execution_reuse_key(s, 7, "semgrep-rules-2026.07")  # rule-set boundary
    assert k != execution_reuse_key("e" * 64, 7, "semgrep-rules-2026.06")


# --- blob cache -----------------------------------------------------------------


def test_warm_cache_matches_cold_and_avoids_rereads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_tree(tmp_path, SOURCE)
    reads = {"n": 0}
    orig = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        reads["n"] += 1
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    cold = make_tree_sha(tmp_path)  # no cache at all
    cache: dict[tuple[str, int, int], str] = {}

    reads["n"] = 0
    first = make_tree_sha(tmp_path, cache=cache)
    assert reads["n"] == len(SOURCE)
    assert len(cache) == len(SOURCE)

    reads["n"] = 0
    second = make_tree_sha(tmp_path, cache=cache)
    assert reads["n"] == 0  # warm cache: no file content re-read
    assert cold == first == second  # cache is a pure optimization

    # content hashes only — no cache key (size/mtime) leaks into the digest
    assert all(len(v) == 64 for v in cache.values())


def test_cache_does_not_mask_content_changes(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    cache: dict[tuple[str, int, int], str] = {}
    before = make_tree_sha(tmp_path, cache=cache)
    (tmp_path / "README.md").write_bytes(b"# demo v2\n")
    assert make_tree_sha(tmp_path, cache=cache) != before


# --- git path (optional, skips without git) --------------------------------------


def test_git_mode_matches_pure_walk(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    write_tree(tmp_path, SOURCE)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(tmp_path),  # ignore user git config
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=env
        )

    try:
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "init", "--no-gpg-sign")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unusable in this environment")

    via_git = normalized_source_tree(tmp_path, prefer_git=True)
    via_walk = normalized_source_tree(tmp_path, prefer_git=False)
    assert via_git == via_walk
