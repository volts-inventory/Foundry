"""Generator-quality planner rules: shipping contracts, entrypoint wiring, scenario
branches, roles, the code-added security scenario, the dev-free manifest scaffold,
and the planner's call efforts and delta cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry import telemetry
from foundry.agents.implementation.tickets.contracts import (
    dependency_symbols,
    entrypoint_redefinitions,
)
from foundry.agents.implementation.tickets.packet import (
    ENTRYPOINT_RULE,
    PRODUCTION_WIRING_RULE,
    _ticket_context,
    _ticket_packet,
    blocked_reads,
    upstream_defect_context,
)
from foundry.agents.planner import _REPAIR_MAX_TOKENS, make_planner_node
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
        given=extra.pop("given", ("g",)),
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
        {"app.py": entry}, {"notes/routes.py": routes, "tests/test_app.py": tests}, {"list_notes"}
    )
    assert finding.startswith("app.py redefines list_notes (defined in notes/routes.py)")
    clean = "from notes.routes import list_notes\n\ndef create_app():\n    ...\n"
    assert (
        entrypoint_redefinitions({"app.py": clean}, {"notes/routes.py": routes}, {"list_notes"})
        == ()
    )


def test_the_post_build_check_flags_only_a_dependencys_contract_symbols() -> None:
    interfaces = {
        "routes": _iface(
            "routes", "function", "def list_notes() -> str: ...\ndef index() -> str: ..."
        ),
        "app": _iface("app", "function", "def create_app() -> object: ..."),
    }
    notes = _ticket("notes", files=("notes/routes.py",), implements=("routes",))
    entry = _ticket("web", "notes", files=("app.py",), implements=("app",), entrypoint=True)
    guarded = dependency_symbols(entry, [notes, entry], interfaces)
    # A common entry name is every module's own, even when a dependency's contract has it.
    assert guarded == {"list_notes"}
    routes = (
        "def list_notes():\n    return 'x'\n\ndef index():\n    return 'i'\n\n"
        "def setup():\n    pass\n\ndef run():\n    pass\n"
    )
    # Shared names no dependency contract declares (index, run, setup, create_app)
    # are no finding: an entry module and a blueprint both naturally define them.
    entry_src = (
        "def create_app():\n    ...\n\ndef index():\n    ...\n\n"
        "def setup():\n    ...\n\ndef run():\n    ...\n"
    )
    others = {"notes/routes.py": routes, "other/app.py": "def create_app():\n    ...\n"}
    assert entrypoint_redefinitions({"app.py": entry_src}, others, guarded) == ()
    copied = entry_src + "\ndef list_notes():\n    ...\n"
    [finding] = entrypoint_redefinitions({"app.py": copied}, others, guarded)
    assert finding.startswith("app.py redefines list_notes (defined in notes/routes.py):")


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


def test_the_packet_says_its_scenarios_are_inline_and_lists_no_foreign_feature() -> None:
    ticket = _ticket(
        "web",
        files=("app.py",),
        reads=("features/web.feature", "features/store.feature", "store.py"),
    )
    task = Task(
        id="task-web",
        ticket_id="web",
        spec_uri="cas://s",
        criterion_ids=(),
        branch="task/web",
        seq=_SEQ,
    )
    packet = _ticket_packet(
        task,
        ticket,
        (),
        spans=("SCENARIO S1 [web]:\n  Given g", "INTERFACE CONTRACT Store (...):\n{}"),
        feature_files=("features/web.feature",),
        prior_failures=(),
        turn_budget=10,
        cost_budget_microusd=0,
    )
    assert packet.task["reads"] == ["features/web.feature", "store.py"]
    [rule] = [r for r in packet.task["rules"] if "inline in this packet" in r]
    assert rule.startswith("Your scenarios and interface contracts are inline")
    assert "features/*.feature" in rule and "requirements.txt" in rule
    harness = _packet_rules(_ticket("bdd-harness", files=("requirements.txt",)))
    assert any(r.startswith("This ticket owns no scenario: do not read") for r in harness)


def test_the_packet_blocks_reads_of_other_tickets_tests_steps_and_features() -> None:
    store = _ticket(
        "store",
        files=("store.py", "tests/test_store.py", "tests/conftest.py", "tests/helpers.py"),
    )
    api = _ticket("api", files=("api.py", "tests/steps/test_api_steps.py"))
    harness = _ticket("bdd-harness", files=("tests/test_harness.py",))
    web = _ticket(
        "web",
        "store",
        "api",
        files=("app.py", "tests/test_web.py"),
        reads=("store.py", "tests/test_store.py"),
    )
    tickets = (store, api, harness, web)
    task = Task(
        id="task-web",
        ticket_id="web",
        spec_uri="cas://s",
        criterion_ids=(),
        branch="task/web",
        seq=_SEQ,
    )
    packet = _ticket_packet(
        task,
        web,
        (),
        spans=("SCENARIO S1 [web]:\n  Given g",),
        feature_files=("features/web.feature",),
        prior_failures=(),
        turn_budget=10,
        cost_budget_microusd=0,
        tickets=tickets,
    )
    assert packet.task["blocked_reads"] == [
        "features/store.feature",
        "tests/test_store.py",
        "features/api.feature",
        "tests/steps/test_api_steps.py",
    ]
    assert packet.task["reads"] == ["store.py"]
    [rule] = [r for r in packet.task["rules"] if "inline in this packet" in r]
    assert "another ticket's feature file or test/step module" in rule
    assert blocked_reads(harness, tickets, ()) == ()  # the harness reads anything


def test_the_packet_inlines_the_contracts_its_dependencies_implement() -> None:
    contracts = {
        "Store": _iface("Store", "class", "class Store:\n    def add(self, text: str) -> int"),
        "Note": _iface("Note", "type", "class Note(TypedDict):\n    id: int"),
        "Web": _iface("Web", "http", "GET /notes"),
    }
    store = _ticket("store", files=("store.py",), implements=("Store", "Note"))
    web = _ticket("web", "store", files=("app.py",), implements=("Web",), consumes=("Note",))
    spans, _ = _ticket_context(_NoCas(), web, {}, contracts, (store, web))
    heads = [s.split("\n", 1)[0] for s in spans]
    assert heads == [
        "INTERFACE CONTRACT Web (this ticket IMPLEMENTS it — match it exactly):",
        "INTERFACE CONTRACT Note (this ticket CALLS it — code against it exactly):",
        "INTERFACE CONTRACT Store (ticket store, which this ticket depends on, implements "
        "it — call it exactly as signed):",
    ]
    assert spans[-1].endswith("def add(self, text: str) -> int")


class _NoCas:
    cas = None


def test_an_upstream_defect_reaches_the_fixer_with_its_owning_tickets_files() -> None:
    from foundry.execution.adapters.tiered import UPSTREAM_DEFECT_TRIAGE
    from foundry.manifest import build_manifest, render_manifest_md, upstream_defects
    from foundry.schemas.gates import GateTriage

    note = GateTriage(
        name=UPSTREAM_DEFECT_TRIAGE,
        run_against="task/web",
        explanation="web/routes.py: DELETE /notes/1 is a 405; repro: client.delete('/notes/1')",
        produced_by="proxy-agent",
        seq=_SEQ,
    )
    state: Any = {"gate_triage": [note]}
    routes = _ticket("routes", files=("web/routes.py", "web/forms.py"))
    other = _ticket("cli", files=("routes.py",))  # a bare name in a path is not this file
    lines, files = upstream_defect_context(state, (routes, other))
    assert lines[0].startswith("[upstream-defect]")
    assert lines[1:] == (
        "task/web: web/routes.py: DELETE /notes/1 is a 405; repro: client.delete('/notes/1')",
    )
    assert files == ("web/routes.py", "web/forms.py")
    assert upstream_defect_context({"gate_triage": []}, (routes,)) == ((), ())
    manifest = build_manifest(
        request="r",
        thread_id="t",
        generated_at="now",
        passed=True,
        phase="done",
        artifacts=(),
        gates=(),
    ).model_copy(update={"upstream_defects": upstream_defects(state)})
    md = render_manifest_md(manifest)
    assert "## Upstream defects" in md and "- task/web: web/routes.py: DELETE" in md


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


def test_a_happy_path_scenario_with_misuse_words_is_not_flagged() -> None:
    api = _ticket("api", entrypoint=True, scenario_ids=("AC-1", "AC-2", "AC-3", "AC-4"))
    criteria = [
        _criterion(
            "AC-1",
            "api",
            given=("an empty list",),
            when=('I add "milk"',),
            then=("the list has 1 item",),
        ),
        _criterion("AC-2", "api", when=("I add a duplicate item",), then=("the list has 2 items",)),
        _criterion("AC-3", "api", when=("I search an unknown tag",), then=("the result is empty",)),
        # Bad input in Given only: the When/Then never show it refused.
        _criterion(
            "AC-4",
            "api",
            given=("an unauthorized visitor",),
            when=("they open the home page",),
            then=("the page shows the title",),
        ),
    ]
    out, flagged, [note] = _ensure_security_scenario([api], criteria, _SEQ)
    assert not any(c.security_abuse for c in flagged[:4])
    assert flagged[-1].id.startswith("AC-ABUSE-") and flagged[-1].security_abuse
    assert flagged[-1].id in out[0].scenario_ids and "Added security scenario" in note


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


def test_the_scaffold_pins_no_dev_tools_but_the_test_libraries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.graph.gate_wiring.materialize as materialize

    monkeypatch.setattr(materialize, "_pin", lambda d: d)
    (tmp_path / "app.py").write_text("import json\nimport mypy\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import pytest\nimport xdist\nimport ruff\n")
    (tmp_path / "conftest.py").write_text("import pytest_bdd\nimport pytest_cov\n")
    # The shipped tests import pytest and pytest-bdd: without them they cannot run.
    assert _python_third_party_requirements(tmp_path) == ["pytest", "pytest-bdd"]


def test_the_scaffold_keeps_a_test_helper_the_harness_does_not_provide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.graph.gate_wiring.materialize as materialize

    # The build/test venv installs from this file: a helper only tests import still
    # has to be listed, or the suite dies on ModuleNotFoundError.
    (tmp_path / "app.py").write_text("import yaml\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import pytest\nimport freezegun\n")
    monkeypatch.setattr(
        materialize._im,
        "packages_distributions",
        lambda: {"yaml": ["PyYAML", "types-PyYAML"], "pytest": ["pytest"]},
    )
    monkeypatch.setattr(materialize, "_pin", lambda d: d)
    # A types-* stub distribution is the type checker's, not the app's.
    assert _python_third_party_requirements(tmp_path) == ["PyYAML", "freezegun", "pytest"]


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

    assert seen == [
        (False, "medium", None),
        (True, "medium", 32_000),
        (False, "medium", _REPAIR_MAX_TOKENS),
    ]
    assert len(tx.requests) == 2  # the delta never reached the transport, nor was re-rolled
    assert counts.snapshot()["planner.delta.rejected"] == 1


def test_the_sqlite_constraints_default_to_a_file_database() -> None:
    from foundry.agents.planner.constraints import _stack_constraints

    pcs = _stack_constraints(
        StackDecision(language="python", framework="SQLAlchemy over SQLite"), {}
    )
    memory, alchemy = (
        next(pc["text"] for pc in pcs if pc["id"] == i)
        for i in ("PC-python-sqlite-memory", "PC-python-sqlalchemy-sqlite")
    )
    assert "a FILE whose path comes from an environment variable" in memory
    assert "one per thread" in memory
    assert "sqlite:///app.db" in alchemy


_WEB = StackDecision(language="python", framework="flask", kind="http")


def test_a_web_app_contract_holding_one_db_connection_is_a_finding() -> None:
    # The notes bench: a service built once with one sqlite3 connection in the app
    # factory failed 3 of 30 concurrent POSTs (InterfaceError, 'no transaction active').
    from foundry.agents.planner.checks import _held_connection_findings

    service = _iface(
        "NoteService",
        "class",
        "class NoteService:\n"
        "    def __init__(self, conn: sqlite3.Connection) -> None: ...\n"
        "    def add(self, title: str) -> int: ...",
    )
    factory = _iface("create_app", "function", "def create_app(conn: sqlite3.Connection) -> Flask")
    record = _iface("Deps", "type", "@dataclass\nclass Deps:\n    db: Optional[psycopg.Connection]")
    java = _iface("Repo", "class", "class Repo {\n  Repo(Connection conn) {}\n}")
    entry = _iface("main", "function", "func main(conn *sql.Conn)")
    tickets = [_ticket("app", entrypoint=True, implements=("main",))]
    found = _held_connection_findings(_WEB, tickets, [service, factory, record, java, entry])
    assert [f.split()[1] for f in found] == ["NoteService", "create_app", "Deps", "Repo", "main"]
    assert "`conn: sqlite3.Connection`" in found[0] and "one connection per request" in found[0]
    assert {finding_kind(f) for f in found} == {"db-shared-connection"}
    # Not a web app: a CLI holds its one connection for the process, which is fine.
    cli = StackDecision(language="python", kind="cli")
    assert _held_connection_findings(cli, tickets, [service, factory]) == ()


def test_a_per_call_connection_or_a_connection_factory_is_no_finding() -> None:
    from foundry.agents.planner.checks import _held_connection_findings

    fine = [
        # A function taking the request's connection per call.
        _iface("add_note", "function", "def add_note(conn: sqlite3.Connection, t: str) -> int"),
        # A factory, a path or a pool held for the app's life.
        _iface(
            "Store",
            "class",
            "class Store:\n"
            "    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None: ...\n"
            "    def get(self, conn: sqlite3.Connection, id: int) -> str: ...",
        ),
        _iface("make_app", "function", "def make_app(db_path: str) -> Flask"),
        _iface("Server", "class", "type Server struct {\n\tdb *sql.DB\n}"),
        _iface("get_db", "function", "def get_db() -> sqlite3.Connection"),
        _iface("Client", "class", "class Client:\n    def __init__(self, c: HTTPConnection): ..."),
    ]
    assert _held_connection_findings(_WEB, [], fine) == ()


def _form_plan() -> tuple[list[Ticket], StackDecision, list[InterfaceContract]]:
    tickets = [
        _ticket(
            "notes", files=("notes/routes.py", "templates/note.html"), implements=("delete_note",)
        ),
        _ticket("app", "notes", files=("app.py",), entrypoint=True, description="Wire it."),
    ]
    web = StackDecision(
        language="python",
        framework="flask",
        kind="http",
        interface=("GET /notes", "POST /notes", "PUT /notes/<int:id>"),
    )
    delete = _iface("delete_note", "http", "DELETE /notes/{id} -> 302")
    return tickets, web, [delete]


def test_a_server_rendered_app_gets_post_routes_for_its_form_actions() -> None:
    # The notes bench: PUT/DELETE routes behind HTML forms, so browser edit and delete
    # returned 405.
    from foundry.agents.planner.folds import FORM_METHODS_NOTE, _form_method_routes

    tickets, web, ifaces = _form_plan()
    out, concerns = _form_method_routes(tickets, web, ifaces, ui=False)
    by_id = {t.id: t for t in out}
    # The http contract's implementer owns DELETE; the entrypoint owns the stack's PUT.
    assert "POST /notes/{id}/delete for DELETE /notes/{id}" in by_id["notes"].description
    assert "PUT" not in by_id["notes"].description
    assert "POST /notes/<int:id> for PUT /notes/<int:id>" in by_id["app"].description
    assert by_id["app"].description.startswith("Wire it.")
    assert len(concerns) == 2
    # Idempotent: a second pass adds nothing.
    assert _form_method_routes(out, web, ifaces, ui=False) == (out, [])
    assert all(FORM_METHODS_NOTE in t.description for t in out)


def test_form_routes_are_left_alone_with_a_post_alias_or_a_js_client_or_no_ui() -> None:
    from foundry.agents.planner.folds import _form_method_routes

    tickets, web, ifaces = _form_plan()
    aliased = web.model_copy(
        update={"interface": (*web.interface, "POST /notes/<id>", "POST /notes/<id>/delete")}
    )
    assert _form_method_routes(tickets, aliased, ifaces, ui=False)[1] == []
    client = [*tickets, _ticket("ui", files=("static/app.js",))]
    assert _form_method_routes(client, web, ifaces, ui=True)[1] == []
    htmx = web.model_copy(update={"framework": "flask + htmx"})
    assert _form_method_routes(tickets, htmx, ifaces, ui=False)[1] == []
    # A JSON API with no templates and no ui capability: no forms at all.
    api = [t.model_copy(update={"files": ("notes/routes.py",)}) for t in tickets]
    assert _form_method_routes(api, web, ifaces, ui=False)[1] == []
    # ...unless the PRD declares a ui capability.
    assert len(_form_method_routes(api, web, ifaces, ui=True)[1]) == 2
    assert _form_method_routes(tickets, None, ifaces, ui=True)[1] == []


def test_a_server_rendered_form_route_gets_a_page_and_the_site_a_root() -> None:
    # The notes review: POST /new and POST /<id>/edit with no GET rendering either
    # form, and no GET /.
    from foundry.agents.planner.folds import FORM_PAGES_NOTE, _form_page_routes

    tickets, web, _ = _form_plan()
    web = web.model_copy(
        update={
            "interface": (
                "GET /notes",
                "POST /notes",
                "POST /new",
                "POST /<id>/edit",
                "GET /notes/<id>",
                "POST /notes/<id>/tags",
                "POST /notes/<id>/delete",
                "POST /api/notes",
            )
        }
    )
    edit = _iface("edit_note", "http", "POST /<id>/edit -> 302")
    notes = tickets[0].model_copy(update={"implements": ("edit_note",)})
    out, concerns = _form_page_routes([notes, tickets[1]], web, [edit], ui=False)
    by_id = {t.id: t for t in out}
    assert "GET /<id>/edit rendering the form that submits to POST /<id>/edit" in (
        by_id["notes"].description
    )
    app = by_id["app"].description
    assert app.startswith("Wire it.") and FORM_PAGES_NOTE in app
    assert "GET /new rendering the form that submits to POST /new" in app
    assert "GET / (the home page, or a redirect to it)" in app
    # Its own page, the page above it, a button action and a JSON route need none.
    for satisfied in ("POST /notes ", "POST /notes/<id>/tags", "/delete", "/api/"):
        assert satisfied not in app + by_id["notes"].description
    assert len(concerns) == 2
    assert _form_page_routes(out, web, [edit], ui=False) == (out, [])
    # Every page served: nothing to note; nor without a server-rendered UI.
    served = web.model_copy(
        update={
            "interface": ("GET /", "GET /new", "POST /new", "GET /<id>/edit", "POST /<id>/edit")
        }
    )
    assert _form_page_routes(tickets, served, [], ui=False) == (tickets, [])
    api = [t.model_copy(update={"files": ("notes/routes.py",)}) for t in tickets]
    assert _form_page_routes(api, web, [], ui=False) == (api, [])


def test_a_step_that_is_a_json_fragment_or_a_placeholder_is_a_finding() -> None:
    # The first planner call emitted `...'Hello world'],` then
    # `criticaality_placeholder_ignore_me_note_2:` as steps; both shipped.
    from foundry.agents.planner.checks import _garbled_step_findings, finding_kind

    def crit(cid: str, *then: str) -> AcceptanceCriterion:
        return _criterion(cid, "T1", then=then)

    bad = crit(
        "AC01",
        "the tags are ['greeting', 'Hello world'],",
        "criticaality_placeholder_ignore_me_note_2:",
        "the list is [1, 2",
        "the title is TODO placeholder",
    )
    (finding,) = _garbled_step_findings([bad])
    assert finding_kind(finding) == "scenario-garbled-step"
    assert all(repr(s) in finding for s in bad.then)
    fine = crit(
        "AC02",
        "the list is [1, 2]",
        'the output is "a, b"',
        "the search box shows the placeholder 'Search notes'",
        "the page says: Saved",
    )
    assert _garbled_step_findings([fine]) == ()


def test_the_sop_says_html_forms_send_only_get_and_post() -> None:
    from foundry.agents.planner.schema import SOP

    assert "HTML forms send only GET and POST" in SOP
