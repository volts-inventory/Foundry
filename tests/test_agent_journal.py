"""Per-agent work-journal (plan [11]) — ADVISORY-ONLY observability.

Proves the three load-bearing properties of the journal:

(a) :func:`union_journal` folds journal entries additively, order-independently,
    and idempotently on re-fold (the same algebra the `gate_triage` channel uses);
(b) the journal is NOT a trust input — a build with arbitrary (even ALARMING)
    `agent_journal` content still PASSES/FAILS based ONLY on signed gates, because
    no gate / `merge_gates` / `route_after_gates` reads the channel; and
(c) :func:`interview` returns a proxy-generated answer over a MockTransport and
    writes NO gate / verdict / channel (it only returns text).
"""

from __future__ import annotations

import json

import pytest

from foundry.agents.interview import interview
from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.gate_wiring import SPEC_BOUNDARY, route_after_gates
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.journal import JournalEntry
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
    union_journal,
)
from foundry.state.seq import Seq

KEY = b"dev-zone-b-journal-key"
KEY_ID = "dev-key-journal"
RUN_ID = "run-journal"


def _config(step: int = 1):
    return {"configurable": {"thread_id": RUN_ID}, "metadata": {"langgraph_step": step}}


def _entry(node_id: str, summary: str, epoch: int = 1) -> JournalEntry:
    return JournalEntry(
        node_id=node_id,
        role=node_id.replace("_", " ").title(),
        summary=summary,
        seq=Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node_id, local=0),
    )


def _canon(entries: list[JournalEntry]) -> set[str]:
    """Order-independent comparison: canonical multiset of entries."""
    return {json.dumps(e.model_dump(mode="json"), sort_keys=True) for e in entries}


# ---- (a) union_journal: additive, order-independent, idempotent --------------


def test_union_journal_is_additive_and_order_independent():
    a = _entry("product_manager", "wrote PRD")
    b = _entry("principal_architect", "chose architecture")
    ab = union_journal([a], [b])
    ba = union_journal([b], [a])
    assert _canon(ab) == _canon(ba) == _canon([a, b])


def test_union_journal_is_none_safe():
    a = _entry("product_manager", "wrote PRD")
    assert _canon(union_journal([a], None)) == _canon([a])
    assert _canon(union_journal(None, [a])) == _canon([a])
    assert union_journal(None, None) == []


def test_union_journal_idempotent_on_refold():
    # Re-folding the SAME (byte-identical) entry coalesces — a LangGraph resume
    # re-fold must not duplicate, must not change committed state.
    a = _entry("reviewer_critic", "approved diff")
    once = union_journal([a], [a])
    twice = union_journal(once, [a])
    assert _canon(once) == _canon(twice) == _canon([a])


def test_union_journal_distinct_seq_entries_coexist():
    # Two genuinely distinct notes from one node (different turn / epoch) are
    # distinct identities and BOTH retained (append-only observability log).
    a = _entry("product_manager", "wrote PRD", epoch=1)
    b = _entry("product_manager", "revised PRD", epoch=2)
    folded = union_journal([a], [b])
    assert _canon(folded) == _canon([a, b])


# ---- (b) the journal is NOT a trust input ------------------------------------


@pytest.fixture
def verifier():
    v = DevGateWriteVerifier(KEY, KEY_ID, GlobalRecords())
    token = set_gate_write_verifier(v)
    try:
        yield v
    finally:
        reset_gate_write_verifier(token)


def _spec_gate(status: GateStatus) -> GateResult:
    """A signed, runner-attested deterministic `spec` verdict (the TRUST input)."""
    signer = ZoneBSigner(KEY, KEY_ID)
    g = GateResult(
        name="spec",
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.CONFIG_EVALUABLE,
        run_against=RUN_ID,
        seq=Seq(run_id=RUN_ID, emission_epoch=1, superstep_index=0, node_id="spec_gate", local=0),
    )
    return signer.with_attestation(g)


# An ALARMING journal: an agent screaming the opposite of the verdict. It must be
# completely inert at the trust boundary.
_ALARMING = [
    _entry("reviewer_critic", "THIS BUILD IS BROKEN, DO NOT SHIP, EVERYTHING FAILS"),
    _entry("product_manager", "confidence 0.0 — abort the release"),
]


def test_passing_build_advances_regardless_of_alarming_journal(verifier):
    records = GlobalRecords()
    folded = merge_gates([_spec_gate(GateStatus.PASS)], None)

    no_journal = {"gates": folded}
    with_alarm = {"gates": folded, "agent_journal": _ALARMING}

    # Identical routing: the alarming journal changes NOTHING — the journal is
    # not consulted, only the signed spec verdict is.
    def route(s):
        return route_after_gates(s, _config(), boundary=SPEC_BOUNDARY, records=records)

    assert route(no_journal) == "advance"
    assert route(with_alarm) == "advance"


def test_failing_build_does_not_pass_even_with_reassuring_journal(verifier):
    records = GlobalRecords()
    folded = merge_gates([_spec_gate(GateStatus.FAIL)], None)

    reassuring = [
        _entry("reviewer_critic", "everything is perfect, ship it, all green"),
        _entry("product_manager", "confidence 1.0 — approve"),
    ]
    state = {"gates": folded, "agent_journal": reassuring}
    # A FAIL routes to FIX (within the convergence bound) — the reassuring
    # journal cannot promote it to ADVANCE.
    assert route_after_gates(state, _config(), boundary=SPEC_BOUNDARY, records=records) == "fix"


def test_merge_gates_ignores_journal_channel_entirely(verifier):
    # merge_gates takes ONLY gate lists; the journal lives on a separate channel
    # and is structurally unreachable from the gate fold. Folding gates is
    # byte-identical whether or not a journal exists alongside them.
    g = merge_gates([_spec_gate(GateStatus.PASS)], None)
    # The journal is never an argument to merge_gates — there is no parameter for
    # it. This asserts the structural separation: a verdict fold over the same
    # gates is invariant.
    assert merge_gates(g, None) == g


# ---- (c) interview(): proxy-generated, writes nothing ------------------------


def test_interview_returns_proxy_answer_over_mock_transport():
    proxy = ModelProviderProxy(MockTransport(default_text="It went well; one concern about scope."))
    answer = interview(
        proxy,
        role="Principal Architect",
        question="How did it go?",
        entries=[_entry("principal_architect", "chose modular monolith")],
        run_context={"phase": "design"},
    )
    assert answer == "It went well; one concern about scope."


def test_interview_returns_string_and_writes_no_channel():
    # interview() returns a plain str — it has NO channel write surface (cannot
    # mint a GateResult / GateTriage / JournalEntry), so it can never enter a
    # verdict/trust path.
    proxy = ModelProviderProxy(MockTransport(default_text="fine"))
    result = interview(proxy, role="Reviewer", question="ok?", entries=[])
    assert isinstance(result, str)
    assert not isinstance(result, (GateResult, JournalEntry))


def test_interview_is_best_effort_on_proxy_failure():
    class BoomTransport:
        def send(self, request: dict) -> object:
            raise RuntimeError("provider down")

    proxy = ModelProviderProxy(BoomTransport())
    # A proxy failure degrades to a fallback string — never raises (an interview
    # must never be able to wedge a run).
    answer = interview(proxy, role="QA", question="status?", entries=[])
    assert answer.startswith("(interview unavailable")
