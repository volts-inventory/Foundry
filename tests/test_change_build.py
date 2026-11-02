"""Change an existing build (``run(base_dir=...)`` / ``--base DIR``).

The planner sees the base tree and plans only the delta; existing contracts count
as implemented; the gates run on base + delta (the base's own tests and scenarios
included); the output is the full updated tree with a manifest naming its base.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.agents.planner import make_planner_node
from foundry.cli import main, run
from foundry.durability.artifact_store import ArtifactStore
from foundry.graph.gate_wiring import MaterializationError, materialize_workdir
from foundry.schemas.gates import GateStatus
from tests.test_planner import PLAN, _plan, _prompt, config_for, runtime_for

_BASE_FEATURE = (
    "Feature: T-002\n\n  Scenario: AC-001\n    Given a user\n    When they add a todo\n"
    "    Then it appears\n"
)


def _base(root: Path, *, test_body: str = "assert double(2) == 4") -> Path:
    base = root / "base"
    (base / "todo").mkdir(parents=True)
    (base / "todo" / "__init__.py").write_text("")
    (base / "todo" / "store.py").write_text("def add(t):\n    return 1\n")
    (base / "lib.py").write_text("def double(x):\n    return 2 * x\n")
    (base / "test_lib.py").write_text(
        f"from lib import double\n\n\ndef test_double():\n    {test_body}\n"
    )
    (base / "BUILD-MANIFEST.json").write_text(json.dumps({
        "thread_id": "base-run",
        "request": "build a todo app",
        "contracts": [
            {"name": "TodoStore.add", "kind": "function", "signature": ""},
            {"name": "TodoStore.list", "kind": "function", "signature": ""},
        ],
        "contracts_files": ["todo/store.py"],
    }))
    return base


def _with_feature(base: Path) -> Path:
    (base / "features").mkdir()
    (base / "features" / "T-002.feature").write_text(_BASE_FEATURE)
    return base


# ---- planner: change mode -------------------------------------------------------

_CHANGE_PLAN: dict[str, Any] = _plan(
    interfaces=[{"name": "CsvExport.export", "spec": "export(todos) -> str",
                 "consumers": ["cli"]}],
    tickets=[
        {
            "id": "T-002", "title": "CSV export", "specialty": "backend",
            "files": ["todo/csv_export.py"], "implements": ["CsvExport.export"],
            "consumes": ["TodoStore.list"],
            "scenarios": [{"id": "AC-010", "given": ["two todos"], "when": ["exported"],
                           "then": ["two csv rows"], "covers": ["MH-1", "SC-1"]}],
        },
        {
            "id": "T-003", "title": "CSV steps", "specialty": "test", "depends_on": ["T-002"],
            "files": ["tests/step_defs/test_csv.py"], "scenarios": [],
        },
    ],
)


def _critique(cas: ArtifactStore, writes: dict[str, Any]) -> Any:
    (handle,) = [a for a in writes["artifacts"] if a.kind == "criteria_findings"]
    return cas.resolve(handle)


def _tickets(cas: ArtifactStore, writes: dict[str, Any]) -> list[Any]:
    return [cas.resolve(a) for a in writes["artifacts"] if a.kind == "ticket"]


def test_change_prompt_carries_the_base_tree_view_and_the_request(tmp_path: Path) -> None:
    base = _with_feature(_base(tmp_path))
    rt, tx = runtime_for(_CHANGE_PLAN)
    make_planner_node(rt)({"request": "add CSV export", "base_dir": str(base)}, config_for("c"))

    prompt = _prompt(tx)
    assert "add CSV export" in prompt
    assert "CHANGE MODE" in prompt
    for needle in ("todo/store.py", "lib.py", "TodoStore.add", "Scenario: AC-001",
                   "build a todo app"):
        assert needle in prompt
    assert "BUILD-MANIFEST" not in prompt  # the base's run report is not project source


def test_existing_contracts_count_as_implemented_only_in_change_mode(tmp_path: Path) -> None:
    base = _with_feature(_base(tmp_path))
    cas = ArtifactStore()
    rt, _ = runtime_for(_CHANGE_PLAN, cas=cas)
    writes = make_planner_node(rt)(
        {"request": "add CSV export", "base_dir": str(base)}, config_for("c")
    )
    critique = _critique(cas, writes)
    assert critique.contract_findings == ()
    assert critique.coverage_findings == ()  # no entrypoint ticket needed for a change
    tickets = {t.id: t for t in _tickets(cas, writes)}
    # The base's feature file name is reserved: the change never replaces it.
    assert "T-002" not in tickets and "T-002-2" in tickets
    assert tickets["T-002-2"].consumes == ("TodoStore.list",)
    # Untouched existing files (the package marker) are not handed to a ticket.
    assert not any("todo/__init__.py" in t.files for t in tickets.values())

    cas2 = ArtifactStore()
    rt2, _ = runtime_for(_CHANGE_PLAN, cas=cas2)
    fresh = make_planner_node(rt2)({"request": "add CSV export"}, config_for("n"))
    # Without the base the name is unknown: the consumes is dropped as unknown.
    assert {t.id: t for t in _tickets(cas2, fresh)}["T-002"].consumes == ()


def test_a_change_may_not_redeclare_a_base_contract(tmp_path: Path) -> None:
    base = _base(tmp_path)
    cas = ArtifactStore()
    plan = {**_CHANGE_PLAN, "interfaces": [*PLAN["interfaces"], *_CHANGE_PLAN["interfaces"]]}
    rt, _ = runtime_for(plan, cas=cas)
    writes = make_planner_node(rt)(
        {"request": "add CSV export", "base_dir": str(base)}, config_for("c")
    )
    findings = _critique(cas, writes).contract_findings
    assert any("TodoStore.add already exists in the base tree" in f for f in findings)


# ---- materialization: base + delta ------------------------------------------------


def test_materialized_tree_starts_from_the_base_and_keeps_its_features(tmp_path: Path) -> None:
    base = _with_feature(_base(tmp_path))
    dest = tmp_path / "work"
    materialize_workdir(
        {"base_dir": str(base)}, dest, resolve=lambda a: None, resolve_blob=lambda u: b""
    )
    assert (dest / "lib.py").is_file() and (dest / "todo" / "store.py").is_file()
    assert (dest / "features" / "T-002.feature").read_text() == _BASE_FEATURE
    assert not (dest / "BUILD-MANIFEST.json").exists()


def test_a_planned_feature_may_not_replace_a_base_feature() -> None:
    from foundry.graph.gate_wiring import _with_base_features

    with pytest.raises(MaterializationError, match=r"T-002\.feature"):
        _with_base_features({"features/T-002.feature": "x"}, {"features/T-002.feature": "y"})


# ---- end to end (mock backend) -------------------------------------------------------


def test_delta_build_writes_base_plus_new_files_and_names_its_base(tmp_path: Path) -> None:
    base = _base(tmp_path)
    out = tmp_path / "out"
    result = run("add CSV export", base_dir=str(base), output_dir=str(out))

    assert result.passed is True
    assert result.thread_id.startswith("change-")  # never the base build's thread
    for rel in ("lib.py", "test_lib.py", "todo/store.py", "app.py", "test_app.py"):
        assert (out / rel).is_file(), rel
    manifest = json.loads((out / "BUILD-MANIFEST.json").read_text())
    assert manifest["base"]["thread_id"] == "base-run"
    assert manifest["base"]["files"] == 4
    names = {c["name"] for c in manifest["contracts"]}
    assert {"TodoStore.add", "TodoStore.list"} <= names
    assert "Change on base" in (out / "BUILD-MANIFEST.md").read_text()


def test_existing_tests_still_run_in_the_test_gate(tmp_path: Path) -> None:
    base = _base(tmp_path, test_body="assert double(2) == 5")
    result = run("add CSV export", base_dir=str(base), max_fix_attempts=0)
    assert result.passed is False
    assert result.gate_statuses.get("test") in (GateStatus.FAIL, GateStatus.ERROR)


def test_reusing_the_base_builds_thread_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="base build's own run"):
        run("add CSV export", base_dir=str(_base(tmp_path)), thread_id="base-run")


def test_main_base_flag(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["--base", str(_base(tmp_path)), "--out", str(out), "add CSV export"]) == 0
    assert (out / "lib.py").is_file() and (out / "app.py").is_file()


def test_base_scenarios_stay_in_force_at_the_test_gate(tmp_path: Path) -> None:
    # The base's feature has no step bindings here: the full Test gate must see it.
    base = _with_feature(_base(tmp_path))
    result = run("add CSV export", base_dir=str(base), max_fix_attempts=0)
    assert result.passed is False
    assert result.gate_statuses.get("test") in (GateStatus.FAIL, GateStatus.ERROR)
    assert any("AC-001" in f for f in result.gate_findings.get("test", ()))
