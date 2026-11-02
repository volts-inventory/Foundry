"""Tests for foundry.state.tree_sha (architecture.md §4, DS4 invariants)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from foundry.state.tree_sha import execution_reuse_key, tree_sha

TOOLCHAIN = "builder-image@sha256:abc123/python3.12.6"


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


def make_tree_sha(root: Path) -> str:
    return tree_sha(root, toolchain_digest=TOOLCHAIN)


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


def test_toolchain_digest_change_changes_hash(tmp_path: Path) -> None:
    write_tree(tmp_path, SOURCE)
    a = tree_sha(tmp_path, toolchain_digest="toolchain-v1")
    b = tree_sha(tmp_path, toolchain_digest="toolchain-v2")
    assert a != b


# --- execution_reuse_key --------------------------------------------------------


def test_execution_reuse_key_binds_rotation_and_rule_version() -> None:
    s = "f" * 64
    k = execution_reuse_key(s, 7, "semgrep-rules-2026.06")
    assert k == execution_reuse_key(s, 7, "semgrep-rules-2026.06")  # deterministic
    assert k != execution_reuse_key(s, 8, "semgrep-rules-2026.06")  # rotation boundary
    assert k != execution_reuse_key(s, 7, "semgrep-rules-2026.07")  # rule-set boundary
    assert k != execution_reuse_key("e" * 64, 7, "semgrep-rules-2026.06")
