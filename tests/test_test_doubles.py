"""Deterministic test-double suite (plan §0.3) — the seam every later wave injects.

Drives the REAL :class:`ModelProviderProxy` over :class:`MockTransport` (no live
API key) and the REAL :class:`StubAdapter` over a real :class:`Sandbox` (no
``claude -p`` binary). Asserts the proxy's synthetic request ids are stable,
scripted responses route by substring, served models mirror the requested tier,
the verdict channel serves valid schema JSON, and that the stub adapter yields a
``cas://`` diff, advisory-only triage, and a deterministic snapshot digest.
"""

from __future__ import annotations

import json
from pathlib import Path

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
)
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

KEY = b"dev-zone-b-doubles-key"
KEY_ID = "dev-key-doubles"


def make_proxy(transport: MockTransport) -> ModelProviderProxy:
    return ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))


def make_seq(node_id: str = "review_node") -> Seq:
    return Seq("run-1", 0, 1, node_id, 0)


# ---- 1. MockTransport drives proxy.complete ----------------------------------


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
