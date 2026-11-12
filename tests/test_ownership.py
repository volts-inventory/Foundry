"""The Build gate's file-ownership check (``foundry.gates.ownership``)."""

from __future__ import annotations

from pathlib import Path

from foundry.durability.artifact_store import ArtifactStore
from foundry.gates.build import BuildGate
from foundry.gates.ownership import UNOWNED_FAILED, unowned_files
from foundry.graph.gate_wiring import FixPolicy, owned_paths
from foundry.schemas.gates import GateStatus
from tests.test_ticket_scheduler import _Recorder, _state, _ticket, _wave_run

# The cloud dice layout: a package with its marker and entry module, tests with
# their package marker, the smoke script, the contracts file, the gate's features.
_DICE_TICKETS = (
    "contracts.py",
    "dice/parser.py",
    "dice/roller.py",
    "dice/cli.py",
    "smoke.py",
    "tests/test_parser.py",
    "tests/test_roller.py",
    "tests/test_cli.py",
)
_DICE_TREE = (
    *_DICE_TICKETS,
    "dice/__init__.py",
    "dice/__main__.py",
    "tests/__init__.py",
    "features/cli.feature",
    "requirements.txt",
    "BUILD-MANIFEST.md",
)


def _tree(root: Path, files: tuple[str, ...]) -> None:
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X = 1\n" if rel.endswith(".py") else "x\n", encoding="utf-8")


def test_a_planned_tree_with_generated_markers_passes(tmp_path: Path) -> None:
    _tree(tmp_path, (*_DICE_TREE, "conftest.py", "features/steps/test_cli_steps.py"))
    gate = BuildGate(owned=frozenset(_DICE_TICKETS))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.PASS, obs.findings
    assert "unowned" not in obs.structured


def test_a_module_no_ticket_owns_fails_the_build(tmp_path: Path) -> None:
    # The local dice run: a fixer wrote parser.py / roller.py / cli.py and top-level
    # tests beside the planned dice_parser.py, dice_roller.py, main.py and tests/.
    planned = ("contracts.py", "dice_parser.py", "dice_roller.py", "main.py", "tests/test_cli.py")
    stray = ("cli.py", "parser.py", "roller.py", "test_cli.py", "pkg/__init__.py")
    _tree(tmp_path, (*planned, *stray, "tests/__init__.py", "smoke.py"))
    gate = BuildGate(owned=frozenset(planned))
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert sorted(obs.structured["unowned"]) == sorted(stray)
    assert obs.findings[0] == UNOWNED_FAILED
    assert obs.findings[1] == (
        "cli.py:1: not owned by any ticket: delete it or move its code into the "
        "owning ticket's file"
    )
    # No plan: nothing to judge against.
    assert BuildGate().decide(BuildGate().execute(tmp_path)) is GateStatus.PASS


def test_a_directory_entry_and_the_base_tree_own_their_files(tmp_path: Path) -> None:
    _tree(tmp_path, ("app/routes.py", "app/models/user.py", "legacy/old.py", "web/vite.config.ts"))
    assert unowned_files(tmp_path, {"app/", "legacy/old.py"}) == []
    assert unowned_files(tmp_path, {"app/routes.py"}) == ["app/models/user.py", "legacy/old.py"]


def test_owned_paths_reads_the_live_tickets() -> None:
    store = ArtifactStore()
    assert owned_paths(_state(store, []), store.resolve) is None
    tickets = [
        _ticket("T-001", 0, files=("./dice/cli.py", "tests/test_cli.py")),
        _ticket("T-002", 1, files=("dice/roller.py",)),
    ]
    assert owned_paths(_state(store, tickets), store.resolve) == {
        "dice/cli.py",
        "tests/test_cli.py",
        "dice/roller.py",
    }


def test_a_fixers_stray_module_fails_the_wave_check_and_reaches_the_next_pass(
    tmp_path: Path,
) -> None:
    rec = _Recorder(
        writes={
            "T-001": {"app.py": "def broken(:\n"},
            "fix-1": {"app.py": "VALUE = 1\n", "parser.py": "X = 1\n"},
        }
    )
    _wave_run(tmp_path, rec, fix_policy=FixPolicy(restarts=False))
    fix1 = rec.packets["fix-1"].task
    assert fix1["owned_files"] == ["app.py"]
    assert any("never create a new module" in r for r in fix1["rules"])
    assert (
        "parser.py:1: not owned by any ticket: delete it or move its code into the "
        "owning ticket's file"
    ) in rec.packets["fix-2"].prior_failures
