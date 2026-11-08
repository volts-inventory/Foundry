"""The planner checks and repairs its own plan before the Spec gate.

* mechanical gaps are repaired in the node (entrypoint wiring, self-consumed
  contracts, scenarios of tickets dropped at a cap, unknown covers ids);
* what the checks still find gets ONE in-node revision — only the epics the
  findings name, else the whole plan — without a Spec-gate round trip;
* the code contracts are compiled at plan time (mypy / tsc) and their errors
  feed that revision; a missing toolchain or a crash is only a concern;
* an unused contract and overflow are concerns, not blocking findings.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from foundry.agents import AgentRuntime, plan_contracts
from foundry.agents.plan_contracts import check_contracts
from foundry.agents.planner import (
    SOP,
    _add_read_deps,
    _affected,
    _drop_self_consumes,
    _merge_delta,
    _structural_findings,
    _wire_entrypoint,
    make_planner_node,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, ResponseScript
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, Ticket
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.planner_payload import (
    DELTA_SOP_KEY,
    PLANNER_SOP_KEY,
    PYTHON_STACK,
    _scn,
    _ticket,
    multi_epic_plan,
)
from tests.test_planner import (
    KEY,
    KEY_ID,
    PLAN,
    _bodies,
    _epic_of,
    _epic_runtime,
    _epic_scripts,
    _failed,
    _first_pass,
    _match,
    _spec_verdict,
    config_for,
    runtime_for,
)

_SEQ = Seq("r", 1, 1, "planner", 0)


def _copy(x: Any) -> Any:
    return json.loads(json.dumps(x))


def _critique(cas: ArtifactStore, writes: dict[str, Any]) -> CriteriaCritique:
    [critique] = _bodies(cas, writes, "criteria_findings")
    return critique


# ---- the in-node revision -----------------------------------------------------------


def _uncovered() -> dict[str, Any]:
    plan = _copy(PLAN)
    plan["tickets"][1]["scenarios"][0]["covers"] = ["SC-1"]  # MH-1 left uncovered
    return plan


def test_the_in_node_revision_fixes_a_finding_without_a_gate_round_trip() -> None:
    cas = ArtifactStore()
    # An empty delta: the full re-plan runs as the fallback.
    rt, tx = runtime_for(_uncovered(), {}, PLAN, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 3
    revision = json.dumps(tx.requests[2], ensure_ascii=False)
    assert "REVISION PASS — the planner's own plan check" in revision
    assert "Plan check findings:\\n- Must-have MH-1 ('add todo') has no covering" in revision
    assert "id=AC-001 (ticket=T-002)" in revision  # the criteria to keep
    critique = _critique(cas, writes)
    assert critique.coverage_findings == () and critique.contract_findings == ()
    assert not critique.spec_gate_blocking()
    summary = writes["agent_journal"][0].summary
    assert "Revised the plan once in-node after its own plan check: 1 -> 0 finding(s)." in summary


def test_a_worse_revision_is_discarded() -> None:
    worse = _copy(_uncovered())
    worse["tickets"][1]["scenarios"][0]["covers"] = []  # now SC-1 is uncovered too
    cas = ArtifactStore()
    rt, tx = runtime_for(_uncovered(), {}, worse, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 3  # one delta, then one full revision, never more
    assert _critique(cas, writes).coverage_findings == (
        "Must-have MH-1 ('add todo') has no covering scenario; add one whose covers lists MH-1.",
    )
    assert "kept the original (1 finding(s); the revision had 2)" in (
        writes["agent_journal"][0].summary
    )


def test_a_clean_plan_makes_no_revision_call() -> None:
    rt, tx = runtime_for(PLAN)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 1
    assert "in-node" not in writes["agent_journal"][0].summary


def _with_unscenarioed(reply: dict[str, Any]) -> dict[str, Any]:
    return {**reply, "tickets": [*reply["tickets"], _ticket("T03", "backend")]}


def test_an_epic_finding_reruns_only_that_epic_in_node() -> None:
    plan, replies = multi_epic_plan()
    clean = replies["E2"]
    replies["E2"] = _with_unscenarioed(clean)
    fixed = ResponseScript(json.dumps(clean), match=_match("YOUR EPIC: E2", "Plan check"))
    cas = ArtifactStore()
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies, fixed), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2"]
    prompt = json.dumps(tx.requests[4], ensure_ascii=False)
    assert "Feature ticket E2-T03 has no scenarios" in prompt
    assert "E2-T01" in prompt  # its current tickets, to reproduce
    assert _critique(cas, writes).coverage_findings == ()
    assert "E2-T03" not in {t.id for t in _bodies(cas, writes, "ticket")}
    assert "Revised epic(s) E2 once in-node" in writes["agent_journal"][0].summary


def test_a_gate_replan_gets_its_own_in_node_rerun() -> None:
    _, replies = multi_epic_plan()
    gate_fix = ResponseScript(
        json.dumps(_with_unscenarioed(replies["E2"])),
        match=_match("YOUR EPIC: E2", "Spec gate findings"),
    )
    check_fix = ResponseScript(
        json.dumps(replies["E2"]), match=_match("YOUR EPIC: E2", "Plan check findings")
    )
    cas = ArtifactStore()
    node, tx, first = _first_pass(cas, gate_fix, check_fix)
    assert len(tx.requests) == 4  # a clean first pass: no in-node revision

    second = node(
        _failed(first, "criterion E2-AC01: scenario has no then step"), config_for("r", step=5)
    )
    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2", "E2"]
    assert "Plan check findings" in json.dumps(tx.requests[5])
    assert _critique(cas, second).coverage_findings == ()
    assert "Revised epic(s) E2 once in-node" in second["agent_journal"][0].summary


# ---- patch, don't re-plan: one delta call instead of a full plan call ----------------


def _two_implementers() -> dict[str, Any]:
    """The local-model mistakes the delta exists for: a scenario-less ticket that
    also implements a contract another ticket implements."""
    plan = _copy(PLAN)
    plan["tickets"].append(
        {
            "id": "contracts-1",
            "title": "Contract types",
            "specialty": "backend",
            "difficulty": "trivial",
            "depends_on": [],
            "files": ["todo/models.py"],
            "implements": ["TodoStore.add"],
            "consumes": [],
            "scenarios": [],
        }
    )
    return plan


def _delta(**ops: Any) -> dict[str, Any]:
    return {
        "tickets": [],
        "remove_tickets": [],
        "remove_scenarios": [],
        "criteria_review": {"missing": [], "security_abuse_omissions": []},
        **ops,
    }


_DROP_CONTRACTS_1 = _delta(remove_tickets=["contracts-1"])


def _is_delta(request: dict[str, Any]) -> bool:
    blob = json.dumps(request)
    return DELTA_SOP_KEY in blob and PLANNER_SOP_KEY not in blob


def test_a_failed_plan_check_is_fixed_by_one_delta_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundry.agents import planner

    monkeypatch.setattr(planner, "_SHOW_ALL_TICKETS", 0)  # slice as a big plan would
    cas = ArtifactStore()
    rt, tx = runtime_for(_two_implementers(), _DROP_CONTRACTS_1, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2 and _is_delta(tx.requests[1])
    prompt = json.dumps(tx.requests[1], ensure_ascii=False)
    assert "Contract TodoStore.add is implemented by 2 tickets" in prompt
    assert "Feature ticket contracts-1 has no scenarios" in prompt
    # The slice: the named tickets in full, the rest by id only.
    full, rest = prompt.split("EVERY OTHER TICKET")
    assert '\\"id\\": \\"contracts-1\\"' in full and '\\"id\\": \\"T-001\\"' in full
    assert "an empty store" in full  # T-001 implements the named contract
    assert '\\"id\\": \\"T-003\\"' in rest and "they add a todo" not in rest
    critique = _critique(cas, writes)
    assert critique.contract_findings == () and critique.coverage_findings == ()
    assert [t.id for t in _bodies(cas, writes, "ticket")] == ["T-001", "T-002", "T-003"]
    assert (
        "Patched the plan in-node with one delta call after its own plan check: "
        "2 -> 0 finding(s)." in writes["agent_journal"][0].summary
    )


def test_an_upsert_merges_into_a_shown_ticket_by_id() -> None:
    contracts_1 = _two_implementers()["tickets"][-1]
    fix = _delta(
        tickets=[
            {**PLAN["tickets"][0], "implements": ["TodoStore.list"], "scenarios": []},
            {
                **contracts_1,
                "scenarios": [
                    {
                        "id": "AC-010",
                        "given": ["a todo"],
                        "when": ["it is added"],
                        "then": ["it gets an id"],
                        "security_abuse": False,
                        "covers": [],
                    },
                ],
            },
        ]
    )
    cas = ArtifactStore()
    rt, tx = runtime_for(_two_implementers(), fix, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2 and _is_delta(tx.requests[1])
    critique = _critique(cas, writes)
    assert critique.contract_findings == () and critique.coverage_findings == ()
    by_id = {t.id: t for t in _bodies(cas, writes, "ticket")}
    assert by_id["T-001"].scenario_ids == ("AC-003",)  # kept unlisted
    assert by_id["T-001"].implements == ("TodoStore.list",)
    assert by_id["contracts-1"].scenario_ids == ("AC-010",)


def test_an_upsert_of_a_summarized_ticket_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from foundry.agents import planner

    # In a big plan MH-1's finding names no ticket, so T-002 goes only as a summary:
    # replacing it would overwrite fields and scenarios the model never saw.
    monkeypatch.setattr(planner, "_SHOW_ALL_TICKETS", 0)
    fix = _delta(
        tickets=[
            {
                **PLAN["tickets"][1],
                "scenarios": [
                    {**PLAN["tickets"][1]["scenarios"][0], "covers": ["MH-1", "SC-1"]},
                ],
            }
        ]
    )
    rt, tx = runtime_for(_uncovered(), fix, PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 3 and _is_delta(tx.requests[1])
    assert PLANNER_SOP_KEY in json.dumps(tx.requests[2])


def test_a_small_plan_lets_the_delta_fix_a_ticket_no_finding_names() -> None:
    fix = _delta(
        tickets=[
            {
                **PLAN["tickets"][1],
                "scenarios": [
                    {**PLAN["tickets"][1]["scenarios"][0], "covers": ["MH-1", "SC-1"]},
                ],
            }
        ]
    )
    rt, tx = runtime_for(_uncovered(), fix, PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 2 and _is_delta(tx.requests[1])


def _flagged(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        **plan,
        "criteria_review": {"missing": [], "security_abuse_omissions": ["no rate limit"]},
    }


def _by_sop(plan: dict[str, Any], delta: dict[str, Any], cas: ArtifactStore) -> Any:
    return _epic_runtime(
        ResponseScript(json.dumps(delta), match=_match(DELTA_SOP_KEY)),
        ResponseScript(json.dumps(plan), match=_match(PLANNER_SOP_KEY)),
        cas=cas,
    )


def test_an_open_review_gap_skips_the_in_node_delta() -> None:
    cas = ArtifactStore()
    rt, tx = _by_sop(_flagged(_two_implementers()), _DROP_CONTRACTS_1, cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert not any(_is_delta(r) for r in tx.requests)
    assert _critique(cas, writes).security_abuse_omissions == ("no rate limit",)


def test_an_open_omission_skips_the_gate_delta() -> None:
    new_ticket = {
        "id": "T-004",
        "title": "Rate limit",
        "specialty": "backend",
        "difficulty": "standard",
        "depends_on": ["T-001"],
        "files": ["todo/limits.py"],
        "implements": [],
        "consumes": [],
        "entrypoint": False,
        "scenarios": [
            {
                "id": "AC-009",
                "given": ["100 adds"],
                "when": ["one more"],
                "then": ["it is rejected"],
                "security_abuse": True,
                "covers": [],
            },
        ],
    }
    cas = ArtifactStore()
    rt, tx = _by_sop(_flagged(PLAN), _delta(tickets=[new_ticket]), cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    assert _critique(cas, first).security_abuse_omissions == ("no rate limit",)
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [
            _spec_verdict(
                GateStatus.FAIL,
                ("criteria-completeness critic flagged a security/abuse omission",),
                epoch=1,
            )
        ],
    }
    second = node(state, config_for("r", step=5))
    assert not any(_is_delta(r) for r in tx.requests)
    assert _critique(cas, second).security_abuse_omissions == ("no rate limit",)


def test_work_dropped_at_a_cap_skips_the_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    from foundry.agents import planner

    monkeypatch.setattr(planner, "MAX_TICKETS", 3)
    plan = _uncovered()
    plan["tickets"].append(
        {
            **PLAN["tickets"][0],
            "id": "T-004",
            "files": ["todo/x.py"],
            "implements": [],
            "scenarios": [],
        }
    )
    fix = _delta(
        tickets=[
            {
                **PLAN["tickets"][1],
                "scenarios": [
                    {**PLAN["tickets"][1]["scenarios"][0], "covers": ["MH-1", "SC-1"]},
                ],
            }
        ]
    )
    cas = ArtifactStore()
    rt, tx = _by_sop(plan, fix, cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert not any(_is_delta(r) for r in tx.requests)
    assert "consolidate tickets" in json.dumps(tx.requests[-1])
    assert _critique(cas, writes).coverage_findings != ()


def test_a_delta_leaving_a_finding_falls_back_to_the_full_replan() -> None:
    useless = _delta(remove_scenarios=["AC-003"])  # merges, closes nothing
    cas = ArtifactStore()
    rt, tx = runtime_for(_two_implementers(), useless, PLAN, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 3 and _is_delta(tx.requests[1])
    assert "REVISION PASS — the planner's own plan check" in json.dumps(
        tx.requests[2], ensure_ascii=False
    )
    assert _critique(cas, writes).contract_findings == ()
    assert "Revised the plan once in-node" in writes["agent_journal"][0].summary


def test_an_unparseable_delta_falls_back_to_the_full_replan() -> None:
    tx = MockTransport(
        completions=[
            json.dumps(_two_implementers()),
            "no json here",
            json.dumps(PLAN),
        ]
    )
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID), max_retries=0)
    cas = ArtifactStore()
    writes = make_planner_node(AgentRuntime(proxy=proxy, cas=cas))(
        {"request": "x"}, config_for("r")
    )
    assert len(tx.requests) == 3 and _is_delta(tx.requests[1])
    assert PLANNER_SOP_KEY in json.dumps(tx.requests[2])
    assert _critique(cas, writes).contract_findings == ()


def test_a_finding_the_tickets_cannot_close_skips_the_delta() -> None:
    cli = _copy(PLAN)
    cli["intent"]["deliverable"] = "cli"  # with stack.run empty: a stack finding
    rt, tx = runtime_for(cli, PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 2 and not _is_delta(tx.requests[1])


def test_a_spec_gate_failure_is_patched_with_one_delta_call() -> None:
    from foundry.agents.implementation.tickets import read_tickets

    cas = ArtifactStore()
    broken = _two_implementers()
    # The first pass keeps both findings (empty delta, unchanged full re-plan).
    rt, tx = runtime_for(broken, {}, broken, _DROP_CONTRACTS_1, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    assert len(_critique(cas, first).contract_findings) == 1
    findings = (
        "contract fit: Contract TodoStore.add is implemented by 2 tickets "
        "(T-001, contracts-1); exactly one ticket may implement it.",
        "coverage: Feature ticket contracts-1 has no scenarios; give it at least one "
        "(or fold it into the ticket whose behaviour it serves).",
    )
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, findings, epoch=1)],
    }
    second = node(state, config_for("r", step=5))

    assert len(tx.requests) == 4 and _is_delta(tx.requests[3])
    assert "(T-001, contracts-1)" in json.dumps(tx.requests[3])
    assert {a.kind for a in second["artifacts"]} == {
        "prd",
        "acceptance_criterion",
        "criteria_findings",
        "ticket",
        "ownership_map",
    }
    critique = _critique(cas, second)
    assert not critique.spec_gate_blocking()
    after: FactoryState = {**state, "artifacts": [*first["artifacts"], *second["artifacts"]]}
    assert [t.id for t in read_tickets(after, cas.resolve)] == ["T-001", "T-002", "T-003"]
    summary = second["agent_journal"][0].summary
    assert "Revised after a Spec-gate failure. Patched with one delta call" in summary


def test_a_gate_delta_leaving_an_omission_falls_back_to_the_full_replan() -> None:
    cas = ArtifactStore()
    rt, tx = runtime_for(
        PLAN,
        _delta(
            remove_scenarios=["AC-003"],
            criteria_review={"missing": [], "security_abuse_omissions": ["no rate limit"]},
        ),
        PLAN,
        cas=cas,
    )
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, ("no rate-limit scenario",), epoch=1)],
    }
    node(state, config_for("r", step=5))
    assert len(tx.requests) == 3 and _is_delta(tx.requests[1])
    assert "REVISION PASS" in json.dumps(tx.requests[2])


_SHOWN = {"A", "B"}


def _item(tid: str, *scenarios: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": tid,
        "specialty": "backend",
        "depends_on": [],
        "files": [f"{tid}.py"],
        "implements": [],
        "consumes": [],
        "scenarios": [{"id": s, "given": ["g"], "when": ["w"], "then": ["t"]} for s in scenarios],
        **extra,
    }


def test_the_merge_replaces_moves_and_removes_by_id() -> None:
    items = [_item("A", "S1", "S2"), _item("B", "S3"), _item("C")]
    merged = _merge_delta(
        items,
        _delta(
            tickets=[
                {**_item("B", "S1"), "title": "B2"},  # S1 moves here from A
                _item("D", "S4"),
                {**_item("contracts"), "title": "never the planner's own"},
            ],
            remove_tickets=["C"],
            remove_scenarios=["S2"],
        ),
        frozenset(),
        _SHOWN,
    )
    assert merged is not None
    by_id = {i["id"]: i for i in merged}
    assert list(by_id) == ["A", "B", "D"]
    assert [s["id"] for s in by_id["A"]["scenarios"]] == []
    assert [s["id"] for s in by_id["B"]["scenarios"]] == ["S3", "S1"]  # S3 kept unlisted
    assert by_id["B"]["title"] == "B2"
    assert items[0]["scenarios"][0]["id"] == "S1"  # the input is not mutated

    assert _merge_delta(items, _delta(), frozenset(), _SHOWN) is None  # changes nothing
    assert _merge_delta(items, _delta(remove_tickets=["A", "B", "C"]), frozenset(), _SHOWN) is None


def test_the_merge_refuses_to_edit_a_summarized_ticket() -> None:
    items = [_item("A", "S1"), _item("B", "S2")]
    shown = {"A"}  # B went out as a summary only
    edits = [
        _delta(tickets=[_item("B", "S9")]),  # upserting it
        _delta(tickets=[_item("A", "S2")]),  # moving its scenario off it
        _delta(remove_scenarios=["S2"]),  # deleting its scenario
    ]
    assert [_merge_delta(items, d, frozenset(), shown) for d in edits] == [None] * 3
    assert _merge_delta(items, _delta(remove_tickets=["B"]), frozenset(), shown) is not None
    assert _merge_delta(items, _delta(tickets=[_item("N", "S7")]), frozenset(), shown)


def test_a_refused_delta_logs_why(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FOUNDRY_PROGRESS", "1")
    items = [_item("A", "S1"), _item("B", "S2")]
    assert _merge_delta(items, _delta(tickets=[_item("B", "S9")]), frozenset(), {"A"}) is None
    assert "planner delta rejected: it rewrites summary-only ticket(s) B" in capsys.readouterr().err


def test_one_scenario_on_two_upserted_tickets_is_refused() -> None:
    items = [_item("A", "S1"), _item("B")]
    twice = _delta(tickets=[_item("A", "S5"), _item("B", "S5")])
    assert _merge_delta(items, twice, frozenset(), {"A", "B"}) is None


def test_an_epic_delta_keeps_tickets_inside_known_epics() -> None:
    items = [{**_item("E1-T01"), "__epic__": "E1"}, {**_item("E2-T01"), "__epic__": "E2"}]
    merged = _merge_delta(
        items,
        _delta(
            tickets=[
                _item("E2-T02", "E2-AC09", depends_on=["E2-T01", "E1-T01"]),
            ]
        ),
        frozenset({"E1", "E2"}),
        {"E1-T01", "E2-T01"},
    )
    assert merged is not None
    new = merged[-1]
    assert new["__epic__"] == "E2" and new["depends_on"] == ["E2-T01"]
    assert (
        _merge_delta(items, _delta(tickets=[_item("T-9")]), frozenset({"E1", "E2"}), set()) is None
    )


def test_a_contract_name_with_spaces_marks_its_tickets_affected() -> None:
    items = [
        _item("A", implements=["POST /users"]),
        _item("B", consumes=["POST /users"]),
        *(_item(f"C{n}") for n in range(8)),
    ]
    finding = "Contract POST /users is implemented by 2 tickets; exactly one may."
    assert _affected([finding], items, ["POST /users"]) == {"A", "B"}


def test_a_small_plan_goes_to_the_delta_whole() -> None:
    items = [_item("T1", "S1"), _item("contracts-2")]
    finding = "Feature ticket contracts-2 has no scenarios; fold it into the ticket it serves."
    assert _affected([finding], items, []) == {"T1", "contracts-2"}


def test_criteria_problems_mirror_the_spec_gate() -> None:
    def crit(cid: str, **extra: Any) -> AcceptanceCriterion:
        fields: dict[str, Any] = {
            "id": cid,
            "text": "Given g",
            "oracle_hint": "o",
            "security_abuse": True,
            "seq": _SEQ,
        }
        return AcceptanceCriterion(**{**fields, **extra})

    assert _structural_findings([crit("A", given=("g",), when=("w",), then=(" ",))]) == (
        "criterion A: scenario has no then step",
    )
    assert _structural_findings([crit("B", oracle_hint="")]) == (
        "criterion B: missing/empty oracle",
    )


def test_a_rejected_gate_patch_mints_no_extra_ticket_seq() -> None:
    cas = ArtifactStore()
    rt, _ = runtime_for(PLAN, {}, PLAN, cas=cas)
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    state: FactoryState = {
        "request": "x",
        "artifacts": first["artifacts"],
        "scope_tier": first["scope_tier"],
        "gates": [_spec_verdict(GateStatus.FAIL, ("some finding",), epoch=1)],
    }
    second = node(state, config_for("r", step=5))
    epoch = {t.seq.emission_epoch for t in _bodies(cas, first, "ticket")}
    again = {t.seq.emission_epoch for t in _bodies(cas, second, "ticket")}
    assert again == {e + 1 for e in epoch}  # the full re-plan's, and only it


# ---- repairs ------------------------------------------------------------------------


def _t(
    tid: str,
    *deps: str,
    entry: bool = False,
    test: bool = False,
    implements: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
) -> Ticket:
    return Ticket(
        id=tid,
        title=tid,
        depends_on=deps,
        entrypoint=entry,
        specialty=Specialty.TEST if test else Specialty.BACKEND,
        implements=implements,
        consumes=consumes,
        seq=_SEQ,
    )


def test_the_entrypoint_is_wired_to_every_feature_unless_that_closes_a_cycle() -> None:
    tickets = [
        _t("A"),
        _t("B", "A"),
        _t("C"),
        _t("APP", "A", entry=True),
        _t("T", "APP", test=True),
    ]
    out, concerns = _wire_entrypoint(tickets)
    by_id = {t.id: t for t in out}
    assert by_id["APP"].depends_on == ("A", "B", "C")  # never the test ticket
    assert concerns == [
        "Ticket APP: added dependencies on feature ticket(s) B, C so the "
        "entrypoint wires every feature in."
    ]

    cyclic = [_t("A", "APP"), _t("APP", entry=True)]
    out, concerns = _wire_entrypoint(cyclic)
    assert out == cyclic and concerns == []  # left for the coverage check


def test_a_reader_is_ordered_after_the_one_ticket_writing_the_file_it_reads() -> None:
    # A local shop run built the repositories beside the models ticket they read:
    # one imported a ProductModel the models ticket never defined.
    def t(tid: str, *deps: str, files: tuple[str, ...] = (), reads: tuple[str, ...] = ()) -> Ticket:
        return _t(tid, *deps).model_copy(update={"files": files, "reads": reads})

    tickets = [
        t("contracts", files=("contracts.py",)),
        t("E1-T01", "contracts", files=("app/database.py",)),
        t("E1-T02", "contracts", "E1-T01", files=("app/models.py",)),
        t(
            "E2-T01",
            "contracts",
            "E1-T01",
            files=("app/repo.py",),
            reads=("app/models.py", "contracts.py", "app/repo.py", "README.md"),
        ),
        t("E2-T02", "contracts", "E2-T01", files=("app/svc.py",), reads=("app/models.py",)),
        t("E3-T01", "contracts", files=("a.py", "b.py"), reads=("b.py", "shared.py")),
        t("E3-T02", files=("shared.py",)),
        t("E3-T03", files=("shared.py",)),
        t("E4-T01", "E4-T02", files=("x.py",)),
        t("E4-T02", files=("y.py",), reads=("x.py",)),
    ]
    out, concerns = _add_read_deps(tickets)
    deps = {o.id: o.depends_on for o in out}
    assert deps["E2-T01"] == ("contracts", "E1-T01", "E1-T02")
    assert deps["E2-T02"] == ("contracts", "E2-T01")  # already after it, through E2-T01
    assert deps["E3-T01"] == ("contracts",)  # its own file; a file with two writers
    assert deps["E4-T02"] == ()  # the edge would close a cycle
    assert concerns == [
        "Ticket E2-T01: added a dependency on E1-T02, which writes app/models.py it reads."
    ]
    assert [o for o in out if o.id != "E2-T01"] == [o for o in tickets if o.id != "E2-T01"]


def test_a_self_consumed_contract_is_dropped_from_consumes() -> None:
    out, concerns = _drop_self_consumes([_t("A", implements=("X",), consumes=("X", "Y"))])
    assert out[0].consumes == ("Y",)
    assert concerns == ["Ticket A: dropped consumes X, which it implements itself."]


def test_the_sop_states_the_scaffold_rules() -> None:
    assert "PACKAGE MARKERS" in SOP and "__init__.py" in SOP
    assert "python contracts.py" in SOP and "typescript src/contracts.ts" in SOP


# ---- plan-time contract compile -----------------------------------------------------


def _mypy_here() -> bool:
    return check_contracts([("X", "x: int")], "python").ran


_SHADOWING = [  # `list` names a method, so the later `-> list[...]` is the method
    {"name": "Todo", "kind": "type", "signature": "@dataclass\nclass Todo:\n    title: str"},
    {"name": "TodoStore.add", "kind": "function", "signature": "def add(self, t: Todo) -> int"},
    {"name": "TodoStore.list", "kind": "function", "signature": "def list(self) -> list[Todo]"},
    {
        "name": "TodoStore.recent",
        "kind": "function",
        "signature": "def recent(self, n: int) -> list[Todo]",
    },
]


def _compiled_plan(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    plan = _copy(PLAN)
    plan["interfaces"] = [
        {"inputs": [], "outputs": [], "errors": [], "description": "", **i} for i in interfaces
    ]
    plan["tickets"][0]["implements"] = [
        i["name"] for i in interfaces if i["name"].startswith("TodoStore.")
    ]
    plan["stack"] = {**PYTHON_STACK, "contracts_file": "todo/contracts.py"}
    return plan


def test_a_contract_compile_error_feeds_the_in_node_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    if not _mypy_here():
        pytest.skip("mypy is not installed for python3 here")
    renamed = [
        {**i, "name": "TodoStore.list_all", "signature": "def list_all(self) -> list[Todo]"}
        if i["name"] == "TodoStore.list"
        else i
        for i in _SHADOWING
    ]
    cas = ArtifactStore()
    rt, tx = runtime_for(_compiled_plan(_SHADOWING), _compiled_plan(renamed), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2
    assert "Contract TodoStore.recent does not compile (mypy)" in json.dumps(tx.requests[1])
    assert _critique(cas, writes).contract_findings == ()


def test_a_contract_compile_error_left_after_the_revision_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    if not _mypy_here():
        pytest.skip("mypy is not installed for python3 here")
    cas = ArtifactStore()
    rt, _ = runtime_for(_compiled_plan(_SHADOWING), cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    findings = _critique(cas, writes).contract_findings
    assert findings == (
        "Contract TodoStore.recent does not compile (mypy): Function "
        '"contracts.TodoStore.list" is not valid as a type [valid-type]',
    )  # mypy is the authority when it ran: no duplicate heuristic finding


def test_python_assembly_and_type_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    if not _mypy_here():
        pytest.skip("mypy is not installed for python3 here")
    result = check_contracts(
        [
            ("Money", "Amount = Decimal"),
            ("Repo", "class Repo(Protocol):\n    def get(self, id: UUID) -> Thing | None: ..."),
            ("parse", "def parse(text: str) -> Amount"),
            ("Prose", "this is prose"),
        ],
        "python",
    )
    assert result.ran
    # An undefined name is an import the fragment can't carry: a concern, not a blocker.
    assert result.findings == ()
    assert result.concerns == (
        "Contract Prose: its signature is not valid Python, so it was not compiled at plan time.",
        'Contract Repo: Name "Thing" is not defined [name-defined] (the contracts ticket '
        "resolves imports)",
    )
    typed = check_contracts([("parse", "def parse(text: str) -> int: return text")], "python")
    assert typed.findings and "Incompatible return value" in typed.findings[0]


def test_typescript_contracts_compile_with_tsc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    result = check_contracts(
        [
            ("Todo", "interface Todo { id: string }"),
            ("TodoStore.add", "add(todo: Todo): string"),
            ("TodoStore.list", "list(): Todoo[]"),
            ("Store", "class Store { get(id: string): Todo }"),
            ("broken", "type X = ;"),
        ],
        "typescript",
    )
    if not result.ran:
        pytest.skip("tsc is not installed here")
    assert result.findings == ()
    assert any("Cannot find name 'Todoo'" in c for c in result.concerns)
    assert any(c.startswith("Contract broken: tsc could not parse") for c in result.concerns)


def test_a_missing_toolchain_or_a_crash_is_a_concern(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    sig = [("X", "def f(x: int) -> int")]
    real_which = plan_contracts.shutil.which
    monkeypatch.setattr(plan_contracts.shutil, "which", lambda _: None)
    assert check_contracts(sig, "python") == plan_contracts.CompileResult(
        concerns=("Contracts not compiled at plan time: no mypy toolchain here.",)
    )
    monkeypatch.setattr(plan_contracts.shutil, "which", real_which)

    def timeout(*_: Any) -> Any:
        raise subprocess.TimeoutExpired("mypy", 60)

    monkeypatch.setattr(plan_contracts, "_run", timeout)
    result = check_contracts(sig, "python")
    assert result.findings == () and not result.ran
    assert result.concerns[0].startswith("Contracts not compiled at plan time: mypy did not run")
    # Other languages and the off switch never run anything.
    assert check_contracts(sig, "go") == plan_contracts.CompileResult()
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "0")
    assert check_contracts(sig, "python") == plan_contracts.CompileResult()


# ---- demoted to concerns --------------------------------------------------------------


def test_an_unused_contract_is_a_concern_not_a_finding() -> None:
    plan = _copy(PLAN)
    plan["interfaces"].append({"name": "Unused", "spec": "x"})
    plan["tickets"][1]["implements"] = ["Unused"]
    plan["tickets"][1]["scenarios"] = []  # no scenario on its implementer
    plan["tickets"].append(
        {
            **_ticket("T-009", "backend"),
            "scenarios": [{**_scn("AC-009", "MH-1", "SC-1"), "security_abuse": True}],
        }
    )
    cas = ArtifactStore()
    rt, _ = runtime_for(plan, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    critique = _critique(cas, writes)
    assert not any("Unused" in f for f in critique.contract_findings)
    assert any(
        c.startswith("Contract Unused (implemented by T-002) has no consumer")
        for c in writes["agent_journal"][0].concerns
    )
