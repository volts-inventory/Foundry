"""Planner node — the whole product + architecture plan in ONE model call.

Drives the REAL planner over a REAL :class:`ModelProviderProxy` backed by
:class:`MockTransport` (no live API key) and asserts:

* one ``rt.proxy.complete`` call emits every artifact kind / channel the old
  persona chain wrote, each a content-addressed handle produced by ``planner``;
* tickets become ``ticket`` records whose scenarios become structured criteria,
  validated into a capped DAG deterministically;
* the ownership map is derived from the tickets' files, carries per-unit
  criterion ids (the leads' deterministic input) and the handles resolve back;
* the criteria self-review drives the advisory Spec triage, never a verdict;
* a ``spec_fix`` re-entry feeds the newest FAILing Spec-gate findings (and the
  criteria already declared) into the prompt, and only a FAIL triggers it;
* exactly one journal entry, node_id ``planner`` / role ``Planner``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime, planner
from foundry.agents.planner import (
    MAX_EPIC_TICKETS,
    MAX_EPICS,
    MAX_TICKETS,
    NODE_ID,
    SOP,
    _build_adrs,
    _build_constraints,
    _build_criterion,
    _build_epics,
    _build_interfaces,
    _build_tickets,
    _epic_items,
    _ownership_from_tickets,
    _prior_plan,
    make_planner_node,
)
from foundry.context import SymbolIndex
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, ResponseScript
from foundry.proxy.token_meter import TokenCapExceeded, TokenMeter
from foundry.schemas.architecture import ConstraintScope, OwnershipMap, lane_of_area
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.implementation import Difficulty, Specialty, Ticket
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    SprintPlan,
)
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import live_constraints, merge_constraints
from foundry.state.seq import Seq
from tests.planner_payload import (
    EPIC_PLANNER_SOP_KEY,
    PLANNER_SOP_KEY,
    _review,
    multi_epic_plan,
)

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
    "tickets": [
        {
            "id": "T-001",
            "title": "Persist todos",
            "specialty": "data",
            "difficulty": "standard",
            "depends_on": [],
            "files": ["todo/store.py"],
            "implements": ["TodoStore.add", "TodoStore.list"],
            "consumes": [],
            "scenarios": [
                {
                    "id": "AC-003",
                    "title": "empty store lists nothing",
                    "given": ["an empty store"],
                    "when": ["todos are listed"],
                    "then": ["the list is empty"],
                    "criticality": "normal",
                    "security_abuse": False,
                },
            ],
        },
        {
            "id": "T-002",
            "title": "Add-todo endpoint",
            "description": "POST /todos stores a todo for the caller.",
            "specialty": "backend",
            "difficulty": "complex",
            "depends_on": ["T-001"],
            "files": ["todo/api.py"],
            "implements": [],
            "consumes": ["TodoStore.add"],
            "scenarios": [
                {
                    "id": "AC-001",
                    "given": ["a user"],
                    "when": ["they add a todo"],
                    "then": ["it appears", "the count is 1"],
                    "criticality": "normal",
                    "security_abuse": False,
                },
                {
                    "id": "AC-002",
                    "given": ["an unauthenticated request"],
                    "when": ["it hits /todos"],
                    "then": ["the response is 401"],
                    "criticality": "high-risk",
                    "security_abuse": True,
                },
            ],
        },
        {
            "id": "T-003",
            "title": "Step definitions for the todo features",
            "specialty": "test",
            "difficulty": "trivial",
            "depends_on": ["T-001", "T-002"],
            "files": ["tests/step_defs/test_todo.py"],
            "implements": [],
            "consumes": [],
            "scenarios": [],
        },
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
    "ticket",
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
    assert sum(a.kind == "acceptance_criterion" for a in artifacts) == 3
    assert sum(a.kind == "ticket" for a in artifacts) == 3
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
    assert ownership.owners == {
        "todo/store.py": "data-T-001",
        "todo/api.py": "backend-T-002",
        "tests/step_defs/test_todo.py": "test-T-003",
    }
    assert ownership.unit_criteria == {
        "todo/store.py": ("AC-003",),
        "todo/api.py": ("AC-001", "AC-002"),
        "tests/step_defs/test_todo.py": ("AC-003", "AC-001", "AC-002"),
    }
    # The current scheduler's lanes still follow the specialty; test files -> backend.
    lanes = {o: lane_of_area(ownership.area_of(o)) for o in ownership.leads()}
    assert lanes == {"data-T-001": "data", "backend-T-002": "backend", "test-T-003": "backend"}
    prd = cas.resolve(by_kind["prd"])
    assert set(prd.criterion_ids) == {"AC-001", "AC-002", "AC-003"}
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
    assert "3 acceptance criteria" in note.summary
    assert "3 tickets" in note.summary
    assert note.concerns == ()  # a clean DAG with every feature ticket tested


def test_builders_skip_non_object_items() -> None:
    # CLI backends do not grammar-enforce nested schemas; junk items are skipped.
    seq = Seq("r", 1, 1, NODE_ID, 0)
    assert [a.id for a in _build_adrs([{"id": "ADR-1"}, "stray", None], seq, store=None)] == [
        "ADR-1"
    ]
    assert [i.name for i in _build_interfaces([{"name": "api"}, "x"], seq)] == ["api"]
    assert [c.id for c in _build_constraints([{"id": "PC-1", "text": "t"}, 42], seq)] == ["PC-1"]


def test_garbled_reply_still_yields_a_well_formed_plan() -> None:
    rt, _ = runtime_for({"intent": "nonsense", "tickets": "not-a-list"})
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert {a.kind for a in writes["artifacts"]} == EXPECTED_KINDS - {
        "acceptance_criterion",
        "adr",
        "interface_contract",
        "ticket",
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
    assert "id=AC-001 (ticket=T-002)" in prompt and "id=AC-002" in prompt  # keep ids
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
    payload = json.loads(_SCRIPTS[matched[0]])
    assert set(payload) >= {"intent", "prd", "tickets"}
    assert "ownership" not in payload and "acceptance_criteria" not in payload


# ---- tolerant parsing at the CLI-backend boundary --------------------------------


def test_off_schema_fields_degrade_to_defaults_instead_of_raising() -> None:
    seq = Seq("r", 1, 1, NODE_ID, 0)
    criteria = [
        _build_criterion(item, ticket_id="T-1", default_id=f"AC-{i:03d}", seq=seq)
        for i, item in enumerate([
            {"id": "AC-1", "given": ["g"], "when": ["w"], "then": ["t"],
             "criticality": "HIGH_RISK"},
            {"id": "AC-2", "given": "g", "when": ["w", None, " "], "then": ["t"],
             "criticality": "High Risk"},
            {"id": None, "text": None, "criticality": "critical!!", "security_abuse": "false"},
            {"id": "AC-4", "criticality": None, "security_abuse": "true", "text": "legacy"},
        ])
    ]
    assert [c.criticality for c in criteria] == [
        Criticality.HIGH_RISK, Criticality.HIGH_RISK, Criticality.NORMAL, Criticality.NORMAL
    ]
    assert criteria[1].given == ("g",) and criteria[1].when == ("w",)
    assert criteria[2].id == "AC-002" and criteria[2].text == "" and not criteria[2].security_abuse
    assert criteria[3].security_abuse is True
    # No steps: the free-text form is kept and the gate validates it as text.
    assert criteria[3].text == "legacy" and criteria[3].given == ()
    # The oracle is always the scenario reference.
    assert criteria[2].oracle_hint == "features/T-1.feature::AC-002"

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
    first = PLAN["tickets"][0]
    bad = {**first, "difficulty": "brutal", "specialty": None,
           "scenarios": [{**first["scenarios"][0], "criticality": "urgent"}]}
    rt, _ = runtime_for(_plan(tickets=[bad], sprint_plan={"usd_cap_microusd": None}))
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert sum(a.kind == "acceptance_criterion" for a in writes["artifacts"]) == 1
    assert sum(a.kind == "ticket" for a in writes["artifacts"]) == 1


# ---- tickets: parsing, DAG validation, cap, ownership derivation -----------------

_TSEQ = Seq("r", 1, 1, NODE_ID, 0)
_CSEQ = Seq("r", 1, 1, NODE_ID, 1)


def _ticket(tid: str, *deps: str, **extra: Any) -> dict[str, Any]:
    return {"id": tid, "title": tid, "specialty": "backend", "difficulty": "standard",
            "depends_on": list(deps), "files": [f"{tid}.py"], "scenarios": [], **extra}


def _scn(cid: str, *, abuse: bool = False) -> dict[str, Any]:
    return {"id": cid, "given": ["g"], "when": ["w"], "then": ["t"], "security_abuse": abuse}


def test_tickets_parse_with_tolerant_enums_and_contract_filtering() -> None:
    tickets, criteria, concerns = _build_tickets(
        [
            _ticket("T-1", specialty="Frontend", difficulty="COMPLEX",
                    implements=["Api.get", "Nope"], consumes="Api.get",
                    scenarios=[_scn("AC-1"), "stray"]),
            _ticket("T-2", "T-1", specialty="devops", difficulty="hard"),
            _ticket("T-3", "T-1", "T-2", specialty="test", scenarios=[_scn("AC-3")]),
            "not-a-ticket",
        ],
        _TSEQ, _CSEQ, {"Api.get"},
    )
    t1, t2, t3 = tickets
    assert all(isinstance(t, Ticket) for t in tickets)
    assert (t1.specialty, t1.difficulty) == (Specialty.FRONTEND, Difficulty.COMPLEX)
    assert (t2.specialty, t2.difficulty) == (Specialty.BACKEND, Difficulty.STANDARD)
    assert t1.implements == ("Api.get",) and t1.consumes == ("Api.get",)
    assert t1.scenario_ids == ("AC-1",) and t2.scenario_ids == ()
    # A test ticket covers its own scenarios plus its feature dependencies'.
    assert t3.scenario_ids == ("AC-3", "AC-1")
    assert [(c.id, c.ticket_id) for c in criteria] == [("AC-1", "T-1"), ("AC-3", "T-3")]
    assert len({c.seq for c in criteria}) == 2 and len({t.seq for t in tickets}) == 3
    assert any("unknown implements contracts Nope" in c for c in concerns)


def test_scenarios_become_structured_criteria() -> None:
    _, [c], _ = _build_tickets(
        [_ticket("T-9", scenarios=[{**_scn("AC-7", abuse=True), "then": ["a", "b"],
                                    "criticality": "high-risk"}])],
        _TSEQ, _CSEQ, set(),
    )
    assert c.given == ("g",) and c.when == ("w",) and c.then == ("a", "b")
    assert c.text == "Given g When w Then a and b"
    assert c.ticket_id == "T-9" and c.security_abuse
    assert c.oracle_hint == "features/T-9.feature::AC-7"
    assert c.criticality is Criticality.HIGH_RISK


def test_unknown_dependencies_are_dropped_with_a_concern() -> None:
    [t], _, concerns = _build_tickets([_ticket("T-1", "T-404", "T-1x")], _TSEQ, _CSEQ, set())
    assert t.depends_on == ()
    assert concerns == ["Ticket T-1: dropped unknown dependencies T-404, T-1x."]


def test_cycles_lose_their_closing_edge() -> None:
    tickets, _, concerns = _build_tickets(
        [_ticket("A", "B"), _ticket("B", "C"), _ticket("C", "A"), _ticket("D", "D", "A")],
        _TSEQ, _CSEQ, set(),
    )
    deps = {t.id: t.depends_on for t in tickets}
    assert deps == {"A": ("B",), "B": ("C",), "C": (), "D": ("A",)}
    assert concerns == [
        "Dropped dependency C -> A: it closed a cycle.",
        "Dropped dependency D -> D: it closed a cycle.",
    ]


def test_ticket_count_is_capped_and_deps_on_dropped_tickets_go() -> None:
    raw = [_ticket(f"T-{i:03d}") for i in range(MAX_TICKETS + 5)]
    raw[0] = _ticket("T-000", f"T-{MAX_TICKETS + 1:03d}")
    tickets, _, concerns = _build_tickets(raw, _TSEQ, _CSEQ, set())
    assert len(tickets) == MAX_TICKETS == 40
    assert tickets[0].depends_on == ()
    assert any("exceed the cap of 40" in c for c in concerns)


def test_duplicate_ticket_and_scenario_ids_later_entry_wins() -> None:
    tickets, criteria, concerns = _build_tickets(
        [
            _ticket("T-1", title="first", scenarios=[_scn("AC-1")]),
            _ticket("T-2", scenarios=[_scn("AC-2")]),
            _ticket("T-1", title="second", files=["b.py"], scenarios=[]),
            _ticket("T-3", scenarios=[{**_scn("AC-2"), "given": ["later"]}]),
        ],
        _TSEQ, _CSEQ, set(),
    )
    by_id = {t.id: t for t in tickets}
    assert by_id["T-1"].title == "second" and by_id["T-1"].files == ("b.py",)
    assert by_id["T-1"].scenario_ids == () and by_id["T-2"].scenario_ids == ()
    assert by_id["T-3"].scenario_ids == ("AC-2",)
    assert [(c.id, c.ticket_id, c.given) for c in criteria] == [("AC-2", "T-3", ("later",))]
    assert "Duplicate ticket id T-1: the later entry wins." in concerns
    assert "Duplicate scenario id AC-2: the later entry wins." in concerns


def test_untested_feature_ticket_is_a_concern() -> None:
    _, _, concerns = _build_tickets([_ticket("T-1", scenarios=[_scn("AC-1")])],
                                    _TSEQ, _CSEQ, set())
    assert concerns == ["Ticket T-1 has scenarios but no test ticket depends on it."]


def test_ownership_is_derived_from_ticket_files() -> None:
    tickets, _, _ = _build_tickets(
        [
            _ticket("T-1", specialty="frontend", files=["web/page.html", "shared.py"],
                    scenarios=[_scn("AC-1")]),
            _ticket("T-2", files=["shared.py"], scenarios=[_scn("AC-2")]),
            _ticket("T-3", files=[]),
            _ticket("T-4", "T-1", specialty="test", files=["tests/test_shared.py"]),
        ],
        _TSEQ, _CSEQ, set(),
    )
    ownership = _ownership_from_tickets(tickets, _TSEQ)
    # A shared file goes to the later ticket and keeps both tickets' scenarios.
    assert ownership.owners == {
        "web/page.html": "frontend-T-1", "shared.py": "backend-T-2",
        "tests/test_shared.py": "test-T-4",
    }
    assert ownership.unit_criteria == {
        "web/page.html": ("AC-1",), "shared.py": ("AC-1", "AC-2"),
        "tests/test_shared.py": ("AC-1",),
    }
    assert lane_of_area(ownership.area_of("frontend-T-1")) == "frontend"
    # Step definitions build in the backend lane.
    assert lane_of_area(ownership.area_of("test-T-4")) == "backend"


def test_emitted_tickets_and_criteria_resolve_with_their_fields() -> None:
    cas = ArtifactStore()
    rt, _ = runtime_for(PLAN, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    tickets = {
        t.id: t for t in (cas.resolve(a) for a in writes["artifacts"] if a.kind == "ticket")
    }
    assert set(tickets) == {"T-001", "T-002", "T-003"}
    t2 = tickets["T-002"]
    assert isinstance(t2, Ticket)
    assert (t2.specialty, t2.difficulty, t2.depends_on) == (
        Specialty.BACKEND, Difficulty.COMPLEX, ("T-001",)
    )
    assert t2.consumes == ("TodoStore.add",) and t2.scenario_ids == ("AC-001", "AC-002")
    assert tickets["T-003"].scenario_ids == ("AC-003", "AC-001", "AC-002")
    criteria = {
        c.id: c
        for c in (cas.resolve(a) for a in writes["artifacts"] if a.kind == "acceptance_criterion")
    }
    ac1 = criteria["AC-001"]
    assert ac1.ticket_id == "T-002" and ac1.then == ("it appears", "the count is 1")
    assert ac1.text == "Given a user When they add a todo Then it appears and the count is 1"
    assert ac1.oracle_hint == "features/T-002.feature::AC-001"


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


def test_revision_with_tickets_emits_the_revised_tickets() -> None:
    flagged = _plan(criteria_review={"missing": [], "security_abuse_omissions": ["no rate limit"]})
    extra = {**PLAN["tickets"][1], "id": "T-004", "depends_on": ["T-001"],
             "files": ["todo/limits.py"],
             "scenarios": [{"id": "AC-009", "given": ["100 adds in a second"],
                            "when": ["one more add arrives"], "then": ["it is rejected"],
                            "criticality": "high-risk", "security_abuse": True}]}
    revised = _plan(tickets=[*PLAN["tickets"], extra])
    cas = ArtifactStore()
    rt, tx = runtime_for(flagged, revised, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2
    assert "same ids for tickets and scenarios" in _prompt(tx)
    assert sum(a.kind == "ticket" for a in writes["artifacts"]) == 4
    [handle] = [a for a in writes["artifacts"] if a.kind == "ownership_map"]
    assert cas.resolve(handle).unit_criteria["todo/limits.py"] == ("AC-009",)


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


def test_replan_retires_tickets_it_no_longer_emits() -> None:
    from foundry.agents.implementation.tickets import read_tickets

    cas = ArtifactStore()
    revised = _plan(tickets=[t for t in PLAN["tickets"] if t["id"] != "T-003"])
    rt, _ = runtime_for(PLAN, revised, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, ("finding",), epoch=1)],
    }
    second = node(state, config_for("r", step=5))
    after: FactoryState = {**state, "artifacts": [*first["artifacts"], *second["artifacts"]]}
    assert [t.id for t in read_tickets(after, cas.resolve)] == ["T-001", "T-002"]
    assert "retired ticket(s) T-003" in " ".join(second["agent_journal"][0].concerns)


def test_ticket_ids_are_made_file_safe_and_references_follow() -> None:
    tickets, criteria, concerns = _build_tickets(
        [_ticket("a/b"), _ticket("a-b"), _ticket("x y", "a/b", scenarios=[_scn("AC-1")])],
        _TSEQ, _CSEQ, set(),
    )
    assert [t.id for t in tickets] == ["a-b-2", "a-b", "x-y"]
    assert tickets[2].depends_on == ("a-b-2",)
    assert [(c.ticket_id, c.oracle_hint) for c in criteria] == [
        ("x-y", "features/x-y.feature::AC-1")
    ]
    assert any("'a/b' renamed to 'a-b-2'" in c for c in concerns)


# ---- epics -> tickets ---------------------------------------------------------------


def _match(*needles: str) -> Callable[[dict[str, Any]], bool]:
    return lambda request: all(n in json.dumps(request, ensure_ascii=False) for n in needles)


def _epic_scripts(
    plan: dict[str, Any], replies: dict[str, dict[str, Any]],
    *extra: ResponseScript,
) -> list[ResponseScript]:
    return [
        *extra,
        ResponseScript(json.dumps(plan), match=_match(PLANNER_SOP_KEY)),
        *(ResponseScript(json.dumps(r), match=_match(f"YOUR EPIC: {eid}"))
          for eid, r in replies.items()),
    ]


def _epic_runtime(
    *scripts: ResponseScript, cas: ArtifactStore | None = None,
    transport: type[MockTransport] = MockTransport,
) -> tuple[AgentRuntime, MockTransport]:
    tx = transport(scripts)
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, cas=cas), tx


def _epic_of(request: dict[str, Any]) -> str | None:
    blob = json.dumps(request, ensure_ascii=False)
    return next((e for e in ("E1", "E2", "E3", "E4") if f"YOUR EPIC: {e}" in blob), None)


def _bodies(cas: ArtifactStore, writes: dict[str, Any], kind: str) -> list[Any]:
    return [cas.resolve(a) for a in writes["artifacts"] if a.kind == kind]


def test_single_epic_with_inline_tickets_is_one_call() -> None:
    epic = {"id": "E1", "title": "Todo", "summary": "all of it", "depends_on": [],
            "provides": [], "consumes": []}
    rt, tx = runtime_for(_plan(epics=[epic]))
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 1
    assert sum(a.kind == "ticket" for a in writes["artifacts"]) == 3


def test_multi_epic_plan_makes_one_ticket_call_per_epic_with_namespaced_ids() -> None:
    plan, replies = multi_epic_plan()
    cas = ArtifactStore()
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "notes app"}, config_for("r"))

    assert len(tx.requests) == 1 + 3
    assert sorted(filter(None, map(_epic_of, tx.requests))) == ["E1", "E2", "E3"]
    epic_prompt = json.dumps(next(r for r in tx.requests if _epic_of(r) == "E2"))
    assert EPIC_PLANNER_SOP_KEY in epic_prompt and PLANNER_SOP_KEY not in epic_prompt
    assert "Store.load: load(id) -> note" in epic_prompt  # the shared contracts
    # No ADRs planned; the epics ride as their own records.
    assert {a.kind for a in writes["artifacts"]} == (EXPECTED_KINDS - {"adr"}) | {"epic"}

    tickets = {t.id: t for t in _bodies(cas, writes, "ticket")}
    assert list(tickets) == [
        "E1-T01", "E1-T02", "E1-T03", "E2-T01", "E2-T02", "E3-web-page", "E3-T02",
    ]
    assert tickets["E3-T02"].depends_on == ("E3-web-page",)  # sanitized refs follow
    criteria = {c.id: c for c in _bodies(cas, writes, "acceptance_criterion")}
    assert set(criteria) == {"E1-AC01", "E1-AC02", "E2-AC01", "E3-AC01"}
    assert criteria["E3-AC01"].oracle_hint == "features/E3-web-page.feature::E3-AC01"
    [note] = writes["agent_journal"]
    assert "7 tickets" in note.summary and "3 parallel call(s) (E1, E2, E3)" in note.summary
    assert any("'E3-web page' renamed to 'E3-web-page'" in c for c in note.concerns)


def test_cross_epic_order_follows_interfaces_then_sinks() -> None:
    plan, replies = multi_epic_plan()
    cas = ArtifactStore()
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    deps = {t.id: t.depends_on for t in _bodies(cas, writes, "ticket")}

    assert deps["E1-T01"] == ()  # E1 depends on no epic
    # E2 consumes Store.load: its root waits for E1's implementer of it only.
    assert deps["E2-T01"] == ("E1-T02",)
    assert deps["E2-T02"] == ("E2-T01",)  # non-roots inherit the order transitively
    # E3 consumes nothing E2 implements: its root waits for E2's sink ticket.
    assert deps["E3-web-page"] == ("E2-T02",)


def test_a_dependency_on_another_epics_ticket_is_dropped() -> None:
    plan, replies = multi_epic_plan()
    replies["E2"]["tickets"][0]["depends_on"] = ["E1-T01"]
    cas = ArtifactStore()
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    deps = {t.id: t.depends_on for t in _bodies(cas, writes, "ticket")}
    assert deps["E2-T01"] == ("E1-T02",)
    assert any("E2-T01: dropped unknown dependencies E2-E1-T01" in c
               for c in writes["agent_journal"][0].concerns)


def test_epic_calls_overlap_in_time() -> None:
    spans: dict[str, tuple[float, float]] = {}

    class Slow(MockTransport):
        def send(self, request: dict[str, Any]) -> Any:
            epic = _epic_of(request)
            start = time.monotonic()
            if epic:
                time.sleep(0.3)
                spans[epic] = (start, time.monotonic())
            return super().send(request)

    plan, replies = multi_epic_plan()
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), transport=Slow)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert set(spans) == {"E1", "E2", "E3"}
    assert max(s for s, _ in spans.values()) < min(e for _, e in spans.values())


def test_token_cap_from_a_parallel_epic_call_propagates() -> None:
    plan, replies = multi_epic_plan()
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies))
    # Call 1 fits; it spends the cap, so every epic call stops before sending.
    rt.proxy.token_meter = TokenMeter(1)
    with pytest.raises(TokenCapExceeded):
        make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 1


def test_global_and_per_epic_ticket_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    plan, replies = multi_epic_plan()
    replies["E1"]["tickets"] = [
        {**replies["E1"]["tickets"][0], "id": f"T{i:02d}", "files": [f"f{i}.py"]}
        for i in range(MAX_EPIC_TICKETS + 3)
    ]
    monkeypatch.setattr(planner, "MAX_PLAN_TICKETS", MAX_EPIC_TICKETS + 2)
    cas = ArtifactStore()
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    ids = [t.id for t in _bodies(cas, writes, "ticket")]
    # Merge order is epic then ticket order, so the global cap drops the tail: E3.
    assert len(ids) == MAX_EPIC_TICKETS + 2
    assert ids[-2:] == ["E2-T01", "E2-T02"]
    concerns = " ".join(writes["agent_journal"][0].concerns)
    assert f"per-epic cap of {MAX_EPIC_TICKETS}" in concerns
    assert f"exceed the cap of {MAX_EPIC_TICKETS + 2}" in concerns


def test_only_flagged_epics_self_revise_and_their_gaps_are_tagged() -> None:
    plan, replies = multi_epic_plan()
    flagged = {**replies["E2"], "criteria_review": _review("no paging scenario")}
    still = {**replies["E2"], "criteria_review": _review("no sort scenario")}
    cas = ArtifactStore()
    rt, tx = _epic_runtime(
        *_epic_scripts(
            plan, {**replies, "E2": flagged},
            ResponseScript(json.dumps(still), match=_match("YOUR EPIC: E2", "SELF-REVIEW")),
        ),
        cas=cas,
    )
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 1 + 3 + 1
    revisions = [_epic_of(r) for r in tx.requests if "SELF-REVIEW" in json.dumps(r)]
    assert revisions == ["E2"]
    [critique] = _bodies(cas, writes, "criteria_findings")
    assert critique.missing == ("[E2] no sort scenario",)
    assert "Self-revised epic(s) E2 once" in writes["agent_journal"][0].summary


def _first_pass(cas: ArtifactStore, *extra: ResponseScript) -> tuple[
    Any, MockTransport, dict[str, Any]
]:
    plan, replies = multi_epic_plan()
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies, *extra), cas=cas)
    node = make_planner_node(rt)
    return node, tx, node({"request": "x"}, config_for("r"))


def _failed(first: dict[str, Any], finding: str) -> FactoryState:
    return {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, (finding,), epoch=1)],
    }


def test_spec_fix_naming_a_criterion_reruns_only_its_epic() -> None:
    from foundry.agents.implementation.tickets import read_tickets

    _, replies = multi_epic_plan()
    revised = {**replies["E2"], "tickets": [
        replies["E2"]["tickets"][0], {**replies["E2"]["tickets"][1], "id": "T03"},
    ]}
    cas = ArtifactStore()
    node, tx, first = _first_pass(
        cas, ResponseScript(json.dumps(revised), match=_match("YOUR EPIC: E2", "REVISION PASS"))
    )
    state = _failed(first, "criterion E2-AC01: scenario has no then step")
    second = node(state, config_for("r", step=5))

    new = tx.requests[4:]
    assert [_epic_of(r) for r in new] == ["E2"]
    prompt = json.dumps(new[0], ensure_ascii=False)
    assert "E2-AC01: scenario has no then step" in prompt and "E2-T02" in prompt
    assert {a.kind for a in second["artifacts"]} == {
        "prd", "acceptance_criterion", "criteria_findings", "ticket", "ownership_map"
    }
    after: FactoryState = {**state, "artifacts": [*first["artifacts"], *second["artifacts"]]}
    live = {t.id: t for t in read_tickets(after, cas.resolve)}
    assert "E2-T02" not in live and "E2-T03" in live  # tombstoned, replaced
    assert live["E3-web-page"].depends_on == ("E2-T03",)  # re-linked to E2's new sink
    assert live["E1-T02"].scenario_ids == ("E1-AC02",)  # untouched epics kept
    assert "Re-planned epic(s) E2" in second["agent_journal"][0].summary


def test_spec_fix_with_a_plan_level_finding_reruns_everything() -> None:
    cas = ArtifactStore()
    node, tx, first = _first_pass(cas)
    state = _failed(first, "no criterion flags security_abuse=true")
    node(state, config_for("r", step=5))
    new = tx.requests[4:]
    assert len(new) == 4
    assert PLANNER_SOP_KEY in json.dumps(new[0]) and "REVISION PASS" in json.dumps(new[0])
    assert sorted(filter(None, map(_epic_of, new))) == ["E1", "E2", "E3"]


def test_epics_are_positional_capped_and_validated() -> None:
    raw = [
        {"id": "store", "depends_on": ["api"], "provides": ["X", "nope"]},
        {"id": "api", "depends_on": ["store", "ghost"]},
        *({"id": f"e{i}"} for i in range(MAX_EPICS)),
    ]
    epics, concerns = _build_epics(raw, {"X"})
    assert len(epics) == MAX_EPICS
    assert [e.id for e in epics[:2]] == ["E1", "E2"]
    assert epics[0].provides == ("X",)
    assert (epics[0].depends_on, epics[1].depends_on) == (("E2",), ())
    assert any("exceed the cap of 12" in c for c in concerns)
    assert "Epic E2: dropped unknown dependencies ghost." in concerns
    assert "Dropped epic dependency E2 -> E1: it closed a cycle." in concerns


def test_epic_items_are_namespaced_and_capped() -> None:
    epic = _build_epics([{"id": "E1"}], set())[0][0]
    items, concerns = _epic_items(epic, [
        {"id": "T01", "depends_on": ["E1-T02"], "scenarios": [{"id": ""}, {"id": "AC9"}]},
        {"id": "E1-T02"},
        {},
        *({"id": f"X{i}"} for i in range(MAX_EPIC_TICKETS)),
    ])
    assert len(items) == MAX_EPIC_TICKETS
    assert [i["id"] for i in items[:3]] == ["E1-T01", "E1-T02", "E1-T03"]
    assert items[0]["depends_on"] == ["E1-T02"]
    assert [s["id"] for s in items[0]["scenarios"]] == ["E1-AC01", "E1-AC9"]
    assert any(f"per-epic cap of {MAX_EPIC_TICKETS}" in c for c in concerns)


def _run_epics(
    plan: dict[str, Any], replies: dict[str, dict[str, Any]], cas: ArtifactStore
) -> tuple[AgentRuntime, dict[str, Any]]:
    rt, _ = _epic_runtime(*_epic_scripts(plan, replies), cas=cas)
    return rt, make_planner_node(rt)({"request": "x"}, config_for("r"))


def test_an_empty_upstream_epic_is_looked_through_to_its_own_upstream() -> None:
    plan, replies = multi_epic_plan()
    replies["E2"]["tickets"] = []
    cas = ArtifactStore()
    _, writes = _run_epics(plan, replies, cas)
    deps = {t.id: t.depends_on for t in _bodies(cas, writes, "ticket")}
    # E3 -> E2 (no tickets) -> E1: E3's root still waits for E1's sink.
    assert deps["E3-web-page"] == ("E1-T03",)


def test_cross_epic_links_never_count_as_test_coverage() -> None:
    plan, replies = multi_epic_plan()
    replies["E1"]["tickets"] = replies["E1"]["tickets"][:2]  # E1 loses its test ticket
    replies["E2"]["tickets"] = [_ticket("T01", specialty="test")]
    cas = ArtifactStore()
    _, writes = _run_epics(plan, replies, cas)
    tickets = {t.id: t for t in _bodies(cas, writes, "ticket")}
    assert tickets["E2-T01"].depends_on == ("E1-T02",)  # ordered after E1 ...
    assert tickets["E2-T01"].scenario_ids == ()  # ... but covers none of its scenarios
    assert "Ticket E1-T02 has scenarios but no test ticket depends on it." in (
        writes["agent_journal"][0].concerns
    )


def test_epics_are_recorded_and_read_back_whole_for_a_targeted_replan() -> None:
    plan, replies = multi_epic_plan()
    replies["E2"]["tickets"] = []
    cas = ArtifactStore()
    rt, writes = _run_epics(plan, replies, cas)
    prior = _prior_plan(_failed(writes, "criterion E1-AC01: vague"), rt)
    assert prior is not None
    assert [(e.id, e.title, e.provides, e.consumes) for e in prior.epics] == [
        ("E1", "Storage", ("Store.save", "Store.load"), ()),
        ("E2", "API", ("Api.http",), ("Store.load",)),  # no tickets, still an epic
        ("E3", "Web", (), ()),
    ]


def test_a_scenario_dropped_by_a_replan_stays_dropped() -> None:
    _, replies = multi_epic_plan()
    e2 = replies["E2"]["tickets"]
    revised = {**replies["E2"], "tickets": [{**e2[0], "scenarios": [_scn("AC05")]}, e2[1]]}
    cas = ArtifactStore()
    node, tx, first = _first_pass(
        cas, ResponseScript(json.dumps(revised), match=_match("YOUR EPIC: E2", "REVISION PASS"))
    )
    state = _failed(first, "criterion E2-AC01: scenario has no then step")
    second = node(state, config_for("r", step=5))
    after: FactoryState = {**state, "artifacts": [*first["artifacts"], *second["artifacts"]]}
    rt = AgentRuntime(proxy=ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID)), cas=cas)
    prior = _prior_plan(after, rt)
    assert prior is not None
    assert {c for c in prior.criteria if c.startswith("E2-")} == {"E2-AC05"}


def test_a_full_replan_pairs_epics_by_title_not_position() -> None:
    plan, replies = multi_epic_plan()
    storage, api, web = plan["epics"]
    search = {"id": "E2", "title": "Search", "summary": "find notes", "depends_on": ["E1"],
              "provides": [], "consumes": []}
    replan = {**plan, "epics": [
        storage, search, {**api, "id": "E3"}, {**web, "id": "E4", "depends_on": ["E3"]},
    ]}
    cas = ArtifactStore()
    node, tx, first = _first_pass(
        cas,
        ResponseScript(json.dumps(replan), match=_match(PLANNER_SOP_KEY, "REVISION PASS")),
        ResponseScript(json.dumps(replies["E3"]), match=_match("YOUR EPIC: E4")),
    )
    node(_failed(first, "no criterion flags security_abuse=true"), config_for("r", step=5))
    prompts = {_epic_of(r): json.dumps(r, ensure_ascii=False) for r in tx.requests[4:]}
    # The API epic moved E2 -> E3: it reproduces its own tickets, re-prefixed.
    assert "E3-T01 [backend] backend T01" in prompts["E3"] and "E3-AC01:" in prompts["E3"]
    # The new E2 continues nothing: the old plan is reference, not ids to reproduce.
    assert "reference only" in prompts["E2"]
    assert "This epic's previous tickets" not in prompts["E2"]


def test_replan_prompt_lists_only_the_current_prds_criteria() -> None:
    """A scenario a later pass dropped must not be offered back to the model."""
    from foundry.agents import AgentRuntime
    from foundry.agents._common import emit_artifact
    from foundry.agents.planner import NODE_ID, _declared_criteria_text
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.proxy import ModelProviderProxy
    from foundry.proxy.test_doubles import MockTransport
    from foundry.schemas.product import PRD, AcceptanceCriterion
    from foundry.state.seq import Seq

    store = ArtifactStore()
    rt = AgentRuntime(proxy=ModelProviderProxy(MockTransport(default_text="{}")), cas=store)

    def seq(epoch: int, local: int) -> Seq:
        return Seq("r", epoch, 1, NODE_ID, local)

    arts = [
        emit_artifact(AcceptanceCriterion(id="AC-1", text="kept", seq=seq(1, 0)),
                      "acceptance_criterion", NODE_ID, seq(1, 0), store=store),
        emit_artifact(AcceptanceCriterion(id="AC-2", text="dropped later", seq=seq(1, 1)),
                      "acceptance_criterion", NODE_ID, seq(1, 1), store=store),
        emit_artifact(PRD(title="v1", prd_uri="cas://p1", criterion_ids=("AC-1", "AC-2"),
                          seq=seq(1, 2)), "prd", NODE_ID, seq(1, 2), store=store),
        emit_artifact(PRD(title="v1", prd_uri="cas://p2", criterion_ids=("AC-1",),
                          seq=seq(2, 0)), "prd", NODE_ID, seq(2, 0), store=store),
    ]
    text = _declared_criteria_text({"artifacts": arts}, rt)
    assert "AC-1" in text and "AC-2" not in text
