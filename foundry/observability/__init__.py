"""The observability plane: structured run telemetry + drift alerts.

(architecture.md §8 determinism policy; operations.md observability;
governance.md §2 "Observability plane: OTel + fingerprints + drift alerts".)

This package is the operator/eval surface that CONSUMES what the single
egress already records. The model-provider proxy
(:mod:`foundry.proxy.model_provider_proxy`) is the ONLY component that talks to
a provider, and it already appends a per-call ``manifest`` record (node_id /
purpose / tier / requested+served model / lineage_map_revision / request_id /
usage / sampling / drift) and a ``drift_events`` list whenever the served model
departs from the pinned request. The observability plane does NOT add a second
egress and does NOT duplicate that manifest — it DRAINS it:

* :mod:`foundry.observability.telemetry` — an OTel-STYLE structured collector of
  spans/events for graph nodes, gate runs, and model calls, carrying the
  determinism fingerprints (model-id, served-model, seed/effort, request_id,
  usage, lineage_map_revision). It is in-memory/structured; a real OTel exporter
  is an ops swap that drains it (it makes no network calls).
* :mod:`foundry.observability.drift_alerts` — drift detection over the telemetry.
  The authoritative pinned-ID drift source is the proxy's ``drift_events``
  (served_model != requested -> calibration-baseline-invalidation); threshold
  alerts (cost / latency / usage drift) are computed over the collected spans.
  Alerts are fail-loud: they can be raised, never silently swallowed.
"""

from __future__ import annotations

from foundry.observability.drift_alerts import (
    Alert,
    AlertSeverity,
    CostDriftAlert,
    DriftAlertError,
    LatencyDriftAlert,
    PinnedIdDriftAlert,
    ThresholdPolicy,
    UsageDriftAlert,
    detect_drift,
    pinned_id_drift_alerts,
    raise_if,
    raise_on_drift,
    threshold_alerts,
)
from foundry.observability.telemetry import (
    RunTelemetry,
    Span,
    SpanKind,
)

__all__ = [
    "Alert",
    "AlertSeverity",
    "CostDriftAlert",
    "DriftAlertError",
    "LatencyDriftAlert",
    "PinnedIdDriftAlert",
    "RunTelemetry",
    "Span",
    "SpanKind",
    "ThresholdPolicy",
    "UsageDriftAlert",
    "detect_drift",
    "pinned_id_drift_alerts",
    "raise_if",
    "raise_on_drift",
    "threshold_alerts",
]
