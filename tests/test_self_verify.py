"""Round-2 trial failures, reproduced with the recording adapter (no real CLI):

* agents never ran the stack's checks on their own work (self-verify packets);
* a later ticket redeclared the contract types in its own module (``models.Category``
  beside ``contracts.Category``) and every caller misfit;
* a contract signature named a method ``list`` and broke its own ``-> list[...]``;
* the per-wave check ran ``npm install`` before the ticket that writes
  ``package.json`` had been built;
* the per-boundary fix budget resets could, in principle, add up without bound.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig

from foundry import cli
from foundry.agents.implementation.junior_engineer import _sandbox_for
from foundry.agents.implementation.personas import persona_for
from foundry.agents.implementation.tickets import (
    CONTRACT_DUP_TRIAGE,
    _flag_contract_duplicates,
    contract_duplicates,
    plan_wave_count,
)
from foundry.agents.planner import _builtin_method_findings
from foundry.execution.adapter_contract import AdapterResult
from foundry.execution.adapters.claude_code import ClaudeCodeAdapter, walk_worktree
from foundry.execution.sandbox import Sandbox
from foundry.graph.gate_wiring import (
    BUILD_BOUNDARY,
    FIX,
    HALT,
    SPEC_BOUNDARY,
    bump_fix_attempts,
    route_after_gates,
    unbuilt_manifests,
)
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import StackDecision
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from tests.test_ticket_scheduler import (
    _builds,
    _diffs,
    _Recorder,
    _runtime,
    _ticket,
    _wave_run,
)

_CONTRACTS = "class Category:\n    name: str\n\n\nclass Account:\n    id: int\n"


# ---- 1. self-verify packets ------------------------------------------------------


def test_ticket_and_fixer_packets_carry_the_stack_commands_to_run(tmp_path: Path) -> None:
    script = tmp_path / "typecheck.py"
    script.write_text("import sys; sys.exit(any('BROKEN' in open(p).read() "
                      "for p in sys.argv[1:] if __import__('os').path.exists(p)))")
    typecheck = f"{sys.executable} {script} contracts.py app.py"
    stack = StackDecision(language="python", build="true", typecheck=typecheck,
                          test="pytest -q")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("app.py",), deps=("contracts",)),
        _ticket("T-002", 2, files=("cli.py",), deps=("T-001",)),
    ]
    rec = _Recorder(writes={"contracts": {"contracts.py": _CONTRACTS},
                            "T-001": {"app.py": "BROKEN\n"},
                            "fix-1": {"app.py": "x = 1\n"}})
    out, ran, _ = _wave_run(tmp_path, rec, tickets=tickets, stack=stack)
    assert "fixer" in ran and set(_diffs(out)) >= {"contracts", "T-001", "T-002"}

    want = {"build": "true", "typecheck": typecheck, "test": "pytest -q"}
    contracts, t1, fix = rec.packets["contracts"], rec.packets["T-001"], rec.packets["fix-1"]
    for packet in (contracts, t1, fix):
        assert packet.task["verify"] == want
    rules = "\n".join(contracts.task["rules"])
    assert "VERIFY BEFORE YOU FINISH" in rules
    assert "DECLARATIONS ONLY in contracts.py" in rules
    assert "`python -m mypy contracts.py`" in rules  # the contract file on its own
    t1_rules = "\n".join(t1.task["rules"])
    assert "VERIFY BEFORE YOU FINISH" in t1_rules
    assert "CONTRACT TYPES ARE THE ONLY TYPES" in t1_rules and "contracts.py" in t1_rules
    assert "DECLARATIONS ONLY" not in t1_rules
    fix_rules = "\n".join(fix.task["rules"])
    assert "re-run the failing command" in fix_rules and "until they all pass" in fix_rules


def test_code_personas_tell_the_engineer_to_check_and_reuse_contract_types() -> None:
    for area in ("frontend", "backend", "data"):
        persona = persona_for(area)
        assert "never redeclare" in persona and "type checker" in persona


# ---- 2. contract types are the only types ----------------------------------------


def test_a_ticket_redeclaring_contract_types_is_flagged_and_sent_to_the_fixer(
    tmp_path: Path,
) -> None:
    # The epics trial: E1-T01 wrote finance/models.py with its own Category/Account,
    # the services mixed both, and mypy failed on every call. The stand-in type
    # checker fails exactly like that: on any copy of a contract class.
    script = tmp_path / "typecheck.py"
    script.write_text(
        "import pathlib, sys\n"
        "sys.exit(any('class Category' in p.read_text() for p in pathlib.Path('.')"
        ".rglob('*.py') if p.name != 'contracts.py'))\n"
    )
    stack = StackDecision(language="python", build="true",
                          typecheck=f"{sys.executable} {script}")
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",)),
        _ticket("T-001", 1, files=("models.py",), deps=("contracts",)),
        _ticket("T-002", 2, files=("app.py",), deps=("T-001",)),
    ]
    rec = _Recorder(writes={
        "contracts": {"contracts.py": _CONTRACTS},
        "T-001": {"models.py": "class Category:\n    name: str\n"},
        "fix-1": {"models.py": "from contracts import Category\n"},
    })
    out, ran, _ = _wave_run(tmp_path, rec, tickets=tickets, stack=stack)
    assert ran == ["engineering", "wave_gate", "engineering", "wave_gate", "fixer",
                   "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.PASS, GateStatus.FAIL, GateStatus.PASS]
    flagged = [t for t in out["gate_triage"] if t.name == CONTRACT_DUP_TRIAGE]
    assert [t.run_against for t in flagged] == ["task/T-001"]
    assert "models.py redeclares contract type(s) Category" in flagged[0].explanation
    failures = rec.packets["fix-1"].prior_failures
    assert f"[{CONTRACT_DUP_TRIAGE}]" in failures
    assert any("models.py redeclares contract type(s) Category" in f for f in failures)


@pytest.mark.parametrize(("files", "want"), [
    ({"models.py": "class Category:\n    pass\n"}, ["models.py: Category"]),
    ({"models.py": "from contracts import Category, Account\n"}, []),
    ({"tests/test_x.py": "def test():\n    class Category: ...\n"}, []),  # not top level
    ({"src/store.ts": "export interface Account { id: number }\n"}, ["src/store.ts: Account"]),
    ({"src/store.ts": "export type { Account } from './contracts';\n"}, []),
    ({"notes.md": "class Category\n"}, []),  # not source
])
def test_contract_duplicates_names_only_top_level_copies(
    files: dict[str, str], want: list[str]
) -> None:
    contract = {"contracts.py": _CONTRACTS, "src/contracts.ts":
                "export interface Account { id: number }\nexport type Category = string;\n"}
    found = contract_duplicates({**contract, **files}, tuple(contract))
    got = [f"{f.split(' ', 1)[0]}: {f.split('type(s) ', 1)[1].split(' ', 1)[0]}"
           for f in found]
    assert got == want


def test_the_contracts_ticket_itself_is_never_flagged(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path)
    try:
        (sandbox.worktree / "contracts.py").write_text(_CONTRACTS)
        blob = json.dumps({"kind": "full-file-snapshot",
                           "files": {"contracts.py": _CONTRACTS}}).encode()
        result = AdapterResult(diff_uri=sandbox.put_blob(blob),
                               snapshot_digest=sandbox.snapshot())
        same = _flag_contract_duplicates(result, sandbox, {"contracts.py": _CONTRACTS},
                                         "contracts")
        assert same.triage == ()
    finally:
        sandbox.destroy()


# ---- 3. contract signatures the type checker accepts -----------------------------


def _iface(signature: str, kind: str = "class") -> InterfaceContract:
    return InterfaceContract(name="BookRepository", spec_uri="cas://x", kind=kind,
                             signature=signature, seq=Seq("run", 1, 1, "planner", 0))


def test_a_method_named_after_a_builtin_its_annotations_use_is_a_contract_finding() -> None:
    # The api trial: `def list(...) -> list[BookOut]` — mypy: "Function
    # contracts.BookRepository.list is not valid as a type".
    api = _iface("class BookRepository(Protocol):\n"
                 "    def get(self, book_id: int) -> BookOut | None: ...\n"
                 "    def list(self, limit: int | None = None) -> list[BookOut]: ...")
    (finding,) = _builtin_method_findings([api], "python")
    assert "BookRepository" in finding and "list" in finding and "rename" in finding
    assert _builtin_method_findings([api], "typescript") == ()
    assert _builtin_method_findings([api.model_copy(update={"kind": "http"})], "python") == ()
    fine = _iface("class BookRepository(Protocol):\n"
                  "    def list_books(self) -> list[BookOut]: ...\n"
                  "    def id(self) -> int: ...")  # `id` is no type: no annotation breaks
    assert _builtin_method_findings([fine], "python") == ()


# ---- 4. the wave check before the package manifest exists -------------------------


def test_the_wave_check_skips_install_and_build_until_the_manifest_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The webapp trial: wave 0 built the contracts, the check ran `npm install`, and
    # package.json belonged to a later ticket — ENOENT, halt. Every step here fails
    # while package.json is missing, so only skipping them passes wave 0.
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")
    needs = "test -f package.json"
    stack = StackDecision(language="typescript", install=needs, build=needs,
                          typecheck=needs, test="true")
    tickets = [
        _ticket("contracts", 0, files=("src/contracts.ts",)),
        _ticket("T-setup", 1, files=("package.json",), deps=("contracts",)),
        _ticket("T-002", 2, files=("src/app.ts",), deps=("T-setup",)),
    ]
    rec = _Recorder(writes={"T-setup": {"package.json": "{}\n"}})
    out, ran, _ = _wave_run(tmp_path, rec, tickets=tickets, stack=stack)
    assert ran == ["engineering", "wave_gate", "engineering", "wave_gate", "engineering"]
    assert _builds(out) == [GateStatus.PASS, GateStatus.PASS]
    (note,) = [t for t in out["gate_triage"] if t.name == "wave-manifest-scope"]
    assert "package.json" in note.explanation and f"install `{needs}`" in note.explanation


def test_unbuilt_manifests(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    files = ["web/package.json", "./api/pyproject.toml", "go.mod", "src/app.ts"]
    assert unbuilt_manifests(files, tmp_path) == ("api/pyproject.toml", "go.mod")


# ---- 5. fix budget ----------------------------------------------------------------


def _cfg() -> RunnableConfig:
    return {"configurable": {"thread_id": "t"}}


def test_the_run_wide_fix_cap_halts_even_with_a_fresh_boundary_budget() -> None:
    def route(total: int) -> str:
        state: FactoryState = {"gates": [], "budget": BudgetLedger(fix_attempts_total=total)}
        return route_after_gates(state, _cfg(), boundary=BUILD_BOUNDARY,
                                 records=GlobalRecords(), max_fix_attempts=3, total_cap=9)

    assert route(8) == FIX
    assert route(9) == HALT


def test_build_side_fixes_count_toward_the_total_and_spec_replans_do_not() -> None:
    state: FactoryState = {"gates": [], "budget": BudgetLedger()}
    for boundary, total in ((SPEC_BOUNDARY, 0), (BUILD_BOUNDARY, 1)):
        out: dict[str, object] = {}
        bump_fix_attempts(state, _cfg(), out, boundary=boundary, records=GlobalRecords(),
                          max_fix_attempts=3, total_cap=9)
        ledger = out["budget"]
        assert isinstance(ledger, BudgetLedger)
        assert ledger.fix_attempts_since_all_green == 1
        assert ledger.fix_attempts_total == total


def test_plan_wave_count() -> None:
    tickets = [
        _ticket("contracts", 0, files=("c.py",)),
        _ticket("T-001", 1, files=("a.py",), deps=("contracts",)),
        _ticket("T-002", 2, files=("b.py",), deps=("contracts",)),
        _ticket("T-003", 3, files=("m.py",), deps=("T-001", "T-002")),
    ]
    assert plan_wave_count(tickets) == 3
    assert plan_wave_count([]) == 0


def test_the_default_fix_budget_is_three() -> None:
    assert inspect.signature(cli.run).parameters["max_fix_attempts"].default == 3


# ---- the agent sandbox can run the stack's commands --------------------------------


def test_claude_code_sandboxes_get_the_toolchain_proxy_and_ca_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example:3128")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ca.pem")
    monkeypatch.setenv("VIRTUAL_ENV", "/host/venv")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    rt = _runtime(ClaudeCodeAdapter(), store=None)  # type: ignore[arg-type]
    sandbox = _sandbox_for(rt, str(tmp_path), None)
    plain = Sandbox(tmp_path)
    try:
        env = sandbox.scrubbed_env()
        assert env["HTTPS_PROXY"] == "http://proxy.example:3128"  # userinfo stripped
        assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ca.pem"
        assert "VIRTUAL_ENV" not in env and "AWS_SECRET_ACCESS_KEY" not in env
        assert env["HOME"] != str(Path.home())
        assert "HTTPS_PROXY" not in plain.scrubbed_env()  # opt-in per adapter
    finally:
        sandbox.destroy()
        plain.destroy()


def test_toolchain_output_never_enters_the_authored_slice(tmp_path: Path) -> None:
    for rel in ("app.py", ".venv/lib/x.py", "venv/bin/pip", ".mypy_cache/a.json",
                "pkg/.pytest_cache/v", "web/node_modules/x/index.js", "pkg/__pycache__/a.pyc"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    assert list(walk_worktree(tmp_path)) == ["app.py"]
