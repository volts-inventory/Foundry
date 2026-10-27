"""Sampling & determinism policy. (architecture.md §8)

Policy: temperature 0 for gates, routing, and all judge/review nodes; higher
only where exploration helps (architecture ideation).

Tiers are capability levels within the primary family (arch §8):
T1 = `claude-opus-4-8`, T2 = `claude-sonnet-4-6`, T3 = `claude-haiku-4-5`.

**Real provider constraint:** `claude-opus-4-8` (T1) does NOT accept the
`temperature` / `top_p` / `top_k` sampling parameters — sending any of them
returns a 400. So a "temperature 0" determinism intent on T1 cannot be
expressed literally; the policy instead records
``{"output_config": {"effort": "high"}}`` (the supported depth control) plus
an explicit substitution note, so the record honestly captures what was
actually sent. For ideation on T1, exploration is expressed via adaptive
thinking (``{"thinking": {"type": "adaptive"}}``) for the same reason.
"""

from __future__ import annotations

from typing import Any

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
