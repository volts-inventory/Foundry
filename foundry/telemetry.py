"""Run-scoped fire counts: how often each pattern hint, planner pinned constraint
and fix-loop stop rule fired in a run, so a hint that never fires can be found
and deleted. :func:`count` is a no-op outside :func:`counting`."""

from __future__ import annotations

import contextlib
import functools
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

COUNTS_FILE = "hint_counts.json"
"""Written per process into the run's ``<state-dir>/calls/<run>/`` dir."""


class Counts:
    """One run's counts. Shared by every context copied from the installing one
    (graph node threads, the work pool, the planner's epic pool), hence the lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def add(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))


_COUNTS: ContextVar[Counts | None] = ContextVar("foundry_telemetry_counts", default=None)


@contextlib.contextmanager
def counting() -> Iterator[Counts]:
    counts = Counts()
    token = _COUNTS.set(counts)
    try:
        yield counts
    finally:
        _COUNTS.reset(token)


def count(name: str) -> None:
    counts = _COUNTS.get()
    if counts is not None:
        counts.add(name)


def counted(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Count ``name`` once per call that returns anything truthy (a hint fired)."""

    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            out = fn(*args, **kwargs)
            if out:
                count(name)
            return out

        return inner

    return wrap
