"""Retrieval as a measured contract, not vibes. (architecture.md §8)

Symbol index PLUS the call/def graph (impact analysis) — not embedding
similarity alone — so cross-file / implicit coupling is surfaced.

This is the **dev-grade tree-sitter/LSP substitute** built on stdlib `ast`:

- `SymbolIndex.build(root)` walks `*.py` files and indexes function/class
  definitions (name -> file:line span).
- The call graph is built from `ast.Call` name edges. **Best-effort by
  design**: edges are keyed on bare names (`Name.id` / `Attribute.attr`), so
  aliasing, dynamic dispatch, and same-name symbols in different modules are
  conflated. That is acceptable for a recall-oriented dev index (extra
  neighbors cost tokens, not correctness).
- `retrieve(query_symbols, k)` returns direct symbol-index hits plus one-hop
  call/def-graph neighbors (callers AND callees — impact analysis), deduped,
  in deterministic order.
- `NeedContext` integration: an empty result means the caller should raise
  `NeedContext` rather than proceed over a partial view —
  `retrieve_or_need_context` does exactly that.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from foundry.context.pinned_constraints import NeedContext

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True, order=True)
class Span:
    """A definition span: symbol -> file:line range."""

    file: str
    line: int
    end_line: int
    symbol: str


def _call_name(node: ast.Call) -> str | None:
    """Best-effort callee name: `f(...)` -> "f", `obj.m(...)` -> "m"."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class SymbolIndex:
    """Function/class index + best-effort call graph over a source tree."""

    def __init__(
        self,
        defs: dict[str, list[Span]],
        calls: dict[str, set[str]],
        callers: dict[str, set[str]],
    ) -> None:
        self._defs = defs
        self._calls = calls  # symbol -> names it calls
        self._callers = callers  # symbol -> names that call it

    @classmethod
    def build(cls, root: str | Path) -> SymbolIndex:
        """Walk `root` for .py files; index defs and call edges.

        Deterministic: files are visited in sorted path order; unparseable
        files are skipped (best-effort dev index, documented above).
        """
        defs: dict[str, list[Span]] = {}
        calls: dict[str, set[str]] = {}
        callers: dict[str, set[str]] = {}

        for path in sorted(Path(root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, _DEF_NODES):
                    continue
                span = Span(
                    file=str(path),
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    symbol=node.name,
                )
                defs.setdefault(node.name, []).append(span)
                # Call edges from inside this definition. Nested defs are
                # attributed to every enclosing def too — best-effort and
                # recall-friendly.
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        callee = _call_name(inner)
                        if callee is not None and callee != node.name:
                            calls.setdefault(node.name, set()).add(callee)
                            callers.setdefault(callee, set()).add(node.name)

        for spans in defs.values():
            spans.sort()
        return cls(defs=defs, calls=calls, callers=callers)

    def retrieve(self, query_symbols: list[str] | tuple[str, ...], k: int) -> list[Span]:
        """Direct symbol hits + one-hop call/def-graph neighbors, deduped.

        Order is deterministic: direct hits first (query order, then span
        order), then one-hop neighbors in sorted name order — so the highest
        confidence spans survive the `k` cut.
        """
        if k <= 0:
            return []
        out: list[Span] = []
        seen: set[Span] = set()

        def _add(spans: list[Span]) -> None:
            for s in spans:
                if s not in seen:
                    seen.add(s)
                    out.append(s)

        for q in query_symbols:
            _add(self._defs.get(q, []))
        for q in query_symbols:
            neighbors = self._callers.get(q, set()) | self._calls.get(q, set())
            for name in sorted(neighbors):
                _add(self._defs.get(name, []))
        return out[:k]

    def retrieve_or_need_context(
        self, query_symbols: list[str] | tuple[str, ...], k: int
    ) -> list[Span]:
        """`retrieve`, but an empty result raises the typed `NeedContext` escape.

        Retrieval failures are detected, not silently tolerated (arch §8):
        the node must re-retrieve or escalate rather than hallucinate.
        """
        spans = self.retrieve(query_symbols, k)
        if not spans:
            raise NeedContext(query=", ".join(query_symbols))
        return spans
