"""Context retrieval and model determinism. (architecture.md §8)

- `retriever` — symbol index + one-hop call/def-graph neighbors (impact
  analysis); an empty retrieval raises the typed `NeedContext` escape.
- `determinism` — sampling policy per (purpose, tier).
"""

from foundry.context.determinism import TIER_MODELS, SamplingPolicy
from foundry.context.pinned_constraints import NeedContext
from foundry.context.retriever import Span, SymbolIndex

__all__ = [
    "TIER_MODELS",
    "NeedContext",
    "SamplingPolicy",
    "Span",
    "SymbolIndex",
]
