"""Context assembly, retrieval, and model determinism. (architecture.md §8)

Three seams, each a measured contract rather than vibes:

- `pinned_constraints` — verbatim injection of pinned constraints: globals
  always injected, locals relevance-scoped under a per-node token budget,
  cardinality monitored (never silently truncated), misclassified locals
  escalated to HITL while fail-safe injected as global (AI-R10-F1).
- `retriever` — symbol index + one-hop call/def-graph neighbors (impact
  analysis), with a `recall_eval` hook so the retriever is gated by a
  measured recall floor (evaluation.md §5).
- `determinism` — sampling policy per (purpose, tier) and the append-only
  `RunManifest` with pinned-ID-drift detection and a canonical digest for
  the WORM record.
"""

from foundry.context.determinism import TIER_MODELS, RunManifest, SamplingPolicy
from foundry.context.pinned_constraints import (
    ConstraintInjector,
    Escalation,
    InjectionResult,
    NeedContext,
    Scorer,
    TokenOverlapScorer,
    estimate_tokens,
    need_context,
)
from foundry.context.retriever import Span, SymbolIndex

__all__ = [
    "TIER_MODELS",
    "ConstraintInjector",
    "Escalation",
    "InjectionResult",
    "NeedContext",
    "RunManifest",
    "SamplingPolicy",
    "Scorer",
    "Span",
    "SymbolIndex",
    "TokenOverlapScorer",
    "estimate_tokens",
    "need_context",
]
