"""Conformance tests for the Layer 1–7 agent artifact schemas.

Contract under test (docs/agents.md; conventions from foundry/schemas/base.py):
every model is a FrozenModel (frozen, extra="forbid"), carries `seq: Seq`, has
a content_identity() that is stable across construction order and sensitive to
any field change. Large bodies are `*_uri` CAS handles, never inline (DS9).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.architecture import ADR, InterfaceContract, OwnershipMap, SystemDesign
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import BudgetAction, BudgetDecision
from foundry.schemas.implementation import ConflictResolution, FeatureDiff, Task, TaskStatus
from foundry.schemas.product import (
    PRD,
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    IntentBrief,
    SprintPlan,
)
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport
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
        SystemDesign,
        {
            "title": "Todo service architecture",
            "design_uri": "cas://design/1",
            "components": ("api", "store"),
            "adr_ids": ("ADR-1",),
            "interface_ids": ("TodoStore.add",),
            "seq": SEQ,
        },
        "title",
        "Notes service architecture",
    ),
    (
        ADR,
        {
            "id": "ADR-1",
            "title": "Use Postgres for persistence",
            "decision": "Postgres over SQLite for concurrency",
            "body_uri": "cas://adr/1",
            "rejected_alternatives": ("SQLite", "in-memory dict"),
            "status": "accepted",
            "seq": SEQ,
        },
        "decision",
        "SQLite for simplicity",
    ),
    (
        InterfaceContract,
        {
            "name": "TodoStore.add",
            "spec_uri": "cas://iface/1",
            "consumers": ("api",),
            "seq": SEQ,
        },
        "name",
        "TodoStore.remove",
    ),
    (
        OwnershipMap,
        {
            "owners": {"src/api.py": "T-1", "src/store.py": "T-2"},
            "seq": SEQ,
        },
        "version",
        2,
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_frozen_and_closed(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)
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


def test_ownership_map_merge_order_is_deterministic():
    """Merge order is a pure function of the mapping — insertion order and
    dict key order do not affect it (resume re-derives the same order)."""
    a = OwnershipMap(owners={"src/z.py": "T-2", "src/a.py": "T-1", "src/b.py": "T-1"}, seq=SEQ)
    b = OwnershipMap(owners={"src/b.py": "T-1", "src/z.py": "T-2", "src/a.py": "T-1"}, seq=SEQ)
    # ordered by (owner, unit): T-1 units first (a, b), then T-2 (z)
    expected = ("src/a.py", "src/b.py", "src/z.py")
    assert a.merge_order() == expected
    assert b.merge_order() == expected  # independent of construction order
    # content_identity also order-independent (canonical dump)
    assert a.content_identity() == b.content_identity()
