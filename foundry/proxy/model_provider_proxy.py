"""The mediating model-provider proxy. (architecture.md §2b, plan §3.3)

Single egress + single credential holder. Every model call in the factory
goes through :class:`ModelProviderProxy`, which:

- observes the ACTUALLY-served model from the provider response
  (``response.model`` + ``request_id`` — there is no system_fingerprint
  field on this API; those two are the recorded observation) and resolves
  it through the curated :class:`~foundry.proxy.lineage.LineageMap` —
  adapter self-report is never consulted;
- owns trusted verdict-extraction for judgment calls: the verdict ``status``
  is parsed from the grammar-constrained structured-output channel
  (``output_config.format`` json_schema), never from adapter free-form text;
- stamps the judgment-verdict binding OUTSIDE the implementing adapter via an
  injected :class:`~foundry.gates.attestation.ZoneBSigner`, including the
  ``judgment_provenance`` stamp (SELF in the self-judge profile, plan §0.2);
- appends a per-call manifest record (requested vs served model, lineage,
  request id, usage, sampling config, cache reads) and raises a
  **pinned-ID-drift** alert when the served model id departs from the
  pinned request (arch §8);
- meters tokens on every call (usage is returned to the caller for the
  budget ledger).

Determinism policy (arch §8) vs current API surface: "temperature 0 for
gates/judges" applies only to T2/T3 — sampling parameters are REMOVED on the
T1 model (``claude-opus-4-8``; sending them is a 400). On T1 the proxy pins
``output_config={"effort": "high"}`` instead and records the substitution in
the manifest. Thinking is enabled (adaptive) only for ideation-class T1
calls and OMITTED for deterministic gate/judge calls.

Degraded-mode rule (fail-closed): on transport failure the proxy retries
with bounded backoff and then raises :class:`ProxyUnavailable`. It NEVER
silently falls back to a different model family — judgment nodes catching
``ProxyUnavailable`` pause-and-escalate (same discipline as the
no-auto-downgrade rule, arch §2b/§8).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from foundry.gates.attestation import ZoneBSigner
from foundry.proxy.lineage import LineageMap
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.state.seq import Seq

# Tier map (plan §4): capability levels WITHIN the single Anthropic lineage —
# a cost choice, not a decorrelation choice (plan §0.2). Pinned ids, exact.
TIER_MODELS: dict[str, str] = {
    "T1": "claude-opus-4-8",
    "T2": "claude-sonnet-4-6",
    "T3": "claude-haiku-4-5",
}

# Sampling parameters are removed on these models (400 if sent); the proxy
# substitutes a pinned effort config and notes it in the manifest.
_NO_SAMPLING_MODELS = frozenset({"claude-opus-4-8"})

_DEFAULT_MAX_TOKENS = 16000

Purpose = Literal["ideation", "gate", "judgment"]
_PURPOSES = ("ideation", "gate", "judgment")

# The grammar-constrained verdict channel (arch §2b): the proxy extracts
# `status` from THIS schema's output, independent of adapter post-processing.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["approve", "request-changes"]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "findings"],
    "additionalProperties": False,
}

_STATUS_BY_VERDICT = {
    "approve": GateStatus.PASS,
    "request-changes": GateStatus.FAIL,
}


class ProxyUnavailable(RuntimeError):
    """Transport exhausted retries. Judgment nodes pause-and-escalate —
    there is no silent fallback to another model or family (fail-closed)."""


class FamilyOverlapError(RuntimeError):
    """reviewer ∩ implementer served_family is non-empty (decorrelated profile)."""


class VerdictChannelError(RuntimeError):
    """The structured verdict channel returned something outside the grammar."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The proxy-observed provider response (the trusted observation)."""

    served_model: str
    request_id: str
    text: str
    usage: dict[str, int]
    stop_reason: str


class ProviderTransport(Protocol):
    """Minimal egress seam: one credentialed request out, one observation in."""

    def send(self, request: dict[str, Any]) -> ProviderResponse: ...


class AnthropicTransport:
    """Default transport over the real Anthropic SDK.

    The client (and the credential it resolves) is constructed lazily on
    first use — importing/instantiating this module never requires network
    or an API key, so tests run entirely on a fake transport.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        response = self._ensure_client().messages.create(**request)
        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        usage = response.usage
        return ProviderResponse(
            served_model=response.model,
            request_id=response._request_id or "",
            text=text,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
            stop_reason=response.stop_reason or "",
        )


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """What a proxy call hands back: text/JSON + the trusted observations."""

    text: str
    parsed: dict[str, Any] | None
    served_model: str
    served_family: frozenset[str]  # resolved by the proxy, never self-reported
    request_id: str
    usage: dict[str, int]  # surfaced on EVERY call for the budget ledger
    stop_reason: str
    manifest_record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """A proxy-extracted, proxy-stamped judgment verdict."""

    gate_result: GateResult  # carries the judgment_binding + provenance stamp
    completion: CompletionResult


@dataclass
class _RetryPolicy:
    max_retries: int = 2
    backoff_base_s: float = 0.5
    sleeper: Callable[[float], None] = time.sleep
    delays: list[float] = field(default_factory=list)

    def sleep_before_retry(self, attempt: int) -> None:
        delay = self.backoff_base_s * (2**attempt)
        self.delays.append(delay)
        self.sleeper(delay)


class ModelProviderProxy:
    """Single egress, lineage observer, manifest writer, judgment stamper."""

    def __init__(
        self,
        transport: ProviderTransport | None = None,
        lineage_map: LineageMap | None = None,
        signer: ZoneBSigner | None = None,
        *,
        profile: Literal["self_judge", "decorrelated"] = "self_judge",
        tier_models: Mapping[str, str] | None = None,
        max_retries: int = 2,
        backoff_base_s: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        if profile not in ("self_judge", "decorrelated"):
            raise ValueError(f"unknown profile: {profile!r}")
        self._transport = transport or AnthropicTransport()
        self._lineage = lineage_map or LineageMap()
        self._signer = signer
        self._profile = profile
        # Tier -> pinned-model-id map. Defaults to the Anthropic lineage
        # (TIER_MODELS) but is injectable so an alternative backend (Codex/
        # Ollama-Gemma) can pin ITS real model ids — the flow is identical, the
        # ids the proxy pins/observes/drift-checks against just change with the
        # configured backend. Empty/unknown tiers still fail closed in complete().
        self._tier_models: dict[str, str] = (
            dict(tier_models) if tier_models is not None else dict(TIER_MODELS)
        )
        if not self._tier_models:
            raise ValueError("tier_models must define at least one tier")
        self._retry = _RetryPolicy(max_retries, backoff_base_s, sleeper)
        self._max_tokens = max_tokens
        self._manifest: list[dict[str, Any]] = []  # append-only
        self._drift_events: list[dict[str, Any]] = []

    # ---- read-only views ----------------------------------------------------

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def lineage_map_revision(self) -> str:
        return self._lineage.revision

    @property
    def tier_models(self) -> dict[str, str]:
        """The configured tier -> pinned-model-id map (read-only copy)."""
        return dict(self._tier_models)

    @property
    def manifest(self) -> tuple[dict[str, Any], ...]:
        """Append-only per-call record set (read-only view)."""
        return tuple(self._manifest)

    @property
    def drift_events(self) -> tuple[dict[str, Any], ...]:
        """Pinned-ID-drift alerts (arch §8): served model != pinned request."""
        return tuple(self._drift_events)

    # ---- decorrelation seam ---------------------------------------------------

    def assert_disjoint(
        self,
        reviewer_family: frozenset[str] | set[str],
        implementer_family: frozenset[str] | set[str],
    ) -> None:
        """Decorrelated profile: reviewer/implementer family sets must not
        intersect (arch §2/§8). In the self-judge profile this check is
        REPLACED by the recorded ``judgment_provenance="self"`` stamp
        (plan §0.2) — same lineage is the declared operating condition, so
        the call is a no-op there rather than a false guarantee.
        """
        if self._profile == "self_judge":
            return
        overlap = frozenset(reviewer_family) & frozenset(implementer_family)
        if overlap:
            raise FamilyOverlapError(
                f"reviewer/implementer served_family overlap {sorted(overlap)} — "
                "judgment node must not proceed in a correlated configuration"
            )

    # ---- the single egress ------------------------------------------------------

    def complete(
        self,
        node_id: str,
        tier: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        *,
        purpose: Purpose,
        schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """One credentialed model call: build → send (bounded retry) →
        observe lineage → record manifest → return text/JSON + usage."""
        if purpose not in _PURPOSES:
            raise ValueError(f"unknown purpose: {purpose!r}")
        if tier not in self._tier_models:
            raise ValueError(
                f"unknown tier: {tier!r} (expected one of {sorted(self._tier_models)})"
            )
        requested_model = self._tier_models[tier]
        request, sampling, sampling_note = self._build_request(
            requested_model, system_sop, messages, purpose, schema
        )

        response = self._send_with_retry(request)

        # Lineage is resolved from the proxy-OBSERVED served model id; an
        # unknown id fails closed inside the LineageMap (never guessed).
        served_family = self._lineage.resolve(response.served_model)

        drift = not response.served_model.startswith(requested_model)
        record: dict[str, Any] = {
            "node_id": node_id,
            "purpose": purpose,
            "tier": tier,
            "requested_model": requested_model,
            "served_model": response.served_model,
            "served_family": sorted(served_family),
            "lineage_map_revision": self._lineage.revision,
            "request_id": response.request_id,
            "usage": dict(response.usage),
            "cache_read_tokens": response.usage.get("cache_read_input_tokens", 0),
            "sampling": sampling,
            "drift": drift,
        }
        if sampling_note:
            record["sampling_note"] = sampling_note
        self._manifest.append(record)
        if drift:
            # Pinned-ID drift (arch §8): invalidates calibration baselines.
            self._drift_events.append(
                {
                    "node_id": node_id,
                    "requested_model": requested_model,
                    "served_model": response.served_model,
                    "request_id": response.request_id,
                }
            )

        parsed: dict[str, Any] | None = None
        if schema is not None:
            # Structured outputs: the first content block is text containing
            # valid JSON conforming to the schema — the constrained channel.
            parsed = json.loads(response.text)

        return CompletionResult(
            text=response.text,
            parsed=parsed,
            served_model=response.served_model,
            served_family=served_family,
            request_id=response.request_id,
            usage=dict(response.usage),
            stop_reason=response.stop_reason,
            manifest_record=record,
        )

    # ---- trusted judgment path ---------------------------------------------------

    def judge(
        self,
        node_id: str,
        subject_artifact_uri: str,
        state_sha: str,
        verdict_nonce: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        *,
        seq: Seq,
        tier: str = "T1",
        gate_name: str = "review",
    ) -> JudgmentResult:
        """Run a judgment call and return a proxy-stamped ``GateResult``.

        The verdict ``status`` is extracted by the PROXY from the
        grammar-constrained structured-output channel — a compromised adapter
        cannot have a free-form "approve" authentically signed (arch §2b).
        The binding is stamped here, outside the adapter, by the injected
        ZoneBSigner; the adapter never holds the key.
        """
        if self._signer is None:
            raise RuntimeError(
                "judge() requires an injected ZoneBSigner — the proxy is the "
                "only component (outside zone B) that stamps judgment bindings"
            )
        if seq.node_id != node_id:
            raise ValueError(
                f"seq.node_id {seq.node_id!r} != node_id {node_id!r}: the binding "
                "signs seq.node_id; a mismatched record would never verify coherently"
            )
        completion = self.complete(
            node_id, tier, system_sop, messages, purpose="judgment", schema=VERDICT_SCHEMA
        )
        verdict = completion.parsed
        if not isinstance(verdict, dict) or verdict.get("status") not in _STATUS_BY_VERDICT:
            raise VerdictChannelError(
                f"constrained verdict channel returned out-of-grammar payload: {verdict!r}"
            )
        provenance = (
            JudgmentProvenance.SELF
            if self._profile == "self_judge"
            else JudgmentProvenance.INDEPENDENT
        )
        unstamped = GateResult(
            name=gate_name,
            status=_STATUS_BY_VERDICT[verdict["status"]],
            decision_deterministic=False,
            execution_deterministic=False,
            verdict_class=VerdictClass.PERCEPTUAL,
            verdict_nonce=verdict_nonce,
            served_family=completion.served_family,  # proxy-observed set
            subject_artifact_uri=subject_artifact_uri,
            lineage_map_revision=self._lineage.revision,
            run_against=state_sha,
            findings=tuple(str(f) for f in verdict.get("findings", ())),
            judgment_provenance=provenance,
            seq=seq,
        )
        stamped = self._signer.with_judgment_binding(unstamped)
        return JudgmentResult(gate_result=stamped, completion=completion)

    # ---- internals ------------------------------------------------------------

    def _build_request(
        self,
        requested_model: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        purpose: Purpose,
        schema: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        """Returns (request, sampling-record, substitution-note).

        The stable SOP prefix is cached (`cache_control` on the system block,
        arch §8: prompt caching is the primary token-cost lever); volatile
        per-call content rides in `messages`, after the breakpoint.
        """
        request: dict[str, Any] = {
            "model": requested_model,
            "max_tokens": self._max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system_sop,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": list(messages),
        }
        sampling: dict[str, Any] = {}
        note: str | None = None
        output_config: dict[str, Any] = {}

        if purpose in ("gate", "judgment"):
            # Determinism policy (arch §8). Thinking is OMITTED on this path.
            if requested_model in _NO_SAMPLING_MODELS:
                # temperature/top_p/top_k are removed on this model (400 if
                # sent) — record the documented substitution instead.
                output_config["effort"] = "high"
                sampling = {"effort": "high"}
                note = (
                    f"sampling params removed on {requested_model}; "
                    'pinned output_config={"effort": "high"} in lieu of temperature 0'
                )
            else:
                request["temperature"] = 0
                sampling = {"temperature": 0}
        else:  # ideation — exploration helps (arch §8)
            if requested_model in _NO_SAMPLING_MODELS:
                request["thinking"] = {"type": "adaptive"}
                sampling = {"thinking": "adaptive"}

        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            request["output_config"] = output_config
        return request, sampling, note

    def _send_with_retry(self, request: dict[str, Any]) -> ProviderResponse:
        """Bounded retry/backoff, then fail closed.

        No model/family fallback ever happens here — unavailability of the
        pinned model is surfaced as ProxyUnavailable so judgment nodes
        pause-and-escalate instead of degrading silently (arch §2b/§8).
        """
        attempt = 0
        while True:
            try:
                return self._transport.send(request)
            except Exception as exc:
                if attempt >= self._retry.max_retries:
                    raise ProxyUnavailable(
                        f"provider transport failed after {attempt + 1} attempt(s); "
                        "judgment nodes must pause-and-escalate (no family fallback)"
                    ) from exc
                self._retry.sleep_before_retry(attempt)
                attempt += 1
