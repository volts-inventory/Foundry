"""Factory self-eval / A-B decision-gate harness (evaluation.md).

Phase-1.5 scaffold. **Methodology dry-run on synthetic fixtures — NOT funded
real evidence.** Re-exports the central-hypothesis schema types (import-cheap:
pure pydantic models + enums, no statistics logic).
"""

from __future__ import annotations

from foundry.evaluation.cost_model import CostBreakdown, CostModel, RunSignals
from foundry.evaluation.hypothesis import (
    Arm,
    CentralHypothesis,
    DecisionClass,
    Equalizer,
    PowerRung,
    PrimaryMetric,
    Provenance,
)

__all__ = [
    "Arm",
    "CentralHypothesis",
    "CostBreakdown",
    "CostModel",
    "DecisionClass",
    "Equalizer",
    "PowerRung",
    "PrimaryMetric",
    "Provenance",
    "RunSignals",
]
