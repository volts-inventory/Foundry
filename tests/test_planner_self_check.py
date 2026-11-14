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

from foundry import telemetry
from foundry.agents import AgentRuntime, plan_contracts
from foundry.agents.plan_contracts import check_contracts
from foundry.agents.planner import (
    _REPAIR_MAX_TOKENS,
    SOP,
    _add_read_deps,
    _affected,
    _drop_self_consumes,
    _merge_delta,
    _structural_findings,
    _wire_entrypoint,
    make_planner_node,
)
from foundry.agents.planner.build import _plan_brief
from foundry.agents.planner.checks import (
    _shared_type_findings,
    _type_contract_findings,
    _wiring_findings,
    finding_kind,
)
from foundry.agents.planner.folds import (
    _assign_shared_types,
    _drop_dangling_refs,
    _settle_contracts_file,
)
from foundry.agents.planner.spec_text import bound_conflicts, design_types, with_referenced
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport, ResponseScript
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Specialty, Ticket
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.planner_payload import (
    DELTA_SOP_KEY,
    PLANNER_SOP_KEY,
    PYTHON_STACK,
    _review,
    _scn,
    _ticket,
    multi_epic_plan,
)
from tests.test_planner import (
    KEY,
    KEY_ID,
    PLAN,
    _bodies,
    _content,
    _efforts,
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


def test_the_in_node_revision_repairs_the_failed_plan_without_a_fresh_replan() -> None:
    cas = ArtifactStore()
    # An empty delta: the repair runs, and closes the finding.
    rt, tx = runtime_for(_uncovered(), {}, PLAN, cas=cas)
    efforts = _efforts(rt)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 3  # plan, delta, repair: no fresh re-plan
    assert efforts == ["medium", "medium", "medium/repair"]
    repair = _content(tx.requests[2])
    assert "REPAIR PASS — the planner's own plan check" in repair
    assert "Plan check findings:\n- Must-have MH-1 ('add todo') has no covering" in repair
    plan = repair.split("YOUR PREVIOUS PLAN:\n", 1)[1]
    assert '"id": "T-002"' in plan and '"AC-001"' in plan  # the failed plan, whole
    assert "todo/api.py" in plan
    critique = _critique(cas, writes)
    assert critique.coverage_findings == () and critique.contract_findings == ()
    assert not critique.spec_gate_blocking()
    summary = writes["agent_journal"][0].summary
    assert "Revised the plan once in-node after its own plan check: 1 -> 0 finding(s)." in summary
    got = counts.snapshot()
    assert got["planner.repair.closed"] == 1 and "planner.repair.fallback" not in got
    assert got["planner.finding.goal-uncovered"] == 1
    assert got["planner.delta.rejected"] == 1


def test_a_repair_leaving_a_finding_falls_back_to_one_fresh_replan() -> None:
    cas = ArtifactStore()
    # The delta changes nothing, the repair returns the plan unchanged, the fresh
    # re-plan fixes it.
    rt, tx = runtime_for(_uncovered(), {}, _uncovered(), PLAN, cas=cas)
    efforts = _efforts(rt)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 4
    assert efforts == ["medium", "medium", "medium/repair", "medium"]
    fresh = _content(tx.requests[3])
    assert "REVISION PASS — the planner's own plan check" in fresh
    assert "id=AC-001 (ticket=T-002)" in fresh  # the criteria to keep
    assert fresh != _content(tx.requests[2])
    assert _critique(cas, writes).coverage_findings == ()
    got = counts.snapshot()
    assert got["planner.repair.fallback"] == 1 and got["planner.replan.used"] == 1
    assert "planner.repair.closed" not in got


def test_a_worse_revision_is_discarded() -> None:
    worse = _copy(_uncovered())
    worse["tickets"][1]["scenarios"][0]["covers"] = []  # now SC-1 is uncovered too
    cas = ArtifactStore()
    rt, tx = runtime_for(_uncovered(), {}, worse, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 4  # one delta, one repair, one fresh revision, never more
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
    # The gate's epic repair leaves a finding: ONE fresh re-run of that epic.
    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2", "E2"]
    repair, fresh = (_content(r) for r in tx.requests[4:])
    assert "REPAIR PASS — the signed Spec gate" in repair and "E2-T02" in repair
    assert "Plan check findings" in fresh and "REPAIR PASS" not in fresh
    assert _critique(cas, second).coverage_findings == ()
    assert "Revised epic(s) E2 once in-node" in second["agent_journal"][0].summary


def test_an_epic_repair_leaving_a_finding_falls_back_to_one_fresh_epic_call() -> None:
    plan, replies = multi_epic_plan()
    clean = replies["E2"]
    replies["E2"] = _with_unscenarioed(clean)
    fixed = ResponseScript(json.dumps(clean), match=_match("YOUR EPIC: E2", "REVISION PASS"))
    rt, tx = _epic_runtime(*_epic_scripts(plan, replies, fixed), cas=ArtifactStore())
    efforts = _efforts(rt)
    with telemetry.counting() as counts:
        make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2", "E2"]
    assert efforts[4:] == ["medium/repair", "medium"]
    repair, fresh = (_content(r) for r in tx.requests[4:])
    assert "REPAIR PASS" in repair and "E2-T03" in repair and "REPAIR PASS" not in fresh
    got = counts.snapshot()
    assert got["planner.repair.fallback"] == 1 and got["planner.replan.used"] == 1
    assert got["planner.finding.ticket-no-scenarios"] == 1


def test_finding_kinds_are_stable_slugs_of_the_check_templates() -> None:
    assert [
        finding_kind(f)
        for f in (
            "Ticket T-002 consumes contract Store.add, but no ticket implements it.",
            "Ticket T-002 consumes contract Store.add but does not depend on T-001, which "
            "implements it.",
            "contract fit: Contract Store.add is implemented by 2 tickets (T-001, T-003); "
            "exactly one ticket may implement it.",
            "coverage: Must-have MH-1 ('add') has no covering scenario; add one whose covers "
            "lists MH-1.",
            "criteria-completeness critic flagged a security/abuse omission",
            "criterion AC-1: scenario has no then step",
            "something new",
        )
    ] == [
        "contract-unimplemented",
        "contract-dep-missing",
        "contract-multi-impl",
        "goal-uncovered",
        "security-gap",
        "scenario-steps-missing",
        "other",
    ]


def test_each_fold_that_fires_is_counted_once_per_emitted_plan() -> None:
    plan = _copy(PLAN)
    plan["tickets"][0]["consumes"] = ["TodoStore.add"]  # a contract it implements itself
    failing = _copy(plan)
    failing["tickets"][1]["scenarios"][0]["covers"] = ["SC-1"]
    # The fold fires in the failed draft, the delta's probe and the repair: the
    # emitted plan counts it once.
    rt, tx = runtime_for(failing, {}, plan)
    with telemetry.counting() as counts:
        make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 3
    assert counts.snapshot()["planner.fold.drop_self_consumes"] == 1


def _unscenarioed_inline(plan: dict[str, Any]) -> dict[str, Any]:
    return {**plan, "tickets": [*plan["tickets"], _ticket("T-009", "backend")]}


def test_a_repair_better_than_both_the_original_and_the_fresh_replan_is_kept() -> None:
    two = _copy(PLAN)
    two["tickets"][1]["scenarios"][0]["covers"] = []  # MH-1 and SC-1 uncovered
    three = _unscenarioed_inline(two)
    cas = ArtifactStore()
    # Original 2 findings, delta no-op, repair 1, fresh re-plan 3.
    rt, tx = runtime_for(two, {}, _uncovered(), three, cas=cas)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 4
    assert _critique(cas, writes).coverage_findings == (
        "Must-have MH-1 ('add todo') has no covering scenario; add one whose covers lists MH-1.",
    )
    summary = writes["agent_journal"][0].summary
    assert "Revised the plan once in-node after its own plan check: 2 -> 1 finding(s)." in summary
    got = counts.snapshot()
    assert got["planner.replan.kept_repair"] == 1 and "planner.replan.used" not in got


def test_a_tie_goes_to_the_later_revision() -> None:
    cas = ArtifactStore()
    # Original 1, repair 1 (unchanged), fresh 1: the fresh re-plan is kept.
    fresh = _uncovered()
    fresh["prd"]["title"] = "Fresh"
    rt, _ = runtime_for(_uncovered(), {}, _uncovered(), fresh, cas=cas)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    [prd] = _bodies(cas, writes, "prd")
    assert prd.title == "Fresh"
    assert counts.snapshot()["planner.replan.used"] == 1


def test_the_gate_repair_view_shows_contracts_and_tickets_whole() -> None:
    plan = _copy(PLAN)
    spec = "add(todo) -> id. " + "x" * 7000
    plan["interfaces"][0]["spec"] = spec
    plan["tickets"][1]["description"] = "d" * 7000
    plan["prd"]["body"] = "p" * 7000
    rt, tx = runtime_for(plan, {}, plan, cas=ArtifactStore())
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))
    node(_failed(first, "no criterion flags security_abuse=true"), config_for("r", step=5))

    repair = _content(tx.requests[2])
    assert "REPAIR PASS — the signed Spec gate" in repair
    view = repair.split("YOUR PREVIOUS PLAN:\n", 1)[1]
    assert spec in view and "d" * 7000 in view  # never cut: they are reproduced
    assert "p" * 6000 + " …[truncated]" in view and "p" * 6001 not in view
    assert "was cut for length: write it out in full again" in repair


def test_the_in_node_repair_shows_the_failed_reply_uncut() -> None:
    plan = _uncovered()
    plan["prd"]["body"] = "p" * 7000
    plan["tickets"][1]["description"] = "d" * 7000
    rt, tx = runtime_for(plan, {}, PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))

    repair = _content(tx.requests[2])
    view = repair.split("YOUR PREVIOUS PLAN:\n", 1)[1]
    assert "p" * 7000 in view and "d" * 7000 in view
    assert "truncated" not in repair


def test_an_epic_with_no_previous_one_to_repair_is_planned_at_the_default_effort() -> None:
    multi, replies = multi_epic_plan()
    scripts = _epic_scripts(
        PLAN,
        replies,
        ResponseScript(json.dumps(multi), match=_match(PLANNER_SOP_KEY, "REPAIR PASS")),
    )
    rt, _ = _epic_runtime(*scripts, cas=ArtifactStore())
    node = make_planner_node(rt)
    first = node({"request": "x"}, config_for("r"))  # inline: no epic to continue
    seen: list[tuple[str | None, str | None]] = []
    real = rt.proxy.complete

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((_epic_of({"args": args}), kwargs.get("max_tokens")))
        return real(*args, **kwargs)

    rt.proxy.complete = spy  # type: ignore[method-assign]
    node(_failed(first, "no criterion flags security_abuse=true"), config_for("r", step=5))

    epic_caps = [cap for epic, cap in seen if epic is not None]
    assert len(epic_caps) >= 3 and set(epic_caps) == {None}  # planned afresh, not repaired
    assert _REPAIR_MAX_TOKENS in {cap for epic, cap in seen if epic is None}


def test_a_gate_epic_repair_with_an_open_review_gap_gets_a_fresh_rerun() -> None:
    _, replies = multi_epic_plan()
    flagged = {**replies["E2"], "criteria_review": _review("no paging scenario")}
    gate_fix = ResponseScript(
        json.dumps(flagged), match=_match("YOUR EPIC: E2", "Spec gate findings")
    )
    check_fix = ResponseScript(
        json.dumps(replies["E2"]), match=_match("YOUR EPIC: E2", "Plan check findings")
    )
    cas = ArtifactStore()
    node, tx, first = _first_pass(cas, gate_fix, check_fix)
    with telemetry.counting() as counts:
        second = node(
            _failed(first, "criterion E2-AC01: scenario has no then step"),
            config_for("r", step=5),
        )

    # The repair and its self-review's two calls leave the gap open: one fresh re-run.
    e2 = [r for r in tx.requests[4:] if _epic_of(r) == "E2"]
    assert len(e2) == 4
    last = _content(e2[-1])
    assert "Plan check findings" in last and "[E2] no paging scenario" in last
    assert _critique(cas, second).missing == ()
    got = counts.snapshot()
    assert got["planner.replan.used"] == 1 and "planner.repair.closed" not in got


def test_a_gate_epic_repair_dropping_the_only_security_flag_is_closed_by_the_fold() -> None:
    _, replies = multi_epic_plan()
    unflagged = _copy(replies["E1"])
    unflagged["tickets"][1]["scenarios"][0]["security_abuse"] = False  # the only one
    gate_fix = ResponseScript(
        json.dumps(unflagged), match=_match("YOUR EPIC: E1", "Spec gate findings")
    )
    cas = ArtifactStore()
    node, tx, first = _first_pass(cas, gate_fix)
    with telemetry.counting() as counts:
        node(
            _failed(first, "criterion E1-AC02: scenario has no then step"),
            config_for("r", step=5),
        )

    # The missing security flag is restored in code: no finding is left to re-run.
    assert [_epic_of(r) for r in tx.requests[4:]] == ["E1"]
    got = counts.snapshot()
    assert "planner.repair.fallback" not in got and got["planner.repair.closed"] == 1
    assert got["planner.fold.ensure_security_scenario"] == 1


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
    from foundry.agents.planner import delta

    monkeypatch.setattr(delta, "_SHOW_ALL_TICKETS", 0)  # slice as a big plan would
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
    from foundry.agents.planner import delta

    # In a big plan MH-1's finding names no ticket, so T-002 goes only as a summary:
    # replacing it would overwrite fields and scenarios the model never saw.
    monkeypatch.setattr(delta, "_SHOW_ALL_TICKETS", 0)
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
    assert "REPAIR PASS — the planner's own plan check" in json.dumps(
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
    # The first pass keeps both findings (empty delta, unchanged repair and re-plan).
    rt, tx = runtime_for(broken, {}, broken, broken, _DROP_CONTRACTS_1, cas=cas)
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

    assert len(tx.requests) == 5 and _is_delta(tx.requests[4])
    assert "(T-001, contracts-1)" in json.dumps(tx.requests[4])
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
    assert "REPAIR PASS" in json.dumps(tx.requests[2])


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


_WSGI_CALL = (
    "    def __call__(self, environ: dict[str, str], start_response: Callable[..., None])"
    " -> Iterable[bytes]: ..."
)


def _ifc(name: str, signature: str, kind: str = "class") -> InterfaceContract:
    return InterfaceContract(
        name=name, spec_uri="cas://x", kind=kind, signature=signature, seq=_SEQ
    )


_STORE = _ifc(
    "DomainStore", "class DomainStore(Protocol):\n    def get(self, key: int) -> str: ..."
)
_BARE_APP = _ifc("WsgiApp", "class WsgiApp(Protocol):\n" + _WSGI_CALL)


def test_an_entrypoint_class_composing_other_contracts_must_declare_how_it_receives_them() -> None:
    # A local shop run: `class WsgiApp: def __call__(...)` with no __init__ or factory,
    # so its stub was a bare class nothing could hand the store to.
    tickets = [
        _t("STORE", implements=("DomainStore",)),
        _t("APP", "STORE", entry=True, implements=("WsgiApp",), consumes=("DomainStore",)),
    ]
    (finding,) = _wiring_findings(tickets, [_STORE, _BARE_APP])
    assert finding.startswith("Contract WsgiApp (entrypoint ticket APP) composes DomainStore")
    assert finding_kind(finding) == "contract-unwired"

    with_init = _ifc(
        "WsgiApp",
        "class WsgiApp(Protocol):\n"
        "    def __init__(self, store: DomainStore) -> None: ...\n" + _WSGI_CALL,
    )
    inline_factory = _ifc(
        "WsgiApp", _BARE_APP.signature + "\n\n\ndef create_app(store: DomainStore) -> WsgiApp: ..."
    )
    # An inline factory that returns the class without naming the dependency.
    inline_maker = _ifc("WsgiApp", _BARE_APP.signature + "\n\n\ndef make_app() -> WsgiApp: ...")
    for app in (with_init, inline_factory, inline_maker):
        assert _wiring_findings(tickets, [_STORE, app]) == ()

    factory = _ifc("create_app", "def create_app(store: DomainStore) -> WsgiApp: ...", "function")
    with_factory = [
        tickets[0],
        tickets[1].model_copy(update={"implements": ("WsgiApp", "create_app")}),
    ]
    assert _wiring_findings(with_factory, [_STORE, _BARE_APP, factory]) == ()

    # A function that neither returns the class nor takes a dependency is no factory.
    main = _ifc("main", "def main() -> None: ...", "function")
    with_main = [tickets[0], tickets[1].model_copy(update={"implements": ("WsgiApp", "main")})]
    assert len(_wiring_findings(with_main, [_STORE, _BARE_APP, main])) == 1
    inline_main = _ifc("WsgiApp", _BARE_APP.signature + "\n\n\ndef main() -> None: ...")
    assert len(_wiring_findings(tickets, [_STORE, inline_main])) == 1
    # Returning the class (or a base of it) is enough, even with no arguments.
    based = _ifc("WsgiApp", "class WsgiApp(BaseApp):\n" + _WSGI_CALL)
    for returns in ("WsgiApp", "BaseApp"):
        make = _ifc("make", f"def make() -> {returns}: ...", "function")
        with_make = [tickets[0], tickets[1].model_copy(update={"implements": ("WsgiApp", "make")})]
        assert _wiring_findings(with_make, [_STORE, based, make]) == ()

    # No dependencies (or only declared data types / errors): nothing to wire in.
    record = _ifc("User", "class User:\n    id: int", "type")
    alone = [_t("APP", entry=True, implements=("WsgiApp",), consumes=("User",))]
    assert _wiring_findings(alone, [record, _BARE_APP]) == ()
    # Only the entrypoint's composition root is checked.
    plain = [tickets[0], tickets[1].model_copy(update={"entrypoint": False})]
    assert _wiring_findings(plain, [_STORE, _BARE_APP]) == ()


_PARSE = _ifc("parse", "def parse(text: str) -> DiceExpression: ...", "function")
_EVALUATE = _ifc(
    "evaluate",
    "def evaluate(expr: DiceExpression, rng: random.Random, limit: Optional[int]) -> int: ...",
    "function",
)


def test_a_data_type_two_tickets_name_must_be_declared_in_the_contracts_file() -> None:
    # A local dice run: the parser and the evaluator each defined their own
    # DiceExpression, and cli.py converted between the two copies.
    tickets = [
        _t("PARSER", implements=("parse",)),
        _t("EVAL", implements=("evaluate",)),
        _t("CLI", "PARSER", "EVAL", entry=True, consumes=("parse", "evaluate")),
    ]
    (finding,) = _shared_type_findings(tickets, [_PARSE, _EVALUATE], frozenset())
    assert finding.startswith("Type DiceExpression is used by the contracts of tickets PARSER, ")
    assert "EVAL" in finding and finding_kind(finding) == "type-undeclared"
    # A consumer of the one ticket naming it shares it too.
    alone = [tickets[0], _t("CLI", "PARSER", consumes=("parse",))]
    assert len(_shared_type_findings(alone, [_PARSE], frozenset())) == 1

    # Declared as a data type (its fields, in the contracts file): no finding.
    expr = _ifc(
        "DiceExpression",
        "class Constant:\n    value: int\n\n"
        "class BinaryOp:\n    op: str\n    left: DiceExpression\n    right: DiceExpression\n\n"
        "DiceExpression = Constant | BinaryOp",
        "type",
    )
    declared = frozenset({"DiceExpression"})
    assert _shared_type_findings(tickets, [expr, _PARSE, _EVALUATE], declared) == ()
    # A name only that type's signature defines is in the contracts file too.
    const = _ifc("parse_constant", "def parse_constant(t: str) -> Constant: ...", "function")
    users = [_t("A", implements=("parse_constant",)), _t("B", consumes=("parse_constant",))]
    assert _shared_type_findings(users, [expr, const], declared) == ()
    # A class contract one ticket implements is imported from that ticket.
    store = [_t("STORE", implements=("DomainStore",)), _t("APP", consumes=("DomainStore",))]
    assert _shared_type_findings(store, [_STORE], frozenset()) == ()


def test_a_shared_undeclared_type_gets_one_owner_in_code_not_a_repair() -> None:
    def files(t: Ticket, *paths: str) -> Ticket:
        return t.model_copy(update={"files": paths})

    tickets = [
        files(_t("PARSER", implements=("parse",)), "tests/test_parse.py", "dice/parser.py"),
        files(_t("EVAL", implements=("evaluate",)), "dice/evaluator.py"),
        files(_t("CLI", "PARSER", "EVAL", entry=True, consumes=("parse", "evaluate")), "main.py"),
    ]
    out, concerns, owned = _assign_shared_types(tickets, [_PARSE, _EVALUATE], frozenset(), "python")
    parser, evaluator, cli = out
    assert "Define the shared data type DiceExpression in dice/parser.py" in parser.description
    assert "Import the shared data type DiceExpression from dice/parser.py" in evaluator.description
    assert evaluator.depends_on == ("PARSER",)  # it imports the owner's module
    assert "Import the shared data type" in cli.description and cli.depends_on == ("PARSER", "EVAL")
    assert concerns == [
        "Shared type DiceExpression: owned by PARSER in dice/parser.py, imported by EVAL, CLI."
    ]
    assert owned == {"DiceExpression": "dice/parser.py"}
    assert _shared_type_findings(out, [_PARSE, _EVALUATE], frozenset(), owned) == ()
    # A type no user can own (none writes a source file) is still a plan finding.
    bare = [t.model_copy(update={"files": ()}) for t in tickets]
    assert _assign_shared_types(bare, [_PARSE, _EVALUATE], frozenset(), "python") == (
        bare,
        [],
        {},
    )
    assert len(_shared_type_findings(bare, [_PARSE, _EVALUATE], frozenset())) == 1


def test_a_shared_type_in_java_gets_its_own_file_in_the_owner_ticket() -> None:
    parse = _ifc("parse", "public static Roll parse(String text);", "function")
    total = _ifc("total", "public static int total(Roll roll);", "function")
    tickets = [
        _t("PARSER", implements=("parse",)).model_copy(
            update={"files": ("src/main/java/dice/Parser.java",)}
        ),
        _t("EVAL", implements=("total",)).model_copy(
            update={"files": ("src/main/java/dice/Evaluator.java",)}
        ),
    ]
    (parser, evaluator), _, owned = _assign_shared_types(
        tickets, [parse, total], frozenset(), "java"
    )
    assert owned == {"Roll": "src/main/java/dice/Roll.java"}
    assert parser.files[-1] == "src/main/java/dice/Roll.java"
    assert "from src/main/java/dice/Roll.java" in evaluator.description


_DICE_DESIGN = """Types live in contracts.py, all frozen dataclasses:

```python
@dataclass(frozen=True)
class DiceTerm:
    count: int  # how many dice
    sides: int
```

ConstantTerm(value: int), SignedTerm(sign: int, term: DiceTerm | ConstantTerm) and
RollExpression(terms: list[SignedTerm]). parse(text) returns a RollExpression.
"""
_ROLL_PARSE = _ifc("parse", "def parse(text: str) -> RollExpression: ...", "function")
_ROLL_TOTAL = _ifc("total", "def total(expr: RollExpression) -> int: ...", "function")


def _with_files(t: Ticket, *paths: str, reads: tuple[str, ...] = ()) -> Ticket:
    return t.model_copy(update={"files": paths, "reads": reads})


def test_design_types_reads_inline_and_block_definitions_in_any_language() -> None:
    types = design_types(
        _DICE_DESIGN
        + "\ninterface Note {\n  id: number;\n  tags?: string[];\n  render(): string;\n}\n"
        + "public record Point(int x, int y) {}\n"
        + "A Helper(the input) is prose, not a type.\n"
    )
    assert types == {
        "ConstantTerm": "ConstantTerm(value: int)",
        "SignedTerm": "SignedTerm(sign: int, term: DiceTerm | ConstantTerm)",
        "RollExpression": "RollExpression(terms: list[SignedTerm])",
        "Point": "Point(int x, int y)",
        "DiceTerm": "DiceTerm(count: int, sides: int)",
        "Note": "Note(id: number, tags: string[])",
    }
    assert with_referenced(["RollExpression"], types) == [
        "RollExpression",
        "SignedTerm",
        "DiceTerm",
        "ConstantTerm",
    ]


def test_a_shared_type_the_design_defines_carries_its_fields_and_the_types_they_use() -> None:
    # A local dice run: the design body gave RollExpression(terms: list[SignedTerm]) but
    # no type contract, so the owner invented the fields and SignedTerm went unused.
    tickets = [
        _with_files(_t("PARSER", implements=("parse",)), "dice/parser.py"),
        _with_files(_t("EVAL", implements=("total",)), "dice/evaluator.py"),
    ]
    types = design_types(_DICE_DESIGN)
    (parser, evaluator), concerns, owned = _assign_shared_types(
        tickets, [_ROLL_PARSE, _ROLL_TOTAL], frozenset(), "python", types
    )
    assert owned == {"RollExpression": "dice/parser.py"}
    assert (
        "Its fields, from the design (define the types they use beside it): "
        "RollExpression(terms: list[SignedTerm]); "
        "SignedTerm(sign: int, term: DiceTerm | ConstantTerm); "
        "DiceTerm(count: int, sides: int); ConstantTerm(value: int)."
    ) in parser.description
    assert "never define your own copy. Its fields, from the design: Roll" in (
        evaluator.description
    )
    # A design type only one ticket's contracts use gets its fields there.
    (alone,), concerns, owned = _assign_shared_types(
        tickets[:1], [_ROLL_PARSE], frozenset(), "python", types
    )
    assert owned == {}
    assert "Define the data type RollExpression. Its fields, from the design" in alone.description
    assert concerns == ["Type RollExpression: ticket PARSER defines it with the design's fields."]
    # No design definition: the notes stay as they were.
    (bare, _), _, _ = _assign_shared_types(
        tickets, [_ROLL_PARSE, _ROLL_TOTAL], frozenset(), "python"
    )
    assert "Its fields" not in bare.description


def test_reads_of_files_no_ticket_writes_and_unknown_dependencies_are_dropped() -> None:
    tickets = [
        _with_files(
            _t("PARSER", "contracts", "GONE"),
            "dice/parser.py",
            reads=("contracts.py", "dice/", "features/parser.feature", "docs/*.md", "README.md"),
        ),
        _with_files(_t("CLI", "PARSER"), "dice/cli.py", reads=("dice/parser.py",)),
    ]
    (parser, cli), concerns = _drop_dangling_refs(tickets, existing=("README.md",))
    assert parser.reads == ("dice/", "features/parser.feature", "docs/*.md", "README.md")
    assert parser.depends_on == ()
    assert cli is tickets[1]
    assert concerns == [
        "Ticket PARSER: dropped reads contracts.py, which no ticket writes.",
        "Ticket PARSER: dropped depends_on contracts, GONE, which no ticket has.",
    ]


def test_a_contracts_file_no_ticket_writes_is_renamed_to_the_shared_types_home() -> None:
    # A local dice plan named contracts.py, but no contract was a data type: the
    # shared-type fold put RollExpression in diceroll/__init__.py, while T1 and T2
    # still said "data types from contracts.py".
    def said(t: Ticket, text: str) -> Ticket:
        return t.model_copy(update={"description": text})

    tickets = [
        said(
            _with_files(_t("T1", implements=("parse",)), "diceroll/__init__.py"),
            "Parse notation into the data types from contracts.py.",
        ),
        said(
            _with_files(_t("T2", implements=("total",)), "diceroll/roll.py"),
            "Total a roll; import RollExpression from `contracts.py` (see contracts.pyc).",
        ),
    ]
    tickets, _, owned = _assign_shared_types(
        tickets, [_ROLL_PARSE, _ROLL_TOTAL], frozenset(), "python"
    )
    assert owned == {"RollExpression": "diceroll/__init__.py"}
    (t1, t2), concerns = _settle_contracts_file(tickets, "contracts.py", owned.values())
    assert t1.description.startswith("Parse notation into the data types from diceroll/__init__")
    assert "import RollExpression from `diceroll/__init__.py` (see contracts.pyc)" in (
        t2.description
    )
    assert "contracts.py" not in t1.description + t2.description.replace("contracts.pyc", "")
    assert len(concerns) == 2
    # A nested path is matched by its name too; with no home the mention is generic.
    (bare,), _ = _settle_contracts_file(
        [said(_t("A"), "Use the types in src/contracts.ts and contracts.ts.")],
        "src/contracts.ts",
        (),
    )
    assert bare.description == (
        "Use the types in the module that defines them and the module that defines them."
    )
    # Written by a ticket, or already in the base tree: left alone.
    owner = [_with_files(said(_t("contracts"), "x"), "contracts.py"), tickets[0]]
    assert _settle_contracts_file(owner, "contracts.py", ()) == (owner, [])
    assert _settle_contracts_file(tickets, "contracts.py", (), ("contracts.py",)) == (
        tickets,
        [],
    )


def test_the_epic_brief_names_the_contracts_file_only_when_one_is_written() -> None:
    payload = {
        "stack": {"language": "python", "contracts_file": "contracts.py"},
        "adrs": [{"id": "ADR-1", "title": "Seeded RNG", "decision": "random.Random(seed)"}],
    }
    assert "contracts file" in _plan_brief(payload)
    brief = _plan_brief(payload, contracts=False)
    assert "contracts.py" not in brief
    # The ADRs reach the epic calls from adrs[], not a design body restating them.
    assert "Decisions:\n- ADR-1 Seeded RNG: random.Random(seed)" in brief


def test_the_sop_asks_for_each_fact_once() -> None:
    # A local dice plan restated ADR-1..4 in design.body (~1.8k chars of a 33k-token
    # reply that took ~25 minutes).
    from foundry.agents.planner.schema import EPIC_SOP, SOP

    assert "every decision ONLY in adrs[]" in SOP and "never in design.body" in SOP
    for sop in (SOP, EPIC_SOP):
        assert "never restate what its own fields carry" in sop
        assert "belong to the first-wave ticket every test depends on" in sop


def test_design_types_for_the_contracts_file_without_type_contracts_are_a_finding() -> None:
    tickets = [_t("PARSER", implements=("parse",)), _t("EVAL", implements=("total",))]
    ifcs = [_ROLL_PARSE, _ROLL_TOTAL]
    types = design_types(_DICE_DESIGN)
    (finding,), concerns = _type_contract_findings(
        tickets, ifcs, frozenset(), types, "dice/contracts.py", _DICE_DESIGN
    )
    assert concerns == ()
    assert finding.startswith(
        "The design defines RollExpression, SignedTerm, DiceTerm, ConstantTerm for "
        "dice/contracts.py, but no interface contract declares them"
    )
    assert finding_kind(finding) == "type-contract-missing"
    # One ticket using them: the fold's field list covers it, a concern only.
    assert _type_contract_findings(
        tickets[:1], ifcs[:1], frozenset(), types, "contracts.py", _DICE_DESIGN
    ) == ((), (finding.replace("dice/contracts.py", "contracts.py"),))
    # A type contract exists, or the design never names the file: nothing.
    record = _ifc("Other", "class Other:\n    x: int", "type")
    assert _type_contract_findings(
        tickets, [*ifcs, record], frozenset({"Other"}), types, "contracts.py", _DICE_DESIGN
    ) == ((), ())
    assert _type_contract_findings(
        tickets, ifcs, frozenset(), types, "src/types.ts", _DICE_DESIGN
    ) == ((), ())


def _crit(cid: str, given: str, when: str, then: str, abuse: bool = False) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given {given} When {when} Then {then}",
        given=(given,),
        when=(when,),
        then=(then,),
        security_abuse=abuse,
        seq=_SEQ,
    )


def test_a_scenario_example_breaking_a_pinned_numeric_bound_is_a_finding() -> None:
    one_side = _crit("AC-1", "a die with 1 side", "it is rolled", "the total is 1")
    (finding,) = bound_conflicts([("PC-001", "Die sides must be ≥ 2.")], [one_side])
    assert finding.startswith("Scenario AC-1 uses sides 1, but pinned constraint PC-001")
    assert "requires sides ≥ 2" in finding
    assert finding_kind(finding) == "scenario-constraint-conflict"
    for text in ("at least 2 sides", "sides between 2 and 100", "sides >= 2"):
        assert len(bound_conflicts([("PC-1", text)], [one_side])) == 1, text
    ok = [
        # The example satisfies the bound.
        _crit("AC-2", "a die with 6 sides", "it is rolled", "the total is at most 6"),
        # It proves the value is refused.
        _crit("AC-3", "a die with 1 side", "it is rolled", "an error is printed"),
        _crit("AC-4", "a die with 1 side", "it is rolled", "the total is 1", abuse=True),
    ]
    assert bound_conflicts([("PC-001", "sides ≥ 2")], ok) == ()
    # Ambiguous bounds are never reported: conditional, generic noun, a unit after it.
    for text in ("sides >= 2 unless the die is fudge", "value >= 2", "names at most 1 word"):
        assert bound_conflicts([("PC-1", text)], [one_side]) == (), text


def test_builtin_and_generic_names_are_never_shared_types() -> None:
    sigs = {
        "py": "def f(xs: List[str], o: Optional[Path], r: Random, m: Literal['Add']) -> "
        'Iterator[Decimal]:\n    """Return the Parsed Result."""',
        "ts": "export function f(xs: Array<string>, m: Map<string, Date>): Promise<Record<"
        "string, Partial<T>>> // Returns Nothing Useful",
        "go": "func F(ctx context.Context, w http.ResponseWriter) (time.Time, error)",
        "consts": "def f(level: int = MAX_LEVEL, c: str = Color.RED) -> None: ...",
        "imported": "from vendor.sdk import Client\ndef f(c: Client) -> None: ...",
    }
    for lang, sig in sigs.items():
        f = _ifc("f", sig, "function")
        tickets = [_t("A", implements=("f",)), _t("B", consumes=("f",))]
        assert _shared_type_findings(tickets, [f], frozenset()) == (), lang
    # One ticket alone naming its own type is its business.
    assert _shared_type_findings([_t("PARSER", implements=("parse",))], [_PARSE], frozenset()) == ()


def _wired_plan(app_signature: str) -> dict[str, Any]:
    plan = _copy(PLAN)
    plan["interfaces"] = [
        {"name": "TodoStore", "kind": "class", "signature": _STORE.signature},
        {"name": "TodoApp", "kind": "class", "signature": app_signature},
    ]
    plan["tickets"][0]["implements"] = ["TodoStore"]
    plan["tickets"][1]["implements"] = ["TodoApp"]
    plan["tickets"][1]["consumes"] = ["TodoStore"]
    return plan


def test_an_unwired_composition_root_feeds_the_in_node_revision() -> None:
    bare = "class TodoApp(Protocol):\n" + _WSGI_CALL
    wired = (
        "class TodoApp(Protocol):\n"
        "    def __init__(self, store: TodoStore) -> None: ...\n" + _WSGI_CALL
    )
    cas = ArtifactStore()
    rt, tx = runtime_for(_wired_plan(bare), _wired_plan(wired), cas=cas)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2
    assert "Contract TodoApp (entrypoint ticket T-002) composes TodoStore" in json.dumps(
        tx.requests[1]
    )
    assert counts.snapshot()["planner.finding.contract-unwired"] == 1
    assert _critique(cas, writes).contract_findings == ()


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


# ---- repairs: medium effort, never re-rolled at a length stop ---------------------


def test_a_repair_cut_at_its_cap_goes_straight_to_the_fresh_revision() -> None:
    from foundry.proxy.model_provider_proxy import OutputLimitReached

    flagged = {**PLAN, "criteria_review": _review("no scenario for an empty title")}
    rt, tx = runtime_for(flagged, PLAN)
    seen: list[tuple[Any, Any]] = []
    real = rt.proxy.complete

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append((kwargs.get("effort"), kwargs.get("max_tokens")))
        if kwargs.get("max_tokens") == _REPAIR_MAX_TOKENS:
            raise OutputLimitReached("cut at the cap")
        return real(*args, **kwargs)

    rt.proxy.complete = spy  # type: ignore[method-assign]
    with telemetry.counting() as counts:
        make_planner_node(rt)({"request": "x"}, config_for("r"))

    # first plan, the cut repair (never re-rolled), the fresh self-review revision
    assert seen == [("medium", None), ("medium", _REPAIR_MAX_TOKENS), ("medium", None)]
    assert "SELF-REVIEW REVISION" in _content(tx.requests[-1])
    got = counts.snapshot()
    assert got["planner.repair.cut"] == 1 and got["planner.repair.fallback"] == 1


def test_the_repair_prompt_carries_the_findings_it_must_close() -> None:
    flagged = {**PLAN, "criteria_review": _review("no scenario for an empty title")}
    rt, tx = runtime_for(flagged, PLAN)
    make_planner_node(rt)({"request": "x"}, config_for("r"))
    repair = _content(tx.requests[1])
    assert "REPAIR PASS" in repair
    assert "Flagged gaps:\n- no scenario for an empty title" in repair


# ---- self-review: what the review rules out of scope never blocks ----------------


def test_a_missing_gap_the_review_rules_out_of_the_spec_triggers_no_repair() -> None:
    from foundry.agents.planner.prompt import _review_deferred, _self_review_gaps

    csrf = (
        "No CSRF protection scenario for the POST forms; the spec does not mention "
        "CSRF. Flagged for a future iteration."
    )
    review = {
        "missing": [csrf, "no scenario for an empty title"],
        "security_abuse_omissions": [],
        "out_of_scope": ["multi-user sharing"],
    }
    assert _self_review_gaps({"criteria_review": review}) == ("no scenario for an empty title",)
    assert _review_deferred(review) == ("multi-user sharing", csrf)

    deferred = {**review, "missing": [csrf]}
    cas = ArtifactStore()
    rt, tx = runtime_for({**PLAN, "criteria_review": deferred}, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert len(tx.requests) == 1  # no self-review repair
    critique = _critique(cas, writes)
    assert critique.missing == ()
    assert "Out of scope (self-review): multi-user sharing" in writes["agent_journal"][0].concerns


def test_the_review_schema_has_a_non_blocking_out_of_scope_field() -> None:
    from foundry.agents.planner.schema import _REVIEW_SCHEMA

    assert "out_of_scope" in _REVIEW_SCHEMA["required"]
    assert "out_of_scope" in SOP and "never under missing" in SOP


# ---- contract properties: required, and patched by the delta -------------------


def _propless() -> dict[str, Any]:
    plan = _copy(PLAN)
    plan["interfaces"].append(
        {
            "name": "parse_title",
            "kind": "function",
            "signature": "def parse_title(text: str) -> str: ...",
            "inputs": ["text"],
            "outputs": ["title"],
            "errors": [],
            "description": "Normalise a todo title.",
            "properties": [],
        }
    )
    plan["tickets"][0]["implements"].append("parse_title")
    return plan


def test_a_contract_without_properties_is_patched_by_the_delta_not_re_planned() -> None:
    from foundry.agents.planner.delta import _delta_properties

    idem = "parse_title(parse_title(s)) == parse_title(s)"
    delta = {
        "tickets": [],
        "remove_tickets": [],
        "remove_scenarios": [],
        "contract_properties": [
            {"contract": "parse_title", "properties": [{"id": "PR1", "text": idem}]}
        ],
        "criteria_review": {"missing": [], "security_abuse_omissions": [], "out_of_scope": []},
    }
    cas = ArtifactStore()
    rt, tx = runtime_for(_propless(), delta, cas=cas)
    efforts = _efforts(rt)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2 and efforts == ["medium", "medium"]
    assert DELTA_SOP_KEY in json.dumps(tx.requests[1])
    assert "parse_title" in _content(tx.requests[1])
    assert counts.snapshot()["planner.delta.accepted"] == 1
    contracts = {c.name: c for c in _bodies(cas, writes, "interface_contract")}
    assert contracts["parse_title"].properties == (f"PR1: {idem}",)

    # A taken or malformed id is renumbered; a data type never takes one.
    taken = InterfaceContract(
        name="roll", spec_uri="cas://x", kind="function", properties=("PR1: a",), seq=_SEQ
    )
    record = InterfaceContract(name="Roll", spec_uri="cas://y", kind="type", seq=_SEQ)
    more = [{"id": "PR1", "text": "b"}, {"id": "x", "text": "c"}]
    out = _delta_properties(
        [taken, record],
        {
            "contract_properties": [
                {"contract": "roll", "properties": more},
                {"contract": "Roll", "properties": [{"id": "PR9", "text": "d"}]},
            ]
        },
    )
    assert [c.properties for c in out] == [("PR1: a", "PR2: b", "PR3: c"), ()]


def test_interface_properties_are_required_and_the_sop_says_to_fill_them() -> None:
    from foundry.agents.planner.schema import _INTERFACE_SCHEMA, DELTA_SCHEMA

    assert "properties" in _INTERFACE_SCHEMA["required"]
    assert "contract_properties" in DELTA_SCHEMA["required"]
    assert "properties[] IS the one field a property lives in" in SOP
    # Each operator or mode the spec names gets an end-to-end property.
    assert "checks the RESULT the spec defines for it" in SOP


def test_a_local_planner_drafts_at_low_effort_and_repairs_at_medium() -> None:
    from foundry.agents.planner.schema import TIER

    rt, _ = runtime_for(_uncovered(), {}, PLAN)
    rt.proxy._local_tiers = frozenset({TIER})
    efforts = _efforts(rt)
    make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert efforts[0] == "low"
    assert "medium/repair" in efforts


def _budgets(rt: Any) -> list[int | None]:
    seen: list[int | None] = []
    real = rt.proxy.complete

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("thinking_budget"))
        return real(*args, **kwargs)

    rt.proxy.complete = spy
    return seen


def test_the_local_plan_budget_scales_with_the_request_and_scope() -> None:
    from foundry.agents.planner import local_plan_budget
    from foundry.schemas.governance import ScopeTier

    short = "a command-line tool that rolls dice given notation like 2d6+3"
    assert local_plan_budget(short, ScopeTier.DEMO) == 5120
    assert local_plan_budget(short, ScopeTier.STANDARD) == 7168
    assert local_plan_budget(short, None) == local_plan_budget(short, ScopeTier.STANDARD)
    medium = short + ", with " + "a feature of several words, " * 30
    assert 5120 < local_plan_budget(medium, ScopeTier.DEMO) < 16384
    assert local_plan_budget("x " * 3000, ScopeTier.STANDARD) == 16384
    assert local_plan_budget("", ScopeTier.DEMO) == 4096


def test_a_local_planners_plan_and_repair_calls_carry_the_sized_budget() -> None:
    from foundry.agents.planner import local_plan_budget
    from foundry.agents.planner.schema import TIER

    rt, _ = runtime_for(_uncovered(), {}, PLAN)
    rt.proxy._local_tiers = frozenset({TIER})
    efforts, budgets = _efforts(rt), _budgets(rt)
    make_planner_node(rt)({"request": "a dice roller"}, config_for("r"))

    assert "medium/repair" in efforts
    assert budgets == [local_plan_budget("a dice roller", None)] * len(budgets)


def test_a_local_planners_delta_call_carries_the_sized_budget() -> None:
    from foundry.agents.planner import local_plan_budget
    from foundry.agents.planner.schema import TIER

    rt, tx = runtime_for(_two_implementers(), _DROP_CONTRACTS_1)
    rt.proxy._local_tiers = frozenset({TIER})
    budgets = _budgets(rt)
    make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert _is_delta(tx.requests[1])
    assert budgets == [local_plan_budget("x", None)] * 2


def test_a_cloud_planner_sends_no_thinking_budget() -> None:
    rt, tx = runtime_for(_two_implementers(), _DROP_CONTRACTS_1)
    budgets = _budgets(rt)
    make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert _is_delta(tx.requests[1]) and budgets == [None, None]
    assert all("local_reasoning_budget" not in r for r in tx.requests)
