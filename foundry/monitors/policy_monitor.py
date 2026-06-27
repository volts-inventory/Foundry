"""Policy monitor — OPA deny-count stand-in + retry-oscillation detector.
(governance.md §3)

Two deterministic detections:

- **Deny-count threshold**: policy denials (fed from the OPA decision log —
  a trusted-infra signal, never agent self-report) are counted per policy;
  reaching the threshold trips.
- **Retry-oscillation** ("retry-until-green"): per key, a window of verdicts
  that strictly alternates pass/fail across the full window trips — the
  signature of an agent flip-flopping a gate rather than converging.

No LLM (gov §3).
"""

from __future__ import annotations

from collections import deque

from foundry.monitors import Trip, TripKind

DEFAULT_DENY_THRESHOLD = 5
DEFAULT_OSCILLATION_WINDOW = 6


class PolicyMonitor:
    def __init__(
        self,
        *,
        deny_threshold: int = DEFAULT_DENY_THRESHOLD,
        oscillation_window: int = DEFAULT_OSCILLATION_WINDOW,
    ) -> None:
        if deny_threshold < 1:
            raise ValueError("deny_threshold must be >= 1")
        if oscillation_window < 4:
            raise ValueError("oscillation_window must be >= 4 to be meaningful")
        self.deny_threshold = deny_threshold
        self.oscillation_window = oscillation_window
        self._deny_counts: dict[str, int] = {}
        self._verdicts: dict[str, deque[bool]] = {}

    # -- deny-count -------------------------------------------------------
    def record_denial(self, policy: str, subject: str = "") -> Trip | None:
        """Record one policy denial; trips when the per-policy count reaches
        the threshold (and on every denial past it)."""
        count = self._deny_counts.get(policy, 0) + 1
        self._deny_counts[policy] = count
        if count >= self.deny_threshold:
            return Trip(
                monitor="policy_monitor",
                kind=TripKind.POLICY_DENY_THRESHOLD,
                rule="deny-count",
                reason=(
                    f"policy {policy!r} denied {count}x "
                    f"(threshold {self.deny_threshold})"
                ),
                matched=(policy, subject) if subject else (policy,),
            )
        return None

    # -- retry-oscillation --------------------------------------------------
    def record_verdict(self, key: str, passed: bool) -> Trip | None:
        """Record a pass/fail verdict for `key`; trips when the last
        `oscillation_window` verdicts strictly alternate."""
        window = self._verdicts.setdefault(key, deque(maxlen=self.oscillation_window))
        window.append(passed)
        if len(window) == self.oscillation_window and self._alternating(window):
            return Trip(
                monitor="policy_monitor",
                kind=TripKind.RETRY_OSCILLATION,
                rule="verdict-oscillation",
                reason=(
                    f"verdicts for {key!r} alternated pass/fail across the last "
                    f"{self.oscillation_window} observations"
                ),
                matched=(key,),
            )
        return None

    @staticmethod
    def _alternating(window: deque[bool]) -> bool:
        seq = list(window)
        return all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))
