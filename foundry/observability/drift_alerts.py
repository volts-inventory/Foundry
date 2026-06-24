"""Drift detection over the run telemetry. (arch §8; governance.md §2)

Two alert families:

1. **Pinned-ID drift** (the primary, arch §8). The AUTHORITATIVE source is the
   proxy's ``drift_events`` — the single egress already flagged every call whose
   served model id departed from the pinned request (served_model != requested).
   :func:`pinned_id_drift_alerts` REUSES that list verbatim (no re-derivation,
   no second egress, LEARNINGS L6) and lifts each event into a
   :class:`PinnedIdDriftAlert` carrying the calibration-baseline-invalidation
   semantics (arch §8: a pinned-ID drift invalidates prior calibration / eval
   baselines, so a T1 judgment must not silently continue on drifted weights —
   operations.md §5).

2. **Threshold drift** over the collected telemetry (the operator surface):
   per-run cost / latency / usage exceeding an operator-set
   :class:`ThresholdPolicy`. These ride the telemetry spans, not a separate feed.

Fail-loud (governance.md §2): an alert is a first-class value that can be
RAISED, never silently swallowed. :func:`raise_on_drift` raises
:class:`DriftAlertError` carrying the full alert set the moment any alert exists;
callers that want to triage instead call :func:`detect_drift` and inspect.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from foundry.observability.telemetry import RunTelemetry, SpanKind


class AlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    """Base structured alert (operator surface + alert taxonomy, ops §)."""

    kind: str
    severity: AlertSeverity
    detail: str
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PinnedIdDriftAlert(Alert):
    """Served model != pinned request — invalidates calibration baselines.

    Built directly from a proxy ``drift_events`` record; never re-derived.
    ``drift_kind`` distinguishes a wholesale ``pinned-id`` drift from a
    ``tier-mismatch`` (same lineage family, wrong/smaller tier id — a silent
    capability degrade the family resolution alone would not surface).
    """

    node_id: str = ""
    requested_model: str = ""
    served_model: str = ""
    request_id: str = ""
    drift_kind: str = ""


@dataclass(frozen=True, slots=True)
class CostDriftAlert(Alert):
    """Aggregate token cost over the run exceeded the operator threshold."""


@dataclass(frozen=True, slots=True)
class LatencyDriftAlert(Alert):
    """A node/gate latency span exceeded the operator threshold."""


@dataclass(frozen=True, slots=True)
class UsageDriftAlert(Alert):
    """A single model call's token usage exceeded the operator threshold."""


@dataclass(frozen=True, slots=True)
class ThresholdPolicy:
    """Operator-set thresholds for the threshold-drift family.

    ``None`` disables that check (the floor pinned-ID check is never disabled).
    Token costs are integer token counts (float-free, mirrors the budget domain).
    """

    max_total_input_tokens: int | None = None
    max_total_output_tokens: int | None = None
    max_call_total_tokens: int | None = None
    max_node_latency_ms: float | None = None


class DriftAlertError(RuntimeError):
    """Raised (fail-loud) when drift alerts exist and the caller demands none.

    Carries the full alert set so nothing is swallowed (governance.md §2).
    """

    def __init__(self, alerts: Sequence[Alert]) -> None:
        self.alerts: tuple[Alert, ...] = tuple(alerts)
        critical = sum(1 for a in self.alerts if a.severity is AlertSeverity.CRITICAL)
        super().__init__(
            f"{len(self.alerts)} drift alert(s) ({critical} critical): "
            + "; ".join(a.detail for a in self.alerts)
        )


class _DriftEventSource(Protocol):
    """The read-only proxy surface the pinned-ID check consumes (NO egress).

    Structurally satisfied by
    :class:`~foundry.proxy.model_provider_proxy.ModelProviderProxy` via its
    ``drift_events`` property — the authoritative pinned-ID drift list.
    """

    @property
    def drift_events(self) -> tuple[dict[str, Any], ...]: ...


def pinned_id_drift_alerts(source: _DriftEventSource) -> list[PinnedIdDriftAlert]:
    """Lift the proxy's authoritative ``drift_events`` into alerts (no re-derive).

    Pinned-ID drift is always CRITICAL: it invalidates prior calibration / eval
    baselines, so a T1 judgment must not silently continue (arch §8 / ops §5).
    """
    alerts: list[PinnedIdDriftAlert] = []
    for event in source.drift_events:
        node_id = str(event.get("node_id", ""))
        requested = str(event.get("requested_model", ""))
        served = str(event.get("served_model", ""))
        request_id = str(event.get("request_id", ""))
        # kind may be absent on legacy events; default to the generic pinned-id.
        drift_kind = str(event.get("kind") or "pinned-id")
        # Both kinds invalidate calibration baselines; a tier-mismatch additionally
        # notes it is a within-family (capability-degrade) drift the family check
        # alone would pass.
        qualifier = (
            "served a smaller/other tier within the SAME lineage family — "
            "capability silently degraded, and prior calibration/eval baselines "
            "are invalidated (arch §8)"
            if drift_kind == "tier-mismatch"
            else "prior calibration/eval baselines are invalidated (arch §8)"
        )
        alerts.append(
            PinnedIdDriftAlert(
                kind="pinned-id-drift",
                severity=AlertSeverity.CRITICAL,
                detail=(
                    f"[{drift_kind}] node {node_id!r} pinned {requested!r} but was "
                    f"served {served!r} (request_id={request_id!r}); {qualifier} — "
                    "a T1 judgment must not continue silently"
                ),
                context=dict(event),
                node_id=node_id,
                requested_model=requested,
                served_model=served,
                request_id=request_id,
                drift_kind=drift_kind,
            )
        )
    return alerts


def threshold_alerts(telemetry: RunTelemetry, policy: ThresholdPolicy) -> list[Alert]:
    """Threshold-style cost/usage/latency drift over the collected telemetry."""
    alerts: list[Alert] = []
    model_spans = telemetry.spans_of(SpanKind.MODEL_CALL)

    total_in = 0
    total_out = 0
    for span in model_spans:
        usage = span.usage
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        total_in += in_tok
        total_out += out_tok
        if policy.max_call_total_tokens is not None:
            call_total = in_tok + out_tok
            if call_total > policy.max_call_total_tokens:
                alerts.append(
                    UsageDriftAlert(
                        kind="usage-drift",
                        severity=AlertSeverity.WARNING,
                        detail=(
                            f"model call {span.name!r} used {call_total} tokens "
                            f"> threshold {policy.max_call_total_tokens}"
                        ),
                        context={"span_digest": span.digest, "usage": usage},
                    )
                )

    if policy.max_total_input_tokens is not None and total_in > policy.max_total_input_tokens:
        alerts.append(
            CostDriftAlert(
                kind="cost-drift",
                severity=AlertSeverity.WARNING,
                detail=(
                    f"run input tokens {total_in} > threshold {policy.max_total_input_tokens}"
                ),
                context={"total_input_tokens": total_in},
            )
        )
    if policy.max_total_output_tokens is not None and total_out > policy.max_total_output_tokens:
        alerts.append(
            CostDriftAlert(
                kind="cost-drift",
                severity=AlertSeverity.WARNING,
                detail=(
                    f"run output tokens {total_out} > threshold {policy.max_total_output_tokens}"
                ),
                context={"total_output_tokens": total_out},
            )
        )

    if policy.max_node_latency_ms is not None:
        for span in (*telemetry.spans_of(SpanKind.NODE), *telemetry.spans_of(SpanKind.GATE)):
            latency = span.attributes.get("latency_ms")
            if isinstance(latency, (int, float)) and latency > policy.max_node_latency_ms:
                alerts.append(
                    LatencyDriftAlert(
                        kind="latency-drift",
                        severity=AlertSeverity.WARNING,
                        detail=(
                            f"{span.name!r} latency {latency}ms "
                            f"> threshold {policy.max_node_latency_ms}ms"
                        ),
                        context={"span_digest": span.digest, "latency_ms": latency},
                    )
                )

    return alerts


def detect_drift(
    *,
    proxy: _DriftEventSource,
    telemetry: RunTelemetry | None = None,
    policy: ThresholdPolicy | None = None,
) -> list[Alert]:
    """All drift alerts for a run: pinned-ID (authoritative) + threshold.

    The pinned-ID family is ALWAYS evaluated (it cannot be disabled); threshold
    alerts only when both ``telemetry`` and ``policy`` are supplied.
    """
    alerts: list[Alert] = list(pinned_id_drift_alerts(proxy))
    if telemetry is not None and policy is not None:
        alerts.extend(threshold_alerts(telemetry, policy))
    return alerts


def raise_on_drift(
    *,
    proxy: _DriftEventSource,
    telemetry: RunTelemetry | None = None,
    policy: ThresholdPolicy | None = None,
) -> None:
    """Fail-loud (governance.md §2): raise :class:`DriftAlertError` iff any alert.

    An alert is never swallowed — it surfaces as an exception carrying the full
    alert set. Callers that want to triage instead use :func:`detect_drift`.
    """
    alerts = detect_drift(proxy=proxy, telemetry=telemetry, policy=policy)
    if alerts:
        raise DriftAlertError(alerts)


def raise_if(alerts: Iterable[Alert]) -> None:
    """Fail-loud over an already-computed alert set (never swallow)."""
    materialized = list(alerts)
    if materialized:
        raise DriftAlertError(materialized)
