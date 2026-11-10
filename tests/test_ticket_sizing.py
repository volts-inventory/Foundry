"""Plans sized to the model that builds them.

Each ticket has a size ceiling (work files, scenarios) set by where its tier runs
(cloud or local, from the run's level table); a ticket over it is a Spec-gate
finding asking to split it. A ticket only one other ticket of its epic depends on
is folded into that ticket when the merge fits the ceiling. Tickets, and so their
feature files and step modules, are named for their domain.
"""

from __future__ import annotations

import json
from typing import Any

from foundry import telemetry
from foundry.agents import AgentRuntime
from foundry.agents.planner import EPIC_SOP, SOP, make_planner_node
from foundry.agents.planner.build import _build_tickets
from foundry.agents.planner.checks import _size_findings
from foundry.agents.planner.folds import (
    _merge_sole_dependents,
    _one_step_module_per_ticket,
    _own_scenario_bindings,
)
from foundry.agents.planner.sizing import (
    ALL_CLOUD,
    CLOUD_CEILING,
    LOCAL_CEILING,
    size_text,
    ticket_ceilings,
    work_files,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.implementation import (
    Difficulty,
    Specialty,
    StackDecision,
    Ticket,
    feature_path,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq
from tests.test_planner import KEY, KEY_ID, PLAN, _bodies, config_for

_SEQ = Seq("r", 1, 1, "planner", 0)
_PY = StackDecision(language="python", test="pytest", run="python3 smoke.py")
_LOCAL = ticket_ceilings({"T1", "T2", "T3"})


def _t(
    tid: str,
    files: tuple[str, ...],
    scenarios: tuple[str, ...] = (),
    deps: tuple[str, ...] = (),
    *,
    difficulty: Difficulty = Difficulty.STANDARD,
    specialty: Specialty = Specialty.BACKEND,
    entry: bool = False,
    implements: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
) -> Ticket:
    return Ticket(
        id=tid,
        title=f"{tid} title",
        description=f"{tid} work.",
        specialty=specialty,
        difficulty=difficulty,
        files=files,
        scenario_ids=scenarios,
        depends_on=deps,
        entrypoint=entry,
        implements=implements,
        consumes=consumes,
        seq=_SEQ,
    )


def _ac(cid: str, tid: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid, text="t", ticket_id=tid, oracle_hint=scenario_ref(tid, cid), seq=_SEQ
    )


def _owners(tickets: list[Ticket], criteria: list[AcceptanceCriterion]) -> dict[str, str]:
    """Scenario id -> the one ticket carrying it; asserts it is exactly one and that
    the criterion record points at it."""
    held: dict[str, list[str]] = {}
    for t in tickets:
        for c in t.scenario_ids:
            held.setdefault(c, []).append(t.id)
    assert all(len(ids) == 1 for ids in held.values()), held
    owners = {c: ids[0] for c, ids in held.items()}
    assert {c.id: c.ticket_id for c in criteria} == owners
    return owners


# ---- the merge fold -----------------------------------------------------------------


def test_a_ticket_folds_into_its_only_dependent() -> None:
    stats = _t(
        "stats",
        ("csvstats/stats.py", "tests/test_stats.py"),
        ("AC1", "AC2"),
        ("contracts",),
        difficulty=Difficulty.COMPLEX,
        implements=("summarize",),
    )
    report = _t(
        "report",
        ("csvstats/report.py", "tests/test_report.py"),
        ("AC3",),
        ("contracts", "stats"),
        consumes=("summarize", "parse"),
    )
    cli = _t("cli", ("csvstats/cli.py",), ("AC4",), ("report",), entry=True)
    criteria = [_ac("AC1", "stats"), _ac("AC2", "stats"), _ac("AC3", "report"), _ac("AC4", "cli")]
    tickets, crit, concerns = _merge_sole_dependents([stats, report, cli], criteria, _PY, ALL_CLOUD)

    merged, entry = tickets
    assert merged.id == "report" and entry == cli.model_copy(update={"seq": entry.seq})
    assert merged.files == (
        "csvstats/report.py",
        "tests/test_report.py",
        "csvstats/stats.py",
        "tests/test_stats.py",
    )
    assert merged.scenario_ids == ("AC3", "AC1", "AC2")
    assert merged.depends_on == ("contracts",)
    assert merged.implements == ("summarize",) and merged.consumes == ("parse",)
    assert merged.difficulty is Difficulty.COMPLEX  # the harder one: its tier builds it
    assert "Also build what stats (stats title) planned: stats work." in merged.description
    assert _owners(tickets, crit) == {
        "AC1": "report",
        "AC2": "report",
        "AC3": "report",
        "AC4": "cli",
    }
    assert {c.oracle_hint for c in crit if c.id == "AC1"} == {"features/report.feature::AC1"}
    assert [t.seq.local for t in tickets] == [0, 1]
    assert concerns == [
        "Ticket stats: merged into report, the only ticket that depends on it (the merge "
        "stays within its size ceiling)."
    ]


def test_a_chain_folds_to_a_fixpoint_and_every_scenario_keeps_one_owner() -> None:
    tickets = [
        _t("parse", ("p.py",), ("A1",)),
        _t("stats", ("s.py",), ("A2",), ("parse",)),
        _t("report", ("r.py",), ("A3",), ("stats",)),
        _t("cli", ("c.py",), ("A4",), ("report",), entry=True),
    ]
    criteria = [_ac("A1", "parse"), _ac("A2", "stats"), _ac("A3", "report"), _ac("A4", "cli")]
    out, crit, _ = _merge_sole_dependents(tickets, criteria, _PY, ALL_CLOUD)
    assert [t.id for t in out] == ["report", "cli"]
    assert _owners(out, crit) == {"A1": "report", "A2": "report", "A3": "report", "A4": "cli"}


def test_the_entrypoint_is_never_a_merge_target_and_its_wired_edges_do_not_count() -> None:
    # Features only the entrypoint uses stay separate: wiring them in is its job.
    tickets = [
        _t("stats", ("s.py",), ("A1",)),
        _t("report", ("r.py",), ("A2",)),
        _t("cli", ("c.py",), ("A3",), ("stats", "report"), entry=True),
    ]
    assert _merge_sole_dependents(tickets, [], _PY, ALL_CLOUD)[0] == tickets
    # parse is used only by stats; _wire_entrypoint also made cli depend on it.
    # That edge is no dependent, and it follows parse into stats.
    wired = [
        _t("parse", ("p.py",), ("A0",)),
        _t("stats", ("s.py",), ("A1",), ("parse",)),
        _t("cli", ("c.py",), ("A3",), ("stats", "parse"), entry=True),
    ]
    out, _, _ = _merge_sole_dependents(wired, [], _PY, ALL_CLOUD, frozenset({("cli", "parse")}))
    assert [t.id for t in out] == ["stats", "cli"]
    assert out[1].depends_on == ("stats",)
    # Counted as a real edge, cli is a second dependent: nothing folds.
    assert _merge_sole_dependents(wired, [], _PY, ALL_CLOUD)[0] == wired


def test_no_fold_with_two_dependents_across_epics_or_over_the_ceiling() -> None:
    shared = _t("db", ("db.py",), ("A1",))
    two = [
        shared,
        _t("auth", ("auth.py",), ("A2",), ("db",)),
        _t("cart", ("c.py",), ("A3",), ("db",)),
    ]
    criteria = [_ac("A1", "db"), _ac("A2", "auth"), _ac("A3", "cart")]
    assert _merge_sole_dependents(two, criteria, _PY, ALL_CLOUD)[0] == two

    cross = [_t("E1-db", ("db.py",), ("A1",)), _t("E2-web", ("w.py",), ("A2",), ("E1-db",))]
    assert _merge_sole_dependents(cross, [], _PY, ALL_CLOUD)[0] == cross
    same = [_t("E1-db", ("db.py",), ("A1",)), _t("E1-web", ("w.py",), ("A2",), ("E1-db",))]
    assert [t.id for t in _merge_sole_dependents(same, [], _PY, ALL_CLOUD)[0]] == ["E1-web"]

    # Locally (4 files) the merge would write 5: both stay.
    big = [
        _t("models", ("m1.py", "m2.py", "m3.py"), ("A1",)),
        _t("routes", ("r1.py", "r2.py", "tests/test_routes.py"), ("A2",), ("models",)),
    ]
    assert _merge_sole_dependents(big, [], _PY, _LOCAL)[0] == big
    assert len(_merge_sole_dependents(big, [], _PY, ALL_CLOUD)[0]) == 1
    # Scenarios count too: 5 + 4 > 8 locally.
    many = [
        _t("a", ("a.py",), tuple(f"A{i}" for i in range(5))),
        _t("b", ("b.py",), tuple(f"B{i}" for i in range(4)), ("a",)),
    ]
    assert _merge_sole_dependents(many, [], _PY, _LOCAL)[0] == many


def test_the_entrypoint_test_and_acceptance_only_tickets_are_never_folded() -> None:
    entry = _t("app", ("app.py",), ("A1",), entry=True)
    after = _t("docs", ("docs.py",), ("A2",), ("app",))
    assert _merge_sole_dependents([entry, after], [], _PY, ALL_CLOUD)[0] == [entry, after]
    feature = _t("store", ("store.py",), ("A1",))
    steps = _t("steps", ("tests/test_steps.py",), (), ("store",), specialty=Specialty.TEST)
    assert _merge_sole_dependents([feature, steps], [], _PY, ALL_CLOUD)[0] == [feature, steps]
    spec = _t("spec", (), ("A2",), ("store",))  # acceptance-only
    assert _merge_sole_dependents([feature, spec], [], _PY, ALL_CLOUD)[0] == [feature, spec]


def test_a_merged_ticket_binds_its_scenarios_from_one_step_module() -> None:
    # The fold runs before the step-module folds: the merged ticket carries both
    # tickets' modules, keeps its own and drops the other.
    tickets = [
        _t("store", ("store.py", "tests/test_store.py"), ("A1",)),
        _t("api", ("api.py", "tests/test_api.py"), ("A2",), ("store",)),
    ]
    merged, _, _ = _merge_sole_dependents(tickets, [], _PY, ALL_CLOUD)
    bound, _ = _own_scenario_bindings(merged, _PY)
    [api], concerns = _one_step_module_per_ticket(bound, _PY)
    assert api.files == ("api.py", "tests/test_api.py", "store.py")
    assert api.scenario_ids == ("A2", "A1")
    assert "dropped test module(s) tests/test_store.py" in concerns[0]


def _runtime(*payloads: dict[str, Any], local: frozenset[str] = frozenset()) -> Any:
    tx = MockTransport(
        completions=[json.dumps(p) for p in payloads], default_text=json.dumps(payloads[-1])
    )
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID), local_tiers=local)
    cas = ArtifactStore()
    return AgentRuntime(proxy=proxy, cas=cas), tx, cas


def test_the_planner_folds_only_a_private_hand_off() -> None:
    # PLAN's store is used only by the entrypoint: it stays its own ticket.
    rt, _, cas = _runtime(PLAN)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert "planner.fold.merge_sole_dependents" not in counts.snapshot()
    assert {t.id for t in _bodies(cas, writes, "ticket")} == {"T-001", "T-002", "T-003"}
    # A validation ticket only the store uses folds into it, and is counted.
    plan = json.loads(json.dumps(PLAN))
    store = plan["tickets"][0]
    store["depends_on"] = [*store["depends_on"], "T-004"]
    plan["tickets"].append(
        {
            **store,
            "id": "T-004",
            "title": "Validate todos",
            "depends_on": [],
            "files": ["todo/validate.py"],
            "implements": [],
            "scenarios": [{**store["scenarios"][0], "id": "AC-004"}],
        }
    )
    rt, _, cas = _runtime(plan)
    with telemetry.counting() as counts:
        writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert counts.snapshot()["planner.fold.merge_sole_dependents"] == 1
    tickets = {t.id: t for t in _bodies(cas, writes, "ticket")}
    assert set(tickets) == {"T-001", "T-002", "T-003"}
    assert tickets["T-001"].scenario_ids == ("AC-003", "AC-004")


# ---- the ceiling ----------------------------------------------------------------------


def test_work_files_leave_out_tests_markers_stack_files_and_manifests() -> None:
    files = (
        "shop/__init__.py",
        "shop/app.py",
        "templates/cart.html",
        "smoke.py",
        "requirements.txt",
        "pyproject.toml",
        "tests/test_web.py",
        "tests/conftest.py",
        "src/cart.test.ts",
        "features/step_definitions/cart.js",
    )
    assert work_files(files, {"smoke.py"}) == ("shop/app.py", "templates/cart.html")


def test_a_ticket_over_its_tiers_ceiling_is_a_finding_to_split_it() -> None:
    six = tuple(f"app/m{i}.py" for i in range(6))
    tickets = [
        _t("models", six, ("A1",)),
        _t("api", six, ("A2",), difficulty=Difficulty.COMPLEX),
        _t("contracts", six),  # generated: never checked
    ]
    hybrid = ticket_ceilings({"T2", "T3"})
    assert _size_findings(tickets, hybrid) == (
        "Ticket models is over its size ceiling: 6 files and 1 scenarios, where a "
        "standard ticket (built on a local model) takes at most 4 files and 8 scenarios: "
        "split it into tickets each within the ceiling.",
    )
    assert _size_findings(tickets, ALL_CLOUD) == ()
    scenarios = tuple(f"A{i}" for i in range(13))
    assert len(_size_findings([_t("cli", ("cli.py",), scenarios)], ALL_CLOUD)) == 1


def test_the_ceiling_follows_where_each_difficultys_tier_runs() -> None:
    assert ticket_ceilings(()) == ALL_CLOUD  # cloud mode builds no level table
    hybrid = ticket_ceilings({"T2", "T3"})
    assert hybrid[Difficulty.COMPLEX] == CLOUD_CEILING
    assert hybrid[Difficulty.STANDARD] == hybrid[Difficulty.TRIVIAL] == LOCAL_CEILING
    assert set(_LOCAL.values()) == {LOCAL_CEILING}
    # The run's difficulty -> tier table moves a difficulty onto another level.
    moved = ticket_ceilings({"T2", "T3"}, {Difficulty.STANDARD: "T1"})
    assert moved[Difficulty.STANDARD] == CLOUD_CEILING
    text = size_text(hybrid)
    assert "complex tickets run on a cloud model: at most 12 files and 12 scenarios" in text
    assert "trivial / standard tickets run on a local model: at most 4 files and 8" in text


def _oversized() -> dict[str, Any]:
    plan = json.loads(json.dumps(PLAN))
    plan["tickets"][0]["files"] = [*plan["tickets"][0]["files"], "todo/a.py", "todo/b.py"]
    plan["tickets"][0]["files"] += ["todo/c.py", "todo/d.py"]  # store.py + 4 = 5 work files
    # Both local in hybrid: folding T-001 into T-002 would only grow it (a cloud
    # T-002 would take it in).
    plan["tickets"][1]["difficulty"] = "standard"
    return plan


def _size_of(writes: dict[str, Any], cas: ArtifactStore) -> tuple[str, ...]:
    [critique] = _bodies(cas, writes, "criteria_findings")
    return tuple(f for f in critique.coverage_findings if "size ceiling" in f)


def test_the_planner_sizes_tickets_by_the_proxys_local_tiers() -> None:
    rt, tx, cas = _runtime(_oversized(), local=frozenset({"T2", "T3"}))  # hybrid
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert "at most 4 files and 8 scenarios" in json.dumps(tx.requests[0])
    assert _size_of(writes, cas) == (
        "Ticket T-001 is over its size ceiling: 5 files and 1 scenarios, where a "
        "standard ticket (built on a local model) takes at most 4 files and 8 scenarios: "
        "split it into tickets each within the ceiling.",
    )
    rt, tx, cas = _runtime(_oversized())  # cloud: no level table
    writes = make_planner_node(rt)({"request": "x"}, config_for("r"))
    assert "at most 12 files and 12 scenarios" in json.dumps(tx.requests[0])
    assert _size_of(writes, cas) == ()


# ---- naming ----------------------------------------------------------------------------


def test_the_prompts_size_tickets_and_name_them_for_their_domain() -> None:
    assert "FEWER, LARGER" not in SOP
    assert "Split the work as finely as it needs" in SOP
    assert "Never split off work that only one other ticket uses" in SOP
    assert "never after a ticket number (T3, T3.feature, test_T2.py)" in SOP
    assert "name each ticket after its domain (catalog, cart-api; never T01)" in EPIC_SOP
    assert "number tickets T01" not in EPIC_SOP


def test_a_ticket_without_an_id_is_named_for_its_title() -> None:
    raw = [
        {"title": "Summary statistics", "specialty": "data", "files": ["stats.py"]},
        {"title": "The CLI", "specialty": "backend", "files": ["cli.py"]},
        {"id": "cli", "title": "Other", "specialty": "backend", "files": ["x.py"]},
        {"title": "", "specialty": "backend", "files": ["y.py"]},
    ]
    tickets, _, _ = _build_tickets(raw, _SEQ, _SEQ, set())
    ids = [t.id for t in tickets]
    # "The CLI" would take the id another ticket has: it keeps the numbered default.
    assert ids == ["summary-statistics", "T-002", "cli", "T-004"]
    assert feature_path(ids[0]) == "features/summary-statistics.feature"
