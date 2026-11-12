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

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.quality import make_reviewer_node, reviewer_critic
from foundry.agents.quality._common import (
    ACCEPTANCE_CRITERION_KIND,
    MissingCriteriaResolverError,
    server_issued_nonce,
)
from foundry.agents.quality.reviewer_critic import failing_checks
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.model_provider_proxy import ADVISORY_PREFIX
from foundry.proxy.test_doubles import MockTransport, verdict_json
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.journal import JournalEntry
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier
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
    assert verdict.verdict_nonce != "N-server-issued"  # derived per request, not the raw base
    assert merge_gates([verdict], None) == [verdict]


def test_request_changes_is_latched_by_the_supervisor_ledger_commit(records: Any) -> None:
    """The quality boundary's trusted ledger write still latches a request-changes
    from the new node, so a later approve on the same key cannot override it."""
    from foundry.graph.supervisor import _commit_judgment_ledger

    finding = {
        "finding": "x",
        "failing_check": "",
        "command": "app",
        "expected": "1",
        "actual": "2",
    }
    final, _cas = _run(MockTransport(verdicts=[verdict_json("request-changes", [finding])]))
    [rc] = final["gates"]
    assert rc.status is GateStatus.FAIL
    assert rc.findings == ("x [evidence: `app` expected '1', got '2']",)

    _commit_judgment_ledger([], [rc], records)
    assert records.negative_verdict_latched(rc.latch_key()) is True

    later, _ = _run(MockTransport(verdicts=["approve"]))
    assert merge_gates(later["gates"], None) == []


def _gate(name: str, status: GateStatus, *findings: str, local: int = 0) -> GateResult:
    return GateResult(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=findings,
        run_against="tree-1",
        seq=Seq("r", 1, local, "gate", 0),
    )


_S001_FAILED = "BDD criterion S001: scenario features/T1.feature::S001 failed"
_CITES_S001 = {
    "finding": "total-sum check is fragile",
    "failing_check": "S001",
    "command": "pytest -k S001",
    "expected": "",
    "actual": "",
}


def _review(
    state: FactoryState, cas: ArtifactStore, tree: str | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[GateResult, str]:
    # Materializing a real tree is gate_wiring's job (and its tests'); pin the result.
    monkeypatch.setattr(reviewer_critic, "_reviewed_tree", lambda *_: tree)
    transport = MockTransport(verdicts=[verdict_json("request-changes", [_CITES_S001])])
    node = make_reviewer_node(_runtime(transport, cas), resolve=cas.resolve)
    [verdict] = node(state, _config())["gates"]
    return verdict, transport.requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("test_gate", "tree", "blocks"),
    [
        (_gate("test", GateStatus.PASS, local=1), "tree-1", False),
        (_gate("test", GateStatus.FAIL, _S001_FAILED, local=1), "tree-1", True),
        # A fix pass: the failure is on the pre-fix tree, not the one under review.
        (_gate("test", GateStatus.FAIL, _S001_FAILED, local=1), "tree-2", False),
    ],
)
def test_reviewer_blocks_only_on_a_check_failing_on_the_reviewed_tree(
    records: Any, monkeypatch: pytest.MonkeyPatch, test_gate: GateResult, tree: str, blocks: bool
) -> None:
    cas = ArtifactStore()
    state = {**_seed(cas), "gates": [_gate("build", GateStatus.PASS), test_gate]}
    verdict, prompt = _review(state, cas, tree, monkeypatch)
    assert verdict.status is (GateStatus.FAIL if blocks else GateStatus.PASS)
    assert verdict.findings[0].startswith(ADVISORY_PREFIX) is not blocks
    # The judge is told which checks fail, so it can cite them.
    assert ("test, features/T1.feature::S001" in prompt) is blocks


def test_failing_checks_keep_findingless_gates_and_skip_reviews_and_other_trees() -> None:
    other = _gate("test", GateStatus.FAIL, _S001_FAILED, local=1)
    review = _gate("review", GateStatus.FAIL, "FAILED tests/t.py::test_a", local=3)
    build = _gate("build", GateStatus.FAIL, local=2).model_copy(update={"run_against": "tree-2"})
    smoke = _gate("smoke", GateStatus.ERROR, "FAILED tests/t.py::test_b", local=4)
    smoke = smoke.model_copy(update={"run_against": "tree-2"})
    state: FactoryState = {"gates": [other, review, build, smoke]}
    assert failing_checks(state, "tree-2") == (("build", "smoke"), ("tests/t.py::test_b",))
    assert failing_checks(state, None) == ((), ())


def test_reviewed_tree_is_not_built_when_nothing_failed() -> None:
    def boom(*_: Any) -> Any:
        raise AssertionError("materialized")

    passing: FactoryState = {"gates": [_gate("test", GateStatus.PASS)]}
    assert reviewer_critic._reviewed_tree(passing, boom, boom) is None
    failed: FactoryState = {"gates": [_gate("test", GateStatus.FAIL)]}
    assert reviewer_critic._reviewed_tree(failed, None, None) is None


# ---- artifacts ---------------------------------------------------------------


def test_reviewer_emits_every_quality_artifact_kind(records: Any) -> None:
    final, _cas = _run()
    arts = [a for a in final["artifacts"] if a.produced_by == "reviewer"]
    kinds = sorted(a.kind for a in arts)
    assert kinds == [
        "e2e_report",
        "oracle_binding",
        "oracle_binding",
        "review_findings",
        "test_plan",
        "test_report",
    ]
    assert all(a.uri.startswith("cas://") for a in final["artifacts"])
    # The seeded criterion + diff handles are still on the channel.
    assert sum(a.kind == ACCEPTANCE_CRITERION_KIND for a in final["artifacts"]) == len(CRITERIA)
    [entry] = final["agent_journal"]
    assert isinstance(entry, JournalEntry)
    assert (entry.node_id, entry.role) == ("reviewer", "Reviewer")


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


# ---- re-review after a fix pass ----------------------------------------------

_ADD_APP = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+def tip(t):\n+    return t * 0.15\n"
    "--- /dev/null\n+++ b/lib.py\n@@ -0,0 +1 @@\n+UNTOUCHED = 'reviewed before'\n"
)
_FIX_APP = (
    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def tip(t):\n"
    "-    return t * 0.15\n+    return round(t * 0.15, 2)\n"
)
_UNROUNDED = {
    "finding": "tip is not rounded to cents",
    "failing_check": "",
    "command": "python -c 'print(tip(0.1))'",
    "expected": "0.02",
    "actual": "0.015",
}


def _stored_diff(cas: ArtifactStore, task_id: str, body: str, epoch: int) -> Any:
    from foundry.schemas.base import Artifact

    seq = Seq("r", epoch, 0, "junior_engineer", 0)
    uri = f"cas://feature_diff/{task_id}-{epoch}"
    diff = FeatureDiff(
        diff_uri=cas.put_blob(body),
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="base-sha",
        seq=seq,
    )
    cas.put_body_at(uri, diff)
    return Artifact(
        kind="feature_diff", uri=uri, produced_by="junior_engineer", task_id=task_id, seq=seq
    )


def _big_file(name: str) -> str:
    lines = reviewer_critic._DIFF_CHAR_CAP // 6
    return f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{lines} @@\n" + "+x = 1\n" * lines


def _two_reviews(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep_snapshot: bool = True,
    built_body: str = _ADD_APP,
    fix_body: str = _FIX_APP,
) -> tuple[list[str], list[str | None], GateResult]:
    cas = ArtifactStore()
    transport = MockTransport(
        verdicts=[
            verdict_json("request-changes", [_UNROUNDED]),
            verdict_json("request-changes", [_UNROUNDED]),
        ]
    )
    rt = _runtime(transport, cas)
    efforts: list[str | None] = []
    judge = rt.proxy.judge

    def spy(*args: Any, **kwargs: Any) -> Any:
        efforts.append(kwargs.get("effort"))
        return judge(*args, **kwargs)

    monkeypatch.setattr(rt.proxy, "judge", spy)
    node = make_reviewer_node(rt, resolve=cas.resolve, resolve_blob=cas.resolve_blob)
    criteria = [
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq, store=cas)
        for c in CRITERIA
    ]
    built = _stored_diff(cas, "T1", built_body, 1)
    first = node({"artifacts": [*criteria, built]}, _config())
    produced = [
        a
        for a in first["artifacts"]
        if keep_snapshot or a.kind != reviewer_critic.REVIEWED_TREE_KIND
    ]
    fix = _stored_diff(cas, "fix-1", fix_body, 2)
    state: FactoryState = {
        "artifacts": [*criteria, built, *produced, fix],
        "gates": first["gates"],
    }
    [verdict] = node(state, _config(step=2))["gates"]
    prompts = [r["messages"][0]["content"] for r in transport.requests]
    return prompts, efforts, verdict


def test_first_review_is_full_and_a_re_review_gets_only_the_change_and_its_findings(
    records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (full, delta), efforts, verdict = _two_reviews(monkeypatch)
    assert efforts == ["low", "low"]
    assert "--- task T1" in full and "UNTOUCHED" in full and "re-review" not in full

    assert "re-review" in delta and "--- task T1" not in delta
    assert "-    return t * 0.15\n+    return round(t * 0.15, 2)" in delta
    assert "UNTOUCHED" not in delta  # unchanged since the last review
    assert "- tip is not rounded to cents [evidence:" in delta
    assert "repeat every unresolved one" in delta
    # The verdict still answers for the whole tree: the unresolved finding stands.
    assert verdict.status is GateStatus.FAIL
    assert verdict.findings[0].startswith("tip is not rounded to cents")


def test_a_re_review_without_the_last_reviewed_tree_is_full(
    records: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_, second), efforts, _ = _two_reviews(monkeypatch, keep_snapshot=False)
    assert efforts == ["low", "low"]
    assert "re-review" not in second and "--- task fix-1" in second


@pytest.mark.parametrize(
    ("built_body", "fix_body"),
    [
        # The first review saw only the head of its change: the rest was never judged.
        (_ADD_APP + _big_file("big.py"), _FIX_APP),
        # The change since the last review would itself be cut.
        (_ADD_APP, _FIX_APP + _big_file("big.py")),
    ],
)
def test_a_re_review_is_full_unless_both_reviews_see_their_whole_change(
    records: Any, monkeypatch: pytest.MonkeyPatch, built_body: str, fix_body: str
) -> None:
    (_, second), efforts, _ = _two_reviews(monkeypatch, built_body=built_body, fix_body=fix_body)
    assert efforts == ["low", "low"]
    assert "re-review" not in second and "--- task T1" in second


def test_a_reviewed_tree_recorded_before_completeness_reads_as_incomplete() -> None:
    from foundry.schemas.quality import ReviewedTree

    legacy = ReviewedTree.model_validate(
        {"verdict_seq": Seq("r", 1, 0, "reviewer", 0), "snapshot_uri": "cas://sha256/x"}
    )
    assert legacy.complete is False


def test_a_review_builds_the_tree_once_and_judges_checks_on_its_gated_sha(
    records: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from foundry.graph import gate_wiring
    from foundry.graph.gate_wiring import materialize

    cas = ArtifactStore()
    criteria = [
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq, store=cas)
        for c in CRITERIA
    ]
    state: FactoryState = {"artifacts": [*criteria, _stored_diff(cas, "T1", _ADD_APP, 1)]}
    gated = gate_wiring.materialize_workdir(
        state, tmp_path, resolve=cas.resolve, resolve_blob=cas.resolve_blob
    )
    failed = _gate("test", GateStatus.FAIL, _S001_FAILED, local=1)
    state["gates"] = [failed.model_copy(update={"run_against": gated})]

    builds: list[str] = []
    real = materialize.materialize_workdir

    def counted(*args: Any, **kwargs: Any) -> str:
        builds.append("built")
        return real(*args, **kwargs)

    monkeypatch.setattr(materialize, "materialize_workdir", counted)
    monkeypatch.setattr(gate_wiring, "materialize_workdir", counted)
    transport = MockTransport(verdicts=[verdict_json("request-changes", [_CITES_S001])])
    node = make_reviewer_node(
        _runtime(transport, cas), resolve=cas.resolve, resolve_blob=cas.resolve_blob
    )
    [verdict] = node(state, _config())["gates"]
    assert builds == ["built"]
    # S001 fails on the very tree under review, so the finding citing it blocks.
    assert verdict.status is GateStatus.FAIL
    assert "test, features/T1.feature::S001" in transport.requests[0]["messages"][0]["content"]


# ---- spec, contracts, launch result, checklist, length stop --------------------


def _reviewer_state(cas: ArtifactStore, body: str = _ADD_APP) -> FactoryState:
    from foundry.schemas.architecture import InterfaceContract

    criteria = [
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq, store=cas)
        for c in CRITERIA
    ]
    contract = InterfaceContract(
        name="tip",
        spec_uri="cas://contract/tip",
        kind="function",
        signature="def tip(total: float) -> float",
        seq=Seq("r", 0, 0, "planner", 0),
    )
    handle = emit_artifact(contract, "interface_contract", "planner", contract.seq, store=cas)
    launch = _gate("launch", GateStatus.FAIL, "GET / returned 500", local=3)
    return {
        "spec": "A tip calculator. Tips round to cents or to whole dollars.",
        "artifacts": [*criteria, handle, _stored_diff(cas, "T1", body, 1)],
        "gates": [launch],
    }


def test_the_review_sees_the_spec_contracts_launch_result_and_checklist(records: Any) -> None:
    cas = ArtifactStore()
    transport = MockTransport(verdicts=["approve"])
    node = make_reviewer_node(
        _runtime(transport, cas), resolve=cas.resolve, resolve_blob=cas.resolve_blob
    )
    node(_reviewer_state(cas), _config())
    [request] = transport.requests
    prompt = request["messages"][0]["content"]
    assert "round to cents or to whole dollars" in prompt
    assert "- tip [function]: def tip(total: float) -> float" in prompt
    assert "launch gate" in prompt and "GET / returned 500" in prompt
    sop = reviewer_critic._SOP
    for item in ("CSRF", "LIKE", "check_same_thread", "transaction", "'X or Y'", "dead modules"):
        assert item in sop
    assert request["max_tokens"] <= reviewer_critic._REVIEW_MAX_TOKENS


def test_advisory_findings_are_kept_on_a_review_findings_record(records: Any) -> None:
    from foundry.agents.quality.reviewer import REVIEW_FINDINGS_KIND
    from foundry.schemas.quality import ReviewFindings

    cas = ArtifactStore()
    advisory = {**_UNROUNDED, "finding": "names are terse", "actual": "0.02"}
    transport = MockTransport(verdicts=[verdict_json("request-changes", [_UNROUNDED, advisory])])
    node = make_reviewer_node(
        _runtime(transport, cas), resolve=cas.resolve, resolve_blob=cas.resolve_blob
    )
    out = node(_reviewer_state(cas), _config())
    [handle] = [a for a in out["artifacts"] if a.kind == REVIEW_FINDINGS_KIND]
    body = cas.get_body(handle.uri)
    assert isinstance(body, ReviewFindings)
    assert body.status == "fail" and body.verdict_seq == out["gates"][0].seq
    assert [f.split(" [")[0] for f in body.blocking] == ["tip is not rounded to cents"]
    assert body.advisory == ("names are terse",)


class _CutOnce(MockTransport):
    """Its first verdict stops at the output cap mid-JSON; the rest are whole."""

    def send(self, request: dict[str, Any]) -> Any:
        from dataclasses import replace

        response = super().send(request)
        if len(self.requests) == 1:
            return replace(response, text='{"status": "request-ch', stop_reason="max_tokens")
        return response


def test_a_review_cut_at_the_output_cap_retries_once_on_the_trimmed_diff(records: Any) -> None:
    cas = ArtifactStore()
    test_file = "--- /dev/null\n+++ b/tests/test_app.py\n@@ -0,0 +1 @@\n+TESTS_ONLY = 1\n"
    transport = _CutOnce(verdicts=["approve", "approve"])
    node = make_reviewer_node(
        _runtime(transport, cas), resolve=cas.resolve, resolve_blob=cas.resolve_blob
    )
    out = node(_reviewer_state(cas, _ADD_APP + test_file), _config())
    first, second = (r["messages"][0]["content"] for r in transport.requests)
    assert "TESTS_ONLY" in first
    assert "after your last reply ran past the output limit" in second
    assert "TESTS_ONLY" not in second and "def tip" in second
    assert out["gates"][0].status is GateStatus.PASS


def test_the_trimmed_diff_keeps_the_files_a_failing_check_names() -> None:
    trimmed = reviewer_critic.trimmed_diff(_ADD_APP, "build: lib.py:1: name error")
    assert "UNTOUCHED" in trimmed and "def tip" not in trimmed
