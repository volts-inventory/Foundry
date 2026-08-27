"""Deterministic monitors — the shared trip primitives. (governance.md §3)

This module owns the shared primitives (`Severity`, `TripKind`, `Trip`) so a
typed trip record can be shared across the codebase without import cycles;
governance (`foundry.governance.human_liaison`) imports `Trip` at runtime.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel

__all__ = [
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
    PINNED_ID_DRIFT = "pinned_id_drift"  # served model != pinned request (arch §8)
    CONSTRAINT_PRESSURE = "constraint_pressure"  # pinned-constraint cardinality (arch §8)
    COVERAGE_DELTA = "coverage_delta"  # runner-attested coverage regression (gov §3)


class Trip(FrozenModel):
    """A deterministic monitor trip. Immutable; there is intentionally NO
    field through which a trip can be marked overridden, cleared, or
    downgraded — gov §3 hard rule."""

    monitor: str  # which monitor tripped
    kind: TripKind
    rule: str  # the specific rule that matched
    reason: str  # human-readable, deterministic explanation
    matched: tuple[str, ...] = ()  # matched paths / keys / signature ids
