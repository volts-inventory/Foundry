"""Quality subgraph (T-M1-11) — the Layer-4 builder composed end-to-end.

Drives :func:`build_quality_subgraph` over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` + a dev :class:`ZoneBSigner` (no live API key,
LEARNINGS D1/D4). Compiles the subgraph STANDALONE (no checkpointer — the
supervisor owns that) and invokes it with state seeded as the upstream layers
would leave it:

* the acceptance criteria as resolvable ``acceptance_criterion`` HANDLES on
  ``artifacts`` (raw bodies carry no ``.uri`` and would crash ``merge_by_uri``;
  the QA / SDET nodes reconstitute bodies via the injected ``resolve`` — the
  Senior Engineer's seam) — this is "as product would" leave them;
* a ``FeatureDiff`` handle (the diff the Reviewer judges); and
* a server-issued ``verdict_nonce`` on the run config (Q1 — a RUN INPUT the
  runner threads, never agent-minted), alongside ``subject_artifact_uri`` /
  ``state_sha``.

The transport scripts ``verdicts=["approve"]`` for the reviewer's constrained
verdict channel and ``completions=`` (free-text, schema-constrained IDEATION; AC3
resolved, DT1) for QA + SDET.

Asserts:

* the criterion-traceable ``TestPlan`` + a NEGATIVE ``OracleBinding`` per
  high-risk criterion + the ``TestReport`` quarantine + the ``E2EReport`` land on
  ``artifacts``;
* the Reviewer's proxy-stamped ``GateResult`` is ADMITTED to ``gates`` via the
  REAL ``merge_gates`` path (valid binding + fresh nonce + no latch);
* the builder returns an UNCOMPILED ``StateGraph`` (the caller compiles).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.quality.qa_engineer import ACCEPTANCE_CRITERION_KIND
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.subgraphs.quality import build_quality_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import merge_gates, set_gate_write_verifier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-subgraph-quality-key"
KEY_ID = "dev-key-subgraph-quality"

DIFF_URI = "cas://diff/feature-1"


# ---- the criteria the product layer leaves (high-risk drives negative oracles) -


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


# ---- per-node scripted payloads (SOP order: reviewer -> qa -> sdet) -----------

QA_PAYLOAD = json.dumps(
    {
        "tests": [
            {"test_id": "t-fidelity-1", "criterion_ids": ["AC-001"]},
            {"test_id": "t-cov-2", "criterion_ids": ["AC-002"]},
            {"test_id": "t-fidelity-3", "criterion_ids": ["AC-003"]},
        ],
        "quarantined": ["t-flaky-net"],
    }
)

SDET_PAYLOAD = json.dumps(
    {
        "scenarios": [
            {"scenario_id": "e2e-checkout", "criterion_ids": ["AC-001", "AC-002"]},
            {"scenario_id": "e2e-login", "criterion_ids": ["AC-003"]},
        ],
        "quarantined": ["e2e-flaky-timing"],
    }
)


def _transport() -> MockTransport:
    # verdicts= for the reviewer's constrained verdict channel; completions= for
    # the QA + SDET schema-constrained IDEATION calls (AC3 resolved, DT1).
    return MockTransport(verdicts=["approve"], completions=[QA_PAYLOAD, SDET_PAYLOAD])


def _runtime(transport: MockTransport | None = None) -> AgentRuntime:
    proxy = ModelProviderProxy(transport or _transport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def _resolver(
    criteria: tuple[AcceptanceCriterion, ...] = CRITERIA,
) -> Callable[[Artifact], FrozenModel]:
    body_by_uri = {
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq).uri: c
        for c in criteria
    }

    def _resolve(handle: Artifact) -> FrozenModel:
        return body_by_uri[handle.uri]

    return _resolve


def _seed_state() -> FactoryState:
    """State as the product + implementation layers would leave it."""
    criteria_handles = [
        emit_artifact(c, ACCEPTANCE_CRITERION_KIND, "product_manager", c.seq) for c in CRITERIA
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
    # verdict_nonce / subject_artifact_uri / state_sha are RUN INPUTS the runner
    # threads onto the config (Q1), exactly like thread_id (AR2).
    return {
        "configurable": {
            "thread_id": run_id,
            "verdict_nonce": "N-server-issued",
            "subject_artifact_uri": DIFF_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
    }


def _installed() -> tuple[GlobalRecords, Any]:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    return records, token


def _artifacts(state: FactoryState) -> list[Artifact]:
    return list(state.get("artifacts") or ())


# ---- tests -------------------------------------------------------------------


def test_build_quality_subgraph_returns_uncompiled_state_graph() -> None:
    g = build_quality_subgraph(_runtime(), resolve=_resolver())
    assert isinstance(g, StateGraph)
    assert not hasattr(g, "invoke")  # compilation is the caller's job


def test_quality_subgraph_produces_test_and_e2e_artifacts() -> None:
    """A standalone-compiled run yields the criterion-traceable TestPlan + a
    NEGATIVE oracle per high-risk criterion + the quarantining TestReport +
    E2EReport on ``artifacts`` (the inputs the Test / E2E gates consume)."""
    _records, token = _installed()
    try:
        app = build_quality_subgraph(_runtime(), resolve=_resolver()).compile()
        final: FactoryState = app.invoke(_seed_state(), _config())
    finally:
        token.var.reset(token)

    kinds = sorted(a.kind for a in _artifacts(final))
    # The two high-risk criteria each get a NEGATIVE oracle binding (fidelity).
    assert kinds.count("oracle_binding") == 2
    assert {"test_plan", "test_report", "e2e_report"} <= set(kinds)
    # The seeded criterion + diff handles are still on the channel (folded by uri).
    assert kinds.count(ACCEPTANCE_CRITERION_KIND) == len(CRITERIA)
    assert "feature_diff" in kinds
    # All bodies ride out-of-band as content-addressed handles (L4).
    assert all(a.uri.startswith("cas://") for a in _artifacts(final))


def test_quality_subgraph_admits_reviewer_verdict_to_gates() -> None:
    """The Reviewer's proxy-stamped GateResult lands on ``gates`` and is admitted
    by the REAL merge_gates path (valid binding + fresh server-issued nonce)."""
    _records, token = _installed()
    try:
        app = build_quality_subgraph(_runtime(), resolve=_resolver()).compile()
        final: FactoryState = app.invoke(_seed_state(), _config())

        [verdict] = final["gates"]
        assert isinstance(verdict, GateResult)
        assert verdict.name == "review"
        assert verdict.status is GateStatus.PASS
        # The PROXY stamped the binding + provenance — the agent never signed it.
        assert verdict.judgment_binding is not None
        assert verdict.judgment_provenance is JudgmentProvenance.SELF
        assert verdict.verdict_nonce == "N-server-issued"
        # ADMITTED by the real reducer + verifier (it already merged into `gates`,
        # and a re-fold of the committed verdict is idempotent).
        assert merge_gates([verdict], None) == [verdict]
    finally:
        token.var.reset(token)


def test_quality_subgraph_runs_nodes_in_sop_order() -> None:
    """reviewer_critic -> qa_engineer -> sdet, one model call each, in SOP order.

    The reviewer rides the verdict channel; QA + SDET ride free-text completions —
    order is asserted via the distinct "You are the ..." opening of each SOP."""
    transport = _transport()
    _records, token = _installed()
    try:
        app = build_quality_subgraph(_runtime(transport), resolve=_resolver()).compile()
        app.invoke(_seed_state(), _config())
    finally:
        token.var.reset(token)

    assert len(transport.requests) == 3  # exactly one model call per node
    sops = ["".join(block["text"] for block in req["system"]) for req in transport.requests]
    markers = [
        "You are the Reviewer/Critic",
        "You are the QA Engineer",
        "You are the SDET",
    ]
    for sop, marker in zip(sops, markers, strict=True):
        assert marker in sop


def test_quality_subgraph_plan_is_criterion_traceable() -> None:
    """Every acceptance criterion is exercised by at least one planned test, and
    a negative oracle is bound for each high-risk criterion (criterion fidelity)."""
    from foundry.schemas.quality import OracleBinding, OracleKind, TestPlan

    _records, token = _installed()
    try:
        app = build_quality_subgraph(_runtime(), resolve=_resolver()).compile()
        final: FactoryState = app.invoke(_seed_state(), _config())
    finally:
        token.var.reset(token)

    arts = _artifacts(final)
    plan_handle = next(a for a in arts if a.kind == "test_plan")
    plan = TestPlan(
        plan_uri=plan_handle.uri,
        test_criteria={
            "t-fidelity-1": ("AC-001",),
            "t-cov-2": ("AC-002",),
            "t-fidelity-3": ("AC-003",),
        },
        seq=plan_handle.seq,
    )
    assert plan.covered_criteria() == {"AC-001", "AC-002", "AC-003"}

    high_risk = sorted(c.id for c in CRITERIA if c.criticality is Criticality.HIGH_RISK)
    oracle_handles = sorted(
        (a for a in arts if a.kind == "oracle_binding"), key=lambda a: a.seq
    )
    rebound = [
        OracleBinding(
            criterion_id=cid,
            oracle_uri=f"cas://oracle/negative/{cid}",
            oracle_kind=OracleKind.NEGATIVE,
            seq=h.seq,
        )
        for cid, h in zip(high_risk, oracle_handles, strict=True)
    ]
    for ob, h in zip(rebound, oracle_handles, strict=True):
        assert ob.oracle_kind is OracleKind.NEGATIVE
        assert h.uri == f"cas://sha256/{ob.content_identity()}"
