"""Sampling & determinism policy + the run manifest. (architecture.md §8)

Policy: temperature 0 for gates, routing, and all judge/review nodes; higher
only where exploration helps (architecture ideation).

Tiers are capability levels within the primary family (arch §8):
T1 = `claude-opus-4-8`, T2 = `claude-sonnet-4-6`, T3 = `claude-haiku-4-5`.

**Real provider constraint:** `claude-opus-4-8` (T1) does NOT accept the
`temperature` / `top_p` / `top_k` sampling parameters — sending any of them
returns a 400. So a "temperature 0" determinism intent on T1 cannot be
expressed literally; the policy instead records
``{"output_config": {"effort": "high"}}`` (the supported depth control) plus
an explicit substitution note, so the manifest honestly captures what was
actually sent. For ideation on T1, exploration is expressed via adaptive
thinking (``{"thinking": {"type": "adaptive"}}``) for the same reason.

`RunManifest` is the append-only per-node record {node_id, model_id,
served_model, fingerprint/request_id, sampling, seed}. A served model that
differs from the pinned ID raises a **pinned-ID-drift** flag — it invalidates
prior calibration/eval baselines. `digest()` is the canonical content digest
of the manifest for the WORM record. Audit is an immutable record of actual
decisions and inputs; replay reproduces internal control state, not bitwise
LLM outputs.
"""

from __future__ import annotations

from typing import Any

from foundry.canonical import content_digest

TIER_MODELS: dict[str, str] = {
    "T1": "claude-opus-4-8",
    "T2": "claude-sonnet-4-6",
    "T3": "claude-haiku-4-5",
}

# Purposes that must run deterministically (temperature 0 / max determinism
# available on the tier). Everything judge-like is fail-closed deterministic.
DETERMINISTIC_PURPOSES = frozenset({"gate", "routing", "judge", "review", "qa"})
EXPLORATORY_PURPOSES = frozenset({"ideation"})

_T1_SUBSTITUTION_NOTE = (
    "claude-opus-4-8 rejects temperature/top_p/top_k (provider 400); "
    "determinism intent recorded via output_config.effort instead of temperature"
)
_T1_IDEATION_NOTE = (
    "claude-opus-4-8 rejects temperature/top_p/top_k (provider 400); "
    "exploration intent recorded via adaptive thinking instead of temperature"
)


class SamplingPolicy:
    """Maps (purpose, tier) -> the sampling parameters actually sent."""

    @staticmethod
    def for_node(purpose: str, tier: str) -> dict[str, Any]:
        if tier not in TIER_MODELS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIER_MODELS)}")
        model = TIER_MODELS[tier]

        if purpose in DETERMINISTIC_PURPOSES:
            if tier == "T1":
                # No temperature key at all — the provider rejects it.
                return {
                    "model": model,
                    "output_config": {"effort": "high"},
                    "note": _T1_SUBSTITUTION_NOTE,
                }
            return {"model": model, "temperature": 0.0}

        if purpose in EXPLORATORY_PURPOSES:
            if tier == "T1":
                return {
                    "model": model,
                    "thinking": {"type": "adaptive"},
                    "note": _T1_IDEATION_NOTE,
                }
            return {"model": model, "temperature": 1.0}

        # Fail closed on classification: an unknown purpose must be named
        # here, never silently defaulted to exploratory sampling.
        raise ValueError(
            f"unknown purpose {purpose!r}; deterministic={sorted(DETERMINISTIC_PURPOSES)} "
            f"exploratory={sorted(EXPLORATORY_PURPOSES)}"
        )


class RunManifest:
    """Append-only per-node sampling/serving record with drift detection."""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._drift_alerts: list[dict[str, Any]] = []

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._entries)

    @property
    def drift_alerts(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._drift_alerts)

    def record(
        self,
        *,
        node_id: str,
        model_id: str,
        served_model: str,
        fingerprint: str,
        sampling: dict[str, Any],
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Append one node's record. `fingerprint` is the provider's
        model-version/system-fingerprint or request id — the drift signal.

        Append-only: a re-executed node appends a new record; nothing is
        ever edited in place.
        """
        entry = {
            "node_id": node_id,
            "model_id": model_id,  # the PINNED id
            "served_model": served_model,  # what the provider actually served
            "fingerprint": fingerprint,
            "sampling": sampling,
            "seed": seed,
        }
        self._entries.append(entry)
        if served_model != model_id:
            self._flag_drift(node_id, pinned=model_id, served=served_model)
        return entry

    def drift_check(self, node_id: str, served_model: str) -> dict[str, Any] | None:
        """Compare `served_model` against the node's pinned model id.

        Returns (and records) a pinned-ID-drift alert on mismatch; `None`
        when the serving matches the pin. Raises `KeyError` for an unknown
        node — checking drift against nothing is itself a bug.
        """
        for entry in reversed(self._entries):
            if entry["node_id"] == node_id:
                pinned = entry["model_id"]
                if served_model != pinned:
                    return self._flag_drift(node_id, pinned=pinned, served=served_model)
                return None
        raise KeyError(f"no manifest entry for node {node_id!r}")

    def _flag_drift(self, node_id: str, *, pinned: str, served: str) -> dict[str, Any]:
        alert = {
            "kind": "pinned-id-drift",
            "node_id": node_id,
            "pinned": pinned,
            "served": served,
            "detail": (
                f"node {node_id!r} pinned {pinned!r} but was served {served!r}; "
                "prior calibration/eval baselines are invalidated (arch §8)"
            ),
        }
        self._drift_alerts.append(alert)
        return alert

    def digest(self) -> str:
        """Canonical digest of the manifest for the WORM record."""
        return content_digest({"entries": self._entries, "drift_alerts": self._drift_alerts})
