"""The shared review verdict (:func:`reviewer_critic.review`) as a verdict-only node.

Drives the REAL ``review()`` call over a REAL :class:`ModelProviderProxy` backed by
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
from foundry.agents.quality._common import MissingVerdictNonceError
from foundry.agents.quality.reviewer_critic import review
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateStatus
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier

KEY = b"dev-zone-b-quality-key"
KEY_ID = "dev-key-quality"



# ---- harness ----------------------------------------------------------------


def make_reviewer_critic_node(rt: AgentRuntime) -> Any:
    """A verdict-only node over the shared ``review()`` call (the quality reviewer
    wraps the same call with its test bookkeeping)."""

    def _node(state: Any, config: RunnableConfig) -> dict[str, Any]:
        result, note = review(rt, state, config, node_id="reviewer_critic", role="Reviewer")
        return {"gates": [result.gate_result], "agent_journal": [note]}

    return _node


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


def test_read_criteria_takes_the_newest_seq_per_id_not_list_order() -> None:
    from foundry.agents._common import emit_artifact
    from foundry.agents.quality._common import ACCEPTANCE_CRITERION_KIND, read_criteria
    from foundry.schemas.product import AcceptanceCriterion
    from foundry.state.seq import Seq

    def _c(text: str, epoch: int) -> AcceptanceCriterion:
        return AcceptanceCriterion(id="AC-1", text=text, seq=Seq("r", epoch, 1, "planner", 0))

    new, old = _c("revised", 2), _c("stale", 1)
    handles = [emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "planner", c.seq) for c in (new, old)]
    bodies = {h.uri: c for h, c in zip(handles, (new, old), strict=True)}

    [current] = read_criteria({"artifacts": handles}, lambda h: bodies[h.uri])
    assert current.text == "revised"


def _diff_state(store: Any, diffs: dict[str, str]) -> dict[str, Any]:
    from foundry.schemas.base import Artifact
    from foundry.schemas.implementation import FeatureDiff
    from foundry.state.seq import Seq

    artifacts = []
    for i, (task_id, text) in enumerate(diffs.items()):
        seq = Seq("run-R", 1, 1, "engineering", i)
        uri = f"cas://feature_diff/key-{task_id}"
        store.put_body_at(
            uri,
            FeatureDiff(
                diff_uri=store.put_blob(text), task_id=task_id,
                branch=f"task/{task_id}", base_tree_sha="base", seq=seq,
            ),
        )
        artifacts.append(Artifact(kind="feature_diff", uri=uri, produced_by="engineering",
                                  task_id=task_id, seq=seq))
    return {"artifacts": artifacts}


def _review_request_text(transport: MockTransport) -> str:
    [request] = transport.requests
    return str(request)


def test_review_request_carries_the_diff_text() -> None:
    """The judge cannot dereference cas:// — the request must inline the diffs."""
    from foundry.durability.artifact_store import ArtifactStore

    transport = MockTransport(verdicts=["approve"])
    rt = make_runtime(transport)
    rt.cas = ArtifactStore()
    state = _diff_state(rt.cas, {
        "T-A": "+++ b/app.py\n+def tip(total): return total * 0.15\n",
        "T-B": "+++ b/test_app.py\n+assert tip(100) == 15\n",
    })
    review(rt, state, config_for("run-R", nonce="N"), node_id="reviewer", role="Reviewer")
    text = _review_request_text(transport)
    assert "def tip(total): return total * 0.15" in text
    assert "assert tip(100) == 15" in text
    assert text.index("task T-A") < text.index("task T-B")


def test_review_request_truncates_and_names_unresolvable_diffs() -> None:
    from foundry.agents.quality import reviewer_critic
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.schemas.base import Artifact
    from foundry.state.seq import Seq

    transport = MockTransport(verdicts=["approve"])
    rt = make_runtime(transport)
    rt.cas = ArtifactStore()
    state = _diff_state(rt.cas, {"T-BIG": "+x\n" * reviewer_critic._DIFF_CHAR_CAP})
    state["artifacts"].append(Artifact(kind="feature_diff", uri="cas://feature_diff/gone",
                                       produced_by="engineering", task_id="T-GONE",
                                       seq=Seq("run-R", 1, 2, "engineering", 0)))
    review(rt, state, config_for("run-R", nonce="N"), node_id="reviewer", role="Reviewer")
    text = _review_request_text(transport)
    assert "TRUNCATED" in text
    assert "cas://feature_diff/gone" in text


def test_review_request_says_when_no_diff_is_resolvable() -> None:
    transport = MockTransport(verdicts=["approve"])
    rt = make_runtime(transport)
    review(rt, {}, config_for("run-R", nonce="N", subject="cas://diff/x"),
           node_id="reviewer", role="Reviewer")
    assert "no diff text to review" in _review_request_text(transport)
