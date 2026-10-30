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

from foundry.agents import plan_contracts
from foundry.agents.plan_contracts import check_contracts
from foundry.agents.planner import (
    SOP,
    _drop_self_consumes,
    _wire_entrypoint,
    make_planner_node,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.proxy.test_doubles import ResponseScript
from foundry.schemas.implementation import Specialty, Ticket
from foundry.schemas.product import CriteriaCritique
from foundry.state.seq import Seq
from tests.planner_payload import PYTHON_STACK, _scn, _ticket, multi_epic_plan
from tests.test_planner import (
    PLAN,
    _bodies,
    _epic_of,
    _epic_runtime,
    _epic_scripts,
    _failed,
    _first_pass,
    _match,
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
    rt, tx = runtime_for(_uncovered(), PLAN, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2
    revision = json.dumps(tx.requests[1], ensure_ascii=False)
    assert "REVISION PASS — the planner's own plan check" in revision
    assert "Plan check findings:\\n- Must-have MH-1 ('add todo') has no covering" in revision
    assert "id=AC-001 (ticket=T-002)" in revision  # the criteria to keep
    critique = _critique(cas, writes)
    assert critique.coverage_findings == () and critique.contract_findings == ()
    assert not critique.spec_gate_blocking()
    summary = writes["agent_journal"][0].summary
    assert "Revised the plan once in-node after its own plan check: 1 -> 0 finding(s)." \
        in summary


def test_a_worse_revision_is_discarded() -> None:
    worse = _copy(_uncovered())
    worse["tickets"][1]["scenarios"][0]["covers"] = []  # now SC-1 is uncovered too
    cas = ArtifactStore()
    rt, tx = runtime_for(_uncovered(), worse, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert len(tx.requests) == 2  # one revision, never more
    assert _critique(cas, writes).coverage_findings == (
        "Must-have MH-1 ('add todo') has no covering scenario; add one whose covers lists "
        "MH-1.",
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

    second = node(_failed(first, "criterion E2-AC01: scenario has no then step"),
                  config_for("r", step=5))
    assert [_epic_of(r) for r in tx.requests[4:]] == ["E2", "E2"]
    assert "Plan check findings" in json.dumps(tx.requests[5])
    assert _critique(cas, second).coverage_findings == ()
    assert "Revised epic(s) E2 once in-node" in second["agent_journal"][0].summary


# ---- repairs ------------------------------------------------------------------------


def _t(tid: str, *deps: str, entry: bool = False, test: bool = False,
       implements: tuple[str, ...] = (), consumes: tuple[str, ...] = ()) -> Ticket:
    return Ticket(id=tid, title=tid, depends_on=deps, entrypoint=entry,
                  specialty=Specialty.TEST if test else Specialty.BACKEND,
                  implements=implements, consumes=consumes, seq=_SEQ)


def test_the_entrypoint_is_wired_to_every_feature_unless_that_closes_a_cycle() -> None:
    tickets = [
        _t("A"), _t("B", "A"), _t("C"), _t("APP", "A", entry=True), _t("T", "APP", test=True),
    ]
    out, concerns = _wire_entrypoint(tickets)
    by_id = {t.id: t for t in out}
    assert by_id["APP"].depends_on == ("A", "B", "C")  # never the test ticket
    assert concerns == ["Ticket APP: added dependencies on feature ticket(s) B, C so the "
                        "entrypoint wires every feature in."]

    cyclic = [_t("A", "APP"), _t("APP", entry=True)]
    out, concerns = _wire_entrypoint(cyclic)
    assert out == cyclic and concerns == []  # left for the coverage check


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
    {"name": "TodoStore.recent", "kind": "function",
     "signature": "def recent(self, n: int) -> list[Todo]"},
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
        if i["name"] == "TodoStore.list" else i
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
        'Contract TodoStore.recent does not compile (mypy): Function '
        '"contracts.TodoStore.list" is not valid as a type [valid-type]',
    )  # mypy is the authority when it ran: no duplicate heuristic finding


def test_python_assembly_and_type_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    if not _mypy_here():
        pytest.skip("mypy is not installed for python3 here")
    result = check_contracts([
        ("Money", "Amount = Decimal"),
        ("Repo", "class Repo(Protocol):\n    def get(self, id: UUID) -> Thing | None: ..."),
        ("parse", "def parse(text: str) -> Amount"),
        ("Prose", "this is prose"),
    ], "python")
    assert result.ran
    assert result.findings == (
        'Contract Repo does not compile (mypy): Name "Thing" is not defined [name-defined]',
    )
    assert result.concerns == (
        "Contract Prose: its signature is not valid Python, so it was not compiled at "
        "plan time.",
    )


def test_typescript_contracts_compile_with_tsc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_PLAN_COMPILE", "1")
    result = check_contracts([
        ("Todo", "interface Todo { id: string }"),
        ("TodoStore.add", "add(todo: Todo): string"),
        ("TodoStore.list", "list(): Todoo[]"),
        ("Store", "class Store { get(id: string): Todo }"),
        ("broken", "type X = ;"),
    ], "typescript")
    if not result.ran:
        pytest.skip("tsc is not installed here")
    assert result.findings == (
        "Contract TodoStore.list does not compile (tsc): Cannot find name 'Todoo'. Did you "
        "mean 'Todo'? [TS2552]",
    )
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
    plan["tickets"].append({**_ticket("T-009", "backend"), "scenarios": [
        {**_scn("AC-009", "MH-1", "SC-1"), "security_abuse": True}
    ]})
    cas = ArtifactStore()
    rt, _ = runtime_for(plan, cas=cas)
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    critique = _critique(cas, writes)
    assert not any("Unused" in f for f in critique.contract_findings)
    assert any(c.startswith("Contract Unused (implemented by T-002) has no consumer")
               for c in writes["agent_journal"][0].concerns)
