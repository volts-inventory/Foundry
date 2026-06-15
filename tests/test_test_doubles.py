"""Deterministic test-double suite (plan §0.3) — the seam every later wave injects.

Drives the REAL :class:`ModelProviderProxy` over :class:`MockTransport` (no live
API key) and the REAL :class:`StubAdapter` over a real :class:`Sandbox` (no
``claude -p`` binary). Asserts the proxy resolves a served family + populated
usage on ``complete``, returns a verifiable proxy-stamped binding on ``judge``
(both approve and request-changes scripted), and that the stub adapter yields a
``cas://`` diff, advisory-only triage, and a deterministic snapshot digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pytest

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
    WorkerAdapter,
)
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.model_provider_proxy import ProviderResponse
from foundry.proxy.test_doubles import (
    DEFAULT_USAGE,
    MockTransport,
    verdict_json,
)
from foundry.schemas.gates import GateStatus, GateTriage, JudgmentProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-doubles-key"
KEY_ID = "dev-key-doubles"


def make_proxy(transport: MockTransport) -> ModelProviderProxy:
    return ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))


def make_seq(node_id: str = "review_node") -> Seq:
    return Seq("run-1", 0, 1, node_id, 0)


@pytest.fixture
def installed_verifier():
    verifier = DevGateWriteVerifier(KEY, KEY_ID, GlobalRecords())
    token = set_gate_write_verifier(verifier)
    try:
        yield verifier
    finally:
        token.var.reset(token)


# ---- 0. Structural Protocol conformance (catch renamed send/run) -------------


@runtime_checkable
class _SendProtocol(Protocol):
    """Local runtime_checkable mirror of ProviderTransport's structural shape.

    ProviderTransport itself is a plain (non-runtime_checkable) Protocol in the
    proxy module, so isinstance() against it raises TypeError. This mirror has the
    same `send` member name, so a renamed MockTransport.send fails the check.
    """

    def send(self, request: dict[str, Any]) -> ProviderResponse: ...


def test_doubles_conform_to_their_runtime_checkable_protocols():
    # Structural checks: a renamed send/run would break these.
    assert isinstance(MockTransport(), _SendProtocol)  # mirrors ProviderTransport.send
    assert isinstance(StubAdapter(), WorkerAdapter)  # runtime_checkable in adapter_contract


# ---- 1. MockTransport drives proxy.complete ----------------------------------


def test_complete_resolves_family_and_populates_usage():
    proxy = make_proxy(MockTransport(default_text="hello from the stub"))
    result = proxy.complete(
        "node-1", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="ideation"
    )
    # Served family is resolved by the proxy (never empty), usage is surfaced.
    assert result.served_family
    assert result.served_family == frozenset({"anthropic-claude"})
    assert result.text == "hello from the stub"
    assert result.usage == DEFAULT_USAGE
    assert result.stop_reason == "end_turn"
    # served_model mirrors the requested tier's pinned model (R1-02: no spurious
    # drift) — a real TIER_MODELS value the LineageMap resolved.
    assert result.served_model == "claude-sonnet-4-6"


def test_request_id_is_stable_in_process():
    # In-process stability smoke test: two transports, two proxies, same request
    # -> identical synthetic id. L7's CROSS-process guarantee comes from deriving
    # the id via content_digest (not builtin hash(), which is seed-randomized per
    # process); this test only checks the within-process stability that follows.
    args = ("node-1", "T2", "SOP", [{"role": "user", "content": "x"}])
    a = make_proxy(MockTransport()).complete(*args, purpose="ideation")
    b = make_proxy(MockTransport()).complete(*args, purpose="ideation")
    assert a.request_id == b.request_id
    assert a.request_id.startswith("req_mock_")


def test_scripted_responses_keyed_by_substring():
    transport = MockTransport.scripted(
        {"REVIEW THE DIFF": "scripted-review-body"},
        default_text="fallback",
    )
    proxy = make_proxy(transport)
    hit = proxy.complete(
        "n", "T2", "REVIEW THE DIFF please", [{"role": "user", "content": "x"}],
        purpose="ideation",
    )
    miss = proxy.complete(
        "n", "T2", "unrelated SOP", [{"role": "user", "content": "x"}],
        purpose="ideation",
    )
    assert hit.text == "scripted-review-body"
    assert miss.text == "fallback"


def test_completion_queue_consumed_fifo():
    transport = MockTransport(completions=["first", "second"], default_text="default")
    proxy = make_proxy(transport)
    msgs = [{"role": "user", "content": "x"}]
    outs = [
        proxy.complete("n", "T2", "SOP", msgs, purpose="ideation").text for _ in range(3)
    ]
    assert outs == ["first", "second", "default"]


def test_served_model_mirrors_requested_tier_no_spurious_drift():
    # R1-02: T2/T3 calls report the requested tier's model -> no pinned-ID drift.
    proxy = make_proxy(MockTransport(default_text="x"))
    for tier, model in (("T1", "claude-opus-4-8"), ("T2", "claude-sonnet-4-6"),
                        ("T3", "claude-haiku-4-5")):
        result = proxy.complete(
            "n", tier, "SOP", [{"role": "user", "content": "x"}], purpose="ideation"
        )
        assert result.served_model == model
    assert proxy.drift_events == ()


def test_served_model_override_forces_drift():
    # R1-02: an explicit override still forces a drift scenario for tests.
    proxy = make_proxy(MockTransport(default_text="x", served_model="claude-opus-4-8"))
    proxy.complete("n", "T2", "SOP", [{"role": "user", "content": "x"}], purpose="ideation")
    assert len(proxy.drift_events) == 1
    assert proxy.drift_events[0]["served_model"] == "claude-opus-4-8"


# ---- 2. MockTransport drives the trusted judgment path -----------------------


def judge(proxy: ModelProviderProxy, *, nonce: str = "nonce-1"):
    return proxy.judge(
        "review_node",
        "cas://artifact/abc",
        "statesha-xyz",
        nonce,
        "REVIEW SOP",
        [{"role": "user", "content": "review this diff"}],
        seq=make_seq(),
    )


def test_judge_default_approve_is_stamped_verifiable_admitted(installed_verifier):
    proxy = make_proxy(MockTransport())  # default_verdict="approve"
    g = judge(proxy).gate_result

    assert g.status == GateStatus.PASS
    assert g.judgment_provenance == JudgmentProvenance.SELF
    assert g.served_family == frozenset({"anthropic-claude"})
    assert g.judgment_binding is not None
    assert g.lineage_map_revision == proxy.lineage_map_revision
    # Binding verifies under the dev signer AND is admitted by the real reducer.
    assert installed_verifier.verify_judgment_binding(g)
    assert merge_gates([g], None) == [g]


def test_judge_scripted_request_changes_maps_to_fail(installed_verifier):
    transport = MockTransport(
        verdicts=[verdict_json("request-changes", ["missing tests"])]
    )
    proxy = make_proxy(transport)
    g = judge(proxy).gate_result
    assert g.status == GateStatus.FAIL
    assert g.findings == ("missing tests",)
    assert installed_verifier.verify_judgment_binding(g)


def test_judge_verdict_queue_sequences_approve_then_request_changes():
    transport = MockTransport(verdicts=["approve", "request-changes"])
    proxy = make_proxy(transport)
    first = judge(proxy, nonce="n1").gate_result
    second = judge(proxy, nonce="n2").gate_result
    assert first.status == GateStatus.PASS
    assert second.status == GateStatus.FAIL


def test_judge_default_verdict_overridable_to_request_changes():
    proxy = make_proxy(MockTransport(default_verdict="request-changes"))
    assert judge(proxy).gate_result.status == GateStatus.FAIL


def test_verdict_channel_receives_valid_json_under_schema():
    transport = MockTransport()
    proxy = make_proxy(transport)
    result = judge(proxy)
    # The proxy json.loads the verdict channel — it must be valid JSON conforming
    # to the verdict grammar, and the request must carry the json_schema format.
    request = transport.requests[0]
    assert request["output_config"]["format"]["type"] == "json_schema"
    # The completion text the transport served on that channel is valid verdict JSON.
    served = json.loads(result.completion.text)
    assert served["status"] == "approve"
    assert "findings" in served


# ---- 3. StubAdapter -----------------------------------------------------------


def _packet(worktree: Path, task_id: str = "T-1") -> TaskPacket:
    return TaskPacket(
        task={"id": task_id, "title": "do a thing"},
        worktree_path=str(worktree),
        turn_budget=1,
        cost_budget_microusd=0,
    )


def test_stub_run_yields_cas_diff_advisory_triage_and_snapshot(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "README.md").write_text("seed\n", encoding="utf-8")
    sandbox = Sandbox(source)
    try:
        result = StubAdapter().run(_packet(sandbox.worktree), sandbox)
        assert isinstance(result, AdapterResult)
        # cas:// diff handle round-trips through the blob store.
        assert result.diff_uri.startswith("cas://sha256/")
        assert sandbox.get_blob(result.diff_uri)
        # Advisory triage ONLY — GateTriage, never a GateResult (L5).
        assert result.triage
        assert all(isinstance(t, GateTriage) for t in result.triage)
        assert result.turns_used == 1
        assert result.snapshot_digest
    finally:
        sandbox.destroy()


def test_stub_run_is_deterministic_same_inputs_same_digest(tmp_path):
    digests: list[str] = []
    diffs: list[str] = []
    for i in range(2):
        source = tmp_path / f"src{i}"
        source.mkdir()
        (source / "README.md").write_text("seed\n", encoding="utf-8")
        sandbox = Sandbox(source)
        try:
            result = StubAdapter().run(_packet(sandbox.worktree), sandbox)
            digests.append(result.snapshot_digest)
            diffs.append(result.diff_uri)
        finally:
            sandbox.destroy()
    # Identical inputs -> identical content-addressed digest + diff handle.
    assert digests[0] == digests[1]
    assert diffs[0] == diffs[1]


def test_stub_run_distinct_tasks_diverge(tmp_path):
    # Symmetric two-sandbox comparison: distinct task bodies -> distinct diffs.
    def run_task(name: str, task_id: str) -> str:
        source = tmp_path / name
        source.mkdir()
        (source / "README.md").write_text("seed\n", encoding="utf-8")
        sandbox = Sandbox(source)
        try:
            return StubAdapter().run(_packet(sandbox.worktree, task_id), sandbox).diff_uri
        finally:
            sandbox.destroy()

    a_diff = run_task("src_a", "T-A")
    b_diff = run_task("src_b", "T-B")
    assert a_diff != b_diff
