"""Cost monitor — thresholds over `BudgetLedger` deltas. (gov §3, §7)

Three deterministic detections:

- **Per-superstep spike**: token / compute delta between consecutive ledger
  snapshots exceeding a configured spike threshold → WARNING alert.
- **Cumulative percentage-of-cap alerts** at 50/80/95% with escalating
  severities (INFO/WARNING/CRITICAL); each threshold fires once per monitor.
- **Absolute cap breach** → deterministic trip (Guardian maps it to HALT).

Pure integer arithmetic over the single-writer ledger — no LLM (gov §3).
"""

from __future__ import annotations

from foundry.monitors import Severity, Trip, TripKind
from foundry.schemas.base import BudgetLedger, FrozenModel

#: percentage thresholds → escalating severity (gov §3 "50/80/95").
DEFAULT_ALERT_THRESHOLDS: tuple[tuple[int, Severity], ...] = (
    (50, Severity.INFO),
    (80, Severity.WARNING),
    (95, Severity.CRITICAL),
)


class CostAlert(FrozenModel):
    severity: Severity
    rule: str  # "cap-pct-50" | "token-spike" | "compute-spike" | ...
    message: str
    threshold_pct: int | None = None


class CostReport(FrozenModel):
    alerts: tuple[CostAlert, ...] = ()
    trip: Trip | None = None  # absolute cap breach only


class CostMonitor:
    def __init__(
        self,
        *,
        superstep_token_spike: int | None = None,
        superstep_compute_spike_ms: int | None = None,
        alert_thresholds: tuple[tuple[int, Severity], ...] = DEFAULT_ALERT_THRESHOLDS,
    ) -> None:
        self.superstep_token_spike = superstep_token_spike
        self.superstep_compute_spike_ms = superstep_compute_spike_ms
        self.alert_thresholds = tuple(sorted(alert_thresholds))
        self._fired_pcts: set[int] = set()
        self._breach_tripped = False

    def observe(self, prev: BudgetLedger, curr: BudgetLedger) -> CostReport:
        """Assess one superstep boundary: `prev` → `curr` ledger snapshots."""
        alerts: list[CostAlert] = []

        token_delta = (curr.tokens_in + curr.tokens_out) - (prev.tokens_in + prev.tokens_out)
        if self.superstep_token_spike is not None and token_delta > self.superstep_token_spike:
            alerts.append(
                CostAlert(
                    severity=Severity.WARNING,
                    rule="token-spike",
                    message=(
                        f"superstep token delta {token_delta} exceeds "
                        f"spike threshold {self.superstep_token_spike}"
                    ),
                )
            )
        compute_delta = curr.compute_ms - prev.compute_ms
        if (
            self.superstep_compute_spike_ms is not None
            and compute_delta > self.superstep_compute_spike_ms
        ):
            alerts.append(
                CostAlert(
                    severity=Severity.WARNING,
                    rule="compute-spike",
                    message=(
                        f"superstep compute delta {compute_delta}ms exceeds "
                        f"spike threshold {self.superstep_compute_spike_ms}ms"
                    ),
                )
            )

        trip: Trip | None = None
        if curr.cap_microusd > 0:
            for pct, severity in self.alert_thresholds:
                # integer comparison: spend/cap >= pct/100  <=>  spend*100 >= cap*pct
                if pct not in self._fired_pcts and curr.spend_microusd * 100 >= (
                    curr.cap_microusd * pct
                ):
                    self._fired_pcts.add(pct)
                    alerts.append(
                        CostAlert(
                            severity=severity,
                            rule=f"cap-pct-{pct}",
                            message=(
                                f"cumulative spend {curr.spend_microusd}µ$ crossed "
                                f"{pct}% of cap {curr.cap_microusd}µ$"
                            ),
                            threshold_pct=pct,
                        )
                    )
            if not self._breach_tripped and curr.spend_microusd >= curr.cap_microusd:
                self._breach_tripped = True
                trip = Trip(
                    monitor="cost_monitor",
                    kind=TripKind.CAP_BREACH,
                    rule="absolute-cap-breach",
                    reason=(
                        f"spend {curr.spend_microusd}µ$ breached absolute cap "
                        f"{curr.cap_microusd}µ$"
                    ),
                    matched=(str(curr.spend_microusd), str(curr.cap_microusd)),
                )

        return CostReport(alerts=tuple(alerts), trip=trip)
