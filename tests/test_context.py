"""Tests for foundry.context — context assembly, retrieval, determinism.

Contract under test: docs/architecture.md §8 — globals always injected
verbatim, locals relevance-scoped under a per-node token budget with visible
(never silent) exclusion, misclassified locals escalated AND fail-safe
injected (AI-R10-F1), supersession applied as a read-time view, retrieval
gated by a measured recall hook (eval §5), and the sampling/determinism
policy + append-only run manifest with pinned-ID-drift detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.context import (
    ConstraintInjector,
    NeedContext,
    RunManifest,
    SamplingPolicy,
    Span,
    SymbolIndex,
    estimate_tokens,
    need_context,
)
from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.state.seq import Seq

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seq(i: int) -> Seq:
    return Seq(run_id="r", emission_epoch=0, superstep_index=i, node_id="n", local=0)


def _c(
    cid: str,
    text: str,
    scope: ConstraintScope = ConstraintScope.LOCAL,
    i: int = 0,
    supersedes: str | None = None,
) -> PinnedConstraint:
    return PinnedConstraint(
        id=cid, text=text, scope=scope, source="adr-7", supersedes=supersedes, seq=_seq(i)
    )


# ---------------------------------------------------------------------------
# pinned constraints
# ---------------------------------------------------------------------------


def test_global_always_injected_even_with_zero_relevance_overlap() -> None:
    g = _c("G1", "Quux frobnicate widget zorp.", scope=ConstraintScope.GLOBAL, i=1)
    inj = ConstraintInjector(token_budget_per_node=1000)
    # Task scope shares not a single token with the global's text.
    result = inj.assemble([g], node_task_scope=["billing", "invoice", "parser"])
    assert g.text in result.global_block
    assert result.warnings == []


def test_local_scoping_prefers_overlapping_constraint_under_tight_budget() -> None:
    a = _c("L-billing", "All billing invoice rows reconcile against the daily ledger.", i=1)
    b = _c("L-planner", "Prefer iterative deepening on the planner search frontier here.", i=2)
    # Budget admits exactly one of the two locals (each ~15 est. tokens).
    budget = max(estimate_tokens(a.text), estimate_tokens(b.text)) + 2
    inj = ConstraintInjector(token_budget_per_node=budget)
    result = inj.assemble([a, b], node_task_scope=["billing", "invoice"])
    assert a.text in result.local_block
    assert b.text not in result.local_block


def test_budget_exclusion_is_recorded_not_silent() -> None:
    a = _c("L-billing", "All billing invoice rows reconcile against the daily ledger.", i=1)
    b = _c("L-planner", "Prefer iterative deepening on the planner search frontier here.", i=2)
    budget = max(estimate_tokens(a.text), estimate_tokens(b.text)) + 2
    inj = ConstraintInjector(token_budget_per_node=budget)
    result = inj.assemble([a, b], node_task_scope=["billing", "invoice"])
    assert any("L-planner" in w for w in result.warnings), result.warnings
    # The included constraint must NOT appear in the warnings.
    assert not any("L-billing" in w for w in result.warnings)


def test_cardinality_monitor_escalates_at_ninety_percent_of_budget() -> None:
    # 380 chars -> 95 est. tokens -> >= 90% of a 100-token budget.
    g = _c("G-big", "x" * 380, scope=ConstraintScope.GLOBAL, i=1)
    inj = ConstraintInjector(token_budget_per_node=100)
    result = inj.assemble([g], node_task_scope=["anything"])
    kinds = {e.kind for e in result.escalations}
    assert "constraint-cardinality" in kinds
    # Still injected — never truncated.
    assert g.text in result.global_block


def test_no_cardinality_escalation_well_under_budget() -> None:
    g = _c("G-small", "Tiny invariant.", scope=ConstraintScope.GLOBAL, i=1)
    inj = ConstraintInjector(token_budget_per_node=1000)
    result = inj.assemble([g], node_task_scope=["anything"])
    assert result.escalations == []


def test_misclassified_local_escalates_and_is_injected_as_global() -> None:
    # "PII" + "logs" match the cross-cutting patterns; scope is (wrongly) local.
    m = _c("L-pii", "Never write PII to the request logs.", scope=ConstraintScope.LOCAL, i=1)
    inj = ConstraintInjector(token_budget_per_node=1000)
    # Zero-overlap task scope: if it were treated as a plain local it could
    # be scoped out; fail-safe means it lands in the GLOBAL block anyway.
    result = inj.assemble([m], node_task_scope=["frontend", "css"])
    assert m.text in result.global_block  # injected as if global (fail-safe)
    assert m.text not in result.local_block
    esc = [e for e in result.escalations if e.kind == "misclassified-local"]
    assert len(esc) == 1 and esc[0].constraint_id == "L-pii"


def test_superseded_constraint_not_injected_live_view() -> None:
    old = _c("C-old", "Use the v1 settlement flow.", scope=ConstraintScope.GLOBAL, i=1)
    new = _c(
        "C-new",
        "Use the v2 settlement flow.",
        scope=ConstraintScope.GLOBAL,
        i=2,
        supersedes="C-old",
    )
    inj = ConstraintInjector(token_budget_per_node=1000)
    result = inj.assemble([old, new], node_task_scope=["settlement"])
    assert new.text in result.global_block
    assert old.text not in result.global_block


def test_need_context_is_a_typed_returnable_escape() -> None:
    escape = need_context("symbol settle_batch and its callers")
    assert isinstance(escape, NeedContext)
    assert escape.query == "symbol settle_batch and its callers"
    with pytest.raises(NeedContext):
        raise escape


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


def test_recall_eval_computes_fraction_recalled(source_tree: Path) -> None:
    index = SymbolIndex.build(source_tree)
    spans = index.retrieve(["target"], k=10)
    target_span = next(s for s in spans if s.symbol == "target")
    caller_span = next(s for s in spans if s.symbol == "caller")
    bogus = Span(file="nope.py", line=1, end_line=2, symbol="ghost")

    assert index.recall_eval([(["target"], [target_span, caller_span])]) == 1.0
    assert index.recall_eval([(["target"], [target_span, bogus])]) == 0.5


def test_empty_retrieval_raises_need_context(source_tree: Path) -> None:
    index = SymbolIndex.build(source_tree)
    assert index.retrieve(["no_such_symbol"], k=5) == []
    with pytest.raises(NeedContext) as exc:
        index.retrieve_or_need_context(["no_such_symbol"], k=5)
    assert "no_such_symbol" in exc.value.query


# ---------------------------------------------------------------------------
# determinism: sampling policy + run manifest
# ---------------------------------------------------------------------------


def test_t2_gate_is_temperature_zero() -> None:
    params = SamplingPolicy.for_node("gate", "T2")
    assert params["model"] == "claude-sonnet-4-6"
    assert params["temperature"] == 0.0


def test_t1_gate_has_no_temperature_and_records_effort_substitution() -> None:
    params = SamplingPolicy.for_node("gate", "T1")
    assert params["model"] == "claude-opus-4-8"
    assert "temperature" not in params  # provider 400s on sampling params
    assert params["output_config"] == {"effort": "high"}
    assert "note" in params  # the substitution is recorded, not silent


def test_ideation_t2_is_temperature_one() -> None:
    params = SamplingPolicy.for_node("ideation", "T2")
    assert params["temperature"] == 1.0


def test_ideation_t1_uses_adaptive_thinking_not_temperature() -> None:
    params = SamplingPolicy.for_node("ideation", "T1")
    assert "temperature" not in params
    assert params["thinking"] == {"type": "adaptive"}


def test_unknown_purpose_fails_closed() -> None:
    with pytest.raises(ValueError):
        SamplingPolicy.for_node("vibes", "T2")


def test_manifest_drift_flag_when_served_differs_from_pinned() -> None:
    m = RunManifest()
    m.record(
        node_id="n1",
        model_id="claude-sonnet-4-6",
        served_model="claude-sonnet-4-6",
        fingerprint="req_001",
        sampling=SamplingPolicy.for_node("gate", "T2"),
        seed=7,
    )
    assert m.drift_check("n1", "claude-sonnet-4-6") is None
    assert m.drift_alerts == ()

    before = m.digest()
    alert = m.drift_check("n1", "claude-sonnet-4-5")
    assert alert is not None
    assert alert["kind"] == "pinned-id-drift"
    assert alert["pinned"] == "claude-sonnet-4-6"
    assert alert["served"] == "claude-sonnet-4-5"
    assert len(m.drift_alerts) == 1
    # The drift is part of the WORM-digested record.
    assert m.digest() != before


def test_manifest_is_append_only_and_digest_is_deterministic() -> None:
    def build() -> RunManifest:
        m = RunManifest()
        m.record(
            node_id="n1",
            model_id="claude-haiku-4-5",
            served_model="claude-haiku-4-5",
            fingerprint="req_a",
            sampling=SamplingPolicy.for_node("routing", "T3"),
            seed=None,
        )
        return m

    m1, m2 = build(), build()
    assert m1.digest() == m2.digest()
    assert len(m1.entries) == 1

    m1.record(
        node_id="n2",
        model_id="claude-opus-4-8",
        served_model="claude-opus-4-8",
        fingerprint="req_b",
        sampling=SamplingPolicy.for_node("judge", "T1"),
        seed=3,
    )
    assert len(m1.entries) == 2
    assert m1.digest() != m2.digest()


def test_manifest_drift_check_unknown_node_raises() -> None:
    with pytest.raises(KeyError):
        RunManifest().drift_check("ghost", "claude-haiku-4-5")
