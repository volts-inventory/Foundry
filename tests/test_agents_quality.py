"""Assurance reviewer node factory: ``reviewer_critic`` (the verdict-only judge).

Drives the REAL node over a REAL :class:`ModelProviderProxy` backed by
:class:`MockTransport` + a dev :class:`ZoneBSigner` (no live API key, LEARNINGS
D1/D4). Asserts its ``judge()`` ``GateResult`` carries a valid binding +
``judgment_provenance`` and is ADMITTED by the real ``merge_gates`` /
``DevGateWriteVerifier`` — scripting both an approve and a request-changes
verdict, and showing a latched request-changes is NOT overridden by a later
approve (the negative latch). The quality-layer ``reviewer`` node is covered in
``test_subgraph_quality.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents.quality import make_reviewer_critic_node
from foundry.agents.quality._common import MissingVerdictNonceError, server_issued_nonce
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, verdict_json
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier

KEY = b"dev-zone-b-quality-key"
KEY_ID = "dev-key-quality"



# ---- harness ----------------------------------------------------------------


def make_runtime(transport: MockTransport) -> AgentRuntime:
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def config_for(
    run_id: str,
    *,
    step: int = 1,
    nonce: str | None = None,
    subject: str | None = None,
    state_sha: str | None = None,
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": run_id}
    if nonce is not None:
        configurable["verdict_nonce"] = nonce
    if subject is not None:
        configurable["subject_artifact_uri"] = subject
    if state_sha is not None:
        configurable["state_sha"] = state_sha
    return {"configurable": configurable, "metadata": {"langgraph_step": step}}


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def installed(records: GlobalRecords):
    verifier = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(verifier)
    try:
        yield records
    finally:
        token.var.reset(token)


# ---- 1. reviewer_critic: verdict admitted by the real merge_gates -----------


def test_reviewer_approve_verdict_admitted_with_binding(installed: GlobalRecords) -> None:
    transport = MockTransport(verdicts=["approve"])
    rt = make_runtime(transport)
    node = make_reviewer_critic_node(rt)

    cfg = config_for("run-A", nonce="N-approve", subject="cas://diff/1", state_sha="sha-1")
    writes = node({}, cfg)
    [verdict] = writes["gates"]
    assert isinstance(verdict, GateResult)
    assert verdict.status is GateStatus.PASS
    # The proxy stamped the binding + provenance; the agent never signed it.
    assert verdict.judgment_binding is not None
    assert verdict.judgment_provenance is JudgmentProvenance.SELF
    # H3: the nonce is the per-request derivation of the verdict's OWN identity
    # (base "N-approve" bound to node/subject/state), not the raw base — so each
    # verdict in a run gets a distinct single-use nonce.
    assert verdict.verdict_nonce == server_issued_nonce(
        cfg, node_id=verdict.seq.node_id,
        subject_uri=verdict.subject_artifact_uri or "", state_sha=verdict.run_against,
    )
    assert verdict.verdict_nonce != "N-approve"

    # ADMITTED by the real reducer + verifier (valid binding, fresh nonce, no latch).
    assert merge_gates([verdict], None) == [verdict]


def test_reviewer_request_changes_verdict_admitted(installed: GlobalRecords) -> None:
    transport = MockTransport(
        verdicts=[verdict_json("request-changes", ["weak assertion in test X"])]
    )
    rt = make_runtime(transport)
    node = make_reviewer_critic_node(rt)

    writes = node({}, config_for("run-B", nonce="N-rc", subject="cas://diff/2", state_sha="sha-2"))
    [verdict] = writes["gates"]
    assert verdict.status is GateStatus.FAIL
    assert verdict.findings == ("weak assertion in test X",)
    assert merge_gates([verdict], None) == [verdict]


def test_request_changes_not_overridden_by_later_approve(installed: GlobalRecords) -> None:
    """The negative latch: a request-changes is not superseded by a later approve."""
    records = installed

    # 1) request-changes verdict on the subject.
    rc_node = make_reviewer_critic_node(
        make_runtime(MockTransport(verdicts=["request-changes"]))
    )
    rc_cfg = config_for("run-C", step=3, nonce="N1", subject="cas://diff/3", state_sha="sha-3")
    rc = rc_node({}, rc_cfg)["gates"][0]
    assert rc.status is GateStatus.FAIL
    assert merge_gates([rc], None) == [rc]

    # The runner latches the negative verdict (its committed side effect).
    records.latch_negative_verdict(rc.latch_key())

    # 2) a LATER approve on the SAME subject/state (higher seq) — must NOT win.
    ap_rt = make_runtime(MockTransport(verdicts=["approve"]))
    ap_node = make_reviewer_critic_node(ap_rt)
    approve = ap_node(
        {}, config_for("run-C", step=9, nonce="N2", subject="cas://diff/3", state_sha="sha-3")
    )["gates"][0]
    assert approve.status is GateStatus.PASS
    assert approve.latch_key() == rc.latch_key()
    assert approve.seq > rc.seq

    # Latched: the later approve is DROPPED even though it carries a higher seq —
    # a request-changes is never overridden by re-emitting an approve. Only a
    # fresh server-issued nonce bound to a genuine re-review releases it.
    assert merge_gates([approve], None) == []
    assert merge_gates([rc], [approve]) == []  # both blocked once latched
    assert merge_gates([approve], [rc]) == []

    # Release via a fresh nonce → a fresh re-review approve is admitted again.
    records.release_negative_verdict(rc.latch_key(), "N-fresh")
    fresh_rt = make_runtime(MockTransport(verdicts=["approve"]))
    fresh_cfg = config_for(
        "run-C", step=12, nonce="N-fresh", subject="cas://diff/3", state_sha="sha-3"
    )
    fresh = make_reviewer_critic_node(fresh_rt)({}, fresh_cfg)["gates"][0]
    assert merge_gates([fresh], None) == [fresh]


def test_reviewer_requires_server_issued_nonce() -> None:
    node = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["approve"])))
    with pytest.raises(MissingVerdictNonceError):
        node({}, config_for("run-D"))  # no verdict_nonce on the config seam


def test_two_reviews_in_one_run_get_distinct_nonces(installed: GlobalRecords) -> None:
    """H3 issuance: the quality + assurance reviews of ONE run no longer share a
    nonce (they bind to distinct subject/state), so committing one can't reject the
    other."""
    node = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["approve"])))
    a = node({}, config_for("run-Z", nonce="N", subject="cas://diff/a", state_sha="s1"))["gates"][0]
    b = node({}, config_for("run-Z", nonce="N", subject="cas://tree/b", state_sha="s2"))["gates"][0]
    assert a.verdict_nonce != b.verdict_nonce            # same base "N", distinct nonces


def test_commit_step_consumes_nonce_so_replay_is_rejected(installed: GlobalRecords) -> None:
    """H3 (live): the trusted commit step consumes an admitted verdict's nonce, so a
    DIFFERENT verdict reusing that nonce is then rejected — the single-use replay
    defense that was previously inert (consume_nonce had no production caller)."""
    from foundry.graph.supervisor import _commit_judgment_ledger

    records = installed
    ap = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["approve"])))(
        {}, config_for("run-X", nonce="N0", subject="cas://diff/x", state_sha="sx")
    )["gates"][0]
    assert merge_gates([ap], None) == [ap]              # admitted (nonce not yet consumed)

    _commit_judgment_ledger([], [ap], records)          # the trusted write

    assert records.nonce_consumed_by_other(ap.verdict_nonce, "another-identity") is True
    # idempotent: the SAME verdict re-folding is not a replay
    assert records.nonce_consumed_by_other(ap.verdict_nonce, ap.content_identity()) is False


def test_commit_step_latches_request_changes_blocking_override(installed: GlobalRecords) -> None:
    """H3 (live): the trusted commit step latches a request-changes, so a later
    approve at the same key can't override it — the no-approve-override property the
    README promises, previously inert (latch_negative_verdict had no caller)."""
    from foundry.graph.supervisor import _commit_judgment_ledger

    records = installed
    rc = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["request-changes"])))(
        {}, config_for("run-Y", nonce="N1", subject="cas://diff/y", state_sha="sy")
    )["gates"][0]
    assert rc.status is GateStatus.FAIL

    _commit_judgment_ledger([], [rc], records)          # the trusted write latches it

    assert records.negative_verdict_latched(rc.latch_key()) is True
    later = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["approve"])))(
        {}, config_for("run-Y", step=9, nonce="N1", subject="cas://diff/y", state_sha="sy")
    )["gates"][0]
    assert later.latch_key() == rc.latch_key()
    assert merge_gates([later], None) == []             # override blocked


def test_latched_review_routes_as_request_changes(installed: GlobalRecords) -> None:
    """H3-1 guard (adversarial review): the negative latch's no-override property
    works by the latched verdict VANISHING from the channel, so the assurance router
    MUST fail-closed on the now-absent review. This pins that load-bearing coupling
    end-to-end: latch -> merge_gates drops it -> _review_requests_changes -> True."""
    from foundry.graph.subgraphs.assurance import _review_requests_changes
    from foundry.graph.supervisor import _commit_judgment_ledger

    records = installed
    rc = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["request-changes"])))(
        {}, config_for("run-L", nonce="N", subject="cas://diff/l", state_sha="sl")
    )["gates"][0]
    _commit_judgment_ledger([], [rc], records)          # latches it

    dropped = merge_gates([rc], None)                    # the latch drops it
    assert dropped == []
    assert _review_requests_changes({"gates": dropped}) is True   # router fail-closes

    # A later approve at the same key is dropped too, so the router still says "no".
    ap = make_reviewer_critic_node(make_runtime(MockTransport(verdicts=["approve"])))(
        {}, config_for("run-L", step=9, nonce="N", subject="cas://diff/l", state_sha="sl")
    )["gates"][0]
    assert _review_requests_changes({"gates": merge_gates([ap], None)}) is True


def test_reviewer_seq_node_id_matches_judge_node_id() -> None:
    """judge() rejects seq.node_id != node_id; the node wires them equal."""
    transport = MockTransport(verdicts=["approve"])
    rt = make_runtime(transport)
    node = make_reviewer_critic_node(rt)
    [verdict] = node({}, config_for("run-E", nonce="N", subject="s", state_sha="sha"))["gates"]
    assert verdict.seq.node_id == "reviewer_critic"
