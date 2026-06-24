"""Observability plane suite (arch §8; governance.md §2; operations.md).

Drives the REAL MockTransport-backed proxy (foundry/proxy/test_doubles.py) so
the whole suite runs KEYLESS and makes NO network call — the telemetry collector
consumes the proxy's manifest and the drift alerts reuse the proxy's
drift_events (no second egress).

Covers:
* telemetry drains the proxy manifest into model-call spans carrying the
  determinism fingerprints (model-id, served-model, request_id, usage, sampling,
  lineage_map_revision);
* node + gate spans are collected alongside model-call spans;
* a pinned-ID drift (served != requested, forced via MockTransport's served_model
  override) raises a fail-loud DriftAlertError sourced from proxy.drift_events;
* alerts are not swallowed (raise_on_drift / raise_if raise);
* threshold cost/usage alerts over the telemetry;
* determinism: identical manifests -> identical span digests / telemetry digest.
"""

from __future__ import annotations

import pytest

from foundry.observability import (
    AlertSeverity,
    CostDriftAlert,
    DriftAlertError,
    PinnedIdDriftAlert,
    RunTelemetry,
    SpanKind,
    ThresholdPolicy,
    UsageDriftAlert,
    detect_drift,
    pinned_id_drift_alerts,
    raise_if,
    raise_on_drift,
    threshold_alerts,
)
from foundry.proxy import LineageMap, ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport


def _proxy(transport: MockTransport) -> ModelProviderProxy:
    """A keyless proxy over a MockTransport (no signer needed for complete())."""
    return ModelProviderProxy(transport, LineageMap())


def _drive_ideation(
    proxy: ModelProviderProxy, node_id: str = "architect", tier: str = "T2"
) -> None:
    proxy.complete(
        node_id,
        tier,
        system_sop="design the thing",
        messages=[{"role": "user", "content": "go"}],
        purpose="ideation",
    )


# --------------------------------------------------------------------------- #
# telemetry: drain the proxy manifest into fingerprint-carrying spans
# --------------------------------------------------------------------------- #


def test_ingest_proxy_drains_manifest_into_model_call_spans() -> None:
    transport = MockTransport(default_text="ok")
    proxy = _proxy(transport)
    _drive_ideation(proxy, node_id="architect", tier="T2")
    _drive_ideation(proxy, node_id="planner", tier="T3")

    tel = RunTelemetry(run_id="run-1")
    emitted = tel.ingest_proxy(proxy)

    assert len(emitted) == 2
    spans = tel.model_call_spans
    assert len(spans) == 2
    # No second egress: span count == manifest record count.
    assert len(spans) == len(proxy.manifest)

    first = spans[0]
    assert first.kind is SpanKind.MODEL_CALL
    # Determinism fingerprints lifted from the manifest (arch §8).
    assert first.attributes["node_id"] == "architect"
    assert first.attributes["requested_model"] == "claude-sonnet-4-6"
    assert first.attributes["served_model"] == "claude-sonnet-4-6"
    assert first.attributes["lineage_map_revision"] == proxy.lineage_map_revision
    assert first.attributes["request_id"].startswith("req_mock_")
    assert "usage" in first.attributes
    assert "sampling" in first.attributes
    assert first.drift is False


def test_ingest_proxy_is_idempotent_over_growing_manifest() -> None:
    transport = MockTransport(default_text="ok")
    proxy = _proxy(transport)
    tel = RunTelemetry()

    _drive_ideation(proxy)
    first = tel.ingest_proxy(proxy)
    assert len(first) == 1

    # Re-draining without a new call emits nothing (no duplicate spans).
    assert tel.ingest_proxy(proxy) == ()
    assert len(tel.model_call_spans) == 1

    # A new call adds exactly one new span on the next drain.
    _drive_ideation(proxy, node_id="planner")
    second = tel.ingest_proxy(proxy)
    assert len(second) == 1
    assert len(tel.model_call_spans) == 2


def test_node_and_gate_spans_collected() -> None:
    tel = RunTelemetry()
    n = tel.record_node_span("architect", superstep=3)
    g = tel.record_gate_span("spec", status="PASS", run_against="sha-abc")

    assert n.kind is SpanKind.NODE
    assert n.attributes["superstep"] == 3
    assert g.kind is SpanKind.GATE
    assert g.attributes["status"] == "PASS"
    assert len(tel.spans_of(SpanKind.NODE)) == 1
    assert len(tel.spans_of(SpanKind.GATE)) == 1


def test_export_is_network_free_structured_dicts() -> None:
    transport = MockTransport(default_text="ok")
    proxy = _proxy(transport)
    _drive_ideation(proxy)
    tel = RunTelemetry()
    tel.ingest_proxy(proxy)

    exported = tel.export()
    assert len(exported) == 1
    rec = exported[0]
    assert rec["kind"] == "model_call"
    assert "digest" in rec and "attributes" in rec


# --------------------------------------------------------------------------- #
# pinned-ID drift: reuse proxy.drift_events, fail-loud
# --------------------------------------------------------------------------- #


def test_pinned_id_drift_forced_keyless_raises() -> None:
    # Force served != requested via MockTransport's served_model override.
    transport = MockTransport(default_text="ok", served_model="claude-sonnet-4-6")
    proxy = _proxy(transport)
    _drive_ideation(proxy, node_id="reviewer", tier="T1")  # pinned T1 == opus

    # The proxy is the authoritative drift source.
    assert len(proxy.drift_events) == 1

    alerts = pinned_id_drift_alerts(proxy)
    assert len(alerts) == 1
    alert = alerts[0]
    assert isinstance(alert, PinnedIdDriftAlert)
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.requested_model == "claude-opus-4-8"
    assert alert.served_model == "claude-sonnet-4-6"
    assert alert.node_id == "reviewer"
    assert "calibration" in alert.detail

    # Fail-loud: the alert is raised, never swallowed.
    with pytest.raises(DriftAlertError) as exc:
        raise_on_drift(proxy=proxy)
    assert exc.value.alerts == tuple(alerts)


def test_no_drift_does_not_raise() -> None:
    transport = MockTransport(default_text="ok")  # served mirrors request
    proxy = _proxy(transport)
    _drive_ideation(proxy, node_id="reviewer", tier="T1")

    assert proxy.drift_events == ()
    assert pinned_id_drift_alerts(proxy) == []
    raise_on_drift(proxy=proxy)  # no raise


def test_drift_reuses_proxy_events_not_re_derived() -> None:
    transport = MockTransport(default_text="ok", served_model="claude-haiku-4-5")
    proxy = _proxy(transport)
    _drive_ideation(proxy, node_id="n1", tier="T1")

    alerts = pinned_id_drift_alerts(proxy)
    # The alert context is the proxy's drift_event verbatim (no re-derivation).
    assert alerts[0].context == dict(proxy.drift_events[0])


# --------------------------------------------------------------------------- #
# threshold alerts over the telemetry
# --------------------------------------------------------------------------- #


def test_usage_and_cost_threshold_alerts() -> None:
    transport = MockTransport(default_text="ok")
    proxy = _proxy(transport)
    _drive_ideation(proxy)
    tel = RunTelemetry()
    tel.ingest_proxy(proxy)

    # DEFAULT_USAGE = 1024 in / 96 out -> trip both a per-call and a total cap.
    policy = ThresholdPolicy(max_call_total_tokens=100, max_total_input_tokens=10)
    alerts = threshold_alerts(tel, policy)
    kinds = {type(a) for a in alerts}
    assert UsageDriftAlert in kinds
    assert CostDriftAlert in kinds

    with pytest.raises(DriftAlertError):
        raise_if(alerts)


def test_threshold_under_limit_no_alert() -> None:
    transport = MockTransport(default_text="ok")
    proxy = _proxy(transport)
    _drive_ideation(proxy)
    tel = RunTelemetry()
    tel.ingest_proxy(proxy)

    policy = ThresholdPolicy(max_call_total_tokens=100_000, max_total_input_tokens=100_000)
    assert threshold_alerts(tel, policy) == []


def test_node_latency_threshold_alert() -> None:
    tel = RunTelemetry()
    tel.record_node_span("slow_node", latency_ms=5000)
    tel.record_node_span("fast_node", latency_ms=10)

    alerts = threshold_alerts(tel, ThresholdPolicy(max_node_latency_ms=1000))
    assert len(alerts) == 1
    assert "slow_node" in alerts[0].detail


def test_detect_drift_combines_pinned_and_threshold() -> None:
    transport = MockTransport(default_text="ok", served_model="claude-sonnet-4-6")
    proxy = _proxy(transport)
    _drive_ideation(proxy, node_id="reviewer", tier="T1")
    tel = RunTelemetry()
    tel.ingest_proxy(proxy)

    alerts = detect_drift(
        proxy=proxy, telemetry=tel, policy=ThresholdPolicy(max_call_total_tokens=1)
    )
    assert any(isinstance(a, PinnedIdDriftAlert) for a in alerts)
    assert any(isinstance(a, UsageDriftAlert) for a in alerts)


# --------------------------------------------------------------------------- #
# determinism / content-addressability
# --------------------------------------------------------------------------- #


def test_span_digest_is_deterministic_and_content_addressed() -> None:
    def build() -> RunTelemetry:
        transport = MockTransport(default_text="ok")
        proxy = _proxy(transport)
        _drive_ideation(proxy, node_id="architect", tier="T2")
        tel = RunTelemetry(run_id="run-fixed")
        tel.ingest_proxy(proxy)
        return tel

    a = build()
    b = build()
    assert [s.digest for s in a.spans] == [s.digest for s in b.spans]
    assert a.digest() == b.digest()


def test_distinct_records_distinct_digests() -> None:
    tel = RunTelemetry()
    s1 = tel.record_node_span("a")
    s2 = tel.record_node_span("b")
    assert s1.digest != s2.digest
