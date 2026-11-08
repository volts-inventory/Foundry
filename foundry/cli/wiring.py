"""Adapter and proxy wiring: the coding substrate, the zone-B signer and gate
verifier, the drift policy, tier models, and the single-egress proxies."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from foundry import progress
from foundry.cli import defaults, doubles
from foundry.cli.defaults import (
    _DEFAULT_ADAPTER,
    _MOCK_BACKEND,
    ADAPTERS,
    BACKENDS,
)
from foundry.execution.adapter_contract import current_packet_tier
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, ZoneBSigner
from foundry.gates.signing import signing_schemes_from_env
from foundry.modes import Level, build_level_proxy, escalation_tier, local_tiers
from foundry.proxy import ModelProviderProxy, build_backend
from foundry.proxy.local_transport import LocalOpenAITransport
from foundry.proxy.model_provider_proxy import DriftMode, DriftPolicy, ProviderTransport
from foundry.proxy.test_doubles import MockTransport

if TYPE_CHECKING:
    from foundry.proxy.cli_transports import BackendName


# Zone-B trust-root key material: a locally-held HMAC secret (the operator is the
# trust boundary on a single-user LAN; see foundry.gates.signing).
_KEY = b"dev-zone-b-cli-key-0123456789-abcdef"


_KEY_ID = "dev-key-cli"


# Minimal system prompt for the raw-react substrate (the full task prompt — pinned
# constraints, task, spans, "emit a unified diff" — is built by the adapter itself).
_RAW_REACT_SOP = (
    "You are a senior software engineer. Implement the requested change exactly, "
    "returning your work as a single fenced ```diff block. Touch nothing outside it."
)


# The proxy-agent's system prompt: its user turn demands one JSON action per reply,
# so the raw-react "single diff block" instruction would contradict it.
_PROXY_AGENT_SOP = (
    "You are a senior software engineer working step by step in a sandbox. Each reply "
    "is exactly one JSON action, as the user message specifies. Touch only what the "
    "task needs."
)


_PROXY_AGENT_TOOLS_SOP = (
    "You are a senior software engineer working step by step in a sandbox. Work by "
    "calling the tools the user message describes. Touch only what the task needs."
)


def make_coding_adapter(
    name: str | None,
    *,
    proxy: ModelProviderProxy,
    thread_id: str = "cli-run",
    activity_cb: Callable[[str], None] | None = None,
    tier_models: Mapping[str, str] | None = None,
    native_tools: bool | None = None,
) -> Any:
    """Construct the selected coding substrate (``ADAPTERS``). ``None`` ⇒ the
    deterministic ``stub`` (back-compat). ``raw-react`` drives the chosen model
    backend through the single-egress proxy (real code over any backend — `claude -p`
    completions under ``claude-cli``, the Anthropic API under ``anthropic``).

    Each packet's ``task["tier"]`` (a ticket's difficulty, mapped) picks the model:
    the proxy closures pass it as the call's tier, and ``claude-code`` resolves it
    through ``tier_models`` (the proxy's tier -> model map) into ``--model``. A
    packet with no tier runs on T1 / the CLI's default model."""
    name = (name or _DEFAULT_ADAPTER).strip().lower()
    if name == "stub":
        return StubAdapter()
    if name == "raw-react":
        from foundry.execution.adapters.raw_react import RawReactAdapter

        def _complete(prompt: str) -> str:
            return (
                proxy.complete(
                    "junior_engineer",
                    current_packet_tier() or "T1",
                    _RAW_REACT_SOP,
                    [{"role": "user", "content": prompt}],
                    purpose="ideation",
                ).text
                or ""
            )

        return RawReactAdapter(complete_fn=_complete, run_id=thread_id)
    if name == "claude-code":
        from foundry.execution.adapters.claude_code import ClaudeCodeAdapter

        # Agentic substrate: the CLI holds its own auth (via the sandbox's typed
        # model-auth grant), so no proxy closure is threaded here — the proxy
        # stays the egress for the AGENT layers; the coding substrate's lineage
        # self-report is carried but IGNORED (arch §2b, documented trade).
        # `activity_cb` (optional) streams what the agent is DOING inside one step —
        # a step is a single blocking CLI call, so without it a multi-minute agentic
        # pass reports nothing until it returns.
        return ClaudeCodeAdapter(run_id=thread_id, on_activity=activity_cb, tier_models=tier_models)
    if name == "proxy-agent":
        from foundry.execution.adapters.proxy_agent import ProxyAgentAdapter

        # The trust-preserving agentic loop (ROADMAP 1c): multi-turn, but every
        # call rides the mediating proxy — sole credential holder, any backend.
        def _agent_complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
            result = proxy.complete(
                "junior_engineer",
                current_packet_tier() or "T1",
                _PROXY_AGENT_SOP,
                list(messages),
                purpose="ideation",
                schema=schema,
            )
            return result.text or "", result.parsed

        def _agent_tools(messages: Any, tools: Any) -> tuple[str, Any]:
            result = proxy.complete(
                "junior_engineer",
                current_packet_tier() or "T1",
                _PROXY_AGENT_TOOLS_SOP,
                list(messages),
                purpose="ideation",
                tools=tools,
            )
            return result.text or "", result.tool_calls

        # Opt-in (Phase 5): the actions as native tool calls; a backend without a
        # tool channel (claude-cli, codex) falls back to JSON actions on its own.
        native = native_tools
        if native is None:
            native = progress.env_flag("FOUNDRY_NATIVE_TOOLS")
        return ProxyAgentAdapter(
            complete_fn=_agent_complete,
            run_id=thread_id,
            tool_complete_fn=_agent_tools if native else None,
            activity_cb=activity_cb,
        )
    raise ValueError(f"unknown adapter {name!r}; choose one of {', '.join(ADAPTERS)}")


def _resolve_coding_adapter(
    adapter: str | Any | None,
    *,
    proxy: ModelProviderProxy,
    thread_id: str,
    activity_cb: Callable[[str], None] | None = None,
    tier_models: Mapping[str, str] | None = None,
    native_tools: bool | None = None,
) -> Any:
    """A WorkerAdapter instance passes through (tests inject one); a name (or None)
    is constructed via :func:`make_coding_adapter`."""
    if adapter is None or isinstance(adapter, str):
        return make_coding_adapter(
            adapter,
            proxy=proxy,
            thread_id=thread_id,
            activity_cb=activity_cb,
            tier_models=tier_models,
            native_tools=native_tools,
        )
    return adapter


def _zone_b_signer() -> ZoneBSigner:
    """The zone-B signer — the locally-held HMAC root."""
    signer_scheme, _ = signing_schemes_from_env(dev_key=_KEY, dev_key_id=_KEY_ID)
    return ZoneBSigner.from_scheme(signer_scheme)


def _gate_verifier(records: Any) -> DevGateWriteVerifier:
    """The gate-write verifier — admits only verdicts carrying a valid zone-B tag,
    so an untrusted agent cannot forge one."""
    _, verifier_scheme = signing_schemes_from_env(dev_key=_KEY, dev_key_id=_KEY_ID)
    return DevGateWriteVerifier.from_scheme(verifier_scheme, records)


def _drift_policy() -> DriftPolicy:
    """Production default: a served-model drift from the pinned id on a gate/judgment
    call FAILS CLOSED (pause-and-escalate) rather than silently trusting the drifted
    model. Opt back to record-only with ``FOUNDRY_DRIFT_POLICY=advisory``."""
    if os.environ.get("FOUNDRY_DRIFT_POLICY", "fail-closed").strip().lower() == "advisory":
        return DriftPolicy(mode=DriftMode.ADVISORY)
    return DriftPolicy(mode=DriftMode.FAIL_CLOSED)


# Per-tier model pinning (12-factor, like FOUNDRY_MODEL_BACKEND): set the EXACT
# model id your backend serves for each reasoning tier. The proxy pins these ids
# and the fail-closed drift policy rejects a gate/judge call whose SERVED model
# doesn't resolve to the pinned one — so if your `claude -p` / API serves a model
# the default map doesn't name, pin it here rather than editing code. A partial
# set overlays the backend's curated default (e.g. set only FOUNDRY_MODEL_T1).
_TIER_ENV: dict[str, str] = {
    "T1": "FOUNDRY_MODEL_T1",
    "T2": "FOUNDRY_MODEL_T2",
    "T3": "FOUNDRY_MODEL_T3",
}


def _default_tier_models(backend: str) -> dict[str, str]:
    """The curated default tier→model map the backend ships with (so an env
    override can overlay a partial set without dropping the other tiers)."""
    from foundry.proxy.cli_transports import (
        CODEX_TIER_MODELS,
        OPENAI_TIER_MODELS,
    )
    from foundry.proxy.model_provider_proxy import TIER_MODELS

    if backend == "openai":
        return dict(OPENAI_TIER_MODELS)
    if backend == "codex":
        return dict(CODEX_TIER_MODELS)
    return dict(TIER_MODELS)  # anthropic / claude-cli share the Anthropic lineage


def _tier_models(backend: str) -> dict[str, str]:
    """The effective tier→model map for ``backend``: the curated default with any
    ``FOUNDRY_MODEL_T{1,2,3}`` env overrides overlaid. Mock has no real models."""
    base = _default_tier_models(backend)
    for tier, var in _TIER_ENV.items():
        pinned = os.environ.get(var, "").strip()
        if pinned:
            base[tier] = pinned
    return base


def _build_proxy(backend: str, *, local_thinking: str | None = None) -> ModelProviderProxy:
    """Build the single-egress proxy for the chosen backend (the dev zone-B signer
    is injected so the judgment path can stamp verdicts regardless of backend).

    ``"mock"`` is the deterministic scripted transport (the default, keyless); any
    other name is delegated to ``build_backend`` (a real egress). An unknown name
    fails closed with a clear ``ValueError``. Drift from the pinned model on a
    gate/judgment call fails closed by default (see :func:`_drift_policy`)."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}")
    signer = _zone_b_signer()
    drift = _drift_policy()
    if backend == _MOCK_BACKEND:
        tx = MockTransport.scripted(dict(doubles._SCRIPTS), default_verdict="approve")
        return ModelProviderProxy(
            tx, signer=signer, drift_policy=drift, local_thinking=local_thinking
        )
    # Past the guard above, `backend` is one of the real BackendName literals.
    # Pin the per-tier models (curated default + FOUNDRY_MODEL_T{1,2,3} overrides)
    # so an operator can name the EXACT ids their egress serves without code edits.
    return build_backend(
        cast("BackendName", backend),
        signer=signer,
        drift_policy=drift,
        tier_models=_tier_models(backend),
        local_thinking=local_thinking,
    )


def _cloud_transport(backend: str) -> ProviderTransport:
    """The egress of one cloud level ("mock" is the scripted keyless double)."""
    if backend == _MOCK_BACKEND:
        return MockTransport.scripted(dict(doubles._SCRIPTS), default_verdict="approve")
    from foundry.proxy.cli_transports import transport_for

    return transport_for(cast("BackendName", backend))


def _local_transport(base_url: str) -> ProviderTransport:
    """The egress of the local levels: an OpenAI-compatible server, no credential."""
    return LocalOpenAITransport(base_url)


def _build_level_proxy(
    levels: Mapping[str, Level], *, local_thinking: str | None = None
) -> ModelProviderProxy:
    """One proxy routing each tier to its level's transport (model modes)."""
    return build_level_proxy(
        levels,
        cloud_transport=_cloud_transport,
        local_transport=_local_transport,
        signer=_zone_b_signer(),
        drift_policy=_drift_policy(),
        local_thinking=local_thinking,
    )


def _level_adapter(
    adapter: str | Any | None,
    *,
    backend: str,
    levels: Mapping[str, Level],
    proxy: ModelProviderProxy,
    thread_id: str,
    activity_cb: Callable[[str], None] | None,
    native_tools: bool | None = None,
) -> Any:
    """The coding substrate under a level table: a ticket on a cloud level runs the
    run's cloud adapter (claude-code under claude-cli) with the cloud levels'
    models; a ticket on a local level runs proxy-agent through the local server,
    one session at a time. In a mixed (hybrid) table a local ticket that can't
    finish re-runs once on the strongest cloud level."""
    from foundry.execution.adapters.tiered import TieredAdapter

    local = make_coding_adapter(
        "proxy-agent",
        proxy=proxy,
        thread_id=thread_id,
        activity_cb=activity_cb,
        native_tools=native_tools,
    )
    cloud_models = {t: lvl.model for t, lvl in levels.items() if not lvl.local}
    cloud = local
    if cloud_models:
        cloud = _resolve_coding_adapter(
            adapter if adapter is not None else defaults._default_adapter_for(backend),
            proxy=proxy,
            thread_id=thread_id,
            activity_cb=activity_cb,
            tier_models=cloud_models,
            native_tools=native_tools,
        )
    return TieredAdapter(
        cloud=cloud,
        local=local,
        local_tiers=local_tiers(levels),
        local_slot=proxy.local_slot,
        escalation_tier=escalation_tier(levels),
    )
