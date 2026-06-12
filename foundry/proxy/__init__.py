"""Mediating model-provider proxy: single egress, single credential holder,
lineage observation, trusted verdict extraction, judgment-binding stamping.
(architecture.md §2b, implementation-plan.md §3.3)
"""

from foundry.proxy.lineage import (
    DEFAULT_LINEAGE_ENTRIES,
    LineageMap,
    UnknownModelLineageError,
)
from foundry.proxy.model_provider_proxy import (
    TIER_MODELS,
    VERDICT_SCHEMA,
    AnthropicTransport,
    CompletionResult,
    FamilyOverlapError,
    JudgmentResult,
    ModelProviderProxy,
    ProviderResponse,
    ProviderTransport,
    ProxyUnavailable,
    VerdictChannelError,
)

__all__ = [
    "DEFAULT_LINEAGE_ENTRIES",
    "TIER_MODELS",
    "VERDICT_SCHEMA",
    "AnthropicTransport",
    "CompletionResult",
    "FamilyOverlapError",
    "JudgmentResult",
    "LineageMap",
    "ModelProviderProxy",
    "ProviderResponse",
    "ProviderTransport",
    "ProxyUnavailable",
    "UnknownModelLineageError",
    "VerdictChannelError",
]
