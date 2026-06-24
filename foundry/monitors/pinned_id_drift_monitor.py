"""Pinned-ID drift monitor — calibration-invalidation trip. (architecture.md §8)

The proxy (single egress) pins a model id per request and OBSERVES the served
model id on the response; when `served_model` departs from the pinned
`requested_model` it records a **drift event** (`ModelProviderProxy.drift_events`).
A pinned-ID drift invalidates the calibration / eval baselines those judgments
were measured against (arch §8), so it is a **deterministic trip** — the served
identity is a trusted-infra observation (the proxy, never agent self-report), so
nothing here is prompt-injectable (gov §3).

There is intentionally no severity knob, allowlist, or override: any
proxy-observed drift event trips. The Guardian maps PINNED_ID_DRIFT to ESCALATE
(HITL must re-validate baselines before trusting downstream judgments).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from foundry.monitors import Trip, TripKind


class PinnedIdDriftMonitor:
    """Deterministic monitor over proxy-observed pinned-ID drift events.

    Input is the proxy's `drift_events` (or manifest records carrying a truthy
    ``drift`` flag) — each a structured mapping with at least
    ``requested_model`` and ``served_model``. A match always trips; the trip
    names every drifted ``requested->served`` pair.
    """

    def inspect(self, drift_events: Iterable[Mapping[str, object]]) -> Trip | None:
        """Return the deterministic trip iff ANY drift event is present.

        Accepts either the proxy's `drift_events` (already-filtered) or raw
        manifest records — a record is a drift iff it has no ``drift`` key
        (already pre-filtered) or ``drift`` is truthy.
        """
        pairs: list[str] = []
        for event in drift_events:
            if "drift" in event and not event["drift"]:
                continue
            requested = str(event.get("requested_model", "?"))
            served = str(event.get("served_model", "?"))
            pairs.append(f"{requested}->{served}")
        if not pairs:
            return None
        unique = sorted(set(pairs))
        return Trip(
            monitor="pinned_id_drift_monitor",
            kind=TripKind.PINNED_ID_DRIFT,
            rule="served-model-drift",
            reason=(
                "pinned model id drifted (served != requested) for "
                f"{len(unique)} pin(s): {', '.join(unique)} — calibration "
                "baselines invalidated, deterministic HITL trip (arch §8)"
            ),
            matched=tuple(unique),
        )
