"""The typed skeleton: deterministic stubs of the planned Python contracts.

Local models drift from the planned interface (renamed methods, a ProductModel
beside the contract's Product), and most compile errors are type errors. After
the contracts ticket builds, every module a later ticket owns that implements a
planned contract is written as a typed stub, type-checked by the wave-0 check, and
an implementation ticket that changes a stubbed public signature fails its attempt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from foundry.agents._common import emit_artifact, persist_prose
from foundry.agents.implementation.stubs import python_skeleton, signature_changes
from foundry.agents.implementation.tickets import (
    EmptyTicketError,
    _require_scope_written,
    skeleton_files,
    stub_rules,
)
from foundry.agents.planner import _safe_ticket_ids
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult
from foundry.execution.sandbox import Sandbox
from foundry.graph.gate_wiring import materialize_workdir
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import SKELETON_TASK_ID, Specialty, StackDecision, Ticket
from foundry.state.seq import Seq
from tests.test_ticket_scheduler import _builds, _diffs, _Recorder, _ticket, _wave_run

_SEQ = Seq("run", 1, 1, "planner", 0)

CONTRACTS_PY = """\
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Todo:
    title: str
    done: bool = False


class TodoStore(Protocol):
    def add(self, title: str) -> Todo: ...
    async def find(self, todo_id: int) -> Optional[Todo]: ...
    def recent(self, n: int = 5) -> list[Todo] | None: ...
"""


def _contract(name: str, kind: str, signature: str, local: int = 0) -> InterfaceContract:
    return InterfaceContract(
        name=name,
        spec_uri=f"cas://x/{name}",
        kind=kind,
        signature=signature,
        seq=_SEQ._replace(local=local),
    )


CONTRACTS = [
    _contract("Todo", "type", "@dataclass\nclass Todo:\n    title: str\n    done: bool = False"),
    _contract(
        "TodoStore",
        "class",
        "class TodoStore(Protocol):\n"
        "    def add(self, title: str) -> Todo\n"
        "    async def find(self, todo_id: int) -> Optional[Todo]",
    ),
    _contract("TodoStore.recent", "function", "def recent(self, n: int = 5) -> list[Todo] | None"),
    _contract(
        "make_app",
        "function",
        "def make_app(store: TodoStore, *, debug: bool = False) -> Callable[[str], str]",
    ),
    _contract("export_csv", "function", "async def export_csv(items: Sequence[Todo]) -> str"),
    _contract("legacy", "function", "def legacy(x) -> int"),
    _contract("serve", "function", "def serve(app: Flask) -> None"),
    _contract("listing", "function", "returns the todos, newest first"),
]


def _tk(tid: str, files: tuple[str, ...], implements: tuple[str, ...]) -> Ticket:
    return Ticket(
        id=tid, title=tid, specialty=Specialty.BACKEND, files=files, implements=implements, seq=_SEQ
    )


TICKETS = [
    _tk("contracts", ("app/contracts.py", "app/__init__.py"), tuple(c.name for c in CONTRACTS)),
    _tk(
        "T-001",
        ("app/store/sql.py", "app/store/__init__.py", "tests/test_store.py"),
        ("TodoStore", "TodoStore.recent"),
    ),
    _tk("T-002", ("app/web.py",), ("make_app", "export_csv", "legacy", "serve", "listing")),
]


def _mypy_strict(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(root / ".cache"),
            "app",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_stubs_carry_the_planned_signatures_and_pass_strict_mypy(tmp_path: Path) -> None:
    sk = python_skeleton(
        CONTRACTS,
        TICKETS,
        {"app/contracts.py": CONTRACTS_PY},
        exists=("app/contracts.py", "app/__init__.py"),
    )
    store = sk.files["app/store/sql.py"]
    # A class contract and its dotted method contract become one subclass of the
    # contract type: the type checker holds every override to it, no redeclaration.
    assert "from app.contracts import Todo, TodoStore" in store
    assert "class TodoStoreImpl(TodoStore):" in store
    assert "class Todo" not in store.replace("TodoStore", "")
    assert "def add(self, title: str) -> Todo:\n        raise NotImplementedError" in store
    assert "async def find(self, todo_id: int) -> Optional[Todo]:" in store
    assert "def recent(self, n: int=5) -> list[Todo] | None:" in store
    assert "from typing import Optional" in store
    web = sk.files["app/web.py"]
    assert "def make_app(store: TodoStore, *, debug: bool=False) -> Callable[[str], str]:" in web
    assert "async def export_csv(items: Sequence[Todo]) -> str:" in web
    assert "from collections.abc import Callable, Sequence" in web.replace(
        "Callable\nfrom collections.abc import Sequence", "Callable, Sequence"
    )
    # Not code, not fully annotated, or naming a third-party type: left to the ticket.
    assert "legacy" not in web and "serve" not in web
    concerns = "\n".join(sk.concerns)
    assert "legacy is not fully annotated" in concerns
    assert "serve uses Flask" in concerns
    assert "Contract listing: its signature is not Python code" in concerns
    # The planned package marker comes with its stub; the declaration-only dataclass
    # has no implementing module, and nothing in the contracts file is overwritten.
    assert sk.files["app/store/__init__.py"] == ""
    assert sk.owners == {
        "app/store/sql.py": "T-001",
        "app/store/__init__.py": "T-001",
        "app/web.py": "T-002",
    }
    assert sk.impls["TodoStore"] == ("TodoStoreImpl", "app/store/sql.py")

    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").write_text("")
    (tmp_path / "app/contracts.py").write_text(CONTRACTS_PY)
    for rel, text in sk.files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    done = _mypy_strict(tmp_path)
    assert done.returncode == 0, done.stdout


def test_a_stub_that_drifts_from_the_contracts_file_fails_mypy(tmp_path: Path) -> None:
    # The contracts ticket returned int where the plan says Todo: the stub's
    # override exposes it in wave 0, before any ticket codes against either.
    drifted = CONTRACTS_PY.replace(
        "def add(self, title: str) -> Todo", "def add(self, title: str) -> int"
    )
    sk = python_skeleton(
        CONTRACTS,
        TICKETS,
        {"app/contracts.py": drifted},
        exists=("app/contracts.py", "app/__init__.py"),
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").write_text("")
    (tmp_path / "app/contracts.py").write_text(drifted)
    for rel, text in sk.files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    done = _mypy_strict(tmp_path)
    assert done.returncode == 1 and '"add" incompatible with return type "int"' in done.stdout


def test_no_stub_for_prose_contracts_existing_files_or_an_ambiguous_module() -> None:
    prose = [
        _contract("TodoStore", "class", "a store of todos"),
        _contract("make_app", "function", "builds the app"),
    ]
    assert python_skeleton(prose, TICKETS, {"app/contracts.py": CONTRACTS_PY}).files == {}
    # A change build's module already exists: never clobbered.
    sk = python_skeleton(
        CONTRACTS,
        TICKETS,
        {"app/contracts.py": CONTRACTS_PY},
        exists=("app/web.py", "app/store/sql.py"),
    )
    assert sk.files == {}
    # Two modules, neither named after the function: the ticket decides.
    two = [_tk("T-002", ("app/routes.py", "app/main.py"), ("make_app",))]
    assert python_skeleton(CONTRACTS, two, {"app/contracts.py": CONTRACTS_PY}).files == {}
    named = [_tk("T-002", ("app/store.py", "app/main.py"), ("TodoStore",))]
    assert set(python_skeleton(CONTRACTS, named, {"app/contracts.py": CONTRACTS_PY}).files) == {
        "app/store.py"
    }


def test_signature_changes_name_renames_parameters_and_returns() -> None:
    stub = python_skeleton(CONTRACTS, TICKETS, {"app/contracts.py": CONTRACTS_PY}).files[
        "app/store/sql.py"
    ]
    filled = stub.replace("raise NotImplementedError", "return None  # body") + (
        "\n\ndef _helper() -> int:\n    return 1\n"
    )
    assert signature_changes(stub, filled) == ()
    renamed = stub.replace("def recent(", "def latest(")
    assert signature_changes(stub, renamed) == (
        "TodoStoreImpl.recent was removed or renamed (planned: "
        "`def recent(self, n: int=5) -> list[Todo] | None`)",
    )
    params = stub.replace("def add(self, title: str)", "def add(self, title: str, tag: str)")
    assert (
        "TodoStoreImpl.add changed from `def add(self, title: str) -> Todo` to "
        "`def add(self, title: str, tag: str) -> Todo`" in signature_changes(stub, params)
    )
    ret = stub.replace("-> Optional[Todo]", "-> Todo")
    assert any("TodoStoreImpl.find changed" in f for f in signature_changes(stub, ret))
    unbased = stub.replace("class TodoStoreImpl(TodoStore):", "class TodoStoreImpl:")
    assert signature_changes(stub, unbased) == ("class TodoStoreImpl dropped its base TodoStore",)
    assert len(signature_changes(stub, None)) == 4  # file deleted: the class and its 3 methods


def test_the_skeleton_id_is_reserved_from_the_plan() -> None:
    assert _safe_ticket_ids(["skeleton", "T-1"]) == {"skeleton": "skeleton-2", "T-1": "T-1"}


# ---- through the engineering node and the wave check ----------------------------

_STORE_CONTRACT = (
    "class TodoStore(Protocol):\n    def add(self, title: str) -> int\n    def count(self) -> int"
)
_GRAPH_CONTRACTS_PY = (
    "from typing import Protocol\n\n\n"
    "class TodoStore(Protocol):\n"
    "    def add(self, title: str) -> int: ...\n"
    "    def count(self) -> int: ...\n"
)
_GOOD_IMPL = (
    "from contracts import TodoStore\n\n\n"
    "class TodoStoreImpl(TodoStore):\n"
    "    def __init__(self) -> None:\n"
    "        self.items: list[str] = []\n\n"
    "    def add(self, title: str) -> int:\n"
    "        self.items.append(title)\n"
    "        return len(self.items)\n\n"
    "    def count(self) -> int:\n"
    "        return len(self.items)\n"
)


def _graph_case(
    tmp_path: Path,
    rec: _Recorder,
    *,
    signature: str = _STORE_CONTRACT,
    max_fix_attempts: int = 3,
) -> tuple[dict, list[str], ArtifactStore]:
    store = ArtifactStore()
    contract = InterfaceContract(
        name="TodoStore",
        spec_uri=persist_prose(signature, store=store),
        kind="class",
        signature=signature,
        seq=Seq("run", 1, 1, "planner", 20),
    )
    extra = [emit_artifact(contract, "interface_contract", "planner", contract.seq, store=store)]
    tickets = [
        _ticket("contracts", 0, files=("contracts.py",), implements=("TodoStore",)),
        _ticket("T-001", 1, files=("store.py",), deps=("contracts",), implements=("TodoStore",)),
        _ticket("T-002", 2, files=("main.py",), deps=("T-001",), consumes=("TodoStore",)),
    ]
    stack = StackDecision(
        language="python",
        build="true",
        typecheck=f"{sys.executable} -m mypy --strict --no-incremental contracts.py store.py",
    )
    out, ran, store = _wave_run(
        tmp_path,
        rec,
        tickets=tickets,
        store=store,
        artifacts=extra,
        stack=stack,
        max_fix_attempts=max_fix_attempts,
    )
    return out, ran, store


def test_the_skeleton_lands_with_the_contracts_and_wave_zero_typechecks_it(
    tmp_path: Path,
) -> None:
    rec = _Recorder(
        writes={
            "contracts": {"contracts.py": _GRAPH_CONTRACTS_PY},
            "T-001": {"store.py": _GOOD_IMPL},
        }
    )
    out, ran, store = _graph_case(tmp_path, rec)
    assert ran[:2] == ["engineering", "wave_gate"]
    assert _builds(out)[0] is GateStatus.PASS  # mypy --strict ran on the stub in wave 0
    assert SKELETON_TASK_ID in _diffs(out)
    stub = skeleton_files(out, store.resolve, store.resolve_blob)["store.py"]
    assert "class TodoStoreImpl(TodoStore):" in stub
    # Wave 1 built on the stub, was told to fill it, and kept its signatures.
    assert rec.seen["T-001"]["store.py"] == stub
    rules = " ".join(rec.packets["T-001"].task["rules"])
    assert "TYPED STUBS: store.py already exist" in rules
    assert rec.calls.count("T-001") == 1
    # Its caller is told which class implements the contract it consumes.
    assert "TodoStore is implemented by TodoStoreImpl in store.py" in " ".join(
        rec.packets["T-002"].task["rules"]
    )
    tree = tmp_path / "tree"
    materialize_workdir(out, tree, resolve=store.resolve, resolve_blob=store.resolve_blob)
    assert (tree / "store.py").read_text() == _GOOD_IMPL
    assert (tree / "contracts.py").read_text() == _GRAPH_CONTRACTS_PY


def test_a_contracts_file_the_stub_does_not_fit_fails_the_wave_zero_check(
    tmp_path: Path,
) -> None:
    drifted = _GRAPH_CONTRACTS_PY.replace(
        "def add(self, title: str) -> int", "def add(self, title: str) -> str"
    )
    rec = _Recorder(writes={"contracts": {"contracts.py": drifted}})
    out, ran, _ = _graph_case(tmp_path, rec, max_fix_attempts=1)
    assert ran == ["engineering", "wave_gate"]
    assert _builds(out) == [GateStatus.FAIL]
    assert "T-001" not in rec.calls


def test_a_changed_signature_fails_the_first_attempt_and_is_noted_on_the_last(
    tmp_path: Path,
) -> None:
    renamed = _GOOD_IMPL.replace("def count(self)", "def size(self)")
    rec = _Recorder(
        writes={"contracts": {"contracts.py": _GRAPH_CONTRACTS_PY}, "T-001": {"store.py": renamed}}
    )
    out, _, _ = _graph_case(tmp_path, rec)
    assert rec.calls.count("T-001") == 2
    retry = [p for p in rec.history if p.task["id"] == "T-001"][1]
    assert any(
        "store.py: TodoStoreImpl.count was removed or renamed" in f for f in retry.prior_failures
    )
    notes = {t.name: t.explanation for t in out["gate_triage"] if t.run_against == "task/T-001"}
    assert "ticket-skipped" not in notes
    assert "TodoStoreImpl.count was removed or renamed" in notes["signature-change"]
    assert "T-001" in _diffs(out)  # the last attempt is kept, with the change noted


def test_a_ticket_that_leaves_its_stub_untouched_counts_as_unwritten(tmp_path: Path) -> None:
    rec = _Recorder(
        writes={"contracts": {"contracts.py": _GRAPH_CONTRACTS_PY}}, silent=frozenset({"T-001"})
    )
    out, _, _ = _graph_case(tmp_path, rec)
    assert rec.calls.count("T-001") == 2
    notes = {t.name for t in out["gate_triage"] if t.run_against == "task/T-001"}
    assert "ticket-skipped" in notes


def test_prose_only_contracts_get_no_skeleton(tmp_path: Path) -> None:
    rec = _Recorder(
        writes={
            "contracts": {"contracts.py": _GRAPH_CONTRACTS_PY},
            "T-001": {"store.py": _GOOD_IMPL},
        }
    )
    out, _, store = _graph_case(tmp_path, rec, signature="a store that adds and counts todos")
    assert SKELETON_TASK_ID not in _diffs(out)
    assert skeleton_files(out, store.resolve, store.resolve_blob) == {}
    assert not any("TYPED STUBS" in r for r in rec.packets["T-001"].task["rules"])


_DATA_CONTRACTS_PY = """\
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple, Protocol, final


class Status(Enum):
    A = "a"

    def label(self) -> str:
        return str(self.value)


@dataclass
class User:
    name: str

    def greeting(self) -> str:
        return "hi " + self.name


@final
class Money:
    def cents(self) -> int:
        return 0


class Point(NamedTuple):
    x: int

    def norm(self) -> int:
        return abs(self.x)


class ProductServiceInterface(Protocol):
    def price(self, sku: str) -> int: ...


class IOrderRepo(Protocol):
    def count(self) -> int: ...


class CartProtocol(Protocol):
    def total(self) -> int: ...


class Cart:
    pass
"""


def test_data_types_are_never_subclassed_and_interfaces_get_natural_names(
    tmp_path: Path,
) -> None:
    contracts = [
        _contract(
            "Status", "class", 'class Status(Enum):\n    A = "a"\n    def label(self) -> str'
        ),
        _contract("User", "type", "class User:\n    name: str\n    def greeting(self) -> str"),
        _contract("User.greeting", "function", "def greeting(self) -> str"),
        _contract("Money", "class", "class Money:\n    def cents(self) -> int"),
        _contract(
            "Point", "class", "class Point(NamedTuple):\n    x: int\n    def norm(self) -> int"
        ),
        _contract(
            "ProductServiceInterface",
            "class",
            "class ProductServiceInterface(Protocol):\n    def price(self, sku: str) -> int",
        ),
        _contract("IOrderRepo", "class", "class IOrderRepo(Protocol):\n    def count(self) -> int"),
        _contract(
            "CartProtocol", "class", "class CartProtocol(Protocol):\n    def total(self) -> int"
        ),
    ]
    tickets = [
        _tk("T-001", ("app/status.py",), ("Status",)),
        _tk("T-002", ("app/user.py",), ("User", "User.greeting")),
        _tk("T-003", ("app/money.py",), ("Money",)),
        _tk("T-004", ("app/point.py",), ("Point",)),
        _tk("T-005", ("app/product.py",), ("ProductServiceInterface",)),
        _tk("T-006", ("app/orders.py",), ("IOrderRepo",)),
        _tk("T-007", ("app/cart.py",), ("CartProtocol",)),
    ]
    sk = python_skeleton(contracts, tickets, {"app/contracts.py": _DATA_CONTRACTS_PY})
    # An Enum (a TypeError to extend), a type-kind dataclass, a @final class and a
    # NamedTuple are defined in full by the contracts file: no Impl subclass.
    assert set(sk.files) == {"app/product.py", "app/orders.py", "app/cart.py"}
    concerns = "\n".join(sk.concerns)
    for name in ("Status", "User", "Money", "Point"):
        assert f"{name} is a data type the contracts file defines in full" in concerns
    assert "class ProductService(ProductServiceInterface):" in sk.files["app/product.py"]
    assert "class OrderRepo(IOrderRepo):" in sk.files["app/orders.py"]
    # The natural name is taken by the contracts file: the Impl suffix instead.
    assert "class CartProtocolImpl(CartProtocol):" in sk.files["app/cart.py"]
    assert sk.impls["ProductServiceInterface"] == ("ProductService", "app/product.py")

    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").write_text("")
    (tmp_path / "app/contracts.py").write_text(_DATA_CONTRACTS_PY)
    for rel, text in sk.files.items():
        (tmp_path / rel).write_text(text)
    done = _mypy_strict(tmp_path)
    assert done.returncode == 0, done.stdout


def test_a_caller_is_told_the_natural_implementation_name() -> None:
    skeleton = {
        "svc.py": "from contracts import ProductServiceInterface\n\n\n"
        "class ProductService(ProductServiceInterface):\n"
        "    def price(self, sku: str) -> int:\n"
        "        raise NotImplementedError\n"
    }
    caller = Ticket(
        id="T-2",
        title="t",
        specialty=Specialty.BACKEND,
        files=("main.py",),
        consumes=("ProductServiceInterface",),
        seq=_SEQ,
    )
    assert any(
        "ProductServiceInterface is implemented by ProductService in svc.py" in r
        for r in stub_rules(caller, skeleton, {})
    )


def test_an_empty_package_marker_is_not_an_unwritten_stub(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg/__init__.py").write_text("")
    stub = "def f() -> int:\n    raise NotImplementedError\n"
    (src / "pkg/mod.py").write_text(stub)
    skeleton = {"pkg/__init__.py": "", "pkg/mod.py": stub}
    with Sandbox(src) as sb:
        blob = b'{"kind": "full-file-snapshot", "files": {}}'
        result = AdapterResult(diff_uri=sb.put_blob(blob), snapshot_digest=sb.snapshot())
        # A ticket whose only scoped file is the marker the skeleton wrote: nothing to do.
        _require_scope_written(result, sb, ("pkg/__init__.py",), "T-001", skeleton)
        # The marker does not stand in for the stub the ticket left untouched.
        with pytest.raises(EmptyTicketError):
            _require_scope_written(result, sb, ("pkg/mod.py", "pkg/__init__.py"), "T-001", skeleton)


_FIXED_STUB = (
    "from contracts import TodoStore\n\n\n"
    "class TodoStoreImpl(TodoStore):\n\n"
    "    def add(self, title: str) -> str:\n"
    "        raise NotImplementedError\n\n"
    "    def count(self) -> int:\n"
    "        raise NotImplementedError\n"
)
_DRIFTED_PY = _GRAPH_CONTRACTS_PY.replace(
    "def add(self, title: str) -> int", "def add(self, title: str) -> str"
)


def test_a_ticket_is_held_to_the_stub_as_the_wave_zero_fixer_left_it(tmp_path: Path) -> None:
    # The contracts file returns str where the plan says int; the fixer re-signs the
    # stub to match, and the owning ticket that keeps the fixer's signature passes.
    impl = _GOOD_IMPL.replace(
        "def add(self, title: str) -> int:\n"
        "        self.items.append(title)\n"
        "        return len(self.items)",
        "def add(self, title: str) -> str:\n        self.items.append(title)\n        return title",
    )
    rec = _Recorder(
        writes={
            "contracts": {"contracts.py": _DRIFTED_PY},
            "fix-1": {"store.py": _FIXED_STUB},
            "T-001": {"store.py": impl},
        }
    )
    out, _, _ = _graph_case(tmp_path, rec)
    assert rec.seen["T-001"]["store.py"] == _FIXED_STUB
    assert rec.calls.count("T-001") == 1
    assert not any(t.name == "signature-change" for t in out["gate_triage"])


def test_a_ticket_that_leaves_the_fixed_stub_untouched_counts_as_unwritten(
    tmp_path: Path,
) -> None:
    rec = _Recorder(
        writes={"contracts": {"contracts.py": _DRIFTED_PY}, "fix-1": {"store.py": _FIXED_STUB}},
        silent=frozenset({"T-001"}),
    )
    out, _, _ = _graph_case(tmp_path, rec)
    assert rec.seen["T-001"]["store.py"] == _FIXED_STUB
    assert rec.calls.count("T-001") == 2
    assert "ticket-skipped" in {t.name for t in out["gate_triage"] if t.run_against == "task/T-001"}
