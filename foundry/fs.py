"""Filesystem helpers shared across the execution and gate sandboxes."""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath


def force_rmtree(root: Path) -> None:
    """``rmtree`` that also removes read-only directories (Go's module cache is
    written 0555), which a plain ``ignore_errors`` rmtree silently leaves behind.
    Symlinks are never chmod-ed: each directory is opened O_NOFOLLOW and chmod-ed
    through its fd, so a symlink swapped in after the walk cannot redirect it."""
    shutil.rmtree(root, ignore_errors=True)
    if not root.exists() or root.is_symlink():
        return
    _chmod_dir_nofollow(str(root))
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            _chmod_dir_nofollow(os.path.join(dirpath, name))
    shutil.rmtree(root, ignore_errors=True)


def _chmod_dir_nofollow(path: str) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)


def apply_overlay(worktree: Path, overlay: Mapping[str, str | None]) -> None:
    """Write predecessor slices onto a fresh clone (content ⇒ write, None ⇒
    delete). Paths are containment-checked defensively even though they come
    from our own CAS (same posture as the integration boundary, D-R1-03)."""
    root = worktree.resolve()
    for rel, content in sorted(overlay.items()):
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        target = (root / pure).resolve()
        # Not a string prefix: a symlink to sibling /wt2 must not pass for root /wt.
        if not target.is_relative_to(root):
            raise ValueError(f"overlay path {rel!r} escapes the worktree")
        if content is None:
            if target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
