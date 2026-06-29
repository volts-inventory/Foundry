"""Model-provider proxy suite (arch §2b, plan §3.3) — FakeTransport only.

Covers: lineage resolution + fail-closed unknown ids; the trusted judgment
path (proxy-extracted verdict, binding stamped outside the adapter,
verifiable via DevGateWriteVerifier AND admitted by merge_gates); the
self-judge provenance stamp; verdict-flip-after-stamp rejection; pinned-ID
drift alerts; the T1 sampling substitution vs T2 temperature 0; bounded
retry then ProxyUnavailable (fail-closed, no family fallback); manifest
usage/cache metering; and the decorrelated-profile disjointness check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import (
    TIER_MODELS,
    DriftMode,
    DriftPolicy,
    FamilyOverlapError,
    LineageMap,
    ModelProviderProxy,
    PinnedIdDriftError,
    ProviderResponse,
    ProxyUnavailable,
    SamplingPolicy,
    UnknownModelLineageError,
    default_drift_policy,
    default_sampling_policy,
)
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-proxy-key"
KEY_ID = "dev-key-proxy"

USAGE = {
    "input_tokens": 1200,
    "output_tokens": 64,
    "cache_read_input_tokens": 900,
    "cache_creation_input_tokens": 100,
}

APPROVE_JSON = json.dumps({"status": "approve", "findings": []})


@dataclass
class FakeTransport:
    """Deterministic ProviderTransport: records requests, echoes the
    requested model as served (unless overridden), fails N times on demand."""

    text: str = APPROVE_JSON
    served_model: str | None = None  # None => echo request["model"]
    fail_times: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("provider unreachable")
        self.requests.append(request)
        return ProviderResponse(
            served_model=self.served_model or request["model"],
            request_id="req_fake_001",
            text=self.text,
            usage=dict(USAGE),
            stop_reason="end_turn",
        )


def make_seq(node_id: str = "review_node", *, step: int = 1) -> Seq:
    return Seq(
        run_id="run-1", emission_epoch=0, superstep_index=step, node_id=node_id, local=0
    )


def make_proxy(
    transport: FakeTransport | None = None,
    *,
    profile: str = "self_judge",
    sleeper=None,
    max_retries: int = 2,
) -> tuple[ModelProviderProxy, FakeTransport]:
    transport = transport or FakeTransport()
    proxy = ModelProviderProxy(
        transport,
        LineageMap(),
        ZoneBSigner(KEY, KEY_ID),
        profile=profile,  # type: ignore[arg-type]
        max_retries=max_retries,
        sleeper=sleeper or (lambda _s: None),
    )
    return proxy, transport


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def verifier(records: GlobalRecords) -> DevGateWriteVerifier:
    return DevGateWriteVerifier(KEY, KEY_ID, records)


@pytest.fixture
def installed(verifier: DevGateWriteVerifier):
    token = set_gate_write_verifier(verifier)
    try:
        yield verifier
    finally:
        token.var.reset(token)


# ---- 1. lineage map ---------------------------------------------------------


def test_lineage_resolves_all_tier_models_to_single_family():
    lineage = LineageMap()
    for model in TIER_MODELS.values():
        assert lineage.resolve(model) == frozenset({"anthropic-claude"})


def test_lineage_unknown_model_fails_closed():
    lineage = LineageMap()
    with pytest.raises(UnknownModelLineageError):
        lineage.resolve("gpt-7-router")
    with pytest.raises(UnknownModelLineageError):
        lineage.resolve("")


def test_lineage_revision_is_content_derived():
    a = LineageMap()
    b = LineageMap()  # identical content -> identical revision
    assert a.revision == b.revision
    c = LineageMap(
        [("claude-*", {"anthropic-claude"}), ("gemma-*", {"google-gemma"})]
    )
    assert c.revision != a.revision
    assert c.resolve("gemma-3") == frozenset({"google-gemma"})


# ---- 2. judgment path: stamped, verifiable, admitted --------------------------


def judge_once(proxy: ModelProviderProxy, *, nonce: str = "nonce-1"):
    return proxy.judge(
        "review_node",
        "cas://artifact/abc",
        "statesha-xyz",
        nonce,
        "REVIEW SOP",
        [{"role": "user", "content": "review this diff"}],
        seq=make_seq("review_node"),
    )


def test_judgment_binding_stamped_verifiable_and_admitted(verifier, installed):
    proxy, transport = make_proxy()
    result = judge_once(proxy)
    g = result.gate_result

    # Stamped OUTSIDE the adapter, over the proxy-observed family set.
    assert g.judgment_binding is not None
    assert g.served_family == frozenset({"anthropic-claude"})
    assert g.lineage_map_revision == proxy.lineage_map_revision
    assert g.status == GateStatus.PASS
    assert verifier.verify_judgment_binding(g)
    # Admitted by the real reducer under the installed verifier.
    assert merge_gates([g], None) == [g]

    # The verdict came from the grammar-constrained channel: the request
    # carried the json_schema format and no thinking config.
    request = transport.requests[0]
    fmt = request["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"]["status"]["enum"] == ["approve", "request-changes"]
    assert "thinking" not in request


def test_self_judge_profile_stamps_provenance_self():
    proxy, _ = make_proxy(profile="self_judge")
    assert judge_once(proxy).gate_result.judgment_provenance == JudgmentProvenance.SELF

    decorrelated, _ = make_proxy(profile="decorrelated")
    assert (
        judge_once(decorrelated).gate_result.judgment_provenance
        == JudgmentProvenance.INDEPENDENT
    )


def test_request_changes_verdict_maps_to_fail_with_findings():
    transport = FakeTransport(
        text=json.dumps({"status": "request-changes", "findings": ["missing tests"]})
    )
    proxy, _ = make_proxy(transport)
    g = judge_once(proxy).gate_result
    assert g.status == GateStatus.FAIL
    assert g.findings == ("missing tests",)


def test_verdict_flip_after_stamping_fails_verification(verifier, installed):
    proxy, _ = make_proxy()
    genuine = judge_once(proxy).gate_result
    flipped = genuine.model_copy(update={"status": GateStatus.FAIL})

    assert verifier.verify_judgment_binding(genuine)
    assert not verifier.verify_judgment_binding(flipped)
    assert merge_gates([genuine, flipped], None) == [genuine]
    assert merge_gates([flipped], None) == []


# ---- 3. pinned-ID drift -------------------------------------------------------


def test_drift_alert_recorded_when_served_model_differs():
    transport = FakeTransport(served_model="claude-sonnet-9-experimental")
    proxy, _ = make_proxy(transport)
    result = proxy.complete(
        "gate_node", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.manifest_record["drift"] is True
    assert result.manifest_record["drift_kind"] == "pinned-id"
    assert proxy.drift_events == (
        {
            "node_id": "gate_node",
            "purpose": "gate",
            "tier": "T2",
            "kind": "pinned-id",
            "requested_model": "claude-sonnet-4-6",
            "served_model": "claude-sonnet-9-experimental",
            "request_id": "req_fake_001",
        },
    )
    # Lineage is still resolved from the OBSERVED id (curated glob match).
    assert result.served_family == frozenset({"anthropic-claude"})


def test_no_drift_for_pinned_id_match():
    proxy, _ = make_proxy()
    result = proxy.complete(
        "gate_node", "T3", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.manifest_record["drift"] is False
    assert proxy.drift_events == ()


def test_versioned_suffix_of_pinned_id_is_not_drift():
    # A date/version-suffixed form of the SAME pinned id is the trusted case:
    # claude-sonnet-4-6-20250101 against the claude-sonnet-4-6 pin is no drift.
    transport = FakeTransport(served_model="claude-sonnet-4-6-20250101")
    proxy, _ = make_proxy(transport)
    result = proxy.complete(
        "gate_node", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.manifest_record["drift"] is False
    assert "drift_kind" not in result.manifest_record
    assert proxy.drift_events == ()


# ---- 3a. drift policy: advisory (default) vs fail-closed -----------------------


def test_advisory_is_the_default_and_records_without_raising():
    # DEFAULT policy is ADVISORY: a gate/judgment drift is recorded + surfaced
    # but never raises — the prior behaviour is preserved.
    assert default_drift_policy().mode is DriftMode.ADVISORY
    transport = FakeTransport(served_model="claude-sonnet-9-experimental")
    proxy, _ = make_proxy(transport)
    assert proxy.drift_policy.mode is DriftMode.ADVISORY
    result = proxy.complete(
        "gate_node", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.manifest_record["drift"] is True
    assert len(proxy.drift_events) == 1


def _fail_closed_proxy(transport: FakeTransport) -> ModelProviderProxy:
    return ModelProviderProxy(
        transport,
        LineageMap(),
        ZoneBSigner(KEY, KEY_ID),
        drift_policy=DriftPolicy(mode=DriftMode.FAIL_CLOSED),
        sleeper=lambda _s: None,
    )


def test_fail_closed_raises_on_gate_drift_but_still_records_it():
    transport = FakeTransport(served_model="claude-sonnet-9-experimental")
    proxy = _fail_closed_proxy(transport)
    with pytest.raises(PinnedIdDriftError) as exc:
        proxy.complete(
            "gate_node", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
        )
    # The drift is recorded BEFORE the raise — nothing is swallowed (the alert
    # surface + manifest still see it; the caller pause-and-escalates).
    assert exc.value.event["kind"] == "pinned-id"
    assert len(proxy.drift_events) == 1
    assert len(proxy.manifest) == 1
    assert proxy.manifest[0]["drift"] is True


def test_fail_closed_raises_on_judgment_drift_via_judge():
    # The trusted judgment path inherits the fail-closed behaviour: a drifted
    # weight set must not be stamped into a verdict binding.
    transport = FakeTransport(served_model="claude-opus-9-experimental")
    proxy = _fail_closed_proxy(transport)
    with pytest.raises(PinnedIdDriftError):
        proxy.judge(
            "review_node", "cas://artifact", "state-sha", "nonce-1", "SOP",
            [{"role": "user", "content": "review"}],
            seq=make_seq("review_node"), tier="T1",
        )


def test_fail_closed_stays_advisory_for_ideation():
    # Ideation is exploratory — a fail-closed policy guards gate/judgment only,
    # so an ideation drift is recorded but does not raise.
    transport = FakeTransport(served_model="claude-opus-9-experimental")
    proxy = _fail_closed_proxy(transport)
    result = proxy.complete(
        "arch", "T1", "SOP", [{"role": "user", "content": "ideate"}], purpose="ideation"
    )
    assert result.manifest_record["drift"] is True
    assert len(proxy.drift_events) == 1


# ---- 3b. tier-mismatch: same family, wrong tier id -----------------------------


def _gemma_proxy(transport: FakeTransport, **kw: Any) -> ModelProviderProxy:
    # An Ollama/Gemma backend with two pinned tiers in ONE lineage family.
    return ModelProviderProxy(
        transport,
        LineageMap([("gemma*", {"google-gemma"})]),
        ZoneBSigner(KEY, KEY_ID),
        tier_models={"T1": "gemma3:27b", "T2": "gemma3:12b"},
        sleeper=lambda _s: None,
        **kw,
    )


def test_tier_mismatch_same_family_is_surfaced_as_drift():
    # A smaller tag (gemma3:12b) served against the larger T1 pin (gemma3:27b)
    # resolves to the SAME family — but the served capability silently degraded,
    # so it is flagged drift with kind tier-mismatch (not family-resolved away).
    transport = FakeTransport(served_model="gemma3:12b")
    proxy = _gemma_proxy(transport)
    result = proxy.complete(
        "gate_node", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.served_family == frozenset({"google-gemma"})  # same family
    assert result.manifest_record["drift"] is True
    assert result.manifest_record["drift_kind"] == "tier-mismatch"
    assert proxy.drift_events[0]["kind"] == "tier-mismatch"
    assert proxy.drift_events[0]["served_model"] == "gemma3:12b"


def test_tier_mismatch_fails_closed_under_fail_closed_policy():
    transport = FakeTransport(served_model="gemma3:12b")
    proxy = _gemma_proxy(transport, drift_policy=DriftPolicy(mode=DriftMode.FAIL_CLOSED))
    with pytest.raises(PinnedIdDriftError) as exc:
        proxy.complete(
            "gate_node", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
        )
    assert exc.value.event["kind"] == "tier-mismatch"


# ---- 4. determinism / sampling policy per tier ---------------------------------


def test_t1_gate_has_no_temperature_t2_gate_has_temperature_zero():
    proxy, transport = make_proxy()
    proxy.complete("n1", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    proxy.complete("n2", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    t1_req, t2_req = transport.requests

    # Sampling params are REMOVED on claude-opus-4-8 (400 if sent): the proxy
    # substitutes pinned effort and records the substitution in the manifest.
    assert t1_req["model"] == "claude-opus-4-8"
    assert all(k not in t1_req for k in ("temperature", "top_p", "top_k"))
    assert t1_req["output_config"] == {"effort": "high"}
    t1_rec, t2_rec = proxy.manifest
    assert t1_rec["sampling"] == {"effort": "high"}
    assert "sampling_note" in t1_rec

    assert t2_req["model"] == "claude-sonnet-4-6"
    assert t2_req["temperature"] == 0
    assert t2_rec["sampling"] == {"temperature": 0}
    assert "sampling_note" not in t2_rec

    # Stable SOP prefix is cached; gate calls omit thinking.
    for req in (t1_req, t2_req):
        assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "thinking" not in req


def test_t1_ideation_uses_adaptive_thinking_not_temperature():
    proxy, transport = make_proxy()
    proxy.complete(
        "arch", "T1", "SOP", [{"role": "user", "content": "ideate"}], purpose="ideation"
    )
    req = transport.requests[0]
    assert req["thinking"] == {"type": "adaptive"}
    assert "temperature" not in req
    assert "output_config" not in req  # no schema, no pinned effort on ideation


# ---- 4b. sampling policy: backend/model-aware determinism control --------------


def test_sampling_policy_routes_effort_vs_temperature_by_model_id():
    policy = default_sampling_policy()
    # Reasoning / no-sampling models => effort (temperature is rejected there).
    for effort_id in ("claude-opus-4-8", "gpt-5", "gpt-5-codex", "o3-mini", "codex-mini"):
        assert policy.takes_effort(effort_id), effort_id
    # Models that honour options.temperature => temperature 0.
    for temp_id in ("claude-sonnet-4-6", "claude-haiku-4-5", "gemma3:27b", "gemma3:4b"):
        assert not policy.takes_effort(temp_id), temp_id


def test_anthropic_opus_gate_uses_effort_sonnet_pins_temperature_zero():
    # The DEFAULT Anthropic backend stays backward-compatible: opus (T1, a
    # reasoning model) gets effort; sonnet (T2) pins temperature 0.
    proxy, transport = make_proxy()
    proxy.complete("n1", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    proxy.complete("n2", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    t1_req, t2_req = transport.requests
    assert t1_req["output_config"] == {"effort": "high"}
    assert "temperature" not in t1_req
    assert t2_req["temperature"] == 0
    assert "output_config" not in t2_req


def test_codex_tier_model_gate_pins_effort_not_temperature_via_policy():
    # With a Codex tier map, a T1 (gpt-5-codex) AND a T2 (gpt-5) gate call both
    # pin effort — proving the decision follows the configured backend's model
    # ids, not a hardcoded Anthropic id.
    transport = FakeTransport(served_model="gpt-5-codex")
    proxy = ModelProviderProxy(
        transport,
        LineageMap([("gpt-*", {"openai-gpt"})]),
        ZoneBSigner(KEY, KEY_ID),
        tier_models={"T1": "gpt-5-codex", "T2": "gpt-5"},
        sleeper=lambda _s: None,
    )
    for tier in ("T1", "T2"):
        proxy.complete(f"n-{tier}", tier, "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    for req in transport.requests:
        assert req["output_config"] == {"effort": "high"}
        assert "temperature" not in req
    for rec in proxy.manifest:
        assert rec["sampling"] == {"effort": "high"}
        assert "sampling_note" in rec


def test_sampling_policy_is_injectable_and_overrides_default():
    # An operator can pin an exact policy for the backend they run.
    transport = FakeTransport()
    proxy = ModelProviderProxy(
        transport,
        LineageMap(),
        sampling_policy=SamplingPolicy(effort_model_globs=(), effort="medium"),
        sleeper=lambda _s: None,
    )
    # Empty effort-glob set => even opus is treated as a sampling model here.
    proxy.complete("n", "T1", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    assert transport.requests[0]["temperature"] == 0
    assert proxy.sampling_policy.effort == "medium"


# ---- 5. unavailability: bounded retry, then fail closed -------------------------


def test_retry_then_proxy_unavailable():
    delays: list[float] = []
    transport = FakeTransport(fail_times=99)
    proxy, _ = make_proxy(transport, sleeper=delays.append, max_retries=2)
    with pytest.raises(ProxyUnavailable):
        proxy.complete("n", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    assert transport.attempts == 3  # initial + 2 bounded retries, then escalate
    assert delays == [0.5, 1.0]  # injected sleeper saw the backoff schedule
    assert proxy.manifest == ()  # no manifest record for a call that never served


def test_transient_failure_recovers_within_retry_budget():
    delays: list[float] = []
    transport = FakeTransport(fail_times=1)
    proxy, _ = make_proxy(transport, sleeper=delays.append)
    result = proxy.complete(
        "n", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.text == APPROVE_JSON
    assert transport.attempts == 2
    assert delays == [0.5]


# ---- 5b. provider rate-limiting: separate budget, honors Retry-After ------------


class _FakeHeaders:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = mapping

    def get(self, key: str, default: Any = None) -> Any:
        return self._m.get(key, default)


class _RateLimitError(Exception):
    """An SDK-shaped 429 with an optional Retry-After header."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("429 Too Many Requests")
        self.status_code = 429
        if retry_after is not None:
            self.response = type("R", (), {"headers": _FakeHeaders(
                {"retry-after": str(retry_after)})})()


@dataclass
class _RateLimitTransport:
    """Raises a 429 ``rl_times`` times, then serves normally."""

    rl_times: int = 0
    retry_after: float | None = None
    text: str = APPROVE_JSON
    attempts: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.attempts += 1
        if self.rl_times > 0:
            self.rl_times -= 1
            raise _RateLimitError(retry_after=self.retry_after)
        self.requests.append(request)
        return ProviderResponse(
            served_model=request["model"], request_id="req_rl",
            text=self.text, usage=dict(USAGE), stop_reason="end_turn",
        )


def _rl_proxy(transport: Any, *, sleeper: Any, rl_retries: int = 6,
              rl_base: float = 2.0) -> ModelProviderProxy:
    return ModelProviderProxy(
        transport, LineageMap(), ZoneBSigner(KEY, KEY_ID), profile="self_judge",
        sleeper=sleeper, rate_limit_max_retries=rl_retries,
        rate_limit_backoff_base_s=rl_base,
    )


def test_rate_limit_recovers_and_honors_retry_after():
    delays: list[float] = []
    transport = _RateLimitTransport(rl_times=2, retry_after=7.0)
    proxy = _rl_proxy(transport, sleeper=delays.append)
    result = proxy.complete(
        "n", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    assert result.text == APPROVE_JSON
    assert transport.attempts == 3            # 2 throttled + 1 success
    assert delays == [7.0, 7.0]               # honored Retry-After, not 0.5/1.0


def test_rate_limit_uses_separate_more_generous_budget():
    # A 429 storm must NOT escalate after the generic 2-retry budget; it rides the
    # rate-limit budget (here 4) with its own exponential backoff.
    delays: list[float] = []
    transport = _RateLimitTransport(rl_times=99)
    proxy = _rl_proxy(transport, sleeper=delays.append, rl_retries=4, rl_base=2.0)
    with pytest.raises(ProxyUnavailable):
        proxy.complete("n", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    assert transport.attempts == 5            # initial + 4 rate-limit waits
    assert delays == [2.0, 4.0, 8.0, 16.0]    # exponential, capped at 60s


def test_rate_limiter_paces_requests_per_minute():
    from foundry.proxy.model_provider_proxy import _RateLimiter
    waits: list[float] = []
    clock = [0.0]
    rl = _RateLimiter(max_rpm=60, sleeper=waits.append, clock=lambda: clock[0])
    with rl.slot():
        pass                                  # first call: no wait
    with rl.slot():
        pass                                  # clock hasn't moved → must space by 1s
    assert waits == [1.0]


# ---- 6. manifest: usage + cache metering ---------------------------------------


def test_manifest_records_usage_and_cache_tokens():
    proxy, _ = make_proxy()
    result = proxy.complete(
        "node-7", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="gate"
    )
    (record,) = proxy.manifest
    assert record["node_id"] == "node-7"
    assert record["purpose"] == "gate"
    assert record["requested_model"] == "claude-sonnet-4-6"
    assert record["served_model"] == "claude-sonnet-4-6"
    assert record["served_family"] == ["anthropic-claude"]
    assert record["lineage_map_revision"] == proxy.lineage_map_revision
    assert record["request_id"] == "req_fake_001"
    assert record["usage"] == USAGE
    assert record["cache_read_tokens"] == 900
    # Usage is surfaced on the result too — the budget-ledger feed.
    assert result.usage == USAGE


def test_manifest_is_append_only_across_calls():
    proxy, _ = make_proxy()
    for i in range(3):
        proxy.complete(f"n{i}", "T3", "SOP", [{"role": "user", "content": "x"}], purpose="gate")
    assert [r["node_id"] for r in proxy.manifest] == ["n0", "n1", "n2"]


# ---- 7. decorrelation seam ------------------------------------------------------


def test_assert_disjoint_raises_on_overlap_in_decorrelated_profile():
    proxy, _ = make_proxy(profile="decorrelated")
    with pytest.raises(FamilyOverlapError):
        proxy.assert_disjoint(
            frozenset({"anthropic-claude"}), frozenset({"anthropic-claude", "x"})
        )
    # Genuinely disjoint sets pass.
    proxy.assert_disjoint(frozenset({"anthropic-claude"}), frozenset({"google-gemma"}))


def test_self_judge_profile_replaces_disjointness_with_stamp():
    proxy, _ = make_proxy(profile="self_judge")
    # No exception: the check is replaced by the recorded SELF stamp (plan §0.2).
    proxy.assert_disjoint(frozenset({"anthropic-claude"}), frozenset({"anthropic-claude"}))
    assert judge_once(proxy).gate_result.judgment_provenance == JudgmentProvenance.SELF
