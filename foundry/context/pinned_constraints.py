"""Typed "insufficient context" escape. (architecture.md §8)"""

from __future__ import annotations


class NeedContext(Exception):
    """Typed "insufficient context" escape. (architecture.md §8)

    A node raises this instead of hallucinating over a partial view; the
    orchestrator re-retrieves with `query` or escalates. Retrieval failures are
    detected, not silently tolerated.
    """

    def __init__(self, query: str) -> None:
        super().__init__(query)
        self.query = query
