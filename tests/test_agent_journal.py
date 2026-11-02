"""Per-agent work-journal (plan [11]) — ADVISORY-ONLY observability.

The journal is NOT a trust input — a build with arbitrary (even ALARMING)
`agent_journal` content still PASSES/FAILS based ONLY on signed gates, because no
gate / `merge_gates` / `route_after_gates` reads the channel. (The
:func:`union_journal` fold algebra is property-tested in test_reducers_properties.)
"""

from __future__ import annotations

import json

import pytest

from foundry.gates.attestation import DevGateWriteVerifier, ZoneBSigner
from foundry.graph.gate_wiring import SPEC_BOUNDARY, route_after_gates
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.journal import JournalEntry
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
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


# ---- the journal is NOT a trust input ------------------------------------


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
