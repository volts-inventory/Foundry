"""Guardian — orchestrates the deterministic monitors. (governance.md §3)

`assess(events)` feeds typed events to the monitors and folds the results
into a `GuardianAssessment` whose action is the most severe mandated action
among the deterministic trips:

- critical-diff  → ESCALATE (to HITL — gov §3 hard rule)
- cap breach     → HALT
- loop           → PAUSE
- deny-threshold → ESCALATE; oscillation → PAUSE
- nothing        → OBSERVE

**Advisory analyzer sandbox contract** (gov §3): the analyzer is consulted
*toolless* and sees only the deterministically-extracted, structured
`features` mapping — never raw transcripts, never tools, never the live
assessment object's mutability (it is frozen). Its `flag()` strings can only
ANNOTATE (`advisory_flags`); the deterministic `action` and `trips` are
computed *before* the analyzer runs and re-asserted *after*, so an analyzer
cannot approve work, clear a trip, or downgrade an action. An analyzer that
raises is recorded as an annotation failure, never an override.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable

from foundry.monitors import Trip, TripKind
from foundry.monitors.constraint_pressure_monitor import ConstraintPressureMonitor
from foundry.monitors.cost_monitor import CostAlert, CostMonitor
from foundry.monitors.coverage_delta_monitor import CoverageDeltaMonitor
from foundry.monitors.loop_detector import SemanticLoopDetector
from foundry.monitors.oracle_monitor import DiffEntry, OracleMonitor
from foundry.monitors.pinned_id_drift_monitor import PinnedIdDriftMonitor
from foundry.monitors.policy_monitor import PolicyMonitor
from foundry.schemas.base import BudgetLedger, FrozenModel


class GuardianAction(StrEnum):
    HALT = "halt"
    PAUSE = "pause"
    ESCALATE = "escalate"  # to HITL
    OBSERVE = "observe"


#: mandated action per trip kind — deterministic code, never analyzer judgment.
MANDATED_ACTIONS: dict[TripKind, GuardianAction] = {
    TripKind.CRITICAL_DIFF: GuardianAction.ESCALATE,
    TripKind.LOOP: GuardianAction.PAUSE,
    TripKind.CAP_BREACH: GuardianAction.HALT,
    TripKind.POLICY_DENY_THRESHOLD: GuardianAction.ESCALATE,
    TripKind.RETRY_OSCILLATION: GuardianAction.PAUSE,
    TripKind.PINNED_ID_DRIFT: GuardianAction.ESCALATE,
    TripKind.CONSTRAINT_PRESSURE: GuardianAction.ESCALATE,
    TripKind.COVERAGE_DELTA: GuardianAction.ESCALATE,
}

#: severity order for folding multiple trips into one action.
_ACTION_SEVERITY: dict[GuardianAction, int] = {
    GuardianAction.OBSERVE: 0,
    GuardianAction.PAUSE: 1,
    GuardianAction.ESCALATE: 2,
    GuardianAction.HALT: 3,
}


@runtime_checkable
class AdvisoryAnalyzer(Protocol):
    """The sandboxed-LLM analyzer seam — ADVISORY ONLY (gov §3).

    Contract: toolless; input is the structured-features mapping only; output
    is a list of flag strings. It cannot return an action, a verdict, or a
    trip mutation — the type itself is the sandbox boundary in this layer.
    """

    def flag(self, features: Mapping[str, object]) -> list[str]: ...


# -- typed events fed to assess() -------------------------------------------


class GateFailureEvent(FrozenModel):
    """A gate failure observation for the loop detector."""

    criterion_or_task_id: str
    gate_name: str
    failing_ids: tuple[str, ...]
    diff_text: str | None = None


class LedgerEvent(FrozenModel):
    """A superstep-boundary ledger transition for the cost monitor."""

    prev: BudgetLedger
    curr: BudgetLedger


class DiffEvent(FrozenModel):
    """A diff (changed paths + hunks) for the oracle monitor."""

    entries: tuple[DiffEntry, ...]


class PolicyDenialEvent(FrozenModel):
    policy: str
    subject: str = ""


class GateVerdictEvent(FrozenModel):
    """A pass/fail verdict observation for the oscillation detector."""

    key: str
    passed: bool


class PinnedIdDriftEvent(FrozenModel):
    """Proxy-observed pinned-ID drift events (arch §8). Each mapping carries at
    least ``requested_model`` + ``served_model`` (the proxy's `drift_events`)."""

    events: tuple[Mapping[str, object], ...]


class ConstraintPressureEvent(FrozenModel):
    """Per-node pinned-constraint injection counts vs the budget (arch §8)."""

    node_id: str
    injected_count: int
    budget: int
    global_count: int = 0


class CoverageDeltaEvent(FrozenModel):
    """Frozen high-risk baseline vs runner-attested satisfied count (gov §3)."""

    baseline_high_risk: int
    attested_satisfied: int
    run_against: str = ""


MonitorEvent = (
    GateFailureEvent
    | LedgerEvent
    | DiffEvent
    | PolicyDenialEvent
    | GateVerdictEvent
    | PinnedIdDriftEvent
    | ConstraintPressureEvent
    | CoverageDeltaEvent
)


class GuardianAssessment(FrozenModel):
    action: GuardianAction
    trips: tuple[Trip, ...] = ()
    alerts: tuple[CostAlert, ...] = ()
    advisory_flags: tuple[str, ...] = ()  # annotations only — never load-bearing


class Guardian:
    def __init__(
        self,
        *,
        loop_detector: SemanticLoopDetector | None = None,
        cost_monitor: CostMonitor | None = None,
        oracle_monitor: OracleMonitor | None = None,
        policy_monitor: PolicyMonitor | None = None,
        pinned_id_drift_monitor: PinnedIdDriftMonitor | None = None,
        constraint_pressure_monitor: ConstraintPressureMonitor | None = None,
        coverage_delta_monitor: CoverageDeltaMonitor | None = None,
        analyzer: AdvisoryAnalyzer | None = None,
    ) -> None:
        self.loop_detector = loop_detector or SemanticLoopDetector()
        self.cost_monitor = cost_monitor or CostMonitor()
        self.oracle_monitor = oracle_monitor or OracleMonitor()
        self.policy_monitor = policy_monitor or PolicyMonitor()
        self.pinned_id_drift_monitor = pinned_id_drift_monitor or PinnedIdDriftMonitor()
        self.constraint_pressure_monitor = (
            constraint_pressure_monitor or ConstraintPressureMonitor()
        )
        self.coverage_delta_monitor = coverage_delta_monitor or CoverageDeltaMonitor()
        self.analyzer = analyzer

    def assess(self, events: Iterable[MonitorEvent]) -> GuardianAssessment:
        trips: list[Trip] = []
        alerts: list[CostAlert] = []
        advisory: list[str] = []

        for event in events:
            match event:
                case GateFailureEvent():
                    verdict = self.loop_detector.observe(
                        event.criterion_or_task_id,
                        event.gate_name,
                        event.failing_ids,
                        event.diff_text,
                    )
                    if verdict.trip is not None:
                        trips.append(verdict.trip)
                    if verdict.similarity_flag:
                        advisory.append(
                            f"diff-similarity {verdict.diff_similarity:.2f} for "
                            f"({event.criterion_or_task_id}, {event.gate_name}) "
                            "— advisory secondary signal"
                        )
                case LedgerEvent():
                    report = self.cost_monitor.observe(event.prev, event.curr)
                    alerts.extend(report.alerts)
                    if report.trip is not None:
                        trips.append(report.trip)
                case DiffEvent():
                    trip = self.oracle_monitor.inspect(event.entries)
                    if trip is not None:
                        trips.append(trip)
                case PolicyDenialEvent():
                    denial_trip = self.policy_monitor.record_denial(event.policy, event.subject)
                    if denial_trip is not None:
                        trips.append(denial_trip)
                case GateVerdictEvent():
                    verdict_trip = self.policy_monitor.record_verdict(event.key, event.passed)
                    if verdict_trip is not None:
                        trips.append(verdict_trip)
                case PinnedIdDriftEvent():
                    drift_trip = self.pinned_id_drift_monitor.inspect(event.events)
                    if drift_trip is not None:
                        trips.append(drift_trip)
                case ConstraintPressureEvent():
                    pressure_trip = self.constraint_pressure_monitor.inspect(
                        node_id=event.node_id,
                        injected_count=event.injected_count,
                        budget=event.budget,
                        global_count=event.global_count,
                    )
                    if pressure_trip is not None:
                        trips.append(pressure_trip)
                case CoverageDeltaEvent():
                    coverage_trip = self.coverage_delta_monitor.inspect(
                        baseline_high_risk=event.baseline_high_risk,
                        attested_satisfied=event.attested_satisfied,
                        run_against=event.run_against,
                    )
                    if coverage_trip is not None:
                        trips.append(coverage_trip)

        deterministic = GuardianAssessment(
            action=self._fold_action(trips),
            trips=tuple(trips),
            alerts=tuple(alerts),
            advisory_flags=tuple(advisory),
        )
        return self._consult_analyzer(deterministic)

    @staticmethod
    def _fold_action(trips: Iterable[Trip]) -> GuardianAction:
        action = GuardianAction.OBSERVE
        for trip in trips:
            mandated = MANDATED_ACTIONS[trip.kind]
            if _ACTION_SEVERITY[mandated] > _ACTION_SEVERITY[action]:
                action = mandated
        return action

    def _consult_analyzer(self, deterministic: GuardianAssessment) -> GuardianAssessment:
        """Annotate-only analyzer consult; the deterministic verdict is law.

        The analyzer receives a structured-features snapshot (its sandbox
        contract: toolless, features-only) and returns strings. We then
        ASSERT the deterministic action/trips are byte-identical — any path
        by which an analyzer could downgrade or clear a trip is a bug here.
        """
        if self.analyzer is None:
            return deterministic
        features: dict[str, object] = {
            "action": str(deterministic.action),
            "trip_kinds": [str(t.kind) for t in deterministic.trips],
            "trip_rules": [t.rule for t in deterministic.trips],
            "alert_rules": [a.rule for a in deterministic.alerts],
        }
        try:
            raw = self.analyzer.flag(features)
            extra = [f for f in raw if isinstance(f, str)]
        except Exception as exc:  # analyzer failure is an annotation, not a veto
            extra = [f"analyzer-error: {type(exc).__name__}"]
        annotated = deterministic.model_copy(
            update={"advisory_flags": deterministic.advisory_flags + tuple(extra)}
        )
        # gov §3 hard rule — advisory layer can NEVER alter the deterministic verdict:
        assert annotated.action == deterministic.action
        assert annotated.trips == deterministic.trips
        return annotated
