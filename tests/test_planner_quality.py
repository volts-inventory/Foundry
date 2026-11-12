"""Generator-quality planner rules: shipping contracts, entrypoint wiring, scenario
branches, roles, the code-added security scenario, the dev-free manifest scaffold,
and the planner's call efforts and delta cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundry import telemetry
from foundry.agents.implementation.tickets.contracts import entrypoint_redefinitions
from foundry.agents.implementation.tickets.packet import (
    ENTRYPOINT_RULE,
    PRODUCTION_WIRING_RULE,
    _ticket_packet,
)
from foundry.agents.planner import make_planner_node
from foundry.agents.planner.checks import (
    _branch_findings,
    _entrypoint_findings,
    _role_findings,
    finding_kind,
)
from foundry.agents.planner.folds import _ensure_security_scenario
from foundry.agents.planner.stack import _attach_generated, _generated_tickets
from foundry.durability.artifact_store import ArtifactStore
from foundry.graph.gate_wiring.materialize import _python_third_party_requirements
from foundry.proxy.model_provider_proxy import OutputLimitReached
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    Specialty,
    StackDecision,
    Task,
    Ticket,
)
from foundry.schemas.product import AcceptanceCriterion, IntentBrief
from foundry.state.seq import Seq
from tests.planner_payload import DELTA_SOP_KEY
from tests.test_planner import PLAN, config_for, runtime_for

_SEQ = Seq("r", 1, 1, "planner", 0)


def _iface(name: str, kind: str, signature: str) -> InterfaceContract:
    return InterfaceContract(
        name=name, spec_uri=f"cas://{name}", kind=kind, signature=signature, seq=_SEQ
    )


def _ticket(tid: str, *deps: str, **extra: Any) -> Ticket:
    return Ticket(
        id=tid,
        title=extra.pop("title", tid),
        specialty=extra.pop("specialty", Specialty.BACKEND),
        depends_on=deps,
        seq=_SEQ,
        **extra,
    )


def _criterion(cid: str, tid: str, *covers: str, **extra: Any) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"scenario {cid}",
        given=("g",),
        when=extra.pop("when", ("w",)),
        then=extra.pop("then", ("t",)),
        ticket_id=tid,
        covers=covers,
        seq=_SEQ,
        **extra,
    )


def _brief(*must_haves: str, intent: str = "") -> IntentBrief:
    return IntentBrief(
        intent=intent,
        must_haves=must_haves,
        must_have_ids=tuple(f"MH-{i}" for i in range(1, len(must_haves) + 1)),
        seq=_SEQ,
    )


# ---- contracts (3, 4) ------------------------------------------------------------


def test_the_contracts_ticket_emits_only_data_types_and_errors() -> None:
    interfaces = [
        _iface("Note", "type", "@dataclass\nclass Note:\n    id: int\n    body: str"),
        _iface(
            "NotFound",
            "class",
            "class NotFound(Exception):\n    def __init__(self, note_id: int) -> None: ...",
        ),
        _iface("NoteStore", "class", "class NoteStore:\n    def add(self, body: str) -> Note: ..."),
        _iface("search", "function", "def search(q: str) -> list[Note]: ..."),
    ]
    [contracts], _ = _generated_tickets(interfaces, {}, StackDecision(language="python"), _SEQ)
    assert contracts.id == CONTRACTS_TICKET_ID
    assert contracts.implements == ("Note", "NotFound")
    assert "This code ships" in contracts.description
    assert "Protocol" not in contracts.description.replace("no Protocol", "")


def test_no_contracts_ticket_when_every_contract_has_behaviour() -> None:
    interfaces = [_iface("search", "function", "def search(q: str) -> list[str]: ...")]
    out, _ = _generated_tickets(interfaces, {}, StackDecision(language="python"), _SEQ)
    assert out == []


def test_a_feature_ticket_implementing_a_contract_type_consumes_it_instead() -> None:
    interfaces = [_iface("Note", "type", "class Note: ...")]
    generated, _ = _generated_tickets(interfaces, {}, StackDecision(language="python"), _SEQ)
    store = _ticket("store", files=("store.py",), implements=("Note",))
    tickets, concerns = _attach_generated([store], generated, _SEQ)
    moved = next(t for t in tickets if t.id == "store")
    assert moved.implements == () and moved.consumes == ("Note",)
    assert any("moved from implements to consumes" in c for c in concerns)


# ---- entrypoint (5) --------------------------------------------------------------


def test_an_entrypoint_redefining_a_dependency_is_a_plan_finding() -> None:
    interfaces = [
        _iface("routes", "function", "def list_notes() -> str: ..."),
        _iface("app", "function", "def create_app() -> object: ...\ndef list_notes() -> str: ..."),
    ]
    notes = _ticket("notes", files=("notes/routes.py",), implements=("routes",))
    entry = _ticket("web", "notes", files=("app.py",), implements=("app",), entrypoint=True)
    [finding] = _entrypoint_findings([notes, entry], interfaces)
    assert finding.startswith("Entrypoint ticket web redefines what ticket notes owns")
    assert "(list_notes)" in finding
    assert finding_kind(finding) == "entrypoint-redefines"
    interfaces[1] = _iface("app", "function", "def create_app() -> object: ...")
    # A shared file alone is no finding: a split ticket's children share their files.
    shared = entry.model_copy(update={"files": ("app.py", "notes/routes.py")})
    assert _entrypoint_findings([notes, shared], interfaces) == ()


def test_the_post_build_check_flags_an_entrypoint_copy_of_a_dependency_route() -> None:
    routes = "def list_notes():\n    return 'x'\n\n\ndef _helper():\n    pass\n"
    entry = (
        "from notes.routes import bp\n\ndef create_app():\n    ...\n\ndef list_notes():\n    ...\n"
    )
    tests = "def list_notes():\n    pass\n"
    [finding] = entrypoint_redefinitions(
        {"app.py": entry}, {"notes/routes.py": routes, "tests/test_app.py": tests}
    )
    assert finding.startswith("app.py redefines list_notes (defined in notes/routes.py)")
    clean = "from notes.routes import list_notes\n\ndef create_app():\n    ...\n"
    assert entrypoint_redefinitions({"app.py": clean}, {"notes/routes.py": routes}) == ()


# ---- packet rules (5, 6) ---------------------------------------------------------


def _packet_rules(ticket: Ticket) -> list[str]:
    task = Task(
        id=f"task-{ticket.id}",
        ticket_id=ticket.id,
        spec_uri="cas://s",
        criterion_ids=(),
        branch=f"task/{ticket.id}",
        seq=_SEQ,
    )
    packet = _ticket_packet(
        task,
        ticket,
        (),
        spans=(),
        feature_files=(),
        prior_failures=(),
        turn_budget=10,
        cost_budget_microusd=0,
    )
    return list(packet.task["rules"])


def test_the_entrypoint_packet_wires_dependencies_and_tests_use_production_wiring() -> None:
    entry = _ticket("web", files=("app.py", "tests/test_web.py"), entrypoint=True)
    rules = _packet_rules(entry)
    assert ENTRYPOINT_RULE in rules and PRODUCTION_WIRING_RULE in rules
    plain = _packet_rules(_ticket("store", files=("store.py",)))
    assert ENTRYPOINT_RULE not in plain and PRODUCTION_WIRING_RULE not in plain
    steps = _packet_rules(_ticket("steps", files=("features/step_definitions/a.steps.ts",)))
    assert PRODUCTION_WIRING_RULE in steps


# ---- scenario rules (11, 13) -----------------------------------------------------


def test_an_or_criterion_with_one_scenario_is_flagged() -> None:
    brief = _brief("search notes by title or tag", "add one or more tags")
    one = [_criterion("AC-1", "t", "MH-1"), _criterion("AC-2", "t", "MH-2")]
    [finding] = _branch_findings(one, brief)
    assert finding.startswith("Must-have MH-1") and "only scenario AC-1" in finding
    assert finding_kind(finding) == "or-branch"
    both = [*one, _criterion("AC-3", "t", "MH-1")]
    assert _branch_findings(both, brief) == ()


def test_a_request_with_roles_needs_a_ticket_owning_them() -> None:
    brief = _brief("an admin can delete any post")
    posts = _ticket("posts", files=("posts.py",))
    [finding] = _role_findings([posts], brief)
    assert finding_kind(finding) == "roles-unowned"
    owner = _ticket("accounts", title="Accounts", description="Roles, the admin account, seed")
    assert _role_findings([posts, owner], brief) == ()
    assert _role_findings([posts], _brief("users write posts")) == ()


# ---- security scenario added in code (25) ----------------------------------------


def test_a_misuse_scenario_is_flagged_when_none_is() -> None:
    tickets = [_ticket("api", entrypoint=True, scenario_ids=("AC-1", "AC-2"))]
    criteria = [
        _criterion("AC-1", "api"),
        _criterion("AC-2", "api", when=("a blank title is posted",), then=("it is rejected",)),
    ]
    out, flagged, [note] = _ensure_security_scenario(tickets, criteria, _SEQ)
    assert out == tickets
    assert [c.security_abuse for c in flagged] == [False, True]
    assert "AC-2" in note


def test_a_security_scenario_is_added_to_the_entrypoint_when_none_covers_misuse() -> None:
    api = _ticket("api", entrypoint=True, scenario_ids=("AC-1",))
    steps = _ticket("api-tests", "api", specialty=Specialty.TEST, scenario_ids=("AC-1",))
    store = _ticket("store", scenario_ids=())
    out, criteria, _ = _ensure_security_scenario(
        [store, api, steps], [_criterion("AC-1", "api")], _SEQ
    )
    added = criteria[-1]
    assert added.security_abuse and added.ticket_id == "api" and added.then
    by_id = {t.id: t for t in out}
    assert added.id in by_id["api"].scenario_ids and added.id in by_id["api-tests"].scenario_ids
    assert by_id["store"].scenario_ids == ()
    flagged = [_criterion("AC-1", "api", security_abuse=True)]
    assert _ensure_security_scenario([api], flagged, _SEQ)[1] == flagged


# ---- the manifest scaffold pins runtime imports only (26) -------------------------


def test_the_scaffold_pins_no_dev_or_test_tools(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import json\nimport mypy\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import pytest\nimport hypothesis\n")
    (tmp_path / "conftest.py").write_text("import pytest_bdd\n")
    assert _python_third_party_requirements(tmp_path) == []


# ---- planner calls (22, 24) ------------------------------------------------------


def _uncovered() -> dict[str, Any]:
    plan = json.loads(json.dumps(PLAN))
    plan["tickets"][1]["scenarios"][0]["covers"] = ["SC-1"]  # MH-1 left uncovered
    return plan


def test_the_first_plan_runs_at_medium_and_a_cut_delta_goes_straight_to_the_repair() -> None:
    rt, tx = runtime_for(_uncovered(), PLAN, cas=ArtifactStore())
    seen: list[tuple[bool, Any, Any]] = []
    real = rt.proxy.complete

    def spy(*args: Any, **kwargs: Any) -> Any:
        delta = DELTA_SOP_KEY in args[2]
        seen.append((delta, kwargs.get("effort"), kwargs.get("max_tokens")))
        if delta:
            raise OutputLimitReached("cut at the cap")
        return real(*args, **kwargs)

    rt.proxy.complete = spy  # type: ignore[method-assign]
    with telemetry.counting() as counts:
        make_planner_node(rt)({"request": "x"}, config_for("r"))

    assert seen == [(False, "medium", None), (True, "medium", 32_000), (False, "xhigh", None)]
    assert len(tx.requests) == 2  # the delta never reached the transport, nor was re-rolled
    assert counts.snapshot()["planner.delta.rejected"] == 1


def test_the_sqlite_constraints_default_to_a_file_database() -> None:
    from foundry.agents.planner.constraints import _stack_constraints

    pcs = _stack_constraints(StackDecision(language="python"), {"x": "SQLAlchemy over SQLite"})
    memory, alchemy = (
        next(pc["text"] for pc in pcs if pc["id"] == i)
        for i in ("PC-python-sqlite-memory", "PC-python-sqlalchemy-sqlite")
    )
    assert "a FILE whose path comes from an environment variable" in memory
    assert "check_same_thread=False" in memory
    assert "sqlite:///app.db" in alchemy
