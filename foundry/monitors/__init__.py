"""Deterministic monitors — the load-bearing detections. (governance.md §3)

Hard rule (gov §3): every monitor in this package is **deterministic** — no
LLM call anywhere, so nothing here is prompt-injectable. The sandboxed LLM
"analyzer" is *advisory-only*: it appears in this layer solely as the
`AdvisoryAnalyzer` protocol the Guardian consults to ANNOTATE an assessment
(`foundry.monitors.guardian`); it can never approve work or clear/downgrade a
deterministic trip.

This module owns the shared primitives (`Severity`, `TripKind`, `Trip`) so the
individual monitors can share one typed trip record without import cycles;
the concrete monitors are lazily re-exported below.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.monitors.cost_monitor import CostMonitor
    from foundry.monitors.guardian import Guardian
    from foundry.monitors.loop_detector import SemanticLoopDetector
    from foundry.monitors.oracle_monitor import OracleMonitor
    from foundry.monitors.policy_monitor import PolicyMonitor

__all__ = [
    "CostMonitor",
    "Guardian",
    "OracleMonitor",
    "PolicyMonitor",
    "SemanticLoopDetector",
    "Severity",
    "Trip",
    "TripKind",
]


class Severity(StrEnum):
    """Escalating alert severities (cost thresholds at 50/80/95% use all three)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class TripKind(StrEnum):
    """Deterministic trip taxonomy; the Guardian maps each kind to its
    mandated action — the mapping is code, never analyzer judgment."""

    LOOP = "loop"  # semantic loop signature repeated (arch §7)
    CAP_BREACH = "cap_breach"  # absolute BudgetLedger cap breached
    CRITICAL_DIFF = "critical_diff"  # oracle/gate/criteria/scanner-config touched (gov §3)
    POLICY_DENY_THRESHOLD = "policy_deny_threshold"  # OPA deny-count stand-in
    RETRY_OSCILLATION = "retry_oscillation"  # alternating pass/fail per key


class Trip(FrozenModel):
    """A deterministic monitor trip. Immutable; there is intentionally NO
    field through which a trip can be marked overridden, cleared, or
    downgraded — gov §3 hard rule."""

    monitor: str  # which monitor tripped
    kind: TripKind
    rule: str  # the specific rule that matched
    reason: str  # human-readable, deterministic explanation
    matched: tuple[str, ...] = ()  # matched paths / keys / signature ids


_LAZY = {
    "CostMonitor": "foundry.monitors.cost_monitor",
    "Guardian": "foundry.monitors.guardian",
    "OracleMonitor": "foundry.monitors.oracle_monitor",
    "PolicyMonitor": "foundry.monitors.policy_monitor",
    "SemanticLoopDetector": "foundry.monitors.loop_detector",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(name)
