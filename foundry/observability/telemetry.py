"""OTel-style structured run-telemetry collector. (governance.md §2; arch §8)

A deterministic, in-memory collector of spans/events for the three things the
operations + eval surfaces care about per run:

* **model-call spans** — one per provider call, carrying the determinism
  fingerprints (pinned model-id, served-model, seed/effort, request_id, usage,
  lineage_map_revision, drift bit). These are NOT re-observed here — they are
  DRAINED from the proxy's append-only ``manifest`` (the single egress already
  recorded them; LEARNINGS L6). :meth:`RunTelemetry.ingest_proxy` reads the
  proxy's read-only ``manifest`` view and turns each record into a span, so
  there is no second egress and no duplicated manifest.
* **node spans** — one per graph node (superstep), recorded by the orchestrator.
* **gate spans** — one per gate run (verdict outcome), recorded by the runner.

This is the structured collector an exporter drains; a real OpenTelemetry
exporter is an ops swap (it would read :meth:`RunTelemetry.export` and ship the
spans). This module makes **no network calls** and does **no I/O**.

Determinism: a span's ``digest`` is the canonical content digest of its
fingerprint payload (LEARNINGS L7: ``content_digest``, never builtin ``hash()``),
so two logically-identical model-call records produce identical span digests
regardless of dict ordering — content-addressable telemetry.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from foundry.canonical import content_digest


class SpanKind(StrEnum):
    """The three structured span kinds the observability plane collects."""

    NODE = "node"  # a graph node / superstep
    GATE = "gate"  # a gate run + verdict
    MODEL_CALL = "model_call"  # one provider call (drained from the proxy manifest)


class _ManifestSource(Protocol):
    """The read-only proxy surface telemetry consumes — NO egress methods.

    Structurally satisfied by :class:`~foundry.proxy.model_provider_proxy.ModelProviderProxy`
    (its ``manifest`` property). Telemetry depends only on this read view, never
    on ``complete``/``judge``, so it can never make a model call itself.
    """

    @property
    def manifest(self) -> tuple[dict[str, Any], ...]: ...


# The determinism-fingerprint fields lifted verbatim from a proxy manifest
# record onto a model-call span (arch §8). Kept as a constant so the span and
# the manifest never drift apart.
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "node_id",
    "purpose",
    "tier",
    "requested_model",
    "served_model",
    "served_family",
    "lineage_map_revision",
    "request_id",
    "usage",
    "sampling",
    "drift",
)


class Span:
    """One structured telemetry span: kind + name + an attributes payload.

    Immutable after construction. ``attributes`` is a plain dict of canonical
    (JSON-serializable) values so the span digest is content-addressable.
    """

    __slots__ = ("attributes", "digest", "kind", "name")

    def __init__(self, kind: SpanKind, name: str, attributes: Mapping[str, Any]) -> None:
        self.kind = kind
        self.name = name
        # Copy defensively so a later mutation of the caller's dict can't change
        # an already-recorded span (append-only telemetry).
        self.attributes: dict[str, Any] = dict(attributes)
        self.digest = content_digest(
            {"kind": str(kind), "name": name, "attributes": self.attributes}
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Span(kind={self.kind!s}, name={self.name!r}, digest={self.digest[:12]})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Span) and other.digest == self.digest

    def __hash__(self) -> int:
        return hash(self.digest)

    @property
    def served_model(self) -> str | None:
        v = self.attributes.get("served_model")
        return v if isinstance(v, str) else None

    @property
    def requested_model(self) -> str | None:
        v = self.attributes.get("requested_model")
        return v if isinstance(v, str) else None

    @property
    def drift(self) -> bool:
        return bool(self.attributes.get("drift", False))

    @property
    def usage(self) -> dict[str, int]:
        u = self.attributes.get("usage")
        return dict(u) if isinstance(u, Mapping) else {}


class RunTelemetry:
    """Append-only, in-memory collector of run spans (the exporter's drain point).

    Not thread-safe by design: one collector per run, threaded like the proxy.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id
        self._spans: list[Span] = []
        # How many proxy manifest records have already been drained — so a
        # repeated ``ingest_proxy`` over a growing append-only manifest only
        # emits spans for the NEW records (idempotent re-drain, no duplicates).
        self._manifest_drained = 0

    # ---- read-only views ----------------------------------------------------

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(self._spans)

    def spans_of(self, kind: SpanKind) -> tuple[Span, ...]:
        return tuple(s for s in self._spans if s.kind is kind)

    @property
    def model_call_spans(self) -> tuple[Span, ...]:
        return self.spans_of(SpanKind.MODEL_CALL)

    # ---- recorders ----------------------------------------------------------

    def record_node_span(
        self, node_id: str, *, superstep: int | None = None, **attributes: Any
    ) -> Span:
        """Record one graph-node (superstep) span."""
        attrs: dict[str, Any] = {"node_id": node_id, **attributes}
        if superstep is not None:
            attrs["superstep"] = superstep
        return self._append(SpanKind.NODE, f"node:{node_id}", attrs)

    def record_gate_span(
        self, gate_name: str, *, status: str, run_against: str | None = None, **attributes: Any
    ) -> Span:
        """Record one gate-run span (the verdict outcome, not the verdict itself)."""
        attrs: dict[str, Any] = {"gate_name": gate_name, "status": status, **attributes}
        if run_against is not None:
            attrs["run_against"] = run_against
        return self._append(SpanKind.GATE, f"gate:{gate_name}", attrs)

    def record_model_call(self, manifest_record: Mapping[str, Any]) -> Span:
        """Record one model-call span from a SINGLE proxy manifest record.

        The record is the proxy's per-call manifest entry (or its
        ``CompletionResult.manifest_record``). Only the determinism-fingerprint
        fields are lifted onto the span — the proxy stays the source of truth.
        """
        attrs = {k: manifest_record[k] for k in _FINGERPRINT_FIELDS if k in manifest_record}
        if "sampling_note" in manifest_record:
            attrs["sampling_note"] = manifest_record["sampling_note"]
        node = attrs.get("node_id", "?")
        return self._append(SpanKind.MODEL_CALL, f"model_call:{node}", attrs)

    def ingest_proxy(self, proxy: _ManifestSource) -> tuple[Span, ...]:
        """Drain the proxy's append-only manifest into model-call spans.

        REUSES the proxy manifest as the telemetry source (no second egress,
        LEARNINGS L6) — telemetry never calls ``complete``/``judge``. Only the
        manifest records not yet drained are turned into spans, so calling this
        repeatedly over a growing manifest is idempotent (no duplicate spans).
        Returns the spans emitted by THIS call.
        """
        manifest = proxy.manifest
        new_records = manifest[self._manifest_drained :]
        emitted = tuple(self.record_model_call(rec) for rec in new_records)
        self._manifest_drained = len(manifest)
        return emitted

    # ---- exporter drain point ----------------------------------------------

    def export(self) -> tuple[dict[str, Any], ...]:
        """Structured span dicts a real OTel exporter would ship (no network)."""
        return tuple(
            {"kind": str(s.kind), "name": s.name, "digest": s.digest, "attributes": s.attributes}
            for s in self._spans
        )

    def digest(self) -> str:
        """Canonical, content-addressable digest of the whole collected run."""
        return content_digest(
            {"run_id": self.run_id, "spans": [s.digest for s in self._spans]}
        )

    # ---- internals ----------------------------------------------------------

    def _append(self, kind: SpanKind, name: str, attributes: Mapping[str, Any]) -> Span:
        span = Span(kind, name, attributes)
        self._spans.append(span)
        return span
