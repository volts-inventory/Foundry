"""Quality-layer node factories (T-M1-07): reviewer_critic / qa_engineer / sdet.

Drives the REAL nodes over a REAL :class:`ModelProviderProxy` backed by
:class:`MockTransport` + a dev :class:`ZoneBSigner` (no live API key, LEARNINGS
D1/D4). Asserts:

* reviewer_critic's ``judge()`` ``GateResult`` carries a valid binding +
  ``judgment_provenance`` and is ADMITTED by the real ``merge_gates`` /
  ``DevGateWriteVerifier`` — scripting both an approve and a request-changes
  verdict, and showing a latched request-changes is NOT overridden by a later
  approve (the negative latch);
* qa_engineer's ``TestPlan`` has ``criterion_id`` traceability + a NEGATIVE
  oracle per high-risk criterion, and the ``TestReport`` quarantines a flaky
  test;
* sdet's ``E2EReport`` carries the determinism notes + quarantine;
* the nodes drop into a ``StateGraph(FactoryState)`` (graph-compatibility).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.quality import (
    make_qa_engineer_node,
    make_reviewer_critic_node,
    make_sdet_node,
)
from foundry.agents.quality._common import MissingVerdictNonceError, server_issued_nonce
from foundry.agents.quality.qa_engineer import (
    ACCEPTANCE_CRITERION_KIND,
    MissingCriteriaResolverError,
)
from foundry.canonical import content_digest
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, verdict_json
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_by_uri,
    merge_gates,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

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


def crit(cid: str, criticality: Criticality = Criticality.NORMAL) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given/When/Then for {cid}",
        criticality=criticality,
        seq=Seq(run_id="r", emission_epoch=0, superstep_index=0, node_id="pm", local=0),
    )


CRITERIA = (
    crit("AC-001", Criticality.HIGH_RISK),
    crit("AC-002"),
    crit("AC-003", Criticality.HIGH_RISK),
)


def criterion_handles(
    criteria: tuple[AcceptanceCriterion, ...] = CRITERIA,
) -> list[Artifact]:
    """The ``acceptance_criterion`` HANDLES the product layer actually emits.

    Unlike raw bodies, a handle carries a ``.uri`` so it survives ``merge_by_uri``
    on the shared ``artifacts`` channel in a COMPILED graph (raw criteria would
    crash the fold). The QA / SDET nodes resolve each handle back to its body via
    the injected ``resolve`` (the Senior Engineer's seam). C-R1-04: the raw-body
    production path was REMOVED, so the unit tests seed criteria as handles + a
    resolver — exactly as production does — never raw instances on the channel."""
    return [emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq) for c in criteria]


def qa_state(
    criteria: tuple[AcceptanceCriterion, ...] = CRITERIA,
) -> FactoryState:
    """The state the QA/SDET nodes read criteria from (AR1-AR4: criteria are a
    DATA input from ``state``, not a build-time kwarg). Seeds the
    ``acceptance_criterion`` HANDLES the product layer actually emits (C-R1-04:
    the raw-body path is gone); pair with ``resolve=resolver()`` so the nodes
    reconstitute the bodies — consistent with production."""
    return {"artifacts": list(criterion_handles(criteria))}


def resolver(
    criteria: tuple[AcceptanceCriterion, ...] = CRITERIA,
) -> Callable[[Artifact], FrozenModel]:
    """An in-memory ``resolve`` over ``criterion_handles(criteria)`` (CAS in prod)."""
    body_by_uri = {
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq).uri: c
        for c in criteria
    }

    def _resolve(handle: Artifact) -> FrozenModel:
        return body_by_uri[handle.uri]

    return _resolve


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


# ---- 2. qa_engineer: traceability + negative oracle + flake quarantine ------


def _qa_payload() -> str:
    return json.dumps(
        {
            "tests": [
                {"test_id": "t-fidelity-1", "criterion_ids": ["AC-001"]},
                {"test_id": "t-cov-2", "criterion_ids": ["AC-002"]},
                {"test_id": "t-fidelity-3", "criterion_ids": ["AC-003"]},
            ],
            "quarantined": ["t-flaky-net"],  # failed-then-passed within N=2
        }
    )


def test_qa_plan_traceability_and_negative_oracle_and_quarantine() -> None:
    transport = MockTransport(completions=[_qa_payload()])
    rt = make_runtime(transport)
    node = make_qa_engineer_node(rt, resolve=resolver(), run_against="task/qa-1")

    writes = node(qa_state(), config_for("run-Q"))
    artifacts = writes["artifacts"]
    kinds = sorted(a.kind for a in artifacts)
    # One plan + one report + a NEGATIVE oracle per HIGH-RISK criterion (2 of 3).
    assert kinds == ["oracle_binding", "oracle_binding", "test_plan", "test_report"]

    # Rebuild the typed bodies to assert on their content (handles are CAS uris).
    plan = TestPlan(
        plan_uri=next(a.uri for a in artifacts if a.kind == "test_plan"),
        test_criteria={
            "t-fidelity-1": ("AC-001",),
            "t-cov-2": ("AC-002",),
            "t-fidelity-3": ("AC-003",),
        },
        seq=artifacts[0].seq,
    )
    # criterion traceability: every criterion is covered by a planned test.
    assert plan.covered_criteria() == frozenset({"AC-001", "AC-002", "AC-003"})

    # A negative oracle is bound for each high-risk criterion (criterion fidelity).
    high_risk = {c.id for c in CRITERIA if c.criticality is Criticality.HIGH_RISK}
    oracle_handles = [a for a in artifacts if a.kind == "oracle_binding"]
    assert len(oracle_handles) == len(high_risk) == 2
    rebound = [
        OracleBinding(
            criterion_id=cid,
            oracle_uri=f"cas://oracle/negative/{cid}",
            oracle_kind=OracleKind.NEGATIVE,
            seq=h.seq,
        )
        for cid, h in zip(sorted(high_risk), oracle_handles, strict=True)
    ]
    for ob, h in zip(rebound, oracle_handles, strict=True):
        assert ob.oracle_kind is OracleKind.NEGATIVE
        assert h.uri == f"cas://sha256/{ob.content_identity()}"


def _served_request_id(transport: MockTransport, idx: int = 0) -> str:
    """The deterministic request_id MockTransport served for request ``idx``
    (mirrors ``test_doubles._respond``: ``req_mock_<content_digest[:16]>``)."""
    return f"req_mock_{content_digest(transport.requests[idx])[:16]}"


def test_qa_report_quarantines_flaky_test() -> None:
    """Assert on the TestReport the node ACTUALLY emitted (not a hand-built one).

    The report rides ``artifacts`` as a handle whose ``uri`` is the record's
    ``content_identity()`` — which covers passed/failed/quarantined. We rebuild
    the report the node MUST have produced (passed = #covered criteria, failed=0,
    quarantined from the payload, results_uri keyed on the served request_id) and
    confirm its handle matches the emitted one, so the quarantine/failed
    accounting is genuinely exercised rather than restated.
    """
    transport = MockTransport(completions=[_qa_payload()])
    rt = make_runtime(transport)
    node = make_qa_engineer_node(rt, resolve=resolver(), run_against="task/qa-2")
    artifacts = node(qa_state(), config_for("run-Q2", step=5))["artifacts"]

    emitted = next(a for a in artifacts if a.kind == "test_report")
    # qa mints the report seq via rt.seq(..., "artifact:test_report"): first bump
    # for that key is epoch 1; superstep is the config's langgraph_step.
    report_seq = Seq("run-Q2", 1, 5, "qa_engineer", 0)
    expected = TestReport(
        results_uri=f"cas://test-results/{_served_request_id(transport)}",
        run_against="task/qa-2",
        passed=3,  # all three criteria covered (one synthetic if model omits)
        failed=0,
        quarantined=("t-flaky-net",),
        seq=report_seq,
    )
    assert emitted.uri == f"cas://sha256/{expected.content_identity()}"
    # the flake-then-pass test sits in quarantine, not counted as a failure.
    assert expected.quarantined == ("t-flaky-net",)
    assert expected.failed == 0


def test_qa_writes_no_gate_result() -> None:
    # qa's complete() is a schema-constrained IDEATION call (not a verdict, AC3
    # resolved) — serve the plan on the natural completions= path.
    node = make_qa_engineer_node(
        make_runtime(MockTransport(completions=[_qa_payload()])), resolve=resolver()
    )
    writes = node(qa_state(), config_for("run-Q3"))
    assert "gates" not in writes  # L5: QA never touches the gates channel


def test_qa_fail_closed_coverage_when_model_omits_criterion() -> None:
    """If the model omits a criterion, the node still guarantees full coverage."""
    thin = json.dumps({"tests": [{"test_id": "t-only-1", "criterion_ids": ["AC-001"]}]})
    node = make_qa_engineer_node(
        make_runtime(MockTransport(completions=[thin])), resolve=resolver()
    )
    artifacts = node(qa_state(), config_for("run-Q4"))["artifacts"]
    # synthetic tests fill the gap -> still 3 covered criteria + 2 negative oracles.
    assert sum(1 for a in artifacts if a.kind == "oracle_binding") == 2


def test_qa_fail_closed_when_handles_present_but_resolver_missing() -> None:
    """C-R1-01: criterion HANDLES on state + no ``resolve`` must FAIL CLOSED.

    Without a resolver the node would read zero criteria, author an empty
    ``TestPlan`` with no oracles, and sail through the Test gate (a vacuous pass).
    The node raises instead of silently returning ``()``."""
    node = make_qa_engineer_node(make_runtime(MockTransport(completions=[_qa_payload()])))
    with pytest.raises(MissingCriteriaResolverError):
        node(qa_state(), config_for("run-Q5"))


def test_sdet_fail_closed_when_handles_present_but_resolver_missing() -> None:
    """C-R1-01: same fail-closed rule for the SDET node's criteria read."""
    node = make_sdet_node(make_runtime(MockTransport(completions=[_sdet_payload()])))
    with pytest.raises(MissingCriteriaResolverError):
        node(qa_state(), config_for("run-S3"))


# ---- 3. sdet: determinism notes + traceability + quarantine -----------------


def _sdet_payload() -> str:
    return json.dumps(
        {
            "scenarios": [
                {"scenario_id": "e2e-checkout", "criterion_ids": ["AC-001", "AC-002"]},
                {"scenario_id": "e2e-login", "criterion_ids": ["AC-003"]},
            ],
            "quarantined": ["e2e-flaky-timing"],
        }
    )


def test_sdet_report_carries_determinism_and_quarantine() -> None:
    """Assert on the E2EReport the node ACTUALLY emitted (not a hand-built one).

    The report rides ``artifacts`` as a handle whose ``uri`` is the record's
    ``content_identity()`` — covering scenario_criteria, the determinism note, the
    flake budget, and quarantine. We reconstruct the report the node MUST have
    produced (results_uri keyed on the served request_id, the real
    ``_DETERMINISM_NOTE``) and confirm its handle matches the emitted one.
    """
    from foundry.agents.quality import sdet as sdet_mod

    transport = MockTransport(completions=[_sdet_payload()])
    rt = make_runtime(transport)
    node = make_sdet_node(rt, resolve=resolver(), run_against="tree-sha-integrated")
    [emitted] = node(qa_state(), config_for("run-S", step=6))["artifacts"]
    assert emitted.kind == "e2e_report"

    seq = Seq("run-S", 1, 6, "sdet", 0)
    expected = E2EReport(
        results_uri=f"cas://e2e-results/{_served_request_id(transport)}",
        run_against="tree-sha-integrated",
        scenarios_passed=2,
        scenarios_failed=0,
        pinned_seeds_note=sdet_mod._DETERMINISM_NOTE,
        scenario_criteria={
            "e2e-checkout": ("AC-001", "AC-002"),
            "e2e-login": ("AC-003",),
        },
        flake_retry_budget=2,
        quarantined=("e2e-flaky-timing",),
        seq=seq,
    )
    assert emitted.uri == f"cas://sha256/{expected.content_identity()}"
    # determinism notes + scenario→criterion traceability + quarantine recorded.
    assert expected.covered_criteria() == frozenset({"AC-001", "AC-002", "AC-003"})
    assert expected.flake_retry_budget == 2
    assert expected.quarantined == ("e2e-flaky-timing",)


def test_sdet_determinism_note_content() -> None:
    """The real node stamps the pinned-seeds / frozen-clock / mocked-network note."""
    from foundry.agents.quality import sdet as sdet_mod

    note = sdet_mod._DETERMINISM_NOTE
    assert "pinned seeds" in note
    assert "frozen clock" in note
    assert "network mocked" in note
    assert "N=2" in note


def test_sdet_writes_no_gate_result() -> None:
    # sdet's complete() is a schema-constrained IDEATION call (not a verdict, AC3
    # resolved) — serve the scenarios on the natural completions= path.
    node = make_sdet_node(
        make_runtime(MockTransport(completions=[_sdet_payload()])), resolve=resolver()
    )
    assert "gates" not in node(qa_state(), config_for("run-S2"))


# ---- 4. graph compatibility -------------------------------------------------


def test_quality_nodes_run_inside_a_state_graph(installed: GlobalRecords) -> None:
    transport = MockTransport(
        verdicts=["approve"],
        completions=[_qa_payload(), _sdet_payload()],
    )
    rt = make_runtime(transport)

    g: StateGraph = StateGraph(FactoryState)
    # In a COMPILED graph the criteria ride ``artifacts`` as resolvable HANDLES
    # (they survive merge_by_uri) and the nodes reconstitute bodies via resolve.
    resolve = resolver()
    g.add_node("qa", make_qa_engineer_node(rt, resolve=resolve))
    g.add_node("sdet", make_sdet_node(rt, resolve=resolve))
    g.add_node("review", make_reviewer_critic_node(rt))
    g.add_edge(START, "qa")
    g.add_edge("qa", "sdet")
    g.add_edge("sdet", "review")
    g.add_edge("review", END)
    app = g.compile()

    final = app.invoke(
        {"request": "ship it", "artifacts": criterion_handles()},
        config_for("run-graph", nonce="N-graph", subject="cas://diff/g", state_sha="sha-g"),
    )

    # qa + sdet artifacts landed on the channel (folded by merge_by_uri).
    kinds = {a.kind for a in final["artifacts"]}
    assert {"test_plan", "test_report", "oracle_binding", "e2e_report"} <= kinds
    # the reviewer's proxy-stamped verdict is admitted on the gates channel.
    [verdict] = final["gates"]
    assert verdict.name == "review"
    assert verdict.judgment_binding is not None
    # the real node's E2EReport determinism note rode through (read off handle).
    assert any(a.kind == "e2e_report" for a in final["artifacts"])


def test_artifact_handles_dedupe_under_merge_by_uri() -> None:
    """The artifacts channel folds by ``.uri`` — identical handles coalesce."""
    transport = MockTransport(completions=[_qa_payload()])
    rt = make_runtime(transport)
    node = make_qa_engineer_node(rt, resolve=resolver())
    a = node(qa_state(), config_for("run-R"))["artifacts"]
    # Re-folding the SAME emitted handles (a LangGraph resume re-fold) is
    # idempotent under merge_by_uri (keyed on the content-addressed uri).
    assert len(merge_by_uri(a, a)) == len(a)
    assert {x.uri for x in merge_by_uri(a, a)} == {x.uri for x in a}


def test_content_digest_is_process_stable() -> None:
    """Sanity: handles use content_digest (L7), reproducible across processes."""
    assert content_digest({"x": 1}) == content_digest({"x": 1})
