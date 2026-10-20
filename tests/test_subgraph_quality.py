"""Quality subgraph — the single Layer-4 ``reviewer`` node, composed end-to-end.

Drives :func:`build_quality_subgraph` over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` + a dev :class:`ZoneBSigner` (no live API key,
LEARNINGS D1/D4), compiled STANDALONE (the supervisor owns the checkpointer), with
state seeded as the upstream layers leave it: ``acceptance_criterion`` HANDLES
(resolved through a real in-process CAS), a ``FeatureDiff`` handle, and the
server-issued ``verdict_nonce`` / ``subject_artifact_uri`` / ``state_sha`` run
inputs on the config (Q1).

Asserts the one node makes exactly ONE model call (the trusted judge), its
proxy-stamped verdict is admitted by the REAL ``merge_gates`` and latched by the
supervisor's ledger commit, and it still emits every artifact kind the docs /
checkpoint / webapp consumers read (``test_plan``, a NEGATIVE ``oracle_binding``
per high-risk criterion, ``test_report``, ``e2e_report``).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.quality import make_reviewer_critic_node, make_reviewer_node
from foundry.agents.quality._common import (
    ACCEPTANCE_CRITERION_KIND,
    MissingCriteriaResolverError,
    server_issued_nonce,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, verdict_json
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.journal import JournalEntry
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_by_uri, merge_gates, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-subgraph-quality-key"
KEY_ID = "dev-key-subgraph-quality"

DIFF_URI = "cas://diff/feature-1"


def _crit(cid: str, crit: Criticality = Criticality.NORMAL) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given/When/Then for {cid}",
        criticality=crit,
        seq=Seq(run_id="r", emission_epoch=0, superstep_index=0, node_id="pm", local=0),
    )


CRITERIA = (
    _crit("AC-001", Criticality.HIGH_RISK),
    _crit("AC-002"),
    _crit("AC-003", Criticality.HIGH_RISK),
)


def _runtime(
    transport: MockTransport | None = None, cas: ArtifactStore | None = None
) -> AgentRuntime:
    proxy = ModelProviderProxy(
        transport or MockTransport(verdicts=["approve"]), signer=ZoneBSigner(KEY, KEY_ID)
    )
    return AgentRuntime(proxy=proxy, cas=cas)


def _seed(cas: ArtifactStore) -> FactoryState:
    """State as the product + implementation layers would leave it."""
    criteria_handles = [
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq, store=cas)
        for c in CRITERIA
    ]
    diff = FeatureDiff(
        diff_uri=DIFF_URI,
        task_id="T1",
        branch="task/T1",
        base_tree_sha="base-sha",
        seq=Seq(
            run_id="r", emission_epoch=0, superstep_index=0, node_id="junior_engineer", local=0
        ),
    )
    diff_handle = emit_artifact(diff, "feature_diff", "junior_engineer", diff.seq)
    return {"request": "ship it", "artifacts": [*criteria_handles, diff_handle]}


def _config(run_id: str = "run-quality-subgraph", step: int = 1) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "verdict_nonce": "N-server-issued",
            "subject_artifact_uri": DIFF_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
    }


@pytest.fixture
def records() -> Any:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        yield records
    finally:
        token.var.reset(token)


def _run(transport: MockTransport | None = None) -> tuple[FactoryState, ArtifactStore]:
    cas = ArtifactStore()
    seed = _seed(cas)
    app = build_quality_subgraph(_runtime(transport, cas), resolve=cas.resolve).compile()
    return app.invoke(seed, _config()), cas


def _bodies(final: FactoryState, cas: ArtifactStore, kind: str) -> list[Any]:
    return [cas.get_body(a.uri) for a in final.get("artifacts") or () if a.kind == kind]


# ---- topology ----------------------------------------------------------------


def test_build_quality_subgraph_returns_uncompiled_single_reviewer_node() -> None:
    cas = ArtifactStore()
    g = build_quality_subgraph(_runtime(cas=cas), resolve=cas.resolve)
    assert isinstance(g, StateGraph)
    assert not hasattr(g, "invoke")  # compilation is the caller's job
    assert set(g.nodes) == {"reviewer"}


def test_build_quality_subgraph_requires_resolve() -> None:
    """C-R1-01: a defaulted ``resolve=None`` was the vacuous-pass trap."""
    resolve = inspect.signature(build_quality_subgraph).parameters["resolve"]
    assert resolve.default is inspect.Parameter.empty


def test_reviewer_fails_closed_without_resolver_before_any_model_call(records: Any) -> None:
    transport = MockTransport(verdicts=["approve"])
    node = make_reviewer_node(_runtime(transport), resolve=None)
    with pytest.raises(MissingCriteriaResolverError):
        node(_seed(ArtifactStore()), _config())
    assert transport.requests == []


# ---- call count --------------------------------------------------------------


def test_one_model_call_per_quality_pass_on_the_judge_channel(records: Any) -> None:
    transport = MockTransport(verdicts=["approve"])
    _run(transport)

    [request] = transport.requests
    # The ONE call is the constrained verdict channel (judge), not a completion.
    schema = request["output_config"]["format"]["schema"]
    assert schema["properties"]["status"]["enum"] == ["approve", "request-changes"]
    # The criteria the old QA/SDET personas planned against are in front of the judge.
    prompt = request["messages"][0]["content"]
    assert all(c.id in prompt for c in CRITERIA)


# ---- the bound verdict -------------------------------------------------------


def test_reviewer_verdict_is_proxy_bound_and_admitted(records: Any) -> None:
    final, _cas = _run()

    [verdict] = final["gates"]
    assert isinstance(verdict, GateResult)
    assert verdict.name == "review"
    assert verdict.status is GateStatus.PASS
    assert verdict.seq.node_id == "reviewer"
    assert verdict.judgment_binding is not None
    assert verdict.judgment_provenance is JudgmentProvenance.SELF
    assert verdict.subject_artifact_uri == DIFF_URI
    assert verdict.run_against == "integrated-sha"
    assert verdict.verdict_nonce == server_issued_nonce(
        _config(), node_id="reviewer", subject_uri=DIFF_URI, state_sha="integrated-sha"
    )
    assert merge_gates([verdict], None) == [verdict]


def test_request_changes_is_latched_by_the_supervisor_ledger_commit(records: Any) -> None:
    """The quality boundary's trusted ledger write still latches a request-changes
    from the new node, so a later approve on the same key cannot override it."""
    from foundry.graph.supervisor import _commit_judgment_ledger

    final, _cas = _run(MockTransport(verdicts=[verdict_json("request-changes", ["x"])]))
    [rc] = final["gates"]
    assert rc.status is GateStatus.FAIL
    assert rc.findings == ("x",)

    _commit_judgment_ledger([], [rc], records)
    assert records.negative_verdict_latched(rc.latch_key()) is True

    later, _ = _run(MockTransport(verdicts=["approve"]))
    assert merge_gates(later["gates"], None) == []


def test_quality_and_assurance_reviews_get_distinct_nonces(records: Any) -> None:
    """H3: the quality ``reviewer`` and the assurance ``reviewer_critic`` judge the
    same subject/state in one run; their per-request nonces must not collide."""
    cas = ArtifactStore()
    seed = _seed(cas)
    q = make_reviewer_node(_runtime(cas=cas), resolve=cas.resolve)(seed, _config())["gates"][0]
    a = make_reviewer_critic_node(_runtime())(seed, _config())["gates"][0]
    assert q.verdict_nonce != a.verdict_nonce
    assert merge_gates([q], None) == [q] and merge_gates([a], None) == [a]


# ---- artifacts ---------------------------------------------------------------


def test_reviewer_emits_every_quality_artifact_kind(records: Any) -> None:
    final, _cas = _run()
    arts = [a for a in final["artifacts"] if a.produced_by == "reviewer"]
    kinds = sorted(a.kind for a in arts)
    assert kinds == ["e2e_report", "oracle_binding", "oracle_binding", "test_plan", "test_report"]
    assert all(a.uri.startswith("cas://") for a in final["artifacts"])
    # The seeded criterion + diff handles are still on the channel.
    assert sum(a.kind == ACCEPTANCE_CRITERION_KIND for a in final["artifacts"]) == len(CRITERIA)


def test_plan_and_e2e_trace_every_criterion(records: Any) -> None:
    final, cas = _run()
    [plan] = _bodies(final, cas, "test_plan")
    [report] = _bodies(final, cas, "test_report")
    [e2e] = _bodies(final, cas, "e2e_report")
    ids = {c.id for c in CRITERIA}

    assert isinstance(plan, TestPlan) and plan.covered_criteria() == ids
    assert isinstance(report, TestReport)
    assert (report.passed, report.failed, report.run_against) == (3, 0, "integrated-sha")
    assert isinstance(e2e, E2EReport)
    assert {c for cids in e2e.scenario_criteria.values() for c in cids} == ids
    assert "pinned seeds" in e2e.pinned_seeds_note and e2e.flake_retry_budget == 2


def test_negative_oracle_per_high_risk_criterion(records: Any) -> None:
    final, cas = _run()
    oracles = _bodies(final, cas, "oracle_binding")
    assert all(isinstance(o, OracleBinding) for o in oracles)
    assert sorted(o.criterion_id for o in oracles) == ["AC-001", "AC-003"]
    assert all(o.oracle_kind is OracleKind.NEGATIVE for o in oracles)


def test_artifact_handles_dedupe_under_merge_by_uri(records: Any) -> None:
    cas = ArtifactStore()
    node = make_reviewer_node(_runtime(cas=cas), resolve=cas.resolve)
    a = node(_seed(cas), _config())["artifacts"]
    assert len(merge_by_uri(a, a)) == len(a)


# ---- journal -----------------------------------------------------------------


def test_one_journal_entry_as_reviewer(records: Any) -> None:
    final, _cas = _run()
    [entry] = final["agent_journal"]
    assert isinstance(entry, JournalEntry)
    assert (entry.node_id, entry.role) == ("reviewer", "Reviewer")
