"""Foundry CLI — run a one-line request end-to-end through the factory graph.

(SPEC.md "SMOKE-TEST CORE PATH"; implementation-plan §0.3, §7; T-M1-15)

This is the Phase-1 smoke-test entry point: from a single natural-language
request it assembles the FULL dev wiring with the test doubles (NO live API key,
NO ``claude -p`` binary, NO network), compiles the factory graph, and runs the
SPEC core path::

    intake -> product -> architecture -> [spec gate] -> implementation
           -> quality -> [build+test gates] -> emit (PR-equivalent action)

end to end, returning the emitted reviewed-artifact handle, the single ``open_pr``
:class:`ExternalAction` (its idempotency key + payload uri), and the attested
gate statuses.

The dev wiring (LEARNINGS D1-D6, AS1-AS5, FG1-3, GW1-3, SUP1-7) — mirrors the
proven assembly in ``tests/test_factory_graph.py`` / ``tests/test_supervisor.py``:

* an in-memory :class:`ArtifactStore` (``rt.cas``) — the real content-addressed
  body store written on emit (AS1-AS5);
* an :class:`AgentRuntime` whose model egress is a real
  :class:`ModelProviderProxy` backed by a :class:`MockTransport` (deterministic,
  no live API key, L6/D1) + a dev :class:`ZoneBSigner`, and whose coding
  substrate is a :class:`StubAdapter` (no ``claude -p``, D3);
* a real :class:`TwoZoneGateRunner` (dev signer + :class:`GlobalRecords` +
  scanner/lineage revs) — the SOLE minter of signed verdicts (L5/GW2);
* a :class:`DevGateWriteVerifier` installed on the ``merge_gates`` contextvar so
  the REAL reducer admits the runner-attested verdicts (D4) — its token is reset
  in a ``finally`` so it never leaks;
* a dev :class:`FoundryCheckpointer` so the run is durable + resumable;
* the materialization seams ``resolve`` / ``resolve_blob`` (GW1a/D-R1-02). The
  StubAdapter authors a deterministic no-op blob (not a passing test suite), so
  to drive the SPEC core path to a clean Spec/Build/Test PASS the smoke run uses
  the SAME KIND-dispatching synthetic resolver the end-to-end tests use (SUP7):
  it synthesizes a faithful body per ``handle.kind`` and a passing unified-diff
  blob. The real :meth:`ArtifactStore.resolve` / :meth:`ArtifactStore.resolve_blob`
  views are wired as the default fallthrough so a future adapter that authors a
  real passing suite needs no CLI change.

ALL model egress stays behind the proxy (L6) — this module never imports
``anthropic`` and never touches GitHub / the network: the PR is expressed ONLY
as an :class:`ExternalAction` on the ``actions`` channel (FG2); the outbox relay
is the sole component that ever touches the outside world.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langgraph.types import Command

from foundry.agents import AgentRuntime
from foundry.docs import write_bundle
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.persistence import (
    DurableBackends,
    durable_backends,
    state_dir_from_env,
)
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.gates.signing import signing_schemes_from_env
from foundry.graph.factory_graph import build_factory_graph, terminal_gates_pass
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    normalize_checkpoints,
)
from foundry.proxy import ModelProviderProxy, build_backend
from foundry.proxy.model_provider_proxy import DriftMode, DriftPolicy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import FrozenModel
from foundry.schemas.checkpoint import CHECKPOINT_BOUNDARIES
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.docs import DocumentationBundle
    from foundry.domains.contract import DomainPackContract
    from foundry.domains.registry import PackRegistry
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.proxy.cli_transports import BackendName
    from foundry.schemas.base import Artifact, ExternalAction
    from foundry.schemas.checkpoint import CheckpointBoundary
    from foundry.state.factory_state import FactoryState

# A programmatic checkpoint responder (plan [10]): given the READ-ONLY inspection
# payload surfaced at a pause, return the human decision (a verdict string, or a
# mapping with ``verdict`` + optional ``steered_by``/``note``). Programmatic by
# design — NO interactive stdin — so the checkpoint flow is fully testable.
CheckpointResponder = Callable[[dict[str, Any]], "str | dict[str, Any]"]

__all__ = ["MODES", "CheckpointResponder", "RunResult", "main", "run"]

# Zone-B trust-root key material: a locally-held HMAC secret (the operator is the
# trust boundary on a single-user LAN; see foundry.gates.signing).
_KEY = b"dev-zone-b-cli-key-0123456789-abcdef"
_KEY_ID = "dev-key-cli"
_SCANNER_REV = "scanner-rev-cli-1"
_LINEAGE_REV = "lineage-rev-cli-1"

# The fixed diff_uri the synthetic FeatureDiff body points at; resolve_blob
# returns the passing unified-diff body keyed on it (SUP7).
_DIFF_BODY_URI = "cas://cli/diff-body"

# The dev-grade trusted-builder identity + deploy target the AUTONOMOUS trust-root
# chain (build → admit → deploy) wires hands-off. They stand in for the production
# KMS/OIDC builder + deploy target (plan §0.3); the builder shares the run's zone-B
# key so admission re-verifies the build signature under the SAME root.
_BUILDER_ID = "dev-trusted-builder-cli"
_DEPLOY_TARGET = "prod"

# The release modes the CLI exposes (the supervisor mode constants; assistant is the
# DEFAULT — byte-identical to the historical request-only run).
MODES = (MODE_ASSISTANT, MODE_GATED, MODE_AUTONOMOUS)

# The model backends selectable via ``--backend`` / run(backend=). "mock" (DEFAULT)
# is the deterministic scripted MockTransport — keyless, no network, the historical
# smoke behavior. The others route the factory's model egress through a REAL backend
# via the single-egress proxy (foundry/proxy/cli_transports.build_backend): "anthropic"
# (SDK; needs ANTHROPIC_API_KEY), "openai" (SDK chat.completions; needs OPENAI_API_KEY),
# "claude-cli" (`claude -p`), "codex" (`codex exec`), "ollama-gemma" (local Ollama).
# Picking a real backend makes real, metered model calls; the coding substrate +
# materialization in THIS smoke CLI stay dev-grade (StubAdapter + synthetic resolver),
# so --backend exercises the proxy/lineage/determinism/manifest path, not a real
# code-writing adapter (that is a separate seam).
_MOCK_BACKEND = "mock"
BACKENDS = (_MOCK_BACKEND, "anthropic", "openai", "claude-cli", "codex", "ollama-gemma")


# Coding substrates (WorkerAdapter): the thing that actually writes the code for
# each task. `stub` is the deterministic no-op (the smoke default); `raw-react` is a
# provider-agnostic single-loop substrate over the model backend (one completion ->
# one diff, any backend); `claude-code` is the AGENTIC substrate (ROADMAP Phase 1a) —
# it runs the `claude` CLI multi-turn INSIDE the task sandbox, where it edits files
# and runs builds/tests itself, and authenticates via the sandbox's typed model-auth
# grant (Max login/token = no API billing, or ANTHROPIC_API_KEY = metered).
ADAPTERS = ("stub", "raw-react", "claude-code", "proxy-agent")
_DEFAULT_ADAPTER = "stub"


# When --adapter is OMITTED the coding substrate is DERIVED from the chosen backend,
# so picking a real brain writes real code without a second flag (the historical
# footgun: a real --backend silently no-op'd under a hardcoded stub default). `mock`
# has no real model, so it keeps the deterministic no-op `stub` (the default run stays
# hermetic + byte-identical). `claude-cli` — the backend whose substrate binary IS
# Claude Code — defaults to the agentic `claude-code` adapter (the roadmap's biggest
# quality lever; same binary, same auth). Every other real backend defaults to
# `raw-react`, the provider-agnostic completion substrate. FOUNDRY_CODING_ADAPTER
# (the same env var the Workshop UI reads) overrides the derivation env-side; an
# explicit --adapter always wins.
def _default_adapter_for(backend: str) -> str:
    """The coding substrate to use when ``--adapter`` is omitted, derived from
    ``backend``: ``mock`` ⇒ ``stub`` (no real model), ``claude-cli`` ⇒
    ``claude-code`` (agentic, same binary + auth), any other real backend ⇒
    ``raw-react`` (the universal completion substrate). ``FOUNDRY_CODING_ADAPTER``
    (when set to a known adapter) overrides the derivation."""
    env = os.environ.get("FOUNDRY_CODING_ADAPTER", "").strip().lower()
    if env in ADAPTERS:
        return env
    if backend == _MOCK_BACKEND:
        return _DEFAULT_ADAPTER
    return "claude-code" if backend == "claude-cli" else "raw-react"

# System-decided parallelism (no user knob): the Tech-Lead decomposes the work
# into independently-buildable tasks, and the IC pool sizes ITSELF to that plan —
# one worker per ready task, bounded by this SAFETY CAP so a large decomposition
# can't spawn unbounded concurrent model/coding sessions (rate-limit / resource
# guard). Effective parallelism = min(tasks the lead created, AUTO_SLOTS_CAP); a
# single-task build still runs serially. An explicit --max-slots / FOUNDRY_MAX_SLOTS
# only LOWERS or RAISES this ceiling for constrained/over-provisioned infra; it is
# never required, and the default is fully automatic. The ceiling itself is tunable
# via FOUNDRY_SLOTS_CAP (default 8) so a complex, many-task build can fan out wider
# without per-call flags.
AUTO_SLOTS_CAP = max(1, int(os.environ.get("FOUNDRY_SLOTS_CAP") or 8))
# Minimal system prompt for the raw-react substrate (the full task prompt — pinned
# constraints, task, spans, "emit a unified diff" — is built by the adapter itself).
_RAW_REACT_SOP = (
    "You are a senior software engineer. Implement the requested change exactly, "
    "returning your work as a single fenced ```diff block. Touch nothing outside it."
)


def make_coding_adapter(
    name: str | None,
    *,
    proxy: ModelProviderProxy,
    thread_id: str = "cli-run",
) -> Any:
    """Construct the selected coding substrate (``ADAPTERS``). ``None`` ⇒ the
    deterministic ``stub`` (back-compat). ``raw-react`` drives the chosen model
    backend through the single-egress proxy (real code over any backend — `claude -p`
    completions under ``claude-cli``, the Anthropic API under ``anthropic``)."""
    name = (name or _DEFAULT_ADAPTER).strip().lower()
    if name == "stub":
        return StubAdapter()
    if name == "raw-react":
        from foundry.execution.adapters.raw_react import RawReactAdapter

        def _complete(prompt: str) -> str:
            return (
                proxy.complete(
                    "junior_engineer",
                    "T1",
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
        return ClaudeCodeAdapter(run_id=thread_id)
    if name == "proxy-agent":
        from foundry.execution.adapters.proxy_agent import ProxyAgentAdapter

        # The trust-preserving agentic loop (ROADMAP 1c): multi-turn, but every
        # call rides the mediating proxy — sole credential holder, any backend.
        def _agent_complete(messages: Any, schema: dict[str, Any]) -> tuple[str, Any]:
            result = proxy.complete(
                "junior_engineer",
                "T1",
                _RAW_REACT_SOP,
                list(messages),
                purpose="ideation",
                schema=schema,
            )
            return result.text or "", result.parsed

        return ProxyAgentAdapter(complete_fn=_agent_complete, run_id=thread_id)
    raise ValueError(f"unknown adapter {name!r}; choose one of {', '.join(ADAPTERS)}")


def _resolve_coding_adapter(
    adapter: str | Any | None, *, proxy: ModelProviderProxy, thread_id: str
) -> Any:
    """A WorkerAdapter instance passes through (tests inject one); a name (or None)
    is constructed via :func:`make_coding_adapter`."""
    if adapter is None or isinstance(adapter, str):
        return make_coding_adapter(adapter, proxy=proxy, thread_id=thread_id)
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
        GEMMA_TIER_MODELS,
        OPENAI_TIER_MODELS,
    )
    from foundry.proxy.model_provider_proxy import TIER_MODELS

    if backend == "openai":
        return dict(OPENAI_TIER_MODELS)
    if backend == "codex":
        return dict(CODEX_TIER_MODELS)
    if backend == "ollama-gemma":
        return dict(GEMMA_TIER_MODELS)
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


def _build_proxy(backend: str) -> ModelProviderProxy:
    """Build the single-egress proxy for the chosen backend (the dev zone-B signer
    is injected so the judgment path can stamp verdicts regardless of backend).

    ``"mock"`` is the deterministic scripted transport (the default, keyless); any
    other name is delegated to ``build_backend`` (a real egress). An unknown name
    fails closed with a clear ``ValueError``. Drift from the pinned model on a
    gate/judgment call fails closed by default (see :func:`_drift_policy`)."""
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}"
        )
    signer = _zone_b_signer()
    drift = _drift_policy()
    if backend == _MOCK_BACKEND:
        tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
        return ModelProviderProxy(tx, signer=signer, drift_policy=drift)
    # Past the guard above, `backend` is one of the real BackendName literals.
    # Pin the per-tier models (curated default + FOUNDRY_MODEL_T{1,2,3} overrides)
    # so an operator can name the EXACT ids their egress serves without code edits.
    return build_backend(
        cast("BackendName", backend),
        signer=signer,
        drift_policy=drift,
        tier_models=_tier_models(backend),
    )

# The first-party domain packs selectable via ``--packs`` (T-M5-08). Mapped by the
# stable pack NAME (``pack.name``); a NEW pack is added here AND via register() — but
# the graph wiring stays open/closed (it reads the COMPOSED policy generically).
_PACK_FACTORIES: dict[str, Callable[[], DomainPackContract]] = {}


def _pack_factories() -> dict[str, Callable[[], DomainPackContract]]:
    """The lazily-built ``name -> pack-factory`` map for ``--packs`` selection.

    Imported lazily (the pack modules pull in the domain contract) so a plain
    ``--mode assistant`` run never imports the domain layer (back-compat: no new
    import cost / no new surface on the default path).
    """
    if not _PACK_FACTORIES:
        from foundry.domains.packs.crypto import CryptoPack
        from foundry.domains.packs.frontend import FrontendPack
        from foundry.domains.packs.game import GamePack
        from foundry.domains.packs.infra import InfraPack
        from foundry.domains.packs.ml_data import MLDataPack
        from foundry.domains.packs.regulated import RegulatedPack

        for cls in (RegulatedPack, MLDataPack, FrontendPack, GamePack, CryptoPack, InfraPack):
            _PACK_FACTORIES[cls().name] = cls
    return _PACK_FACTORIES


def _build_pack_registry(packs: list[str]) -> tuple[PackRegistry, frozenset[str]]:
    """Build a :class:`PackRegistry` from selected pack NAMES + their capabilities.

    Returns the registry (with each named pack registered) and the UNION of the
    selected packs' declared capabilities. The capabilities are seeded onto the
    request's ``required_capabilities`` channel so the COMPOSED policy actually
    engages (a pack's veto / approver-of-record / HITL fires off the request
    capabilities, not merely off registration — mirrors the supervisor-pack
    tests). Raises :class:`ValueError` for an unknown pack name (fail-closed; a
    typo never silently engages nothing)."""
    from foundry.domains.registry import PackRegistry

    factories = _pack_factories()
    registry = PackRegistry()
    capabilities: set[str] = set()
    for name in packs:
        factory = factories.get(name)
        if factory is None:
            raise ValueError(
                f"unknown pack {name!r}; available: {', '.join(sorted(factories))}"
            )
        pack = factory()
        registry.register(pack)
        capabilities.update(pack.capabilities)
    return registry, frozenset(capabilities)


def _enforce_mode_veto(
    rt: AgentRuntime,
    config: RunnableConfig,
    registry: PackRegistry,
    capabilities: frozenset[str],
    *,
    requested_mode: str,
) -> None:
    """Fail-closed: refuse to run a mode an engaged domain pack VETOES (T-M5-08).

    The composed ``mode_tier`` policy is a PURE function of the request's
    capabilities; :func:`resolve_effective_mode_tier` resolves the EFFECTIVE mode
    after the veto. If the requested mode is no longer reachable (e.g. a regulated
    pack vetoes hands-off Autonomous, degrading it to Gated), we RAISE rather than
    silently run the (now-vetoed) mode — the operator must re-issue with the
    allowed mode. This mirrors the supervisor's at-triage degrade, surfaced as an
    explicit CLI error so the trust posture the operator believes they ran is the
    posture that actually ran."""
    from foundry.domains.governance_hooks import resolve_effective_mode_tier

    # The view carries the requested mode so mode-aware packs (game/infra/crypto
    # relax their human-only requirements for a declared-autonomous run) compose
    # the same policy here as at the release boundary.
    request_view = {"required_capabilities": sorted(capabilities), "mode": requested_mode}
    policy = registry.compose(request_view)
    eff = resolve_effective_mode_tier(
        policy.mode_tier,
        requested_mode=requested_mode,
        request=request_view,
        capabilities=sorted(capabilities),
        seq=rt.seq(config, "cli", "pack:mode_veto"),
    )
    if requested_mode not in eff.reachable_modes:
        raise ValueError(
            f"mode {requested_mode!r} is VETOED by the engaged pack(s) "
            f"{', '.join(p.name for p in registry.packs())}; the strictest "
            f"reachable mode is {eff.effective_mode!r} "
            f"(re-run with --mode {eff.effective_mode})"
        )

# Spec ingestion (task [8]). A whole-run cap on the design-spec text seeded into
# state: bounds checkpoint size + token cost on a large doc / directory; the
# product nodes truncate again for their prompts. Read as UTF-8, errors replaced
# (a spec is plain input data, never executed — no new trust surface). Tunable via
# FOUNDRY_SPEC_MAX_BYTES (default 2 MiB) so a complex build can ground itself in a
# large design doc; raising it trades a bigger checkpoint / more tokens for depth.
_SPEC_MAX_BYTES = max(64 * 1024, int(os.environ.get("FOUNDRY_SPEC_MAX_BYTES") or (2 * 1024 * 1024)))
_SPEC_SUFFIXES = (".md", ".txt")

# A unified-diff ADDITION blob carrying a passing test, so the integrated tree
# the gates run against yields Build PASS (compiles) + Test PASS (pytest green).
_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
)

# The Gated/Autonomous spine ADDS the assurance phase (Security/Audit/supply-chain
# on the integrated tree). The Audit gate reads a lockfile inventory (AW2), so the
# assurance-mode default diff carries a ``uv.lock`` alongside the passing test (no
# hardcoded secret → Security PASS). Used as the run() default ONLY for
# gated/autonomous (assistant stays byte-identical to the Phase-1 _PASSING_DIFF).
_ASSURANCE_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)


# ---- scripted per-node payloads (free-text completions, SOP order) ----------
# Substring-keyed on each agent's SOP marker (count-independent, so a fix-loop
# can re-run the implementation+quality phases without exhausting a queue; SUP7).

_INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Run the one-line request through the factory.",
    "assumptions": ["single user"],
    "unknowns": [],
    "success_criteria": ["the requested behavior works"],
    "must_haves": ["core feature"],
    "nice_to_haves": [],
}

_PRD_PAYLOAD: dict[str, Any] = {
    "title": "Requested service",
    "prd_body": "Deliver the requested behavior.",
    "required_capabilities": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given the service, When invoked, Then it responds.",
            "criticality": "normal",
            "oracle_hint": "call it; observe the response",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits the API, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401; negative oracle",
            "security_abuse": True,
        },
    ],
}

_CRITIQUE_PAYLOAD: dict[str, Any] = {"missing": [], "security_abuse_omissions": []}

_SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "none",
    "done_definition": "all criteria green",
}

_DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Requested service",
    "design_body": "api -> store",
    "components": ["api"],
    "adrs": [],
    "interfaces": [],
    "ownership": {"app.py": "owner-a"},  # one unit -> one task -> one diff
    "pinned_constraints": [],
}

_DECOMPOSITION_PAYLOAD: dict[str, Any] = {
    "tasks": [{"unit": "app.py", "title": "build app", "criterion_ids": ["AC-001"]}]
}

_QA_PAYLOAD: dict[str, Any] = {
    "tests": [{"test_id": "t-1", "criterion_ids": ["AC-001", "AC-002"]}],
    "quarantined": [],
}

_SDET_PAYLOAD: dict[str, Any] = {
    "scenarios": [{"scenario_id": "e2e-1", "criterion_ids": ["AC-001"]}],
    "quarantined": [],
}

_RUNBOOK_PAYLOAD: dict[str, Any] = {
    "overview": "Operational guide for deploying and running the service.",
    "prerequisites": ["provisioned runtime", "configured secrets"],
    "deploy_steps": ["build artifact", "run gates", "promote to prod"],
    "rollback_steps": ["re-point to previous artifact digest", "verify health"],
    "health_checks": ["GET /healthz returns 200"],
    "monitoring": ["error rate", "p99 latency"],
    "oncall_escalation": ["page primary on-call after 5m of failing health checks"],
}
_KNOWN_ISSUES_PAYLOAD: dict[str, Any] = {
    "issues": [
        {
            "summary": "Dev-grade in-memory persistence",
            "impact": "State is lost on restart",
            "workaround": "Wire a durable store before production",
            "severity": "medium",
        }
    ],
    "limitations": ["single-region deployment assumed"],
}
_UPGRADE_PAYLOAD: dict[str, Any] = {
    "from_version": "0.0.0",
    "to_version": "0.1.0",
    "breaking_changes": [],
    "migration_steps": ["no migration required for the initial release"],
    "rollback_notes": "Re-deploy the previous artifact digest.",
}
_PERF_COST_PAYLOAD: dict[str, Any] = {
    "summary": "Build completed within the configured budget.",
    "observations": ["most tokens were spent in the architecture and quality stages"],
    "recommendations": ["enable prompt caching on the stable SOP prefixes"],
}

# The assurance-phase agents (Gated/Autonomous spine only). Mirrors the gated /
# autonomous end-to-end test fixtures: a clean STRIDE model, a clean SBOM, and a
# clean supply-chain review → Security/Audit/supply_chain PASS on the integrated
# tree. Unused on the Assistant path (the assurance subgraph is never mounted).
_APPSEC_PAYLOAD: dict[str, Any] = {
    "threat_model_body": "STRIDE model",
    "oracle_bindings": {"S1": "test_auth"},
    "residual_item_ids": [],
    "severity": "low",
    "findings": [],
}
_AUDIT_PAYLOAD: dict[str, Any] = {"sbom_body": "sbom", "severity": "low", "findings": []}
_SUPPLY_CHAIN_PAYLOAD: dict[str, Any] = {
    "report_body": "supply-chain review",
    "policy_violations": [],
    "trust_root_touched": False,
}

_SCRIPTS: dict[str, str] = {
    "Stakeholder / Client Proxy": json.dumps(_INTENT_PAYLOAD),
    "Product Manager": json.dumps(_PRD_PAYLOAD),
    "Criteria-Completeness Critic": json.dumps(_CRITIQUE_PAYLOAD),
    "Engineering / Program Manager": json.dumps(_SPRINT_PAYLOAD),
    "Principal Architect": json.dumps(_DESIGN_PAYLOAD),
    "Senior Engineer": json.dumps(_DECOMPOSITION_PAYLOAD),
    "QA Engineer": json.dumps(_QA_PAYLOAD),
    "SDET": json.dumps(_SDET_PAYLOAD),
    # Assurance phase (Gated / Autonomous spine).
    "You are the Security / AppSec Engineer": json.dumps(_APPSEC_PAYLOAD),
    "You are the Audit / Compliance agent": json.dumps(_AUDIT_PAYLOAD),
    "You are the Supply-Chain Reviewer": json.dumps(_SUPPLY_CHAIN_PAYLOAD),
    # Technical Writer (foundry/docs) authored docs.
    "OPERATIONAL RUNBOOK": json.dumps(_RUNBOOK_PAYLOAD),
    "KNOWN-ISSUES": json.dumps(_KNOWN_ISSUES_PAYLOAD),
    "UPGRADE / MIGRATION GUIDE": json.dumps(_UPGRADE_PAYLOAD),
    "PERFORMANCE & COST": json.dumps(_PERF_COST_PAYLOAD),
}


# ---- in-memory materialization doubles (SUP7) -------------------------------


def _synthetic_resolve(store: ArtifactStore) -> ArtifactResolver:
    """A KIND-dispatching typed resolver that falls through to the real store.

    The architecture/product agents mint content-addressed HANDLES mid-run whose
    uri encodes an epoch-dependent ``content_identity()`` (so it can't be
    pre-keyed) and, with the StubAdapter, no faithful passing-test body is ever
    authored. So for the handle kinds the SPEC core path consumes we synthesize a
    faithful body (SUP7); anything else falls through to the REAL store
    (``rt.cas``) so a future real-suite adapter needs no CLI change.
    """

    def _resolve(handle: Artifact) -> FrozenModel:
        if handle.kind in {SystemDesign.__name__, "system_design"}:
            return SystemDesign(
                title="Requested service",
                design_uri="cas://design/body",
                components=("api",),
                interface_ids=(),
                seq=handle.seq,
            )
        if handle.kind == "ownership_map":
            return OwnershipMap(owners={"app.py": "owner-a"}, seq=handle.seq)
        if handle.kind == "acceptance_criterion":
            cid = f"AC-{handle.seq.local:03d}-{handle.seq.emission_epoch}"
            return AcceptanceCriterion(
                id=cid,
                text=f"Given/When/Then for {cid}",
                criticality=Criticality.HIGH_RISK,
                oracle_hint=f"oracle for {cid}",
                security_abuse=True,  # guarantees Spec-gate security coverage
                seq=handle.seq,
            )
        if handle.kind == "feature_diff":
            return FeatureDiff(
                diff_uri=_DIFF_BODY_URI,
                task_id=handle.task_id or "T1",
                branch=f"task/{handle.task_id or 'T1'}",
                base_tree_sha="base-sha",
                seq=handle.seq,
            )
        # Fall through to the REAL store for any other body kind.
        return store.resolve(handle)

    return _resolve


def _synthetic_resolve_blob(store: ArtifactStore, diff_body: str) -> BlobResolver:
    """Return the passing unified-diff body for the synthetic FeatureDiff uri.

    Mirrors ``sandbox.get_blob``: keyed on the ``cas://`` uri. The fixed
    synthetic FeatureDiff diff_uri (``_DIFF_BODY_URI``) returns ``diff_body``;
    anything else falls through to the REAL store (``rt.cas.resolve_blob``).
    """

    def _resolve_blob(uri: str) -> bytes:
        if uri == _DIFF_BODY_URI:
            return diff_body.encode("utf-8")
        return store.resolve_blob(uri)

    return _resolve_blob


def _materialization_seam(
    real_artifacts: bool, *, store: ArtifactStore, diff_body: str
) -> tuple[ArtifactResolver, BlobResolver]:
    """The (resolve, resolve_blob) pair for the run (Phase-1 2b).

    Real backend ⇒ resolve straight off the real CAS (the agents' authored
    designs/criteria + the adapter's real FeatureDiff are the bytes the gates
    run). Mock ⇒ the synthetic SUP7 doubles that guarantee the deterministic
    Spec/Build/Test PASS with the StubAdapter (byte-identical smoke)."""
    if real_artifacts:
        return store.resolve, store.resolve_blob
    return _synthetic_resolve(store), _synthetic_resolve_blob(store, diff_body)


def _judgment_anchors(real_artifacts: bool) -> dict[str, str]:
    """The pre-build judgment-binding LABELS pinned on the run config (Phase-1 2c).

    Mock ⇒ pin the synthetic subject + tree label the synthetic resolver
    produces (the reviewer's advisory verdict binds them; byte-identical smoke).
    Real backend ⇒ pin NOTHING: the reviewer then binds the REAL newest
    FeatureDiff uri (state fallback) + the REAL integrated tree_sha (read off the
    attested gate verdicts), and the gated sign-off anchors derive consistently
    on both read sites. Admission/provenance/evidence already bind the real
    verdict-carried ``run_against``, never these labels, so unpinning is safe."""
    if real_artifacts:
        return {}
    return {"subject_artifact_uri": _DIFF_BODY_URI, "state_sha": "integrated-sha"}


# ---- final source-tree materialization (on a passing build) -----------------

# The gate-input file the integrated tree carries for the Spec gate; it is NOT a
# source file the user asked the factory to produce, so it is excluded from the
# emitted output tree.
_GATE_INPUT_FILES = frozenset({"criteria.json"})


def _write_output_tree(
    final_state: FactoryState,
    output_dir: str,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> str:
    """Materialize the FINAL accepted integrated tree and write the source files.

    Reuses :func:`graph.gate_wiring.materialize_workdir` over the reviewed
    FeatureDiff handles + the same ``resolve`` / ``resolve_blob`` seams the run
    wired (so the bytes written are EXACTLY what the terminal gates ran against,
    never a parallel re-render). The materialization happens in a throwaway temp
    dir; the real source files (everything except the Spec gate's ``criteria.json``
    input) are then copied into ``output_dir`` on disk. Returns the resolved
    absolute path written.

    ``output_dir`` is operator-supplied, but the per-file relative paths come
    from agent-influenced diff bodies; ``materialize_workdir`` already rejects an
    absolute/``..`` diff path fail-closed (D-R1-03), and we re-check each copied
    relative path against the destination root (mirrors
    :func:`docs.assemble.write_bundle`) so a write can never escape ``output_dir``.
    """
    from foundry.graph.gate_wiring import materialize_workdir

    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="foundry-cli-out-") as staging:
        stage_root = Path(staging)
        materialize_workdir(
            final_state, stage_root, resolve=resolve, resolve_blob=resolve_blob
        )
        for src in sorted(stage_root.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(stage_root)
            if rel.name in _GATE_INPUT_FILES and len(rel.parts) == 1:
                continue
            # Defense-in-depth (mirrors docs/assemble.write_bundle): reject an
            # absolute or '..' relative path before writing under out_root.
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe source path {str(rel)!r}")
            target = (out_root / rel).resolve()
            if not target.is_relative_to(out_root):
                raise ValueError(f"source path escapes output dir: {str(rel)!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
    return str(out_root)


def _write_draft_on_halt(
    final_state: FactoryState,
    output_dir: str,
    *,
    gate_findings: dict[str, tuple[str, ...]],
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> str | None:
    """Write a best-effort DRAFT on a HALTED run: the latest attempt's source tree
    plus a ``DRAFT-STATUS.md`` naming what's still failing — so a held build hands back
    a usable starting point, never empty hands. Best-effort: a tree that can't even be
    materialized (e.g. an unparseable diff) is skipped (returns ``None``), never crashes
    the run. Clearly marked NOT shipped — admission still rejected the tree."""
    try:
        draft_root = _write_output_tree(
            final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob
        )
    except Exception:
        return None
    gates = final_state.get("gates") or ()
    statuses = {g.name: g.status for g in gates}
    lines = [
        "# DRAFT — held at the gate (NOT shipped)\n\n",
        "This build did not pass every gate, so it was **not admitted/released**. The\n",
        "source below is the best attempt — fix what's failing, then re-run.\n\n",
        "## Gate verdicts\n\n",
    ]
    for name, status in statuses.items():
        mark = "✅" if status is GateStatus.PASS else "❌"
        lines.append(f"- {mark} **{name}**: {str(status).split('.')[-1]}\n")
    failing = {
        name: gate_findings.get(name, ())
        for name, status in statuses.items()
        if status is not GateStatus.PASS
    }
    if any(failing.values()):
        lines.append("\n## What's still failing\n")
        for name, findings in failing.items():
            if findings:
                body = "\n".join(findings)
                lines.append(f"\n### {name}\n\n```\n{body}\n```\n")
    (Path(draft_root) / "DRAFT-STATUS.md").write_text("".join(lines), encoding="utf-8")
    return draft_root


# ---- design-spec ingestion (task [8]) ---------------------------------------


def _read_spec_path(path: str) -> str:
    """Read a design-spec FILE or DIRECTORY of ``.md``/``.txt`` docs to text.

    A directory is read recursively in sorted (deterministic) path order; only
    ``.md``/``.txt`` files are included. Each file's text is prefixed with a
    ``# === <relative-path> ===`` marker so a multi-file spec stays legible.
    Raises :class:`FileNotFoundError` for a missing path and :class:`ValueError`
    when a directory contains no spec files (a clean, actionable error).

    The spec is INPUT DATA only: it is read as UTF-8 (decode errors replaced) and
    never executed — no new egress, no new trust surface. The combined text is
    capped at :data:`_SPEC_MAX_BYTES` to bound checkpoint size / token cost.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"spec path not found: {path}")
    if p.is_dir():
        files = sorted(
            f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _SPEC_SUFFIXES
        )
        if not files:
            raise ValueError(
                f"spec directory has no {'/'.join(_SPEC_SUFFIXES)} files: {path}"
            )
        parts = [
            f"# === {f.relative_to(p)} ===\n{f.read_text(encoding='utf-8', errors='replace')}"
            for f in files
        ]
        return "\n\n".join(parts)
    return p.read_text(encoding="utf-8", errors="replace")


def _is_existing_path(value: str) -> bool:
    """True iff ``value`` names an existing path. Guards ``Path.exists()`` against
    an ``OSError`` (e.g. a long literal-spec string exceeding the OS name limit),
    so literal spec text never crashes the path probe. An empty/whitespace string
    is NEVER a path (``Path("").exists()`` resolves to cwd) — else an explicit
    ``spec=""`` would silently ingest the entire working directory."""
    if not value.strip():
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _load_spec(spec: str | list[str] | None) -> str:
    """Resolve the ``spec`` argument to the design-spec text seeded into state.

    ``spec`` is additive (task [8]) and may be:

    * ``None`` — no spec (request-only run, unchanged behavior);
    * a single string that names an existing file/dir — its text is read;
    * a single string that is NOT a path — treated as literal spec text;
    * a list of file/dir paths — each read and concatenated (sorted by path
      marker, deterministic).

    The combined text is capped at :data:`_SPEC_MAX_BYTES`.
    """
    if spec is None:
        return ""
    if isinstance(spec, str):
        if not spec.strip():
            return ""  # empty/blank literal == no spec (never probed as a path)
        text = _read_spec_path(spec) if _is_existing_path(spec) else spec
    else:
        text = "\n\n".join(_read_spec_path(s) for s in sorted(spec))
    encoded = text.encode("utf-8")
    if len(encoded) > _SPEC_MAX_BYTES:
        # Cap on ENCODED BYTES (deterministic, truly bounds checkpoint size even
        # for multibyte specs); decode back, dropping a partial trailing char.
        # The product nodes truncate again for their prompts.
        text = encoded[:_SPEC_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


# ---- per-mode trust posture (lifecycle.md §4 deployment-trust modes) --------
# A one-line human summary of the trust root each mode engages, surfaced in the
# run summary + --help so the operator sees WHAT authority releases the artifact.
_TRUST_POSTURE: dict[str, str] = {
    MODE_ASSISTANT: (
        "advisory review, no trust root — produces a bare open_pr a human reviews "
        "+ merges via normal CI (the reviewer's verdict is ADVISORY)"
    ),
    MODE_GATED: (
        "full gate set + human sign-off — a release candidate behind Spec/Build/Test "
        "+ the tier-scaled assurance family, carrying a WORM evidence pack, gated on "
        "a recorded Design + Release sign-off"
    ),
    MODE_AUTONOMOUS: (
        "full trust root — hands-off build -> admit -> deploy; deploy-time admission "
        "is the SOLE release gate (no human sign-off)"
    ),
}


# ---- result ----------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """The outcome of a smoke run — what the CLI reports.

    Holds only content-addressed handles / identifiers + the attested gate
    statuses; carries no bodies (L4) and makes no external effect.
    """

    request: str
    thread_id: str
    passed: bool
    phase: str
    mode: str = MODE_ASSISTANT
    """The release mode the run was driven in (assistant | gated | autonomous)."""
    backend: str = _MOCK_BACKEND
    """The model backend the proxy used (mock | anthropic | openai | claude-cli | codex |
    ollama-gemma)."""
    packs: tuple[str, ...] = ()
    """The domain packs engaged (by name) — empty when no ``--packs`` were given."""
    artifact_uri: str | None = None
    """The reviewed FeatureDiff handle uri the PR opens against (the PR subject)."""
    open_pr_idempotency_key: str | None = None
    open_pr_payload_uri: str | None = None
    gate_statuses: dict[str, GateStatus] = field(default_factory=dict)
    """Newest-by-name attested status per gate (spec / build / test / review...)."""
    gate_findings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Findings (the human-readable WHY) for the newest verdict per gate — surfaced
    in the summary for FAILING gates so a halted real build is diagnosable."""
    doc_root: str | None = None
    """Where the per-build documentation set was written (when ``docs_dir`` set)."""
    output_root: str | None = None
    """Where the final accepted source tree was written (when ``output_dir`` is
    set AND the build passed); ``None`` on a halted run (no PASS, no write)."""
    draft_root: str | None = None
    """Where a best-effort DRAFT tree was written on a HALTED run (output_dir set,
    gates not all green): the latest attempt's source + a ``DRAFT-STATUS.md`` naming
    what's still failing — so a held build hands back a usable starting point, never
    empty hands. ``None`` on a PASS (use ``output_root``) or when no draft could be
    materialized."""
    checkpoints: tuple[str, ...] = ()
    """The checkpoint stage boundaries the run was driven with (plan [10]); empty
    when checkpoints were OFF (the default)."""
    paused_at: str | None = None
    """The stage boundary the run is PAUSED at awaiting a human decision (plan
    [10]) — set only when checkpoints are on and no responder drove past the pause.
    ``None`` means the run completed (or was never checkpointed)."""

    def summary(self) -> str:
        """A concise human-readable report of the run (printed by ``main``)."""
        lines = [
            f"request:   {self.request!r}",
            f"thread_id: {self.thread_id}",
            f"mode:      {self.mode}",
            f"  trust:   {_TRUST_POSTURE.get(self.mode, 'unknown')}",
            f"backend:   {self.backend}  "
            + ("(deterministic mock — no model call)"
               if self.backend == _MOCK_BACKEND else "(real model egress)"),
        ]
        if self.packs:
            lines.append(f"packs:     {', '.join(self.packs)}")
        if self.checkpoints:
            lines.append(f"checkpoints: {', '.join(self.checkpoints)}")
        if self.paused_at is not None:
            lines.append(
                f"PAUSED at the {self.paused_at!r} checkpoint awaiting a human "
                "decision (resume with a CheckpointDecision)."
            )
        lines += [
            f"phase:     {self.phase}",
            f"result:    {'PASS' if self.passed else 'HALTED (no PR)'}",
            "gates:",
        ]
        for name in sorted(self.gate_statuses):
            lines.append(f"  - {name}: {self.gate_statuses[name].value}")
            # Surface WHY a gate didn't pass (real builds halt here; the findings
            # are the actionable detail — a missing oracle hint, a failing test, …).
            if self.gate_statuses[name] is not GateStatus.PASS:
                for finding in self.gate_findings.get(name, ()):
                    lines.append(f"      · {finding}")
        if self.passed:
            lines.append("reviewed artifact (PR subject):")
            lines.append(f"  {self.artifact_uri}")
            lines.append("open_pr action:")
            lines.append(f"  idempotency_key: {self.open_pr_idempotency_key}")
            lines.append(f"  payload_uri:     {self.open_pr_payload_uri}")
        else:
            lines.append("no open_pr action emitted (gates did not all PASS).")
        if self.output_root:
            lines.append(f"source tree written to: {self.output_root}")
        if self.draft_root:
            lines.append(
                f"DRAFT written to: {self.draft_root}  (held — see DRAFT-STATUS.md "
                "for what's failing; not shipped)"
            )
        if self.doc_root:
            lines.append(f"documentation set written to: {self.doc_root}")
        return "\n".join(lines)


# ---- checkpoint pause/resume driver (plan [10]) -----------------------------


def _pending_interrupt(result: Any) -> Any:
    """The pending LangGraph ``Interrupt`` on a paused invoke result, or ``None``.

    A checkpointed run that hits a pause returns a dict carrying ``__interrupt__``
    (the active interrupts); we read the first one's ``value`` (our read-only
    inspection payload) to drive the responder. ``None`` means the run completed."""
    if isinstance(result, dict):
        interrupts = result.get("__interrupt__")
        if interrupts:
            return interrupts[0]
    return None


def _progress_enabled() -> bool:
    """``FOUNDRY_PROGRESS`` truthy ⇒ stream per-node stage progress to stderr.

    A real backend build makes many sequential model calls and can run for
    minutes with NO output (the summary prints only at the end) — on a Pi /
    over the LAN that looks hung. This surfaces each node as it completes so the
    operator can SEE the build advancing. Off by default (byte-identical)."""
    return os.environ.get("FOUNDRY_PROGRESS", "").strip().lower() not in ("", "0", "false", "no")


def _invoke_streaming(app: Any, seed: FactoryState, config: RunnableConfig) -> FactoryState:
    """Drive the graph with ``stream`` (updates mode), printing each completed
    node + elapsed time to stderr, then return the terminal channel state."""
    import time

    t0 = time.monotonic()
    # subgraphs=True surfaces the INNER stage nodes (product/architecture/…), not
    # just the top-level supernode; each item is (namespace_tuple, {node: update}).
    for item in app.stream(seed, config, stream_mode="updates", subgraphs=True):
        ns, chunk = item if isinstance(item, tuple) else ((), item)
        if not isinstance(chunk, dict):
            continue
        scope = ":".join(str(n).split(":")[0] for n in ns) if ns else ""
        for node in chunk:
            label = f"{scope} ▸ {node}" if scope else str(node)
            print(f"  [{time.monotonic() - t0:6.1f}s] · {label}", file=sys.stderr, flush=True)
    return cast("FactoryState", app.get_state(config).values)


def _stream_segment(
    app: Any,
    x: Any,
    config: RunnableConfig,
    progress_cb: Callable[[str, str], None],
) -> dict[str, Any]:
    """Drive ONE graph segment with ``stream`` (updates mode), forwarding each
    node as it completes to ``progress_cb(node, scope)`` for LIVE telemetry (so a
    long real stage shows agents working at their desks node-by-node, not a single
    burst at the pause). ``subgraphs=True`` surfaces the inner stage nodes; the
    namespace's leading scope names the stage/room. Returns a dict carrying
    ``__interrupt__`` if the segment paused, so the caller's interrupt detection
    (:func:`_pending_interrupt`) is byte-identical to the ``invoke`` path."""
    pending: Any = None
    for item in app.stream(x, config, stream_mode="updates", subgraphs=True):
        ns, chunk = item if isinstance(item, tuple) else ((), item)
        if not isinstance(chunk, dict):
            continue
        if "__interrupt__" in chunk:
            pending = chunk["__interrupt__"]
            continue
        scope = ":".join(str(n).split(":")[0] for n in ns) if ns else ""
        for node in chunk:
            # telemetry is advisory — never let a UI callback break the build
            with contextlib.suppress(Exception):
                progress_cb(str(node), scope)
    return {"__interrupt__": pending} if pending else {}


def _invoke_with_checkpoints(
    app: Any,
    seed: FactoryState,
    config: RunnableConfig,
    *,
    checkpointed: bool,
    responder: CheckpointResponder | None,
    progress_cb: Callable[[str, str], None] | None = None,
) -> tuple[FactoryState, str | None]:
    """Drive the compiled graph, pausing/resuming at the checkpoint boundaries.

    Non-checkpointed runs (the default) invoke ONCE — byte-identical to today.
    A checkpointed run invokes, and while the result carries a pending
    ``__interrupt__`` it asks the ``responder`` for a decision and RESUMES via
    ``Command(resume=...)`` (the run is durable on the checkpointer). With NO
    responder, it stops at the first pause and returns ``(state, boundary)`` so the
    caller can resume out-of-band. The bound on the resume loop is the number of
    checkpoint boundaries (a responder that always rejects halts immediately).

    When ``progress_cb`` is given each segment is STREAMED (per-node liveness) via
    :func:`_stream_segment` instead of a silent ``invoke`` — same control flow,
    same interrupt handling, plus live telemetry."""
    def _drive(x: Any) -> Any:
        if progress_cb is not None:
            return _stream_segment(app, x, config, progress_cb)
        return app.invoke(x, config)

    if not checkpointed:
        # Byte-identical to the historical single-invoke path: the invoke return
        # IS the terminal channel state (no interrupts on a non-checkpointed run).
        # With progress on, stream per-node so a long real build shows liveness.
        if progress_cb is not None:
            _stream_segment(app, seed, config, progress_cb)
            return cast("FactoryState", app.get_state(config).values), None
        if _progress_enabled():
            return _invoke_streaming(app, seed, config), None
        return cast("FactoryState", app.invoke(seed, config)), None
    result = _drive(seed)

    # Bound the loop: at most one pause per stage boundary (3) + slack.
    for _ in range(len(CHECKPOINT_BOUNDARIES) + 1):
        interrupt = _pending_interrupt(result)
        if interrupt is None:
            break
        payload = dict(interrupt.value) if isinstance(interrupt.value, dict) else {}
        if responder is None:
            # No responder: surface the pause and stop (caller resumes out-of-band).
            boundary = str(payload.get("boundary") or "")
            return cast("FactoryState", app.get_state(config).values), boundary or None
        decision = responder(payload)
        result = _drive(Command(resume=decision))
    # The authoritative terminal state is the checkpointed channel view.
    return cast("FactoryState", app.get_state(config).values), None


# ---- the smoke run ----------------------------------------------------------


def run(
    request: str,
    *,
    mode: str = MODE_ASSISTANT,
    packs: list[str] | None = None,
    spec: str | list[str] | None = None,
    backend: str = _MOCK_BACKEND,
    thread_id: str = "cli-run",
    state_dir: str | None = None,
    resolve: ArtifactResolver | None = None,
    resolve_blob: BlobResolver | None = None,
    adapter: str | Any | None = None,
    diff_body: str | None = None,
    max_fix_attempts: int = 5,
    docs_dir: str | None = None,
    output_dir: str | None = None,
    checkpoints: bool | list[str | CheckpointBoundary] | None = None,
    checkpoint_responder: CheckpointResponder | None = None,
    progress_cb: Callable[[str, str], None] | None = None,
    max_slots: int = AUTO_SLOTS_CAP,
    cost_cap_microusd: int | None = None,
    base_dir: str | None = None,
    profile: str | None = "auto",
) -> RunResult:
    """Run ``request`` end-to-end through the compiled factory graph (smoke path).

    Assembles the full dev wiring with the test doubles (NO live API key, NO
    ``claude -p`` binary, NO network), compiles :func:`build_factory_graph`,
    invokes it with ``{"request": request}`` + a ``thread_id`` config, and
    returns a :class:`RunResult` reporting the reviewed artifact handle, the
    single ``open_pr`` :class:`ExternalAction`, and the attested gate statuses.

    Parameters
    ----------
    request:
        The one-line natural-language request (e.g. ``"build a URL shortener"``).
    mode:
        The deployment-trust mode (lifecycle.md §4; default ``"assistant"`` — the
        run is byte-identical to the historical request-only run). ``"gated"``
        threads the Phase-2 supervisor (tiering + Design/Release sign-off +
        assurance) and the evidence-pack-carrying, release-gated emit; the
        reviewer's verdict is advisory in Assistant — the deterministic gates own
        the release bit. ``"autonomous"`` (Phase-4) threads the SAME assurance
        spine and additionally constructs the dev-grade TRUST ROOT (a hermetic
        :class:`TrustedBuilder` + the fail-closed :class:`AdmissionController` +
        the least-privileged :class:`Deployer` + a :class:`DevGateWriteVerifier`,
        all sharing this run's zone-B key + cross-fork records), so the emit is
        the hands-off build → admit → deploy chain (admission is the SOLE release
        gate). Unknown modes are rejected fail-closed.
    packs:
        OPTIONAL domain packs to engage (by NAME — ``regulated`` / ``ml_data`` /
        ``frontend``; T-M5-08). The selected packs are registered in a
        :class:`PackRegistry` (passed to the graph) AND their declared
        capabilities are seeded onto ``required_capabilities`` so the composed
        policy actually engages at triage + the release boundary. A pack can only
        TIGHTEN the release; crucially it may VETO an unsafe mode (a regulated pack
        makes hands-off Autonomous UNREACHABLE / forces a human approver-of-record).
        The veto is enforced FAIL-CLOSED up front: if the requested mode is vetoed
        the run raises a clear error rather than silently running the vetoed mode.
    spec:
        OPTIONAL full design-spec document (task [8]). ADDITIVE to ``request``: a
        short request summarizes intent while the spec provides the detail. May be
        a path to a ``.md``/``.txt`` FILE, a path to a DIRECTORY of such files
        (read recursively, sorted), a list of paths, or literal spec text. The
        resolved text is seeded onto the ``spec`` state channel, where the
        Stakeholder/Client Proxy and Product Manager GROUND the IntentBrief/PRD in
        it. It is plain input data — it INFORMS the criteria but never bypasses the
        acceptance-criteria derivation or any signed gate.
    thread_id:
        The run identity (LangGraph ``thread_id`` / ``Seq.run_id``); also seeds
        the deterministic idempotency keys (verdict nonce, PR key).
    resolve / resolve_blob:
        Materialization seams (GW1a/D-R1-02). Default to the proven synthetic
        SUP7 doubles (which fall through to the real ``rt.cas`` views) so the
        SPEC core path reaches a clean Spec/Build/Test PASS with the StubAdapter.
    diff_body:
        The unified-diff body the synthetic ``resolve_blob`` returns for the
        FeatureDiff. ``None`` (the default) selects a passing addition: the
        Phase-1 ``_PASSING_DIFF`` for Assistant (byte-identical to today) or the
        lockfile-bearing ``_ASSURANCE_PASSING_DIFF`` for Gated/Autonomous (the
        assurance Audit gate reads a lockfile inventory). Pass an explicit
        error/failing body to exercise the halted-run path.
    max_fix_attempts:
        Convergence bound for the gate fix-loop (default 5). On a gate FAIL/ERROR the
        run fixes FORWARD — back to the responsible layer WITH the specific failures —
        up to this many times before HALTing (escalate). The failed tree's digest stays
        permanently disqualified each round (anti-gaming); only a genuinely-passing NEW
        tree ships. Raise it for "keep working until it's green"; 0 disables retries.
    output_dir:
        When set AND the build PASSES (terminal gates pass), write a single
        **GitHub-ready repository** here: the accepted integrated source tree at
        the root + a real ``README.md`` + the doc set under ``docs/`` + the
        ``BUILD-MANIFEST``. Surfaced on ``RunResult.output_root``. On a HALTED run a
        best-effort DRAFT (the latest attempt's tree + a ``DRAFT-STATUS.md`` naming
        what's failing) is written instead, surfaced on ``RunResult.draft_root`` — so
        a held build hands back a usable starting point, never empty hands.
    docs_dir:
        OPTIONAL. When set, ALSO write the documentation set to this SEPARATE
        directory (back-compat / docs elsewhere). When only ``output_dir`` is
        given, the docs already live inside that repo under ``docs/``.
    checkpoints:
        OPTIONAL checkpoint/HITL mode (plan [10]). ``None``/``False`` (the
        DEFAULT) runs straight through with NO pauses (byte-identical to today).
        ``True`` pauses at all three stage boundaries (after product/spec, after
        architecture/design, after implementation+quality / before release); a
        list of boundary names pauses at exactly those. The run is resumable on
        the durable checkpointer. **Trust:** a checkpoint approve only lets the run
        proceed to the (unchanged) gate boundary — it can NEVER turn a gate FAIL
        into a release; a release reject HALTS even on a gate PASS.
    checkpoint_responder:
        OPTIONAL programmatic responder driving the pauses (plan [10]; NO
        interactive stdin, so the flow is fully testable). Called with the
        READ-ONLY inspection payload at each pause; returns the human decision (a
        verdict string ``"approve"``/``"reject"``/``"needs_changes"``, or a mapping
        with ``verdict`` + optional ``steered_by``/``note`` recording steering
        authorship). When ``checkpoints`` are on but NO responder is supplied, the
        run PAUSES at the first boundary and returns with ``RunResult.paused_at``
        set (the caller resumes out-of-band on the same ``thread_id``).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose one of {', '.join(BACKENDS)}")
    # Resolve the checkpoint boundary set up front (fail-closed on a typo'd name).
    ckpt_set = normalize_checkpoints(checkpoints)
    ckpt_names = tuple(sorted(b.value for b in ckpt_set))
    # The default passing diff is mode-specific: Assistant uses the Phase-1
    # _PASSING_DIFF (byte-identical to today); Gated/Autonomous use the
    # lockfile-bearing variant the assurance Audit gate needs. An explicit
    # diff_body (e.g. a failing body) always wins.
    if diff_body is None:
        diff_body = _PASSING_DIFF if mode == MODE_ASSISTANT else _ASSURANCE_PASSING_DIFF
    spec_text = _load_spec(spec)
    # Single-host durability seam (durability/persistence.py): when
    # FOUNDRY_STATE_DIR is SET, the checkpointer (+ its outbox/WORM audit),
    # the GlobalRecords trust ledgers and the CAS are all file-backed under that
    # dir so the run survives a process restart. UNSET (the DEFAULT) ⇒ in-memory
    # everywhere — byte-identical to the historical request-only run.
    # An explicit state_dir= (the --state-dir flag) OVERRIDES the env; otherwise
    # fall back to FOUNDRY_STATE_DIR. None either way ⇒ in-memory (the default).
    resolved_state_dir = state_dir if state_dir is not None else state_dir_from_env()
    durable: DurableBackends | None = (
        durable_backends(resolved_state_dir) if resolved_state_dir is not None else None
    )
    if durable is not None:
        # FilesystemArtifactStore is a documented structural drop-in for the
        # in-memory ArtifactStore (identical put_body/.../resolve/resolve_blob
        # seams, pinned by tests/test_fs_artifact_store.py); cast so the rest of
        # the assembly reads it as the same cas type with no call-site change.
        store: ArtifactStore = cast("ArtifactStore", durable.cas)
        records = durable.global_records
        ckpt = durable.checkpointer
    else:
        store = ArtifactStore()
        records = GlobalRecords()
        ckpt = FoundryCheckpointer()
    proxy = _build_proxy(backend)
    # --adapter omitted (None) ⇒ DERIVE the substrate from the backend so a real brain
    # writes real code (mock→stub, any real backend→raw-react). An explicit adapter —
    # a name OR an injected WorkerAdapter instance (tests) — is never None and wins.
    if adapter is None:
        adapter = _default_adapter_for(backend)
    rt = AgentRuntime(
        proxy=proxy,
        adapter=_resolve_coding_adapter(adapter, proxy=proxy, thread_id=thread_id),
        cas=store,
    )

    # Seal the zone-A observation channel (fail-closed forged-results hardening):
    # the gate-wiring builds each gate with `runner.seal_key`, so the observation a
    # gate produces is sealed under the same per-run key the runner verifies.
    runner = TwoZoneGateRunner(
        _zone_b_signer(), records, _SCANNER_REV, _LINEAGE_REV, seal_key=os.urandom(32)
    )

    # -- materialization seam (Phase-1 2b) ------------------------------------
    # The `mock` smoke backend uses the synthetic SUP7 doubles: with the
    # StubAdapter no faithful test body is ever authored and the architecture
    # agents mint epoch-keyed handles, so the doubles synthesize faithful bodies
    # that guarantee the deterministic Spec/Build/Test PASS. A REAL backend means
    # the agents author real designs / criteria / ownership AND (with a real
    # coding adapter) a real FeatureDiff is written into the CAS, so we resolve
    # straight off the real store — the bytes the gates run are EXACTLY what was
    # authored, never a canned override. (A real backend with the `stub` adapter
    # authors no diff; the reviewer then fail-closes honestly — a real build needs
    # a real coding adapter.)
    real_artifacts = backend != _MOCK_BACKEND
    _resolve, _resolve_blob = _materialization_seam(
        real_artifacts, store=store, diff_body=diff_body
    )
    resolve = resolve or _resolve
    resolve_blob = resolve_blob or _resolve_blob

    # -- judgment-binding anchors (Phase-1 2c) --------------------------------
    # `subject_artifact_uri` / `state_sha` are pre-build RUN-INPUT LABELS the
    # reviewer's advisory verdict + the gated sign-off bind to. For the `mock`
    # smoke they are PINNED to the synthetic subject/label the synthetic resolver
    # produces (byte-identical to before). For a REAL backend we DON'T pin them:
    # the reviewer then binds the REAL newest FeatureDiff uri (state fallback) +
    # the REAL integrated tree_sha (read off the attested gate verdicts), so the
    # verdict honestly cites the artifact that was actually built, and the gated
    # sign-off anchors derive consistently on both read sites. The LOAD-BEARING
    # trust binds — admission's expected per-gate trees, the BuildOutput
    # provenance, the evidence pack, the stable-FAIL latch — already use the real
    # verdict-carried `run_against`, NEVER this label (factory_graph
    # `_attested_run_against` / `_autonomous_admission_inputs`), so unpinning is
    # safe: admission re-verifies each gate against its own real tree.
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "verdict_nonce": f"N-{thread_id}",
        **_judgment_anchors(real_artifacts),
    }
    config: RunnableConfig = {
        "configurable": configurable,
        "metadata": {"langgraph_step": 1},
        "recursion_limit": 200,
    }

    # -- domain packs (T-M5-08) -----------------------------------------------
    # Build the registry from the selected pack NAMES + collect their capabilities
    # (seeded onto required_capabilities so the composed policy engages). Then
    # ENFORCE the mode veto fail-closed BEFORE running: a regulated pack vetoes
    # hands-off Autonomous (and auto-approve Assistant) — we refuse rather than
    # silently downgrade, so the operator never believes a vetoed mode ran.
    pack_names = tuple(packs or ())
    pack_registry: PackRegistry | None = None
    pack_capabilities: frozenset[str] = frozenset()
    if pack_names:
        pack_registry, pack_capabilities = _build_pack_registry(list(pack_names))
        _enforce_mode_veto(rt, config, pack_registry, pack_capabilities, requested_mode=mode)

    # -- trust-root chain for AUTONOMOUS (lifecycle.md §4) --------------------
    # Construct the SAME dev-grade trust-root components the autonomous tests wire,
    # all sharing THIS run's zone-B key + cross-fork records (so admission
    # re-verifies the runner-attested gate-state + reads the disqualification
    # latch). For gated/assistant these stay None (ignored by build_factory_graph).
    builder = admission = deployer = gate_verifier = None
    if mode == MODE_AUTONOMOUS:
        from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

        builder = TrustedBuilder(_zone_b_signer(), builder_id=_BUILDER_ID)
        admission = AdmissionController(builder_id=_BUILDER_ID)
        deployer = Deployer(target=_DEPLOY_TARGET)
        gate_verifier = _gate_verifier(records)

    # The terminal ``docs`` graph node assembles the company-shaped documentation
    # set + BUILD-MANIFEST IN-GRAPH (task [3]): the Technical Writer's model spend
    # is budget-metered through the SAME proxy and the authored doc artifacts land
    # on the checkpointed ``artifacts`` channel. The node appends the assembled
    # bundle to this in-process sink so the CLI can WRITE the already-assembled
    # tree to disk without re-authoring (no second round of model spend). The sink
    # is only wired when ``--docs DIR`` was requested.
    doc_sink: list[DocumentationBundle] = []

    # Install the dev gate-write verifier so the REAL merge_gates admits the
    # runner-attested verdicts (D4); reset its token in finally so it never leaks.
    token = set_gate_write_verifier(_gate_verifier(records))
    with tempfile.TemporaryDirectory(prefix="foundry-cli-src-") as source_dir:
        # Seed the base tree with an existing codebase (the evolution / iterate
        # path): the implementation agents clone ``source_dir`` into each sandbox
        # worktree and produce a diff AGAINST it, so a seeded base makes the change
        # build ON TOP of the prior code instead of greenfield. Plain input bytes —
        # copied, never executed (the gates still decide what ships).
        if base_dir:
            base = Path(base_dir)
            if base.is_dir():
                shutil.copytree(base, source_dir, dirs_exist_ok=True)
        try:
            # Verification profile (ROADMAP Phase 3): stack-appropriate pinned
            # gate commands. "auto" (default) infers from the request + spec;
            # None keeps the historical python defaults (the platform
            # orchestrator opts out — its deepening loop owns per-service
            # verification); a profile NAME pins that profile explicitly.
            from foundry.gates.profiles import PROFILES, profile_for_request

            if profile == "auto":
                run_profile = profile_for_request(f"{request}\n{spec_text or ''}")
            elif profile is None:
                run_profile = None
            else:
                if profile not in PROFILES:
                    raise ValueError(
                        f"unknown profile {profile!r}; choose one of {sorted(PROFILES)}"
                    )
                run_profile = PROFILES[profile]
            app = build_factory_graph(
                rt,
                runner=runner,
                resolve=resolve,
                resolve_blob=resolve_blob,
                source_dir=source_dir,
                checkpointer=ckpt.saver,
                max_fix_attempts=max_fix_attempts,
                mode=mode,
                builder=builder,
                admission=admission,
                deployer=deployer,
                gate_verifier=gate_verifier,
                profile=run_profile,
                pack_registry=pack_registry,
                # Author the doc set when EITHER a separate --docs dir OR an --out
                # tree is requested (the docs land inside the --out repo, below).
                doc_sink=doc_sink if (docs_dir is not None or output_dir is not None) else None,
                checkpoints=sorted(ckpt_set) if ckpt_set else None,
                max_slots=max_slots,
                cost_cap_microusd=cost_cap_microusd,
            )
            # `spec` is additive and seeded ONLY when provided (request-only runs
            # keep the historical {"request": ...} seed exactly). The product
            # nodes read it off the `spec` channel to ground the IntentBrief/PRD.
            seed: FactoryState = {"request": request}
            if spec_text:
                seed["spec"] = spec_text
            # Seed the selected packs' capabilities so the composed policy engages
            # at triage + the release boundary (a pack's veto / approver-of-record
            # / HITL fires off required_capabilities). Default run: never set.
            if pack_capabilities:
                seed["required_capabilities"] = sorted(pack_capabilities)
            final, paused_at = _invoke_with_checkpoints(
                app,
                seed,
                config,
                checkpointed=bool(ckpt_set),
                responder=checkpoint_responder,
                progress_cb=progress_cb,
            )
        finally:
            reset_gate_write_verifier(token)
            # Release the SQLite handles: durable mode closes the checkpointer +
            # the global-records connection together; otherwise just the ckpt.
            if durable is not None:
                durable.close()
            else:
                ckpt.close()

    final_state = final
    result = _result(
        request,
        thread_id,
        final_state,
        mode=mode,
        backend=backend,
        packs=pack_names,
        checkpoints=ckpt_names,
        paused_at=paused_at,
    )
    if output_dir is not None and result.passed:
        # On a PASSING build, materialize the FINAL accepted integrated tree
        # (reusing the SAME materialize_workdir + resolve/resolve_blob seams the
        # terminal gates ran against) and write the real source files to disk.
        # A HALTED run writes a best-effort DRAFT instead (see the elif below).
        output_root = _write_output_tree(
            final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob
        )
        result = replace(result, output_root=output_root)
        # GitHub-ready repo: write the doc set INTO the same tree (a real README
        # at the root + the design/ops docs under docs/ + the BUILD-MANIFEST), so
        # `--out` produces a single push-ready repository, not code-then-docs in
        # two places. The bundle's RenderedDoc paths (README.md / docs/… /
        # BUILD-MANIFEST.*) compose with the source files already at the root.
        if doc_sink:
            write_bundle(doc_sink[-1], output_dir)
            result = replace(result, doc_root=output_dir)
    elif output_dir is not None and not result.passed:
        # HALTED with an output dir: still hand back a usable DRAFT (the best attempt
        # + a DRAFT-STATUS.md saying what's failing), so the run is never empty-handed.
        draft_root = _write_draft_on_halt(
            final_state, output_dir, gate_findings=result.gate_findings,
            resolve=resolve, resolve_blob=resolve_blob,
        )
        if draft_root is not None:
            result = replace(result, draft_root=draft_root)
            if doc_sink:  # docs help orient a human finishing the draft
                write_bundle(doc_sink[-1], output_dir)
                result = replace(result, doc_root=output_dir)
    if docs_dir is not None and doc_sink:
        # An EXPLICIT --docs DIR still writes the bundle to that separate location
        # (back-compat / docs elsewhere). AUTHORING + the manifest already happened
        # IN-GRAPH (budget-metered, checkpointed); the CLI only writes to disk.
        write_bundle(doc_sink[-1], docs_dir)
        result = replace(result, doc_root=docs_dir)
    return result


def _result(
    request: str,
    thread_id: str,
    final: FactoryState,
    *,
    mode: str = MODE_ASSISTANT,
    backend: str = _MOCK_BACKEND,
    packs: tuple[str, ...] = (),
    checkpoints: tuple[str, ...] = (),
    paused_at: str | None = None,
) -> RunResult:
    """Distill the terminal state into a :class:`RunResult` (no bodies, L4).

    The release action is mode-specific: Assistant/Gated emit an ``open_pr``
    (Gated's payload is the evidence-pack handle), Autonomous emits a ``deploy``
    (payload is the admitting decision handle). We surface whichever was emitted
    on the same ``open_pr_*`` fields so the summary reports the release uniformly.
    """
    statuses: dict[str, GateStatus] = {}
    findings: dict[str, tuple[str, ...]] = {}
    # newest-by-Seq per gate (the verdict that gates the release; mirrors emit guard)
    newest: dict[str, Any] = {}
    for g in final.get("gates") or ():
        cur = newest.get(g.name)
        if cur is None or g.seq > cur.seq:
            newest[g.name] = g
    for name, g in newest.items():
        statuses[name] = g.status
        if getattr(g, "findings", None):
            findings[name] = tuple(g.findings)

    # The mode-specific release action: open_pr (assistant/gated) or deploy
    # (autonomous). At most one is emitted on a released run (fail-closed otherwise).
    release: ExternalAction | None = next(
        (a for a in (final.get("actions") or ()) if a.kind in ("open_pr", "deploy")),
        None,
    )
    # A run "passed" only if a release action was actually emitted (gates PASS AND
    # not halted by a checkpoint reject / pause). On a non-checkpoint PASS a
    # release is always emitted, so this is byte-identical to the prior
    # ``terminal_gates_pass`` semantics; a checkpoint reject (gates PASS but no
    # PR) or a paused run reports ``passed=False`` — the gates remain the trust
    # authority, the checkpoint only subtracts the release.
    passed = terminal_gates_pass(final) and release is not None
    return RunResult(
        request=request,
        thread_id=thread_id,
        mode=mode,
        backend=backend,
        packs=packs,
        passed=passed,
        phase=str(final.get("phase") or "unknown"),
        artifact_uri=release.payload_uri if release is not None else None,
        open_pr_idempotency_key=release.idempotency_key if release is not None else None,
        open_pr_payload_uri=release.payload_uri if release is not None else None,
        gate_statuses=statuses,
        gate_findings=findings,
        checkpoints=checkpoints,
        paused_at=paused_at,
    )


# ---- the evolution route (task [12]) ----------------------------------------


def _read_overlay(overlay_dir: str) -> dict[str, str | None]:
    """Read an OVERLAY directory into a change set (relpath -> new content).

    The overlay's files are the added/modified files of the maintained change;
    they are laid over the prior tree by :func:`foundry.lifecycle.evolve.evolve`.
    Read as UTF-8 (errors replaced) — plain input data, never executed. (Deletes
    are expressed via the programmatic ``evolve(changes={rel: None})`` API; the
    shell affordance overlays added/modified files, the common evolution shape.)
    """
    root = Path(overlay_dir)
    if not root.is_dir():
        raise ValueError(f"overlay path is not a directory: {overlay_dir}")
    changes: dict[str, str | None] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            changes[p.relative_to(root).as_posix()] = p.read_text(
                encoding="utf-8", errors="replace"
            )
    if not changes:
        raise ValueError(f"overlay directory is empty: {overlay_dir}")
    return changes


def _autopilot_main(args: list[str]) -> int:
    """`foundry autopilot "<target>" --out DIR [...]` — Director + design panel.

    Builds the target, then loops: the Director proposes candidate next actions,
    a software/usability panel scores + vetoes them (deterministic tally), and
    the winner becomes the next gated iteration. Exit 0 iff every iteration
    released; 1 when the loop stopped (gate refusal / panel decline / budget).
    """
    try:
        args, out_dir = _take_value(args, "--out", what="a directory argument")
        args, _mode = _take_value(args, "--mode", what="a value")
        mode = _mode if _mode is not None else MODE_ASSISTANT
        if mode not in MODES:
            raise _CliUsageError(f"unknown --mode {_mode!r}")
        args, _backend = _take_value(args, "--backend", what="a value")
        backend = _backend if _backend is not None else _MOCK_BACKEND
        if backend not in BACKENDS:
            raise _CliUsageError(f"unknown --backend {_backend!r}")
        args, _adapter = _take_value(args, "--adapter", what="a value")
        if _adapter is not None and _adapter not in ADAPTERS:
            raise _CliUsageError(f"unknown --adapter {_adapter!r}")
        args, state_dir = _take_value(args, "--state-dir", what="a directory argument")
        args, _thread = _take_value(args, "--thread-id", what="a value")
        args, _iters = _take_value(args, "--iterations", what="an integer")
        no_git = "--no-git" in args
        args = [a for a in args if a not in ("--git", "--no-git")]  # --git = default
        request = " ".join(args).strip()
        if not request or not out_dir:
            raise _CliUsageError(
                'usage: python -m foundry.cli autopilot "<target>" --out DIR '
                "[--iterations N] [--backend B] [--adapter A] [--mode M] "
                "[--state-dir DIR] [--thread-id NAME] [--no-git]"
            )
        iterations = int(_iters) if _iters else 3
    except (_CliUsageError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from foundry.lifecycle.autopilot import autopilot

    stage_dir = out_dir if no_git else str(Path(out_dir) / ".stages")
    report = autopilot(
        request,
        out_dir=stage_dir,
        iterations=iterations,
        proxy=_build_proxy(backend),
        thread_id=_thread or "autopilot",
        mode=mode,
        backend=backend,
        adapter=_adapter,
        state_dir=state_dir,
    )
    print(report.summary())
    if report.released_dir:
        print(f"released:  {report.released_dir}")
    if not no_git and report.released_dir:
        _export_git(out_dir, [(f"i{r.index:02d}", r.out_dir, r.request[:72])
                              for r in report.iterations if r.passed])
    return 0 if all(r.passed for r in report.iterations) else 1


def _milestones_main(args: list[str]) -> int:
    """`foundry milestones "<big target>" --out DIR [...]` — the Phase-5 loop.

    Plans (or takes ``--milestones "a; b; c"`` verbatim) and runs the full
    factory per milestone, chaining released trees. Exit 0 iff EVERY milestone
    released; 1 when the loop stopped at a gate refusal; 2 on usage error.
    """
    try:
        args, out_dir = _take_value(args, "--out", what="a directory argument")
        args, _mode = _take_value(args, "--mode", what="a value")
        mode = _mode if _mode is not None else MODE_ASSISTANT
        if mode not in MODES:
            raise _CliUsageError(f"unknown --mode {_mode!r}")
        args, _backend = _take_value(args, "--backend", what="a value")
        backend = _backend if _backend is not None else _MOCK_BACKEND
        if backend not in BACKENDS:
            raise _CliUsageError(f"unknown --backend {_backend!r}")
        args, _adapter = _take_value(args, "--adapter", what="a value")
        if _adapter is not None and _adapter not in ADAPTERS:
            raise _CliUsageError(f"unknown --adapter {_adapter!r}")
        args, plan_arg = _take_value(args, "--milestones", what="a '; '-separated list")
        args, state_dir = _take_value(args, "--state-dir", what="a directory argument")
        args, _thread = _take_value(args, "--thread-id", what="a value")
        args, _max = _take_value(args, "--max-milestones", what="an integer")
        no_git = "--no-git" in args
        args = [a for a in args if a not in ("--git", "--no-git")]  # --git = default
        request = " ".join(args).strip()
        if not request or not out_dir:
            raise _CliUsageError(
                'usage: python -m foundry.cli milestones "<big target>" --out DIR '
                "[--milestones 'a; b; c'] [--backend B] [--adapter A] [--mode M] "
                "[--state-dir DIR] [--thread-id NAME] [--max-milestones N] [--no-git]"
            )
        max_milestones = int(_max) if _max else 8
    except (_CliUsageError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from foundry.lifecycle.milestones import run_milestones

    explicit = [m.strip() for m in (plan_arg or "").split(";") if m.strip()] or None
    proxy = None if explicit else _build_proxy(backend)
    stage_dir = out_dir if no_git else str(Path(out_dir) / ".stages")
    report = run_milestones(
        request,
        out_dir=stage_dir,
        milestones=explicit,
        proxy=proxy,
        thread_id=_thread or "milestones",
        max_milestones=max_milestones,
        mode=mode,
        backend=backend,
        adapter=_adapter,
        state_dir=state_dir,
    )
    print(report.summary())
    if report.released_dir:
        print(f"released:  {report.released_dir}")
    if not no_git and report.released_dir:
        _export_git(out_dir, [(f"m{r.index:02d}", r.out_dir, r.title)
                              for r in report.results if r.passed])
    return 0 if report.passed else 1


def _export_git(out_dir: str, released: list[tuple[str, str, str]]) -> None:
    """Project the released increments into ONE git repo at <out>/repo (a VIEW —
    the CAS/verdicts are untouched). Failure is reported, never fatal: the
    released trees stand either way."""
    from pathlib import Path

    from foundry.lifecycle.git_export import GitExportError, ReleaseExport, export_lineage

    try:
        head = export_lineage(
            out_dir,  # <out> IS the repo; increments stage under <out>/.stages
            [ReleaseExport(tag=tag, tree_dir=Path(d), message=title)
             for tag, d, title in released],
        )
        print(f"git repo:  {out_dir}  (HEAD {head[:12]}, "
              f"{len(released)} tagged commit(s); increments in .stages/)")
    except GitExportError as exc:
        print(f"git export skipped ({exc}) — increments stand in "
              f"{Path(out_dir) / '.stages'}", file=sys.stderr)


def _evolve_main(args: list[str]) -> int:
    """``foundry evolve <prior repo> "<change>" --apply OVERLAY`` (task [12]).

    Re-enters an EXISTING released codebase as a MAINTAINED change: re-verify
    change-proportionally, re-gate via the SAME runner, re-admit fail-closed, link
    provenance to the prior version. The maintained change is RELEASED only when
    it RE-PASSES the gates + RE-ADMITS (a failing re-gate ⇒ NOT released — no PR /
    deploy). ``--apply OVERLAY`` supplies the changed files (overlaid on the prior
    tree); ``--guardian-approve`` records the HITL decision for a protected
    criterion/oracle/gate edit (which otherwise BLOCKS release, deterministic HITL).

    Returns 0 iff the maintained change was RELEASED, 1 if NOT released
    (fail-closed), 2 on a usage error.
    """
    from foundry.lifecycle.evolve import EvolveRefused, evolve

    guardian_approval = False
    if "--guardian-approve" in args:
        args = [a for a in args if a != "--guardian-approve"]
        guardian_approval = True
    # --strict: a not-released re-gate RAISES (EvolveRefused) instead of returning a
    # result — for scripts/CI that want a hard failure on any non-release.
    strict = False
    if "--strict" in args:
        args = [a for a in args if a != "--strict"]
        strict = True
    overlay_dir: str | None = None
    if "--apply" in args:
        i = args.index("--apply")
        if i + 1 >= len(args):
            print("error: --apply requires a directory argument", file=sys.stderr)
            return 2
        overlay_dir = args[i + 1]
        args = args[:i] + args[i + 2 :]
    if len(args) < 2:
        print(
            'usage: python -m foundry.cli evolve <prior-repo-dir> "<change>" '
            "--apply OVERLAY_DIR [--guardian-approve] [--strict]\n"
            "  re-enter an EXISTING released repo as a maintained change: re-verify\n"
            "  change-proportionally, re-gate (same runner), re-admit (fail-closed),\n"
            "  link provenance to the prior version. A failing re-gate => NOT released.\n"
            "  --apply OVERLAY_DIR: the changed/added files overlaid on the prior tree\n"
            "  --guardian-approve: record the HITL decision for a protected\n"
            "    criterion/oracle/gate edit (otherwise it BLOCKS release)\n"
            "  --strict: a not-released re-gate exits non-zero (raises) instead of reporting",
            file=sys.stderr,
        )
        return 2
    prior_dir = args[0]
    change = " ".join(args[1:]).strip()
    if not change:
        print("error: change request must be non-empty", file=sys.stderr)
        return 2
    if overlay_dir is None:
        print("error: evolve requires --apply OVERLAY_DIR", file=sys.stderr)
        return 2
    if not Path(prior_dir).is_dir():
        print(f"error: prior repo not found: {prior_dir}", file=sys.stderr)
        return 2
    try:
        changes = _read_overlay(overlay_dir)
        result = evolve(
            prior_dir, change, changes=changes,
            guardian_approval=guardian_approval, strict=strict,
        )
    except EvolveRefused as exc:
        # --strict: the re-gate refused to release; report and exit non-zero.
        print(f"evolve REFUSED (not released): {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.summary())
    return 0 if result.released else 1


# ---- entry point ------------------------------------------------------------


class _CliUsageError(Exception):
    """A flag-parsing usage error; ``main`` prints ``error: <msg>`` and returns 2."""


def _take_value(args: list[str], flag: str, *, what: str) -> tuple[list[str], str | None]:
    """Pop ``flag VALUE`` out of ``args``; return ``(remaining, value)`` — or
    ``(args, None)`` if the flag is absent. Raises :class:`_CliUsageError` if the
    flag is present with no following value. (Replaces the 13 copy-pasted
    ``if flag in args: i = args.index(flag); …`` blocks; H5.)"""
    if flag not in args:
        return args, None
    i = args.index(flag)
    if i + 1 >= len(args):
        raise _CliUsageError(f"{flag} requires {what}")
    return args[:i] + args[i + 2 :], args[i + 1]


def _take_int(
    args: list[str], flag: str, *, minimum: int | None = None
) -> tuple[list[str], int | None]:
    """Pop an integer-valued ``flag``. Raises :class:`_CliUsageError` on a missing
    value, a non-integer, or a value below ``minimum``."""
    args, raw = _take_value(args, flag, what="an integer")
    if raw is None:
        return args, None
    try:
        value = int(raw)
    except ValueError as exc:
        raise _CliUsageError(f"{flag} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise _CliUsageError(f"{flag} must be >= {minimum}")
    return args, value


def _request_from_spec(spec_text: str) -> str:
    """Derive a one-line request from a spec's text — its first heading / non-blank
    line — so a build can run from ``--spec FILE`` alone (no positional request).
    Falls back to a generic line if the spec has no usable heading."""
    for line in spec_text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:200]
    return "Build the system described in the provided spec."


def main(argv: list[str] | None = None) -> int:
    """``python -m foundry.cli "build a ..."`` — run the smoke path, print a report.

    Returns process exit code 0 on a clean Spec/Build/Test PASS (a PR action was
    emitted), 1 on a halted/failed run, 2 on a usage error.
    """
    args = sys.argv[1:] if argv is None else argv
    # The evolution route (task [12]): `foundry evolve <prior repo> "<change>"`
    # (see the `evolve` dispatch below; `milestones` follows the same shape.)
    # re-enters an EXISTING released codebase as a maintained change (re-gate +
    # re-admit, fail-closed). Dispatched as a leading subcommand so the greenfield
    # path stays byte-identical (the default — no subcommand — is unchanged).
    if args and args[0] == "evolve":
        return _evolve_main(args[1:])
    # The milestone route (ROADMAP Phase 5): `foundry milestones "<big target>"
    # --out DIR` plans an ordered milestone list and runs the FULL factory per
    # milestone, chaining each released tree as the next milestone's base.
    if args and args[0] == "milestones":
        return _milestones_main(args[1:])
    # The autopilot route: Director proposes, a software/usability panel votes,
    # the winner becomes the next gated iteration (fail-closed on panel decline).
    if args and args[0] == "autopilot":
        return _autopilot_main(args[1:])
    # Simple value flags parse through `_take_value`/`_take_int` (H5: one helper
    # instead of 13 copy-pasted index/slice blocks); a usage error raises and is
    # rendered identically below. Comments mark the non-obvious semantics.
    try:
        args, docs_dir = _take_value(args, "--docs", what="a directory argument")
        args, output_dir = _take_value(args, "--out", what="a directory argument")

        # --mode {assistant|gated|autonomous} (default assistant; task [9]); the
        # default keeps the run byte-identical to the historical request-only path.
        args, _mode = _take_value(args, "--mode", what="a value")
        mode = _mode if _mode is not None else MODE_ASSISTANT
        if mode not in MODES:
            raise _CliUsageError(f"unknown --mode {mode!r}; choose one of {', '.join(MODES)}")

        # --backend default is FOUNDRY_MODEL_BACKEND if set, else "mock" (keyless,
        # deterministic — keeps the no-credential quickstart + hermetic suite
        # byte-identical). A deployment sets the env once; an explicit flag overrides.
        backend = os.environ.get("FOUNDRY_MODEL_BACKEND", _MOCK_BACKEND) or _MOCK_BACKEND
        if backend not in BACKENDS:
            backend = _MOCK_BACKEND
        args, _backend = _take_value(args, "--backend", what="a value")
        if _backend is not None:
            if _backend not in BACKENDS:
                raise _CliUsageError(
                    f"unknown --backend {_backend!r}; choose one of {', '.join(BACKENDS)}"
                )
            backend = _backend

        # --state-dir DIR: durable file/volume-backed state (overrides
        # FOUNDRY_STATE_DIR); unset ⇒ in-memory. --thread-id NAME: the run identity
        # (also seeds idempotency keys). --max-fix-attempts N: the gate fix-loop bound.
        args, state_dir = _take_value(args, "--state-dir", what="a directory argument")
        args, _tid = _take_value(args, "--thread-id", what="a value")
        thread_id = _tid if _tid is not None else "cli-run"
        args, _mfa = _take_int(args, "--max-fix-attempts", minimum=1)
        max_fix_attempts = _mfa if _mfa is not None else 5

        # Parallelism is SYSTEM-DECIDED: the Tech-Lead decomposes the work and the
        # IC pool sizes itself to that plan (min(tasks, AUTO_SLOTS_CAP)). --max-slots
        # / FOUNDRY_MAX_SLOTS only OVERRIDE the safety ceiling (e.g. lower it on a
        # constrained box); neither is required. --cost-cap-microusd N: the global
        # spend envelope the pool sub-allocates (default FOUNDRY_COST_CAP_MICROUSD).
        try:
            _slots_default = int(os.environ.get("FOUNDRY_MAX_SLOTS", "") or AUTO_SLOTS_CAP)
        except ValueError:
            _slots_default = AUTO_SLOTS_CAP
        args, _slots = _take_int(args, "--max-slots", minimum=1)
        max_slots = _slots if _slots is not None else _slots_default

        _cost_env = os.environ.get("FOUNDRY_COST_CAP_MICROUSD", "").strip()
        cost_cap_microusd: int | None = int(_cost_env) if _cost_env.isdigit() else None
        args, _cost = _take_int(args, "--cost-cap-microusd")
        if _cost is not None:
            cost_cap_microusd = _cost

        # --packs name1,name2 (task [9]): domain packs to engage. A pack may VETO the
        # requested mode (enforced fail-closed in run()).
        args, _packs = _take_value(args, "--packs", what="a comma-separated list")
        packs: list[str] = [p.strip() for p in _packs.split(",") if p.strip()] if _packs else []

        # --adapter {stub|raw-react|claude-code}: the coding substrate that writes
        # the code. OMITTED ⇒ DERIVED from --backend in run() (mock→stub,
        # claude-cli→claude-code agentic, other real backends→raw-react) so a real
        # backend writes real code without a second flag.
        args, _adapter = _take_value(args, "--adapter", what="a value")
        if _adapter is not None and _adapter not in ADAPTERS:
            raise _CliUsageError(
                f"unknown --adapter {_adapter!r}; choose one of {', '.join(ADAPTERS)}"
            )
        adapter = _adapter  # None ⇒ run() derives it from --backend

        # --spec FILE|DIR (task [8]): may REPEAT to ingest several spec files/dirs.
        spec_paths: list[str] = []
        while "--spec" in args:
            args, _sp = _take_value(args, "--spec", what="a file or directory argument")
            if _sp is not None:
                spec_paths.append(_sp)
    except _CliUsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # --checkpoints [all|name1,name2] (plan [10]): pause at the stage boundaries
    # for human verification. ``--checkpoints`` alone (or ``all``) enables all
    # three; a comma list selects specific boundaries. From the shell the CLI uses
    # an APPROVE-ALL responder (a human partner would inspect + decide via the
    # ``run(checkpoint_responder=...)`` API — programmatic, no interactive stdin);
    # an approve still cannot turn a gate FAIL into a release (the gates decide).
    checkpoints: bool | list[str] | None = None
    if "--checkpoints" in args:
        i = args.index("--checkpoints")
        # An optional value follows ONLY if the next token isn't the request /
        # another flag (heuristic: contains no spaces and isn't the last arg or a
        # known sub-token). We accept "all" or a comma list; otherwise enable all.
        nxt = args[i + 1] if i + 1 < len(args) else None
        if nxt is not None and (nxt == "all" or ("," in nxt) or nxt in {
            b.value for b in CHECKPOINT_BOUNDARIES
        }):
            checkpoints = True if nxt == "all" else [s.strip() for s in nxt.split(",") if s.strip()]
            args = args[:i] + args[i + 2 :]
        else:
            checkpoints = True
            args = args[:i] + args[i + 1 :]
    # Resolve --spec paths to text FIRST: a spec can STAND IN for the one-line
    # request (run from just `--spec FILE`), so we resolve it before deciding whether
    # a positional request is required. A missing/empty path is a clean usage error
    # (exit 2); a deep ValueError from the run itself is not masked. The resolved text
    # is handed to run() (which seeds it onto `spec`).
    try:
        spec_text = _load_spec(spec_paths) if spec_paths else None
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The one-line request is OPTIONAL when a --spec is given: with no request we
    # derive one from the spec; with NEITHER a request nor a spec, print usage.
    request = " ".join(args).strip()
    if not request:
        if spec_text:
            request = _request_from_spec(spec_text)
        else:
            print(
                "usage: python -m foundry.cli "
                "[--mode {assistant|gated|autonomous}] [--packs name1,name2] "
                "[--backend {mock|anthropic|openai|claude-cli|codex|ollama-gemma}] "
                "[--adapter {stub|raw-react|claude-code}] "
                "[--checkpoints [all|product,design,release]] "
                "[--docs DIR] [--out DIR] [--spec FILE|DIR ...] "
                "[--state-dir DIR] [--thread-id NAME] [--max-fix-attempts N] "
                '["<one-line request>"]   (optional when --spec is given)\n'
                "  modes (lifecycle.md §4 deployment-trust):\n"
                f"    assistant  {_TRUST_POSTURE[MODE_ASSISTANT]}\n"
                f"    gated      {_TRUST_POSTURE[MODE_GATED]}\n"
                f"    autonomous {_TRUST_POSTURE[MODE_AUTONOMOUS]}\n"
                "  backend: mock (default — keyless, deterministic) | anthropic | openai | "
                "claude-cli | codex | ollama-gemma (real model egress)\n"
                "  adapter (coding substrate): omitted => DERIVED from --backend "
                "(mock=>stub no-op, claude-cli=>claude-code AGENTIC, other real "
                "backends=>raw-react); override with stub | raw-react | claude-code\n"
                "  packs: regulated | ml_data | frontend | game | crypto | infra "
                "(a pack may VETO an unsafe mode — fail-closed)\n"
                "  checkpoints (plan [10]): pause at stage boundaries for human "
                "verification; an approve can NEVER turn a gate FAIL into a release",
                file=sys.stderr,
            )
            return 2

    # A shell --checkpoints run uses an APPROVE-ALL responder so it completes
    # without interactive stdin (a human partner inspects + decides via the
    # run(checkpoint_responder=...) API). The approve only proceeds to the gates;
    # it can never make a FAILing gate release.
    responder: CheckpointResponder | None = (
        (lambda _payload: "approve") if checkpoints else None
    )

    # An unknown pack name OR a pack VETO of the requested mode is a fail-closed
    # usage error (exit 2) — we never silently run a vetoed/empty configuration.
    try:
        result = run(
            request,
            mode=mode,
            packs=packs or None,
            spec=spec_text,
            backend=backend,
            adapter=adapter,
            thread_id=thread_id,
            state_dir=state_dir,
            max_fix_attempts=max_fix_attempts,
            docs_dir=docs_dir,
            output_dir=output_dir,
            checkpoints=checkpoints,
            checkpoint_responder=responder,
            max_slots=max_slots,
            cost_cap_microusd=cost_cap_microusd,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
