"""Phase-2 (Gated mode) FactoryState channels + the sign-off reducer (T-M2-02).

Covers the three channels Phase-2 needs on `FactoryState`:

- `mode` — single-writer ("assistant" | "gated"); no reducer.
- `risk_tier` — single-writer `TierDecision | None`; no reducer.
- `sign_offs` — a keyed-union channel of `SignOffRecord` folded by
  `union_sign_offs` (deduped by `content_identity`).

The reducer suite mirrors the algebra contract enforced for every channel in
`tests/test_reducers_properties.py` (docs/architecture.md §3, LEARNINGS L8):
None-safe, commutative, associative, idempotent under re-fold. Single-writer
channels carry NO reducer (last-writer wins; LangGraph rejects concurrent
writes) — asserted by their absence from the `Annotated` metadata.
"""

from __future__ import annotations

import json
from typing import Annotated, get_args, get_origin, get_type_hints

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import SignOffRecord, TierDecision
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import union_sign_offs
from foundry.state.seq import Seq

RUN_ID = "run-0"
NODE_IDS = ["nA", "nB", "nC"]


# ---------------------------------------------------------------------------
# Canonical multiset comparison (order-free) — mirrors the property suite.
# ---------------------------------------------------------------------------


def canon(records) -> list[str]:
    def one(r) -> str:
        if isinstance(r, BaseModel):
            return json.dumps(r.model_dump(mode="json"), sort_keys=True)
        return repr(r)

    return sorted(one(r) for r in records)


# ---------------------------------------------------------------------------
# 1. The channels are present + typed; single-writer channels carry no reducer.
# ---------------------------------------------------------------------------


def _hints() -> dict[str, object]:
    # include_extras keeps the Annotated[...] metadata (the reducer) attached.
    return get_type_hints(FactoryState, include_extras=True)


def test_phase2_channels_present_and_typed() -> None:
    hints = _hints()
    assert {"mode", "risk_tier", "sign_offs"} <= set(hints)
    assert hints["mode"] is str
    # risk_tier: TierDecision | None (single-writer)
    assert TierDecision in get_args(hints["risk_tier"])
    assert type(None) in get_args(hints["risk_tier"])
    # sign_offs: Annotated[list[SignOffRecord], union_sign_offs]
    assert get_origin(hints["sign_offs"]) is Annotated
    base, *meta = get_args(hints["sign_offs"])
    assert get_origin(base) is list
    assert get_args(base) == (SignOffRecord,)
    assert union_sign_offs in meta


def test_single_writer_channels_have_no_reducer() -> None:
    """`mode` and `risk_tier` are single-writer: plain (un-Annotated) types so
    LangGraph applies last-writer-wins (no merge), exactly like `request`/
    `phase`/`budget`."""
    hints = _hints()
    assert get_origin(hints["mode"]) is not Annotated
    assert get_origin(hints["risk_tier"]) is not Annotated


def test_existing_channels_untouched() -> None:
    """Append-only: the pre-existing channels are still present (no semantics
    change to the existing reducers)."""
    hints = _hints()
    existing = {
        "request",
        "route",
        "phase",
        "required_capabilities",
        "pinned_constraints",
        "artifacts",
        "gates",
        "gate_triage",
        "tasks",
        "budget",
        "actions",
        "history_ref",
    }
    assert existing <= set(hints)


# ---------------------------------------------------------------------------
# 2. Sign-off record builders / strategies.
# ---------------------------------------------------------------------------


def _seq(epoch: int, superstep: int, node: str, local: int) -> Seq:
    return Seq(RUN_ID, epoch, superstep, node, local)


def mk_sign_off(
    *,
    subject: str,
    approver: str,
    role: str,
    approved: bool,
    nonce: str,
    evidence: str,
    provenance: JudgmentProvenance,
    seq: Seq,
) -> SignOffRecord:
    return SignOffRecord(
        subject_artifact_uri=subject,
        state_sha="sha-abc",
        approver=approver,
        role=role,
        approved=approved,
        server_nonce=nonce,
        presented_evidence_hash=evidence,
        judgment_provenance=provenance,
        seq=seq,
    )


_seq_fields = st.tuples(
    st.integers(0, 3),
    st.integers(0, 3),
    st.sampled_from(NODE_IDS),
    st.integers(0, 3),
)


@st.composite
def sign_off_pool(draw) -> list[SignOffRecord]:
    seqs = [Seq(RUN_ID, *t) for t in draw(st.lists(_seq_fields, unique=True, max_size=8))]
    return [
        mk_sign_off(
            subject=draw(st.sampled_from(["cas://a", "cas://b"])),
            approver=draw(st.sampled_from(["alice", "bob"])),
            role=draw(st.sampled_from(["release-mgr", "security-lead"])),
            approved=draw(st.booleans()),
            nonce=draw(st.sampled_from(["n1", "n2"])),
            evidence=draw(st.sampled_from(["ev1", "ev2"])),
            provenance=draw(st.sampled_from(list(JudgmentProvenance))),
            seq=s,
        )
        for s in seqs
    ]


def _split(data, n: int) -> list[list[SignOffRecord]]:
    pool = data.draw(sign_off_pool(), label="pool")
    idx = data.draw(
        st.lists(st.integers(0, n - 1), min_size=len(pool), max_size=len(pool)),
        label="parts",
    )
    parts: list[list[SignOffRecord]] = [[] for _ in range(n)]
    for x, i in zip(pool, idx, strict=True):
        parts[i].append(x)
    return parts


# ---------------------------------------------------------------------------
# 3. union_sign_offs algebra — None-safe / commutative / associative /
#    re-fold idempotent (L8). Mirrors the property suite's reducer contract.
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=75, deadline=None)
def test_sign_offs_none_safety(data) -> None:
    assert union_sign_offs(None, None) == []
    x = data.draw(sign_off_pool())
    expect = canon(union_sign_offs(None, x))
    for form in (union_sign_offs(x, None), union_sign_offs([], x), union_sign_offs(x, [])):
        assert canon(form) == expect


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_sign_offs_commutativity(data) -> None:
    a, b = _split(data, 2)
    assert canon(union_sign_offs(a, b)) == canon(union_sign_offs(b, a))


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_sign_offs_associativity(data) -> None:
    a, b, c = _split(data, 3)
    left = union_sign_offs(union_sign_offs(a, b), c)
    right = union_sign_offs(a, union_sign_offs(b, c))
    assert canon(left) == canon(right)


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_sign_offs_refold_idempotency(data) -> None:
    a, b = _split(data, 2)
    merged = union_sign_offs(a, b)
    assert canon(union_sign_offs(merged, b)) == canon(merged)
    assert canon(union_sign_offs(merged, merged)) == canon(merged)


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_sign_offs_dedup_by_content_identity(data) -> None:
    """Output keys are exactly the union of input content identities — one record
    per identity, no silent drop (a recorded sign-off is never dropped)."""
    a, b = _split(data, 2)
    out = union_sign_offs(a, b)
    out_ids = [s.content_identity() for s in out]
    assert len(out_ids) == len(set(out_ids)), "duplicate identity survived"
    assert set(out_ids) == {s.content_identity() for s in a + b}


def test_sign_offs_identical_record_collapses() -> None:
    s = mk_sign_off(
        subject="cas://a",
        approver="alice",
        role="release-mgr",
        approved=True,
        nonce="n1",
        evidence="ev1",
        provenance=JudgmentProvenance.SELF,
        seq=_seq(0, 0, "nA", 0),
    )
    assert union_sign_offs([s], [s]) == [s]
    assert union_sign_offs([s, s], None) == [s]


def test_sign_offs_self_vs_independent_are_distinct() -> None:
    """The self-judge stamp is part of content identity (AS4): a SELF and an
    otherwise-identical INDEPENDENT sign-off coexist (recorded, never hidden)."""
    base = dict(
        subject="cas://a",
        approver="alice",
        role="release-mgr",
        approved=True,
        nonce="n1",
        evidence="ev1",
        seq=_seq(0, 0, "nA", 0),
    )
    self_so = mk_sign_off(provenance=JudgmentProvenance.SELF, **base)
    indep_so = mk_sign_off(provenance=JudgmentProvenance.INDEPENDENT, **base)
    out = union_sign_offs([self_so], [indep_so])
    assert len(out) == 2
    assert canon(out) == canon([self_so, indep_so])
