"""Mediating model-provider proxy: single egress, single credential holder,
lineage observation, trusted verdict extraction, judgment-binding stamping.
(architecture.md §2b, implementation-plan.md §3.3)
"""

from foundry.proxy.cli_transports import (
    ClaudeCliTransport,
    CodexCliTransport,
    TransportError,
    build_backend,
    claude_cli_backend,
    codex_backend,
)
from foundry.proxy.lineage import (
    DEFAULT_LINEAGE_ENTRIES,
    OPENAI_LINEAGE_ENTRIES,
    LineageMap,
    UnknownModelLineageError,
)
from foundry.proxy.model_provider_proxy import (
    TIER_MODELS,
    VERDICT_SCHEMA,
    AnthropicTransport,
    CompletionResult,
    DriftMode,
    DriftPolicy,
    JudgmentResult,
    ModelProviderProxy,
    PinnedIdDriftError,
    ProviderResponse,
    ProviderTransport,
    ProxyUnavailable,
    SamplingPolicy,
    VerdictChannelError,
    default_drift_policy,
    default_sampling_policy,
)

__all__ = [
    "DEFAULT_LINEAGE_ENTRIES",
    "OPENAI_LINEAGE_ENTRIES",
    "TIER_MODELS",
    "VERDICT_SCHEMA",
    "AnthropicTransport",
    "ClaudeCliTransport",
    "CodexCliTransport",
    "CompletionResult",
    "DriftMode",
    "DriftPolicy",
    "JudgmentResult",
    "LineageMap",
    "ModelProviderProxy",
    "PinnedIdDriftError",
    "ProviderResponse",
    "ProviderTransport",
    "ProxyUnavailable",
    "SamplingPolicy",
    "TransportError",
    "UnknownModelLineageError",
    "VerdictChannelError",
    "build_backend",
    "claude_cli_backend",
    "codex_backend",
    "default_drift_policy",
    "default_sampling_policy",
]
