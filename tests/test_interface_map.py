"""The signature map of the integrated tree a ticket builds on."""

from __future__ import annotations

from foundry.agents.implementation.interface_map import (
    MAP_LABEL,
    go_outline,
    python_outline,
    rust_outline,
    signature_map,
    ts_outline,
)

_PY = """\
from dataclasses import dataclass
from typing import Protocol

MAX_ITEMS = 10
_private = 1


@dataclass(frozen=True)
class User:
    id: int
    email: str = "x"

    def display(self) -> str:
        return self.email

    def _hidden(self) -> None:
        pass


class Repo(Protocol):
    def get(self, uid: int) -> User | None: ...


async def fetch(url: str, *, timeout: float = 1.0) -> bytes:
    return b""


def _helper() -> None:
    pass
"""


def test_python_outline_is_the_public_interface_without_bodies() -> None:
    assert python_outline(_PY) == [
        "MAX_ITEMS = 10",
        "@dataclass(frozen=True)",
        "class User:",
        "    id: int",
        "    email: str = 'x'",
        "    def display(self) -> str",
        "class Repo(Protocol):",
        "    def get(self, uid: int) -> User | None",
        "async def fetch(url: str, *, timeout: float=1.0) -> bytes",
    ]
    assert python_outline("def broken(:\n") == []


def test_brace_language_outlines_keep_exported_signatures_and_members() -> None:
    ts = (
        "/* { */\nexport interface User {\n  id: number;\n  display(p: string): string;\n}\n"
        "export async function login(\n  email: string,\n): Promise<User> {\n"
        "  if (x) { return y; }\n}\nfunction internal() {}\n"
        'export class Store extends Base {\n  private secret = "}";\n'
        "  async get(id: number): Promise<User> {\n    return this.find({ id });\n  }\n}\n"
    )
    assert ts_outline(ts) == [
        "export interface User",
        "  id: number",
        "  display(p: string): string",
        "export async function login( email: string, ): Promise<User>",
        "export class Store extends Base",
        "  async get(id: number): Promise<User>",
    ]
    go = (
        "type User struct {\n\tID int\n\temail string\n}\n"
        "func NewStore(db *DB) *Store {\n\treturn nil\n}\nfunc helper() {}\n"
    )
    assert go_outline(go) == ["type User struct", "  ID int", "func NewStore(db *DB) *Store"]
    rs = (
        "pub struct User {\n    pub id: u64,\n    secret: String,\n}\n"
        "impl Display for User {\n    fn fmt(&self) {}\n}\n"
        "impl User {\n    pub fn new() -> Self { User {} }\n}\n"
    )
    assert rust_outline(rs) == [
        "pub struct User",
        "  pub id: u64",
        "impl User",
        "  pub fn new() -> Self",
    ]


def _tree() -> dict[str, str | None]:
    return {
        "app/contracts.py": "class Order(Protocol):\n    def total(self) -> int: ...\n",
        "app/billing.py": "def charge(cents: int) -> bool:\n    return True\n",
        "app/catalog.py": "def price(sku: str) -> int:\n    return 1\n",
        "app/mine.py": "def own() -> None:\n    pass\n",
        "tests/test_billing.py": "def test_x() -> None:\n    pass\n",
        "features/T-1.feature": "Feature: x\n",
        "README.md": "readme\n",
        "gone.py": None,
    }


def test_map_orders_contracts_then_reads_then_dependencies_ranked_by_mentions() -> None:
    m = signature_map(
        _tree(),
        contract_files=["app/contracts.py"],
        reads=["app/catalog.py", "app/billing.py"],
        dependency_files=["app/billing.py", "tests/test_billing.py", "app/mine.py"],
        exclude=["app/mine.py"],
        mention_text="charge the card, then charge again",
    )
    assert m.startswith(MAP_LABEL + " (integrated tree ")
    order = [line for line in m.splitlines() if line.startswith("## ")]
    assert order == ["## app/contracts.py", "## app/billing.py", "## app/catalog.py"]
    assert "return" not in m and "own()" not in m and "test_x" not in m


def test_map_expands_directories_and_is_empty_without_declarations() -> None:
    m = signature_map(_tree(), reads=["app/"], exclude=["app/mine.py"])
    assert [ln for ln in m.splitlines() if ln.startswith("## ")] == [
        "## app/billing.py",
        "## app/catalog.py",
        "## app/contracts.py",
    ]
    assert signature_map(_tree(), reads=["README.md", "gone.py"]) == ""


def test_map_stays_within_budget_and_truncates_the_last_file() -> None:
    big = "\n".join(f"def f{i}(x: int) -> int:\n    return x\n" for i in range(400))
    tree: dict[str, str | None] = {"a.py": "def a() -> None:\n    pass\n", "b.py": big}
    m = signature_map(tree, reads=["a.py", "b.py"], budget=1200)
    assert len(m) <= 1200
    assert "## a.py" in m and "## b.py" in m and m.endswith("... (truncated)")


def test_map_header_changes_with_the_tree() -> None:
    tree = _tree()
    first = signature_map(tree, reads=["app/billing.py"])
    tree["app/billing.py"] = "def charge(cents: int) -> bool:\n    return False\n"
    second = signature_map(tree, reads=["app/billing.py"])
    assert first.splitlines()[1:] == second.splitlines()[1:]
    assert first.splitlines()[0] != second.splitlines()[0]
