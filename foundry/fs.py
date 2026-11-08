"""Filesystem helpers shared across the execution and gate sandboxes."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path


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
