"""``apply_overlay`` writes predecessor slices onto a clone and refuses any path
that would land outside it."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.fs import apply_overlay


def test_overlay_writes_and_deletes(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "old.py").write_text("x\n")
    apply_overlay(wt, {"pkg/new.py": "y\n", "old.py": None})
    assert (wt / "pkg" / "new.py").read_text() == "y\n"
    assert not (wt / "old.py").exists()


@pytest.mark.parametrize("rel", ["/etc/x", "../wt2/x", "a/../../x"])
def test_overlay_refuses_traversal(tmp_path: Path, rel: str) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(ValueError, match="escapes the worktree"):
        apply_overlay(wt, {rel: "z\n"})


def test_overlay_refuses_symlink_to_sibling_prefix_dir(tmp_path: Path) -> None:
    # /…/wt2 shares the string prefix /…/wt: only a real containment check refuses it.
    wt = tmp_path / "wt"
    wt.mkdir()
    sibling = tmp_path / "wt2"
    sibling.mkdir()
    (wt / "link").symlink_to("../wt2")
    with pytest.raises(ValueError, match="escapes the worktree"):
        apply_overlay(wt, {"link/pwned.py": "z\n"})
    assert not (sibling / "pwned.py").exists()
