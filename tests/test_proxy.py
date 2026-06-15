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
    FamilyOverlapError,
    LineageMap,
    ModelProviderProxy,
    ProviderResponse,
    ProxyUnavailable,
    UnknownModelLineageError,
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
    assert proxy.drift_events == (
        {
            "node_id": "gate_node",
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
