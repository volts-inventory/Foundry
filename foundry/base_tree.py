"""The base tree of a change build: an earlier build's output that a new request
extends.

A change build (``run(..., base_dir=...)`` / ``--base DIR``) plans and builds only
the delta. The base tree is the starting point of every ticket sandbox and of
every gate's materialized workdir, so the gates always judge base + delta. This
module owns which base files count (:func:`base_files`), copying them
(:func:`copy_base_tree`), and the compact view the planner reads
(:func:`read_base` / :func:`render_base_view`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from foundry.canonical import content_digest
from foundry.schemas.implementation import FEATURE_DIR
from foundry.state.tree_sha import default_exclude

__all__ = [
    "MANIFEST_FILES",
    "BaseView",
    "base_digest",
    "base_features",
    "base_files",
    "change_thread_id",
    "copy_base_tree",
    "read_base",
    "render_base_view",
]

# Run reports of the base build, not project source: a change writes its own.
MANIFEST_FILES = frozenset({"BUILD-MANIFEST.json", "BUILD-MANIFEST.md", "DRAFT-STATUS.md"})
_EXTRA_EXCLUDED_DIRS = frozenset({".mypy_cache", ".ruff_cache", ".run"})
_VIEW_FILES_CAP = 400
_VIEW_TEXT_CAP = 20_000


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    return (
        default_exclude(rel)
        or any(p in _EXTRA_EXCLUDED_DIRS for p in parts)
        or rel in MANIFEST_FILES
        or rel == "criteria.json"
    )


def base_files(base: Path | str) -> list[str]:
    """Sorted repo-relative paths of the base tree's regular files. Symlinks are
    skipped: a link could point outside the tree the gates are confined to."""
    root = Path(base).resolve()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        prefix = here.relative_to(root).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        dirnames[:] = [d for d in dirnames if not _excluded(prefix + d)]
        for name in filenames:
            path = here / name
            rel = prefix + name
            if not path.is_symlink() and path.is_file() and not _excluded(rel):
                out.append(rel)
    return sorted(out)


def copy_base_tree(base: Path | str, dest: Path | str) -> None:
    """Copy the base tree's files into ``dest`` (overwriting what is there)."""
    root, out = Path(base).resolve(), Path(dest)
    for rel in base_files(root):
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)


def base_digest(base: Path | str) -> str:
    """Content digest of the base tree's files (what a change builds on)."""
    root = Path(base).resolve()
    files = [
        [rel, hashlib.sha256((root / rel).read_bytes()).hexdigest()]
        for rel in sorted(base_files(root), key=lambda p: p.encode("utf-8"))
    ]
    return content_digest({"kind": "base_tree", "files": files})


def base_features(base: Path | str) -> dict[str, str]:
    """``features/<name>.feature`` -> text: the base build's scenarios, which stay
    in force (gate-owned) in every change on it."""
    root = Path(base).resolve()
    return {
        rel: (root / rel).read_text(encoding="utf-8")
        for rel in base_files(root)
        if PurePosixPath(rel).parent == PurePosixPath(FEATURE_DIR) and rel.endswith(".feature")
    }


@dataclass(frozen=True)
class BaseView:
    """What the planner needs to know about the base build."""

    files: tuple[str, ...]
    features: dict[str, str]
    stack: dict[str, Any] = field(default_factory=dict)
    contracts: tuple[dict[str, str], ...] = ()
    contracts_files: dict[str, str] = field(default_factory=dict)
    request: str = ""
    thread_id: str = ""

    @property
    def contract_names(self) -> frozenset[str]:
        return frozenset(c.get("name", "") for c in self.contracts if c.get("name"))

    @property
    def feature_names(self) -> frozenset[str]:
        return frozenset(PurePosixPath(f).stem for f in self.features)


def _manifest(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_base(base: Path | str) -> BaseView:
    """The base build's file list, feature files, and — from its BUILD-MANIFEST,
    when it records them — stack, contracts and contracts files."""
    root = Path(base).resolve()
    files = base_files(root)
    manifest = _manifest(root)
    contracts = tuple(
        {k: str(c.get(k, "")) for k in ("name", "kind", "signature")}
        for c in manifest.get("contracts") or ()
        if isinstance(c, dict)
    )
    contracts_files: dict[str, str] = {}
    for rel in manifest.get("contracts_files") or ():
        if isinstance(rel, str) and rel in files:
            try:
                contracts_files[rel] = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    stack = manifest.get("stack")
    return BaseView(
        files=tuple(files),
        features=base_features(root),
        stack=stack if isinstance(stack, dict) else {},
        contracts=contracts,
        contracts_files=contracts_files,
        request=str(manifest.get("request") or ""),
        thread_id=str(manifest.get("thread_id") or ""),
    )


def _capped(text: str, budget: list[int]) -> str:
    take = text[: max(budget[0], 0)]
    budget[0] -= len(take)
    return take + ("\n… (truncated)" if len(take) < len(text) else "")


def render_base_view(view: BaseView) -> str:
    """The size-capped base-tree block of the planner prompt."""
    files = list(view.files[:_VIEW_FILES_CAP])
    more = len(view.files) - len(files)
    lines = [
        "EXISTING PROJECT (this is a CHANGE to a shipped build, not a new project).",
        *([f"Original request: {view.request}"] if view.request else []),
        f"Files ({len(view.files)}):",
        *(f"- {f}" for f in files),
        *([f"- … and {more} more"] if more > 0 else []),
    ]
    if view.stack:
        stack = {k: v for k, v in view.stack.items() if v and k != "notes"}
        lines.append("Stack: " + json.dumps(stack, sort_keys=True))
        if view.stack.get("run"):
            lines.append(f"Run command: {view.stack['run']}")
    if view.contracts:
        lines.append("Existing interface contracts (already implemented in the tree):")
        lines += [
            f"- {c['name']} ({c['kind']}): {c['signature']}".rstrip(": ") for c in view.contracts
        ]
    budget = [_VIEW_TEXT_CAP]
    for rel, text in view.contracts_files.items():
        lines.append(f"--- {rel} ---\n{_capped(text, budget)}")
    for rel, text in view.features.items():
        lines.append(f"--- {rel} (existing scenarios, stay in force) ---\n{_capped(text, budget)}")
    return "\n".join(lines)


def change_thread_id(base: Path | str, request: str) -> str:
    """A run id for one change on one base: never the base build's own thread (a
    reused thread would resume that build), yet stable, so rerunning the same
    change resumes it."""
    key = hashlib.sha256(f"{base_digest(base)}\n{request}".encode()).hexdigest()[:16]
    return f"change-{key}"
