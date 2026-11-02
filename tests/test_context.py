"""Tests for foundry.context — retrieval and the sampling/determinism policy.

Contract under test: docs/architecture.md §8 — symbol + one-hop call-graph
retrieval that raises the typed ``NeedContext`` escape on an empty result, and
the per-(purpose, tier) sampling policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.context import NeedContext, SamplingPolicy, SymbolIndex

# ---------------------------------------------------------------------------
# retriever
# ---------------------------------------------------------------------------


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(
        "def target():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "from a import target\n\n\ndef caller():\n    return target()\n",
        encoding="utf-8",
    )
    return tmp_path


def test_symbol_index_finds_function_and_one_hop_caller(source_tree: Path) -> None:
    index = SymbolIndex.build(source_tree)
    spans = index.retrieve(["target"], k=10)
    symbols = [s.symbol for s in spans]
    assert "target" in symbols  # direct symbol-index hit
    assert "caller" in symbols  # one-hop caller (impact analysis)
    # Direct hits come first, deterministically.
    assert symbols[0] == "target"
    target_span = next(s for s in spans if s.symbol == "target")
    assert target_span.file == str(source_tree / "a.py")
    assert target_span.line == 1


def test_retrieve_dedupes_and_respects_k(source_tree: Path) -> None:
    index = SymbolIndex.build(source_tree)
    spans = index.retrieve(["target", "target"], k=10)
    assert len(spans) == len(set(spans))
    assert index.retrieve(["target"], k=1) == spans[:1]


def test_empty_retrieval_raises_need_context(source_tree: Path) -> None:
    index = SymbolIndex.build(source_tree)
    assert index.retrieve(["no_such_symbol"], k=5) == []
    with pytest.raises(NeedContext) as exc:
        index.retrieve_or_need_context(["no_such_symbol"], k=5)
    assert "no_such_symbol" in exc.value.query


# ---------------------------------------------------------------------------
# determinism: sampling policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("purpose", "temperature"), [("gate", 0.0), ("ideation", 1.0)])
def test_t2_temperature_by_purpose(purpose: str, temperature: float) -> None:
    params = SamplingPolicy.for_node(purpose, "T2")
    assert params["model"] == "claude-sonnet-4-6"
    assert params["temperature"] == temperature


def test_t1_gate_has_no_temperature_and_records_effort_substitution() -> None:
    params = SamplingPolicy.for_node("gate", "T1")
    assert params["model"] == "claude-opus-4-8"
    assert "temperature" not in params  # provider 400s on sampling params
    assert params["output_config"] == {"effort": "high"}
    assert "note" in params  # the substitution is recorded, not silent


def test_ideation_t1_uses_adaptive_thinking_not_temperature() -> None:
    params = SamplingPolicy.for_node("ideation", "T1")
    assert "temperature" not in params
    assert params["thinking"] == {"type": "adaptive"}


def test_unknown_purpose_fails_closed() -> None:
    with pytest.raises(ValueError):
        SamplingPolicy.for_node("vibes", "T2")
