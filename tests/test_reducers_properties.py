"""Property tests for foundry.state.reducers — Phase 0 exit (implementation-plan §9).

Contract under test (docs/architecture.md §3): LangGraph folds pending writes
as an UNORDERED set, so every reducer must be None-safe, commutative,
associative, idempotent under re-fold (resume), and must derive ordering from
data (`Seq`) — never iteration order.

merge_gates' grounding/security behavior (attestation, nonce, latch) is
covered in a separate suite; here a permissive stub verifier is installed via
set_gate_write_verifier so these tests exercise the ALGEBRA only.

Comparison is always over canonical multisets (sorted canonical dumps) —
reducers return dict-ordered lists and list order is not part of the contract.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.schemas.base import Artifact, ExternalAction, IdempotencyPath
from foundry.schemas.gates import GateResult, GateStatus, GateTriage, VerdictClass
from foundry.state.reducers import (
    DETERMINISTIC_GATES,
    JUDGMENT_VERDICTS,
    live_constraints,
    merge_by_uri,
    merge_constraints,
    merge_gates,
    set_gate_write_verifier,
    union,
    union_actions,
    union_by_id,
    union_triage,
)
from foundry.state.seq import Seq

# ---------------------------------------------------------------------------
# Permissive stub verifier: admits everything, so merge_gates degenerates to
# its algebraic core (latest-by-Seq per (name, run_against)).
# ---------------------------------------------------------------------------


class AdmitAll:
    def verify_attestation(self, g: GateResult) -> bool:
        return True

    def verify_judgment_binding(self, g: GateResult) -> bool:
        return True

    def nonce_consumed_by_other_verdict(self, g: GateResult) -> bool:
        return False

    def negative_verdict_latched(self, g: GateResult) -> bool:
        return False


ADMIT_ALL = AdmitAll()


def _install_permissive_verifier() -> None:
    # ContextVar: must be set inside the test body so it is active for every
    # hypothesis example in the current execution context.
    set_gate_write_verifier(ADMIT_ALL)


# ---------------------------------------------------------------------------
# Canonical comparison helper — multiset of canonical dumps, order-free.
# ---------------------------------------------------------------------------


def canon(records) -> list[str]:
    def one(r) -> str:
        if isinstance(r, BaseModel):
            d = r.model_dump(mode="json")
            # frozensets dump as lists in arbitrary order — canonicalize.
            for k, v in d.items():
                if isinstance(v, list) and k == "served_family":
                    d[k] = sorted(v)
            return json.dumps(d, sort_keys=True)
        if isinstance(r, dict):
            return json.dumps(r, sort_keys=True)
        return repr(r)

    return sorted(one(r) for r in records)


# ---------------------------------------------------------------------------
# Strategies.
#
# Single-run fold: run_id is FIXED (cross-run folds never happen). Records
# that share a channel key must still carry distinct seqs — Seq is a total
# order, so we draw UNIQUE (epoch, superstep, node, local) tuples per pool.
# ---------------------------------------------------------------------------

RUN_ID = "run-0"
NODE_IDS = ["nA", "nB", "nC"]

_seq_fields = st.tuples(
    st.integers(0, 3),  # emission_epoch
    st.integers(0, 3),  # superstep_index
    st.sampled_from(NODE_IDS),  # node_id
    st.integers(0, 3),  # local
)


def to_seq(t: tuple[int, int, str, int]) -> Seq:
    return Seq(RUN_ID, t[0], t[1], t[2], t[3])


def unique_seqs(draw, max_size: int = 8) -> list[Seq]:
    return [to_seq(t) for t in draw(st.lists(_seq_fields, unique=True, max_size=max_size))]


GATE_NAMES = sorted(DETERMINISTIC_GATES | JUDGMENT_VERDICTS | {"custom_metric"})
RUN_AGAINST = ["task/1", "task/2", "sha-aaa"]
CONSTRAINT_IDS = ["c1", "c2", "c3", "c4"]
ACTION_KEYS = ["k1", "k2", "k3"]


def mk_gate(*, name: str, run_against: str, status: GateStatus, seq: Seq) -> GateResult:
    return GateResult(
        name=name,
        status=status,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.CONFIG_EVALUABLE,
        run_against=run_against,
        seq=seq,
    )


@st.composite
def gate_pool(draw) -> list[GateResult]:
    return [
        mk_gate(
            name=draw(st.sampled_from(GATE_NAMES)),
            run_against=draw(st.sampled_from(RUN_AGAINST)),
            status=draw(st.sampled_from(list(GateStatus))),
            seq=s,
        )
        for s in unique_seqs(draw)
    ]


@st.composite
def triage_pool(draw) -> list[GateTriage]:
    return [
        GateTriage(
            name=draw(st.sampled_from(GATE_NAMES)),
            run_against=draw(st.sampled_from(RUN_AGAINST)),
            explanation=draw(st.sampled_from(["flaky", "fix imports", "rerun"])),
            produced_by=draw(st.sampled_from(NODE_IDS)),
            seq=s,
        )
        for s in unique_seqs(draw)
    ]


@st.composite
def action_pool(draw) -> list[ExternalAction]:
    return [
        ExternalAction(
            idempotency_key=draw(st.sampled_from(ACTION_KEYS)),
            kind=draw(st.sampled_from(["git_push", "open_pr", "notify"])),
            payload_uri=draw(st.sampled_from(["cas://p1", "cas://p2"])),
            path=draw(st.sampled_from(list(IdempotencyPath))),
            gate_bearing=draw(st.booleans()),
            seq=s,
        )
        for s in unique_seqs(draw)
    ]


@st.composite
def constraint_pool(draw) -> list[PinnedConstraint]:
    out = []
    for s in unique_seqs(draw):
        cid = draw(st.sampled_from(CONSTRAINT_IDS))
        supersedes = draw(
            st.none() | st.sampled_from([i for i in CONSTRAINT_IDS if i != cid])
        )
        out.append(
            PinnedConstraint(
                id=cid,
                text=draw(st.sampled_from(["use integer cents", "no floats in money"])),
                scope=draw(st.sampled_from(list(ConstraintScope))),
                source="adr-1",
                supersedes=supersedes,
                seq=s,
            )
        )
    return out


# Artifacts are content-addressed: the uri IS the identity, so the whole
# record (including seq) is a deterministic function of the uri. Same uri =>
# byte-identical record; distinct uris carry distinct seqs. This is what
# "set-union by content address" assumes.
def artifact_for(i: int) -> Artifact:
    return Artifact(
        kind="doc",
        uri=f"cas://obj-{i}",
        produced_by=NODE_IDS[i % len(NODE_IDS)],
        version=1,
        seq=Seq(RUN_ID, 0, 0, NODE_IDS[i % len(NODE_IDS)], i),
    )


artifact_pool = st.lists(st.integers(0, 5).map(artifact_for), max_size=8)


# Tasks (union_by_id) are plain dicts with no seq attribute: the reducer is
# commutative only for identical payloads per key — which is what content-
# keyed channels emit — so the payload is a function of the id.
def task_for(i: int) -> dict:
    return {"id": f"t{i}", "title": f"task {i}", "owner": NODE_IDS[i % len(NODE_IDS)]}


task_pool = st.lists(st.integers(0, 5).map(task_for), max_size=8)

plain_pool = st.lists(st.sampled_from(["lint", "test", "gpu", "db", "net"]), max_size=8)


REDUCERS = {
    "union": union,
    "union_by_id": union_by_id,
    "union_triage": union_triage,
    "union_actions": union_actions,
    "merge_by_uri": merge_by_uri,
    "merge_constraints": merge_constraints,
    "merge_gates": merge_gates,
}

POOLS = {
    "union": plain_pool,
    "union_by_id": task_pool,
    "union_triage": triage_pool(),
    "union_actions": action_pool(),
    "merge_by_uri": artifact_pool,
    "merge_constraints": constraint_pool(),
    "merge_gates": gate_pool(),
}

ALL_REDUCERS = sorted(REDUCERS)


def draw_parts(data, name: str, n: int) -> list[list]:
    """Draw one record pool and split it into n parts (unordered fan-in writes)."""
    pool = data.draw(POOLS[name], label="pool")
    idx = data.draw(
        st.lists(st.integers(0, n - 1), min_size=len(pool), max_size=len(pool)),
        label="part-assignment",
    )
    parts: list[list] = [[] for _ in range(n)]
    for x, i in zip(pool, idx, strict=True):
        parts[i].append(x)
    return parts


# ---------------------------------------------------------------------------
# 1. None-safety: None is an identity on either side; R(None, None) == [].
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_REDUCERS)
@given(data=st.data())
@settings(max_examples=75, deadline=None)
def test_none_safety(name, data):
    _install_permissive_verifier()
    r = REDUCERS[name]
    assert r(None, None) == []
    x = data.draw(POOLS[name], label="x")
    forms = [r(None, x), r(x, None), r([], x), r(x, [])]
    expect = canon(forms[0])
    for f in forms[1:]:
        assert canon(f) == expect


# ---------------------------------------------------------------------------
# 2. Commutativity: fold order of the two sides never changes the result.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_REDUCERS)
@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_commutativity(name, data):
    _install_permissive_verifier()
    r = REDUCERS[name]
    a, b = draw_parts(data, name, 2)
    assert canon(r(a, b)) == canon(r(b, a))


# ---------------------------------------------------------------------------
# 3. Associativity: grouping of folds never changes the result.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_REDUCERS)
@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_associativity(name, data):
    _install_permissive_verifier()
    r = REDUCERS[name]
    a, b, c = draw_parts(data, name, 3)
    assert canon(r(r(a, b), c)) == canon(r(a, r(b, c)))


# ---------------------------------------------------------------------------
# 4. Re-fold idempotency: a LangGraph re-fold on resume (the same pending
#    write set folded again into the already-merged value) is a no-op.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_REDUCERS)
@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_refold_idempotency(name, data):
    _install_permissive_verifier()
    r = REDUCERS[name]
    a, b = draw_parts(data, name, 2)
    merged = r(a, b)
    assert canon(r(merged, b)) == canon(merged)
    assert canon(r(merged, merged)) == canon(merged)


# ---------------------------------------------------------------------------
# 5. merge_by_uri: set-union by content address — exactly one record per uri,
#    last-writer-irrelevant, output uris exactly the union of input uris.
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_merge_by_uri_set_union_by_content_address(data):
    a, b = draw_parts(data, "merge_by_uri", 2)
    out = merge_by_uri(a, b)
    uris = [x.uri for x in out]
    assert len(uris) == len(set(uris)), "more than one record for a uri"
    assert set(uris) == {x.uri for x in a + b}
    assert canon(out) == canon(merge_by_uri(b, a))


def test_merge_by_uri_duplicate_uri_collapses():
    x = artifact_for(1)
    assert merge_by_uri([x], [x]) == [x]
    assert merge_by_uri([x, x], None) == [x]


# ---------------------------------------------------------------------------
# 6. merge_gates (permissive stub): per (name, run_against) the survivor is
#    max-by-Seq, fold order irrelevant; and the spec regression — at the SAME
#    superstep_index a higher emission_epoch outranks (epoch is ordered AHEAD
#    of superstep_index in Seq; langgraph_step is not monotonic across resume).
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_merge_gates_latest_by_seq_per_key(data):
    _install_permissive_verifier()
    a, b = draw_parts(data, "merge_gates", 2)
    expected: dict[tuple[str, str], GateResult] = {}
    for g in a + b:
        k = (g.name, g.run_against)
        if k not in expected or g.seq > expected[k].seq:
            expected[k] = g
    out = merge_gates(a, b)
    assert canon(out) == canon(expected.values())
    assert canon(merge_gates(b, a)) == canon(out)


@given(
    name=st.sampled_from(GATE_NAMES),
    run_against=st.sampled_from(RUN_AGAINST),
    superstep=st.integers(0, 5),
    epoch=st.integers(0, 3),
    bump=st.integers(1, 3),
    node_stale=st.sampled_from(NODE_IDS),
    node_fresh=st.sampled_from(NODE_IDS),
    local_stale=st.integers(0, 9),
    local_fresh=st.integers(0, 9),
)
@settings(max_examples=100, deadline=None)
def test_merge_gates_emission_epoch_outranks_same_superstep(
    name, run_against, superstep, epoch, bump, node_stale, node_fresh, local_stale, local_fresh
):
    """Resume regression: a re-executed node keeps its langgraph_step, so the
    re-emitted verdict has the SAME superstep_index — only the bumped
    emission_epoch makes it outrank the stale record. The stale record's
    trailing fields (node_id, local) must never win the comparison, and fold
    order must not change the winner."""
    _install_permissive_verifier()
    stale = mk_gate(
        name=name,
        run_against=run_against,
        status=GateStatus.FAIL,
        seq=Seq(RUN_ID, epoch, superstep, node_stale, local_stale),
    )
    fresh = mk_gate(
        name=name,
        run_against=run_against,
        status=GateStatus.PASS,
        seq=Seq(RUN_ID, epoch + bump, superstep, node_fresh, local_fresh),
    )
    for folded in (
        merge_gates([stale], [fresh]),
        merge_gates([fresh], [stale]),
        merge_gates(merge_gates([stale], None), [fresh]),
        merge_gates(merge_gates(None, [fresh]), [stale]),
    ):
        assert canon(folded) == canon([fresh])


# ---------------------------------------------------------------------------
# 7. merge_constraints: pure keyed union, latest-by-seq per id — supersession
#    is a READ-TIME view (live_constraints), never destructive in the reducer.
#    (Property testing caught the doc pseudocode's "merge then drop" shape
#    breaking associativity/idempotency; see reducers.merge_constraints.)
#    Output cardinality is exactly the number of ids (no silent truncation).
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_merge_constraints_model_single_fold(data):
    a, b = draw_parts(data, "merge_constraints", 2)
    by_id: dict[str, PinnedConstraint] = {}
    for c in a + b:
        if c.id not in by_id or c.seq > by_id[c.id].seq:
            by_id[c.id] = c
    superseded = {c.supersedes for c in by_id.values() if c.supersedes}
    live = [c for cid, c in by_id.items() if cid not in superseded]

    out = merge_constraints(a, b)
    # Reducer retains every id's latest record (tombstones stay as data)...
    assert canon(out) == canon(list(by_id.values()))
    assert canon(merge_constraints(b, a)) == canon(out)
    out_ids = [c.id for c in out]
    assert len(out_ids) == len(set(out_ids)) == len(by_id)
    # ...and the live view applies supersession deterministically.
    assert canon(live_constraints(out)) == canon(live)


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_merge_constraints_supersession_fold_order_irrelevant(data):
    """The LIVE VIEW must hold regardless of how the unordered write set is
    grouped into folds — every grouping must equal the single flat fold."""
    a, b, c = draw_parts(data, "merge_constraints", 3)
    reference = live_constraints(merge_constraints(a + b + c, None))
    groupings = [
        merge_constraints(merge_constraints(a, b), c),
        merge_constraints(a, merge_constraints(b, c)),
        merge_constraints(merge_constraints(c, a), b),
        merge_constraints(c, merge_constraints(b, a)),
    ]
    for g in groupings:
        assert canon(live_constraints(g)) == canon(reference)


# ---------------------------------------------------------------------------
# 8. union / make_union_by_key: output keys are exactly the union of input
#    keys; when seqs differ, the later-by-seq payload wins per key.
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_union_plain_values(data):
    a, b = draw_parts(data, "union", 2)
    out = union(a, b)
    assert len(out) == len(set(out)), "union produced duplicates"
    assert set(out) == set(a) | set(b)


@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_union_by_id_keys_are_union_of_inputs(data):
    a, b = draw_parts(data, "union_by_id", 2)
    out = union_by_id(a, b)
    ids = [t["id"] for t in out]
    assert len(ids) == len(set(ids))
    assert set(ids) == {t["id"] for t in a + b}


@pytest.mark.parametrize(
    "name,key",
    [
        ("union_triage", lambda t: t.union_key()),
        ("union_actions", lambda x: x.idempotency_key),
    ],
    ids=["union_triage", "union_actions"],
)
@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_union_by_key_latest_seq_wins(name, key, data):
    a, b = draw_parts(data, name, 2)
    r = REDUCERS[name]
    expected: dict[object, object] = {}
    for x in a + b:
        k = key(x)
        if k not in expected or x.seq > expected[k].seq:
            expected[k] = x
    out = r(a, b)
    assert {key(x) for x in out} == set(expected)
    assert canon(out) == canon(expected.values())
    assert canon(r(b, a)) == canon(out)
