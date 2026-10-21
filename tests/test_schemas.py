"""Conformance tests for the Layer 1–7 agent artifact schemas.

Contract under test (docs/agents.md; conventions from foundry/schemas/base.py):
every model is a FrozenModel (frozen, extra="forbid"), carries `seq: Seq`, has
a content_identity() that is stable across construction order and sensitive to
any field change. Large bodies are `*_uri` CAS handles, never inline (DS9).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import (
    BudgetAction,
    BudgetDecision,
    ChangeRiskTier,
    GuardianAction,
    GuardianDecision,
    HumanDecision,
    RequestRoute,
    TriageDecision,
)
from foundry.schemas.implementation import (
    ConflictResolution,
    FeatureDiff,
    ReviewRequest,
    Task,
    TaskStatus,
)
from foundry.schemas.product import (
    PRD,
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    IntentBrief,
    SprintPlan,
)
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport
from foundry.schemas.security import (
    SBOMRef,
    SecurityTriage,
    Severity,
    SupplyChainReport,
    ThreatModel,
)
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="pm", local=0)

# (model, valid kwargs, field to change, changed value) — one row per model.
CASES: list[tuple[type[FrozenModel], dict, str, object]] = [
    (
        IntentBrief,
        {
            "intent": "build a todo service",
            "assumptions": ("single tenant",),
            "unknowns": ("expected scale",),
            "success_criteria": ("user can add a todo",),
            "must_haves": ("crud",),
            "nice_to_haves": ("dark mode",),
            "seq": SEQ,
        },
        "intent",
        "build a notes service",
    ),
    (
        PRD,
        {
            "title": "Todo service",
            "prd_uri": "cas://prd/1",
            "required_capabilities": ("ui",),
            "criterion_ids": ("AC-1",),
            "seq": SEQ,
        },
        "title",
        "Notes service",
    ),
    (
        AcceptanceCriterion,
        {
            "id": "AC-1",
            "text": "Given a user, When they add a todo, Then it is listed",
            "criticality": Criticality.HIGH_RISK,
            "oracle_hint": "unit test on add() + list()",
            "security_abuse": True,
            "seq": SEQ,
        },
        "text",
        "Given a user, When they add a todo, Then it persists",
    ),
    (
        CriteriaCritique,
        {
            "prd_uri": "cas://prd/1",
            "missing": ("no rate-limit criterion",),
            "security_abuse_omissions": ("no authz abuse case for shared lists",),
            "judgment_provenance": JudgmentProvenance.INDEPENDENT,
            "seq": SEQ,
        },
        "prd_uri",
        "cas://prd/2",
    ),
    (
        SprintPlan,
        {
            "milestones": ("m1: crud", "m2: auth"),
            "usd_cap_microusd": 5_000_000,
            "risk_register_uri": "cas://risk/1",
            "done_definition": "all acceptance criteria green",
            "seq": SEQ,
        },
        "done_definition",
        "m1 green",
    ),
    (
        Task,
        {
            "id": "T-1",
            "spec_uri": "cas://task/T-1",
            "criterion_ids": ("AC-1", "AC-2"),
            "branch": "task/T-1",
            "status": TaskStatus.PENDING,
            "seq": SEQ,
        },
        "branch",
        "task/T-1-retry",
    ),
    (
        FeatureDiff,
        {
            "diff_uri": "cas://diff/1",
            "task_id": "T-1",
            "branch": "task/T-1",
            "base_tree_sha": "a" * 40,
            "seq": SEQ,
        },
        "base_tree_sha",
        "b" * 40,
    ),
    (
        ReviewRequest,
        {
            "diff_uri": "cas://diff/1",
            "task_id": "T-1",
            "run_against": "task/T-1",
            "verdict_nonce": "nonce-1",
            "seq": SEQ,
        },
        "verdict_nonce",
        "nonce-2",
    ),
    (
        ConflictResolution,
        {
            "task_ids": ("T-1", "T-2"),
            "paths": ("src/app.py",),
            "base_tree_sha": "a" * 40,
            "resolved_diff_uri": "cas://diff/2",
            "rationale": "ownership_map: T-1 owns src/app.py",
            "seq": SEQ,
        },
        "rationale",
        "ownership_map: T-2 owns src/app.py",
    ),
    (
        TestPlan,
        {
            "plan_uri": "cas://plan/1",
            "test_criteria": {"test_add": ("AC-1",), "test_list": ("AC-1", "AC-2")},
            "seq": SEQ,
        },
        "plan_uri",
        "cas://plan/2",
    ),
    (
        OracleBinding,
        {
            "criterion_id": "AC-1",
            "oracle_uri": "cas://oracle/1",
            "oracle_kind": OracleKind.NEGATIVE,
            "seq": SEQ,
        },
        "oracle_uri",
        "cas://oracle/2",
    ),
    (
        TestReport,
        {
            "results_uri": "cas://results/1",
            "run_against": "task/T-1",
            "passed": 10,
            "failed": 0,
            "quarantined": ("test_flaky_clock",),
            "seq": SEQ,
        },
        "passed",
        9,
    ),
    (
        E2EReport,
        {
            "results_uri": "cas://e2e/1",
            "run_against": "c" * 40,
            "scenarios_passed": 3,
            "scenarios_failed": 0,
            "pinned_seeds_note": "seed=42, clock frozen 2026-01-01T00:00Z, network mocked",
            "seq": SEQ,
        },
        "scenarios_passed",
        2,
    ),
    (
        ThreatModel,
        {
            "threat_model_uri": "cas://tm/1",
            "run_against": "c" * 40,
            "oracle_bindings": {"TM-1": "cas://oracle/3"},
            "residual_item_ids": ("TM-2",),
            "seq": SEQ,
        },
        "threat_model_uri",
        "cas://tm/2",
    ),
    (
        SecurityTriage,
        {
            "run_against": "c" * 40,
            "severity": Severity.HIGH,
            "findings": ("add input validation on /login",),
            "seq": SEQ,
        },
        "run_against",
        "d" * 40,
    ),
    (
        SupplyChainReport,
        {
            "base_tree_sha": "a" * 40,
            "tree_sha": "c" * 40,
            "report_uri": "cas://supplychain/1",
            "policy_violations": (),
            "seq": SEQ,
        },
        "tree_sha",
        "d" * 40,
    ),
    (
        SBOMRef,
        {"sbom_uri": "cas://sbom/1", "tree_sha": "c" * 40, "seq": SEQ},
        "sbom_uri",
        "cas://sbom/2",
    ),
    (
        TriageDecision,
        {
            "route": RequestRoute.EVOLUTION,
            "tier": ChangeRiskTier.STANDARD,
            "usd_cap_microusd": 10_000_000,
            "seq": SEQ,
        },
        "usd_cap_microusd",
        20_000_000,
    ),
    (
        GuardianDecision,
        {
            "action": GuardianAction.PAUSE,
            "advisory": False,
            "reason": "loop signature (AC-1, test, sig-9f2) tripped",
            "seq": SEQ,
        },
        "reason",
        "cost threshold crossed",
    ),
    (
        BudgetDecision,
        {
            "action": BudgetAction.ESCALATE,
            "node_id": "architect",
            "spend_microusd": 1_500_000,
            "cap_microusd": 1_000_000,
            "seq": SEQ,
        },
        "node_id",
        "qa",
    ),
    (
        HumanDecision,
        {
            "subject_artifact_uri": "cas://design/1",
            "approver": "alice@example.com",
            "approved": True,
            "server_nonce": "sn-hitl-1",
            "seq": SEQ,
        },
        "approver",
        "bob@example.com",
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_constructs_and_is_frozen(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_extra_fields_rejected(cls, kwargs, field, changed):
    with pytest.raises(ValidationError):
        cls(**kwargs, not_a_declared_field="boom")


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_content_identity_stable_and_field_sensitive(cls, kwargs, field, changed):
    a = cls(**kwargs)
    b = cls(**dict(reversed(list(kwargs.items()))))  # construction order irrelevant
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic
    mutated = a.model_copy(update={field: changed})
    assert mutated.content_identity() != a.content_identity()


def test_set_valued_field_digest_is_order_independent():
    """M2: a frozenset field must hash stably regardless of iteration order, so the
    content digest can't drift across processes (CAS / evidence-chain integrity)."""
    from foundry.canonical import content_digest

    class _M(FrozenModel):
        tags: frozenset[str] = frozenset()

    m = _M(tags=frozenset({"z", "a", "m", "q", "b"}))
    # The digest equals the canonical digest with the set field explicitly SORTED —
    # i.e. iteration order can't leak into the hash.
    expected = content_digest({"tags": sorted(["z", "a", "m", "q", "b"])})
    assert m.content_identity() == expected


# ---------------------------------------------------------------------------
# Spot checks on key invariants
# ---------------------------------------------------------------------------


def test_triage_tier_is_the_governance_1e_enum():
    assert {t.value for t in ChangeRiskTier} == {"low", "standard", "high"}
    with pytest.raises(ValidationError):
        TriageDecision(route=RequestRoute.BUGFIX, tier="medium", usd_cap_microusd=1, seq=SEQ)


def test_task_carries_criterion_traceability():
    t = Task(
        id="T-9",
        spec_uri="cas://task/T-9",
        criterion_ids=["AC-1"],  # list coerces to tuple
        branch="task/T-9",
        seq=SEQ,
    )
    assert t.criterion_ids == ("AC-1",)
    assert t.status is TaskStatus.PENDING
    with pytest.raises(ValidationError):
        Task(id="T-9", spec_uri="u", branch="task/T-9", seq=SEQ)  # criterion_ids required


def test_criteria_critique_security_omission_blocks_spec_gate():
    base = {
        "prd_uri": "cas://prd/1",
        "judgment_provenance": JudgmentProvenance.SELF,
        "seq": SEQ,
    }
    clean = CriteriaCritique(**base)
    assert not clean.spec_gate_blocking()
    dirty = CriteriaCritique(**base, security_abuse_omissions=("missing abuse case",))
    assert dirty.spec_gate_blocking()


def test_review_request_is_not_a_verdict_and_carries_server_nonce():
    r = ReviewRequest(
        diff_uri="cas://diff/1", task_id="T-1", run_against="task/T-1",
        verdict_nonce="nonce-1", seq=SEQ,
    )
    assert r.verdict_nonce == "nonce-1"
    assert not hasattr(r, "status")  # the verdict lives in GateResult, not here


def test_e2e_report_flake_budget_defaults_to_n2():
    r = E2EReport(
        results_uri="cas://e2e/1", run_against="c" * 40,
        scenarios_passed=1, scenarios_failed=0,
        pinned_seeds_note="seed=42", seq=SEQ,
    )
    assert r.flake_retry_budget == 2


def test_test_plan_covered_criteria_union():
    tp = TestPlan(
        plan_uri="cas://plan/1",
        test_criteria={"t1": ("AC-1",), "t2": ("AC-1", "AC-2")},
        seq=SEQ,
    )
    assert tp.covered_criteria() == frozenset({"AC-1", "AC-2"})


def test_threat_model_residual_flag_and_dict_key_order_identity():
    kwargs = {
        "threat_model_uri": "cas://tm/1",
        "run_against": "c" * 40,
        "seq": SEQ,
    }
    a = ThreatModel(**kwargs, oracle_bindings={"TM-1": "u1", "TM-2": "u2"})
    b = ThreatModel(**kwargs, oracle_bindings={"TM-2": "u2", "TM-1": "u1"})
    assert a.content_identity() == b.content_identity()  # canonical: dict order irrelevant
    assert not a.has_residual()
    c = a.model_copy(update={"residual_item_ids": ("TM-3",)})
    assert c.has_residual()


def test_guardian_analyzer_is_advisory_monitor_trip_is_not():
    trip = GuardianDecision(action=GuardianAction.HALT, reason="oracle diff", seq=SEQ)
    assert trip.advisory is False  # deterministic trips are load-bearing by default
    flag = GuardianDecision(
        action=GuardianAction.ESCALATE, advisory=True, reason="semantic drift", seq=SEQ
    )
    assert flag.advisory is True


def test_versioned_supersession_fields_present_where_designed():
    prd = PRD(title="t", prd_uri="cas://prd/2", supersedes="cas://prd/1", version=2, seq=SEQ)
    assert prd.version == 2 and prd.supersedes == "cas://prd/1"
    ob = OracleBinding(
        criterion_id="AC-1", oracle_uri="cas://oracle/2", oracle_kind=OracleKind.GOLDEN,
        version=2, supersedes="cas://oracle/1", seq=SEQ,
    )
    assert ob.supersedes == "cas://oracle/1"
