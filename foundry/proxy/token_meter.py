"""Per-run token cap shared by the proxy (persona calls) and the coding adapter.

The cap is checked BEFORE each model call or coding session, so a run stops at a
clean boundary instead of mid-call; the call already in flight may overshoot it.
With durable state the stopped run resumes from its last checkpoint.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

__all__ = ["TokenCapExceeded", "TokenMeter", "weighted_tokens"]


class TokenCapExceeded(RuntimeError):
    """The run's token cap is spent. Not a provider failure: never auto-retried."""


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
    """Thread-safe running total (the IC pool runs coding sessions in parallel)."""

    def __init__(self, cap: int) -> None:
        if cap < 1:
            raise ValueError("token cap must be >= 1")
        self.cap = cap
        self.used = 0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.used >= self.cap:
                raise TokenCapExceeded(
                    f"token cap reached: {self.used:,} of {self.cap:,} tokens used"
                )

    def charge(self, usage: Mapping[str, int]) -> None:
        with self._lock:
            self.used += weighted_tokens(usage)
