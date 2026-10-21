"""Checkpoint anchors: the subject artifact + integrated ``state_sha`` a decision binds to.

Pure functions of the bound inputs, so they are stable across a kill-then-resume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

__all__ = ["newest_artifact_uri", "state_sha"]


def newest_artifact_uri(state: FactoryState, kind: str) -> str:
    """The newest-by-``Seq`` artifact uri of ``kind`` on the ``artifacts`` channel.

    A checkpoint decision binds to this artifact. ``artifacts`` folds by
    ``merge_by_uri`` (insertion-order, not seq),
    so we pick the max-``Seq`` handle of the requested kind (mirrors
    ``senior_engineer._first_of_kind`` / QS8) rather than ``artifacts[-1]``.
    Empty when no such artifact exists yet.
    """
    best: Artifact | None = None
    for a in state.get("artifacts") or ():
        if a.kind != kind:
            continue
        if best is None or a.seq > best.seq:
            best = a
    return best.uri if best is not None else ""


def state_sha(config: RunnableConfig) -> str:
    """The integrated ``state_sha`` a checkpoint decision binds to (a RUN INPUT; Q1).

    Threaded onto ``config['configurable']`` by the runtime exactly like
    ``thread_id`` / ``verdict_nonce``; empty when absent."""
    configurable = config.get("configurable") or {}
    return str(configurable.get("state_sha") or "")
