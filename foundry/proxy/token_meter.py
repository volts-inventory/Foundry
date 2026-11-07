"""Per-run caps shared by the proxy (persona calls) and the coding adapter.

Two budgets, both optional: a token cap (cloud tokens only — a local model's
tokens cost nothing and are never charged) and a wall-clock cap. Each is checked
BEFORE each model call or coding session, so a run stops at a clean boundary
instead of mid-call; the call already in flight may overshoot it. With durable
state the stopped run resumes from its last checkpoint, still held to both caps.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "BuildStopped",
    "RunCapExceeded",
    "TimeCapExceeded",
    "TokenCapExceeded",
    "TokenMeter",
    "weighted_tokens",
]


class RunCapExceeded(RuntimeError):
    """A run budget (tokens or wall-clock time) is spent. Not a provider failure:
    never auto-retried; the run stops cleanly and a rerun resumes it."""


class TokenCapExceeded(RunCapExceeded):
    """The run's token cap is spent."""


class TimeCapExceeded(RunCapExceeded):
    """The run's wall-clock cap is spent."""


def _now() -> float:
    return time.monotonic()


class BuildStopped(TokenCapExceeded):
    """The operator stopped the run. A subclass so it takes the cap's clean-stop path
    (implementation writes kept, the run resumable from its checkpoint)."""


def weighted_tokens(usage: Mapping[str, int]) -> int:
    """Tokens a call cost against the cap. Cache reads count at a tenth, matching
    their price; counted in full they would trip the cap on every long agent
    session, whose re-read context dominates its raw token count."""
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0) // 10
    )


class TokenMeter:
    """Thread-safe running totals (the IC pool runs coding sessions in parallel).

    ``cap=None`` counts tokens without capping them; ``time_cap_s`` caps the run's
    wall-clock time. With a ``ledger`` file both totals survive a restart, so a
    resumed run is held to its caps across every process it took; :meth:`reset`
    starts a fresh run. A set ``cancel`` event makes :meth:`check` raise
    :class:`BuildStopped`, at the same seams the caps are checked."""

    def __init__(
        self, cap: int | None, ledger: Path | None = None,
        cancel: threading.Event | None = None, *, time_cap_s: float | None = None,
    ) -> None:
        if cap is not None and cap < 1:
            raise ValueError("token cap must be >= 1")
        if time_cap_s is not None and time_cap_s <= 0:
            raise ValueError("time cap must be > 0 seconds")
        self.cap = cap
        self.time_cap_s = time_cap_s
        self.cancel = cancel
        self.ledger = ledger
        self.used = 0
        self._elapsed_base = 0.0
        if ledger is not None and ledger.exists():
            saved = json.loads(ledger.read_text())
            self.used = int(saved.get("used", 0))
            self._elapsed_base = float(saved.get("elapsed_s", 0.0))
        self._started = _now()
        self._lock = threading.Lock()

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds this run has spent, across every process it took."""
        return self._elapsed_base + (_now() - self._started)

    @property
    def remaining_s(self) -> float | None:
        """Wall-clock seconds left under the time cap; ``None`` without one."""
        if self.time_cap_s is None:
            return None
        return max(0.0, self.time_cap_s - self.elapsed_s)

    def reset(self) -> None:
        with self._lock:
            self.used = 0
            self._elapsed_base = 0.0
            self._started = _now()
            self._save()

    def _save(self, elapsed: float | None = None) -> None:
        if self.ledger is None:
            return
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger.with_suffix(".tmp")
        spent = self.elapsed_s if elapsed is None else elapsed
        tmp.write_text(json.dumps({"used": self.used, "elapsed_s": spent}))
        os.replace(tmp, self.ledger)

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise BuildStopped("stopped by the operator")

    def check(self) -> None:
        self.check_cancel()
        with self._lock:
            if self.cap is not None and self.used >= self.cap:
                raise TokenCapExceeded(
                    f"token cap reached: {self.used:,} of {self.cap:,} tokens used"
                )
            if self.time_cap_s is not None:
                # Saved on every check, not only on charge: local calls are never
                # charged, and an all-local run must still resume with its time spent.
                elapsed = self.elapsed_s
                self._save(elapsed)
                if elapsed >= self.time_cap_s:
                    raise TimeCapExceeded(
                        f"time cap reached: {elapsed / 60:.1f} of "
                        f"{self.time_cap_s / 60:.1f} minutes used"
                    )

    def charge(self, usage: Mapping[str, int]) -> None:
        with self._lock:
            self.used += weighted_tokens(usage)
            self._save()
