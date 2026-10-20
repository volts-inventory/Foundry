"""Planner node — the whole product + architecture plan in ONE model call.

Drives the REAL planner over a REAL :class:`ModelProviderProxy` backed by
:class:`MockTransport` (no live API key) and asserts:

* one ``rt.proxy.complete`` call emits every artifact kind / channel the old
  persona chain wrote, each a content-addressed handle produced by ``planner``;
* the ownership map carries per-unit criterion ids (the leads' deterministic
  input) and the handles resolve back through the CAS;
* the criteria self-review drives the advisory Spec triage, never a verdict;
* a ``spec_fix`` re-entry feeds the newest FAILing Spec-gate findings (and the
  criteria already declared) into the prompt, and only a FAIL triggers it;
* exactly one journal entry, node_id ``planner`` / role ``Planner``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents.planner import (
    NODE_ID,
    SOP,
    _build_adrs,
    _build_constraints,
    _build_criteria,
    _build_interfaces,
    _build_ownership,
    make_planner_node,
)
from foundry.context import SymbolIndex
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import ConstraintScope, OwnershipMap
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    SprintPlan,
)
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import live_constraints, merge_constraints
from foundry.state.seq import Seq

KEY = b"dev-zone-b-planner-key"
KEY_ID = "dev-key-planner"

PLAN: dict[str, Any] = {
    "scope": {"tier": "standard", "rationale": "a real service", "signals": []},
    "intent": {
        "intent": "Let users manage a personal todo list.",
        "assumptions": ["single user"],
        "unknowns": [],
        "success_criteria": ["a user can add a todo"],
        "must_haves": ["add todo"],
        "nice_to_haves": [],
    },
    "prd": {
        "title": "Todo service",
        "body": "Users add and list todos.",
        "required_capabilities": [],
    },
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "normal",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401",
            "security_abuse": True,
        },
    ],
    "criteria_review": {"missing": [], "security_abuse_omissions": []},
    "sprint_plan": {
        "milestones": ["M1"],
        "usd_cap_microusd": 30_000_000,
        "risk_register": "none",
        "done_definition": "all criteria green",
    },
    "design": {"title": "Todo service", "body": "api -> store", "components": ["api", "store"]},
    "adrs": [
        {
            "id": "ADR-001",
            "title": "Use SQLite",
            "decision": "Persist todos in SQLite",
            "body": "single-node, embedded",
            "rejected_alternatives": ["Postgres", "in-memory dict"],
        },
        {
            "id": "ADR-002",
            "title": "Synchronous API",
            "decision": "Expose a synchronous REST API",
            "body": "simplicity over throughput",
            "rejected_alternatives": ["async"],
        },
    ],
    "interfaces": [
        {"name": "TodoStore.add", "spec": "add(todo) -> id", "consumers": ["api"]},
        {"name": "TodoStore.list", "spec": "list() -> [todo]", "consumers": ["api"]},
    ],
    "ownership": [
        {"unit": "todo/api.py", "owner": "task-api", "criterion_ids": ["AC-001", "AC-002"]},
        {"unit": "todo/store.py", "owner": "task-store", "criterion_ids": ["AC-001"]},
    ],
    "pinned_constraints": [
        {
            "id": "PC-001",
            "text": "All access tokens must be redacted from logs.",
            "scope": "global",
            "source": "ADR-001",
        },
        {
            "id": "PC-002",
            "text": "Todo titles are limited to 200 characters.",
            "scope": "local",
            "source": "ADR-002",
        },
    ],
}

EXPECTED_KINDS = {
    "intent_brief",
    "prd",
    "acceptance_criterion",
    "criteria_findings",
    "sprint_plan",
    "system_design",
    "adr",
    "interface_contract",
    "ownership_map",
}


def _plan(**overrides: Any) -> dict[str, Any]:
    return {**PLAN, **overrides}


def runtime_for(
    *payloads: dict[str, Any], cas: ArtifactStore | None = None
) -> tuple[AgentRuntime, MockTransport]:
    tx = MockTransport(completions=[json.dumps(p) for p in payloads])
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, cas=cas), tx


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": step}}


def _prompt(tx: MockTransport) -> str:
    return json.dumps(tx.requests[-1], ensure_ascii=False)


def _spec_verdict(status: GateStatus, findings: tuple[str, ...], epoch: int) -> GateResult:
    return GateResult(
        name="spec",
        status=status,
        decision_deterministic=True,
        execution_deterministic=False,
        verdict_class=VerdictClass.CONFIG_EVALUABLE,
        findings=findings,
        run_against="sha-spec",
        seq=Seq("r", epoch, epoch, "spec_gate", 0),
    )


# ---- one call, every downstream record ----------------------------------------


def test_one_model_call_emits_every_planning_artifact_kind() -> None:
    rt, tx = runtime_for(PLAN)
    writes = make_planner_node(rt)({"request": "build a todo app"}, config_for("r"))

    assert len(tx.requests) == 1
    artifacts = writes["artifacts"]
    assert {a.kind for a in artifacts} == EXPECTED_KINDS
    assert all(a.produced_by == NODE_ID for a in artifacts)
    assert all(a.uri.startswith("cas://") for a in artifacts)
    assert sum(a.kind == "acceptance_criterion" for a in artifacts) == 2
    assert sum(a.kind == "adr" for a in artifacts) == 2
    assert sum(a.kind == "interface_contract" for a in artifacts) == 2
    # Distinct ADRs get distinct handles (content_identity, not body digest).
    assert len({a.uri for a in artifacts if a.kind == "adr"}) == 2
    assert writes["scope_tier"].tier.value == "standard"
    assert {c.id for c in writes["pinned_constraints"]} == {"PC-001", "PC-002"}
    assert "gates" not in writes  # agents triage, never bless (L5)


def test_pinned_constraints_ride_their_own_channel_verbatim() -> None:
    rt, _ = runtime_for(PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    by_id = {c.id: c for c in writes["pinned_constraints"]}
    assert by_id["PC-001"].scope is ConstraintScope.GLOBAL
    assert "redacted from logs" in by_id["PC-001"].text
    assert by_id["PC-002"].scope is ConstraintScope.LOCAL
    seqs = [c.seq for c in writes["pinned_constraints"]]
    assert len(set(seqs)) == len(seqs)


def test_bodies_resolve_back_and_ownership_carries_unit_criteria() -> None:
    cas = ArtifactStore()
    rt, _ = runtime_for(PLAN, cas=cas)
    writes = make_planner_node(rt)({"request": "x", "route": "bugfix"}, config_for("r"))

    by_kind = {a.kind: a for a in writes["artifacts"]}
    ownership = cas.resolve(by_kind["ownership_map"])
    assert isinstance(ownership, OwnershipMap)
    assert ownership.owners == {"todo/api.py": "task-api", "todo/store.py": "task-store"}
    assert ownership.unit_criteria == {
        "todo/api.py": ("AC-001", "AC-002"),
        "todo/store.py": ("AC-001",),
    }
    prd = cas.resolve(by_kind["prd"])
    assert prd.criterion_ids == ("AC-001", "AC-002")
    criteria = [
        cas.resolve(a) for a in writes["artifacts"] if a.kind == "acceptance_criterion"
    ]
    assert all(isinstance(c, AcceptanceCriterion) and c.oracle_hint for c in criteria)
    # The sprint cap is clamped into the route's Triage envelope, never widened.
    plan = cas.resolve(by_kind["sprint_plan"])
    assert isinstance(plan, SprintPlan)
    assert plan.usd_cap_microusd == 5_000_000


def test_required_capabilities_flow_to_their_channel() -> None:
    rt, _ = runtime_for(_plan(prd={**PLAN["prd"], "required_capabilities": ["ui"]}))
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert writes["required_capabilities"] == ["ui"]


def test_one_journal_entry_as_the_planner() -> None:
    rt, _ = runtime_for(PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    [note] = writes["agent_journal"]
    assert note.node_id == "planner"
    assert note.role == "Planner"
    assert "2 acceptance criteria" in note.summary


def test_builders_skip_non_object_items() -> None:
    # CLI backends do not grammar-enforce nested schemas; junk items are skipped.
    seq = Seq("r", 1, 1, NODE_ID, 0)
    assert [a.id for a in _build_adrs([{"id": "ADR-1"}, "stray", None], seq, store=None)] == [
        "ADR-1"
    ]
    assert [i.name for i in _build_interfaces([{"name": "api"}, "x"], seq)] == ["api"]
    assert [c.id for c in _build_constraints([{"id": "PC-1", "text": "t"}, 42], seq)] == ["PC-1"]


def test_garbled_reply_still_yields_a_well_formed_plan() -> None:
    rt, _ = runtime_for({"intent": "nonsense", "ownership": "not-a-list"})
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert {a.kind for a in writes["artifacts"]} == EXPECTED_KINDS - {
        "acceptance_criterion",
        "adr",
        "interface_contract",
    }
    assert "Empty ownership map" in " ".join(writes["agent_journal"][0].concerns)


# ---- criteria self-review -> advisory Spec triage ------------------------------


def test_self_review_omission_is_flagged_blocking_and_stamped_self() -> None:
    cas = ArtifactStore()
    review = {"missing": [], "security_abuse_omissions": ["no rate limit on add"]}
    flagged = _plan(criteria_review=review)
    rt, _ = runtime_for(flagged, flagged, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    [triage] = writes["gate_triage"]
    assert triage.name == "spec"
    assert triage.produced_by == NODE_ID
    assert triage.explanation.startswith("BLOCKING")
    [handle] = [a for a in writes["artifacts"] if a.kind == "criteria_findings"]
    critique = cas.resolve(handle)
    assert isinstance(critique, CriteriaCritique)
    assert critique.spec_gate_blocking()
    assert critique.judgment_provenance is JudgmentProvenance.SELF


def test_clean_self_review_is_non_blocking() -> None:
    rt, _ = runtime_for(PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert not writes["gate_triage"][0].explanation.startswith("BLOCKING")


# ---- spec_fix re-entry -----------------------------------------------------------


def test_reentry_after_spec_fail_carries_the_findings_and_prior_criteria() -> None:
    cas = ArtifactStore()
    rt, tx = runtime_for(PLAN, PLAN, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    assert "REVISION PASS" not in _prompt(tx)

    finding = "no criterion flags security_abuse=true — security/abuse coverage is mandatory"
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, (finding,), epoch=1)],
    }
    second = node(state, config_for("r", step=5))

    prompt = _prompt(tx)
    assert len(tx.requests) == 2
    assert "REVISION PASS" in prompt
    assert "no criterion flags security_abuse=true" in prompt
    assert "id=AC-001" in prompt and "id=AC-002" in prompt  # reproduce, keep ids
    assert "scope_tier" not in second  # single-writer: the first pass's decision holds
    assert "Revised after a Spec-gate failure" in second["agent_journal"][0].summary


def test_a_newer_spec_pass_supersedes_an_older_fail() -> None:
    rt, tx = runtime_for(PLAN)
    state: FactoryState = {
        "request": "x",
        "gates": [
            _spec_verdict(GateStatus.FAIL, ("old finding",), epoch=1),
            _spec_verdict(GateStatus.PASS, (), epoch=2),
        ],
    }
    make_planner_node(rt)(state, config_for("r"))
    assert "REVISION PASS" not in _prompt(tx)
    assert "old finding" not in _prompt(tx)


# ---- prompt inputs -----------------------------------------------------------------


def test_design_spec_grounds_the_prompt() -> None:
    rt, tx = runtime_for(PLAN)
    make_planner_node(rt)(
        {"request": "short links", "spec": "# Spec\nSPEC_MARKER_9Q"}, config_for("r")
    )
    prompt = _prompt(tx)
    assert "DESIGN SPEC (detailed source of truth)" in prompt
    assert "SPEC_MARKER_9Q" in prompt


def test_request_only_prompt_has_no_spec_framing() -> None:
    rt, tx = runtime_for(PLAN)
    make_planner_node(rt)({"request": "short links"}, config_for("r"))
    assert "REQUEST (intent summary)" not in _prompt(tx)


def test_retrieves_existing_code_through_the_symbol_index(tmp_path: Any) -> None:
    (tmp_path / "mod.py").write_text(
        "def TodoStore():\n    return persist()\n\n\ndef persist():\n    return 1\n",
        encoding="utf-8",
    )
    rt, tx = runtime_for(PLAN)
    node = make_planner_node(rt, SymbolIndex.build(tmp_path))
    node({"request": "extend todos", "required_capabilities": ["TodoStore"]}, config_for("r"))

    prompt = _prompt(tx)
    assert "RETRIEVED CODE SPANS" in prompt
    assert "TodoStore" in prompt


def test_the_mock_backend_scripts_the_planner() -> None:
    # An unscripted node serves "" under --backend mock and dies as ProxyUnavailable.
    from foundry.cli import _SCRIPTS

    matched = [key for key in _SCRIPTS if key in SOP]
    assert matched == ["You are the Planner"]
    assert set(json.loads(_SCRIPTS[matched[0]])) >= {"intent", "prd", "ownership"}


# ---- tolerant parsing at the CLI-backend boundary --------------------------------


def test_off_schema_fields_degrade_to_defaults_instead_of_raising() -> None:
    seq = Seq("r", 1, 1, NODE_ID, 0)
    criteria = _build_criteria(
        [
            {"id": "AC-1", "text": "t", "criticality": "HIGH_RISK", "oracle_hint": "o"},
            {"id": "AC-2", "text": "t", "criticality": "High Risk", "oracle_hint": "o"},
            {"id": None, "text": None, "criticality": "critical!!", "oracle_hint": None,
             "security_abuse": "false"},
            {"id": "AC-4", "criticality": None, "security_abuse": "true"},
        ],
        seq,
    )
    assert [c.criticality for c in criteria] == [
        Criticality.HIGH_RISK, Criticality.HIGH_RISK, Criticality.NORMAL, Criticality.NORMAL
    ]
    assert criteria[2].id == "AC-002" and criteria[2].text == "" and not criteria[2].security_abuse
    assert criteria[3].security_abuse is True

    constraints = _build_constraints(
        [{"id": "PC-1", "text": "t", "scope": "LOCAL"}, {"id": "PC-2", "text": "t", "scope": 7}],
        seq,
    )
    # Unknown scope fails safe to global injection.
    assert [c.scope for c in constraints] == [ConstraintScope.LOCAL, ConstraintScope.GLOBAL]


def test_numeric_strings_and_floats_are_accepted_for_the_cap() -> None:
    for raw, expected in (("3000000", 3_000_000), (3e6, 3_000_000), ("lots", 5_000_000),
                          (float("nan"), 5_000_000)):
        cas = ArtifactStore()
        sprint = {**PLAN["sprint_plan"], "usd_cap_microusd": raw}
        rt, _ = runtime_for(_plan(sprint_plan=sprint), cas=cas)
        writes = make_planner_node(rt)({"request": "x", "route": "bugfix"}, config_for("r"))
        [handle] = [a for a in writes["artifacts"] if a.kind == "sprint_plan"]
        assert cas.resolve(handle).usd_cap_microusd == expected


def test_one_bad_field_does_not_kill_the_plan() -> None:
    bad = [{**PLAN["acceptance_criteria"][0], "criticality": "urgent"}]
    rt, _ = runtime_for(_plan(acceptance_criteria=bad, sprint_plan={"usd_cap_microusd": None}))
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert sum(a.kind == "acceptance_criterion" for a in writes["artifacts"]) == 1


# ---- ownership: later entry wins wholesale, unknown criterion ids dropped ------


def test_duplicate_ownership_unit_later_entry_wins_wholesale() -> None:
    ownership = _build_ownership(
        [
            {"unit": "a.py", "owner": "first", "criterion_ids": ["AC-001"]},
            {"unit": "b.py", "owner": "b", "criterion_ids": ["AC-001", "AC-999"]},
            {"unit": "a.py", "owner": "second", "criterion_ids": []},
        ],
        Seq("r", 1, 1, NODE_ID, 0),
        {"AC-001", "AC-002"},
    )
    assert ownership.owners == {"a.py": "second", "b.py": "b"}
    assert ownership.unit_criteria == {"b.py": ("AC-001",)}


# ---- self-review: at most one in-node revision ----------------------------------


def test_clean_self_review_makes_one_call() -> None:
    rt, tx = runtime_for(PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 1


def test_flagged_self_review_revises_once_in_node() -> None:
    flagged = _plan(criteria_review={"missing": [], "security_abuse_omissions": ["no rate limit"]})
    # Even if the revision still flags a gap, there is no third call.
    rt, tx = runtime_for(flagged, flagged, PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2
    assert "SELF-REVIEW REVISION" not in json.dumps(tx.requests[0])
    assert "SELF-REVIEW REVISION" in _prompt(tx) and "no rate limit" in _prompt(tx)
    assert "Self-revised once" in writes["agent_journal"][0].summary


def test_revision_output_is_what_gets_emitted() -> None:
    flagged = _plan(criteria_review={"missing": ["no delete criterion"],
                                     "security_abuse_omissions": []})
    rt, tx = runtime_for(flagged, PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 2
    assert writes["gate_triage"][0].explanation == "No criteria-completeness omissions found."


# ---- re-plan retracts its own dropped constraints ----------------------------------


def test_replan_retires_constraints_it_no_longer_emits() -> None:
    only_pc1 = _plan(pinned_constraints=[PLAN["pinned_constraints"][0]])
    rt, _ = runtime_for(PLAN, only_pc1)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    channel = merge_constraints(None, first["pinned_constraints"])
    operator_pin = first["pinned_constraints"][0].model_copy(
        update={"id": "OP-1", "seq": Seq("r", 1, 1, "operator", 0)}
    )
    channel = merge_constraints(channel, [operator_pin])

    state: FactoryState = {
        "request": "x",
        "pinned_constraints": channel,
        "gates": [_spec_verdict(GateStatus.FAIL, ("finding",), epoch=1)],
    }
    second = node(state, config_for("r", step=5))
    channel = merge_constraints(channel, second["pinned_constraints"])

    assert {c.id for c in live_constraints(channel)} == {"PC-001", "OP-1"}
