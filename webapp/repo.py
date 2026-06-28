"""Read a finished build's repository straight from the durable store (no output dir).

The code a build produces is NOT primarily a directory on disk — it lives in the
content-addressed store (the CAS) as FeatureDiff blobs, and the integrated tree
is *materialized* from those blobs over the build's final :class:`FactoryState`.
``output_dir`` is only an EXPORT of that tree; the database is the source of truth.

This module reads that source of truth back out for the Workshop's self-hosted
"GitHub" tab. Given a finished build's ``thread_id`` and the durable
``state_dir`` (``FOUNDRY_STATE_DIR``), it:

* loads the build's terminal :class:`FactoryState` from the durable checkpointer
  (the ``channel_values`` of the latest checkpoint for that thread), and
* materializes the integrated tree from the durable CAS via the SAME
  :func:`materialize_workdir` + ``resolve`` / ``resolve_blob`` seams the gates ran
  against — so the bytes read back are EXACTLY what shipped, never a re-render.

It also surfaces the GitHub-style metadata the tab shows: the gate verdicts as
"checks", and each FeatureDiff's unified-diff body as the "pull request" diff.

Everything here is READ-ONLY: it opens the durable backends, reads, and closes
them. It never drives a build, writes a gate, or mutates the store.
"""

from __future__ import annotations

import contextlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState

# The Spec gate's criteria document is a gate INPUT, not a source file — excluded
# from the repo view exactly as the CLI's ``_write_output_tree`` excludes it.
_GATE_INPUT_FILES = frozenset({"criteria.json"})

# Files we never surface as part of the "repo" (materialization scaffolding).
_HIDDEN = _GATE_INPUT_FILES


@dataclass(frozen=True)
class RepoFile:
    """One file in the materialized tree."""

    path: str
    size: int


@dataclass(frozen=True)
class GateCheck:
    """A gate verdict rendered as a GitHub-style status check."""

    name: str
    status: str  # pass | fail | error | …
    ok: bool
    advisory: bool
    findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullDiff:
    """One FeatureDiff's unified-diff body — the 'pull request' the build merged."""

    uri: str
    summary: str
    text: str


@dataclass(frozen=True)
class Repo:
    """A finished build's repository read from the durable store."""

    thread_id: str
    tree_sha: str
    files: tuple[RepoFile, ...]
    checks: tuple[GateCheck, ...]
    diffs: tuple[PullDiff, ...]
    _blobs: dict[str, bytes] = field(default_factory=dict, repr=False)

    def file_bytes(self, path: str) -> bytes | None:
        """The bytes of ``path`` in the materialized tree, or ``None`` if absent."""
        return self._blobs.get(path)


class RepoUnavailable(RuntimeError):
    """The build's tree could not be read from the store (no checkpoint / CAS miss)."""


def _load_final_state(thread_id: str, backends: Any) -> FactoryState:
    """The terminal :class:`FactoryState` for ``thread_id`` from the checkpointer.

    The latest checkpoint's ``channel_values`` IS the FactoryState shape
    (``request`` / ``gates`` / ``artifacts`` / …). Fail-closed: a thread with no
    durable checkpoint raises :class:`RepoUnavailable`.
    """
    tup = backends.checkpointer.saver.get_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None or not getattr(tup, "checkpoint", None):
        raise RepoUnavailable(f"no durable checkpoint for build {thread_id!r}")
    values = tup.checkpoint.get("channel_values") or {}
    if not isinstance(values, dict):
        raise RepoUnavailable(f"checkpoint for {thread_id!r} has no channel state")
    # The checkpoint's channel_values IS the FactoryState shape (request / gates /
    # artifacts / …) — the same dict the graph nodes read and write.
    return cast("FactoryState", values)


def _gate_checks(state: FactoryState) -> tuple[GateCheck, ...]:
    """The newest verdict per gate, rendered as status checks (order-stable)."""
    newest: dict[str, Any] = {}
    for g in state.get("gates") or ():
        cur = newest.get(g.name)
        if cur is None or g.seq > cur.seq:
            newest[g.name] = g
    checks: list[GateCheck] = []
    for name, g in newest.items():
        status = str(g.status).split(".")[-1].lower()
        checks.append(
            GateCheck(
                name=name,
                status=status,
                ok=status == "pass",
                advisory=bool(getattr(g, "advisory", False)),
                findings=tuple(getattr(g, "findings", ()) or ()),
            )
        )
    return tuple(checks)


def _pull_diffs(state: FactoryState, backends: Any) -> tuple[PullDiff, ...]:
    """Each FeatureDiff's raw unified-diff body — the build's 'pull requests'."""
    from foundry.graph.gate_wiring import FEATURE_DIFF_KIND, _of_kind, _resolve_typed
    from foundry.schemas.implementation import FeatureDiff

    diffs: list[PullDiff] = []
    for artifact in sorted(_of_kind(state, FEATURE_DIFF_KIND), key=lambda a: a.seq):
        try:
            diff = _resolve_typed(artifact, backends.cas.resolve, FeatureDiff, FEATURE_DIFF_KIND)
            body = backends.cas.resolve_blob(diff.diff_uri)
            text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        except Exception:
            continue
        task = str(getattr(diff, "task_id", "") or "")
        branch = str(getattr(diff, "branch", "") or "")
        summary = " · ".join(p for p in (task, branch) if p) or "(feature diff)"
        diffs.append(PullDiff(uri=artifact.uri, summary=summary, text=text))
    return tuple(diffs)


def read_repo(thread_id: str, state_dir: str | Path) -> Repo:
    """Read a finished build's repository from the durable store (no output dir).

    Opens the durable backends rooted at ``state_dir``, loads the build's terminal
    state from the checkpointer, materializes the integrated tree from the durable
    CAS, and reads back the gate checks + FeatureDiff 'pull requests'. The backends
    are always closed before returning.

    Raises :class:`RepoUnavailable` if the build has no durable checkpoint (e.g. an
    older build that predates ``FOUNDRY_STATE_DIR`` durability).
    """
    from foundry.durability.persistence import durable_backends
    from foundry.graph.gate_wiring import materialize_workdir

    backends = durable_backends(state_dir)
    try:
        state = _load_final_state(thread_id, backends)
        checks = _gate_checks(state)
        diffs = _pull_diffs(state, backends)
        blobs: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="foundry-repo-") as staging:
            root = Path(staging)
            try:
                tree_sha = materialize_workdir(
                    state, root,
                    resolve=backends.cas.resolve, resolve_blob=backends.cas.resolve_blob,
                )
            except Exception as exc:  # an unresolvable handle / unparseable diff
                raise RepoUnavailable(
                    f"could not materialize tree for {thread_id!r}: {exc}"
                ) from exc
            for src in sorted(root.rglob("*")):
                if not src.is_file():
                    continue
                rel = src.relative_to(root)
                if rel.name in _HIDDEN and len(rel.parts) == 1:
                    continue
                blobs[str(rel)] = src.read_bytes()
        files = tuple(RepoFile(path=p, size=len(b)) for p, b in sorted(blobs.items()))
        return Repo(
            thread_id=thread_id,
            tree_sha=tree_sha,
            files=files,
            checks=checks,
            diffs=diffs,
            _blobs=blobs,
        )
    finally:
        with contextlib.suppress(Exception):
            backends.close()
