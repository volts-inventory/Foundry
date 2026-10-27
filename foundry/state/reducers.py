"""Channel reducers — associative, commutative, None-safe. (architecture.md §3)

LangGraph folds pending writes as an UNORDERED set, so ordering must come
from data (`Seq`), never iteration order. Every reducer here is:

- **None-safe** — either side may be None;
- **commutative + associative** — fold order can't change the result;
- **pure** — `_merge_gates` READS the global cross-fork record set
  (nonce consumption, negative latch) as committed input via a read-only
  view; it NEVER mutates it, so a LangGraph re-fold on resume is idempotent.

Efficiency notes (deliberate deviations from the doc pseudocode, same
semantics):

1. The doc's ``(a or []) + (b or [])`` materializes an intermediate
   concatenated list on every fold; we iterate both sides directly.
2. The doc pseudocode re-verifies every attestation/binding signature on
   every fold — O(records × supersteps) signature checks over immutable
   records. Signature validity depends only on record content (+ key/map
   revision), so verifiers are expected to MEMOIZE by content identity
   (see gates/attestation.py). The *nonce-consumed* and *latch* reads are
   the mutable committed inputs and are never cached across folds.
3. Verifier wiring uses a contextvar set per superstep by the runtime;
   the default is fail-closed: with no verifier configured, every
   deterministic-gate or judgment write is dropped (never trusted).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from typing import Protocol, TypeVar

from foundry.schemas.architecture import PinnedConstraint
from foundry.schemas.base import ExternalAction
from foundry.schemas.gates import GateResult, GateTriage
from foundry.schemas.governance import SignOffRecord
from foundry.schemas.journal import JournalEntry

# Gate names whose verdicts MUST carry a valid zone-B runner attestation.
# security/audit/supply_chain/e2e are no longer run but stay listed: older builds
# persisted signed verdicts under them, and dropping a name here would let an
# unsigned write under it into the channel (and the release manifest).
DETERMINISTIC_GATES = frozenset(
    {"spec", "design", "build", "test", "security", "audit", "supply_chain", "e2e"}
)
# Gate names that are judgment verdicts (class 4/5) — binding-gated, latch-aware.
JUDGMENT_VERDICTS = frozenset({"review", "threat_review", "release"})


class GateWriteVerifier(Protocol):
    """Everything `_merge_gates` needs to admit a gate write.

    Implementations: gates/attestation.py (dev-grade HMAC; memoized) backed
    by a `GlobalRecordsView` snapshot for the two mutable reads.
    """

    def verify_attestation(self, g: GateResult) -> bool: ...

    def verify_judgment_binding(self, g: GateResult) -> bool: ...

    def nonce_consumed_by_other_verdict(self, g: GateResult) -> bool: ...

    def negative_verdict_latched(self, g: GateResult) -> bool: ...


class _RejectAll:
    """Fail-closed default: no configured verifier => no trusted writes."""

    def verify_attestation(self, g: GateResult) -> bool:
        return False

    def verify_judgment_binding(self, g: GateResult) -> bool:
        return False

    def nonce_consumed_by_other_verdict(self, g: GateResult) -> bool:
        return True

    def negative_verdict_latched(self, g: GateResult) -> bool:
        return True


_verifier: ContextVar[GateWriteVerifier] = ContextVar("gate_write_verifier")
_REJECT_ALL = _RejectAll()


def set_gate_write_verifier(v: GateWriteVerifier):
    """Install the verifier for the current execution context (per superstep).

    Returns the contextvar token; pass it to :func:`reset_gate_write_verifier`
    to restore the previous verifier (e.g. test teardown).
    """
    return _verifier.set(v)


def reset_gate_write_verifier(token) -> None:
    """Restore the verifier the matching :func:`set_gate_write_verifier` replaced."""
    _verifier.reset(token)


def _active_verifier() -> GateWriteVerifier:
    return _verifier.get(_REJECT_ALL)


T = TypeVar("T")


def _iter(a: Iterable[T] | None, b: Iterable[T] | None) -> Iterable[T]:
    if a:
        yield from a
    if b:
        yield from b


# ---- generic keyed unions ---------------------------------------------------


def union(a: list[T] | None, b: list[T] | None) -> list[T]:
    """Deduped order-stable-by-key union for plain values (e.g. capabilities)."""
    out: dict[T, None] = {}
    for x in _iter(a, b):
        out[x] = None
    return list(out)


def make_union_by_key(
    key: Callable[[T], object],
) -> Callable[[list[T] | None, list[T] | None], list[T]]:
    """Keyed set-union; later-by-Seq wins when both carry a `seq` attribute,
    otherwise first-keyed wins deterministically by key identity (commutative
    only for identical payloads — which is what content-keyed channels emit).
    """

    def _merge(a: list[T] | None, b: list[T] | None) -> list[T]:
        m: dict[object, T] = {}
        for x in _iter(a, b):
            k = key(x)
            prev = m.get(k)
            if prev is None:
                m[k] = x
            else:
                ps, xs = getattr(prev, "seq", None), getattr(x, "seq", None)
                if ps is not None and xs is not None and xs > ps:
                    m[k] = x
        return list(m.values())

    return _merge


def _task_key(d: dict[str, object]) -> object:  # tasks: list[dict] keyed by id
    return d["id"]


def _action_key(a: ExternalAction) -> object:
    return a.idempotency_key


def _sign_off_key(s: SignOffRecord) -> object:
    """Stable per-record identity for the Gated-mode sign-off union (gov §1e/§6).

    A `SignOffRecord` carries no natural domain key, so dedup is by full
    content identity (`content_identity()` — a digest over the whole canonical
    record, INCLUDING `seq` and `judgment_provenance`, AS4). Two sign-offs that
    differ in any field (approver/role/state_sha/presented_evidence_hash/
    self-judge profile/seq) are DISTINCT records and coexist; re-folding the
    SAME record (a LangGraph resume re-fold) coalesces because the key is a pure
    function of the record and the per-key payload is byte-identical (L8). A
    sign-off is ALWAYS recorded (recorded sign-off, no-op HITL in the self-judge
    profile), never dropped — there is no admission/trust gate on this channel.
    """
    return s.content_identity()


union_by_id = make_union_by_key(_task_key)
union_triage = make_union_by_key(GateTriage.union_key)
union_actions = make_union_by_key(_action_key)
union_sign_offs = make_union_by_key(_sign_off_key)

# Advisory `agent_journal` channel (plan [11]). Mirrors `union_triage` EXACTLY:
# an additive, order-independent, re-fold-idempotent keyed union. It is a SEPARATE
# channel from gates/artifacts and is read by NO gate, by `merge_gates`, by
# `route_after_gates`, or by `admission` — the journal can never be a trust input.
# (`union_key` keys on content identity, so a resume re-fold of a byte-identical
# entry coalesces; genuinely distinct notes coexist.)
union_journal = make_union_by_key(JournalEntry.union_key)


# ---- artifacts: set-union by content address --------------------------------


def merge_by_uri(a, b):
    m = {}
    for x in _iter(a, b):
        m[x.uri] = x
    return list(m.values())


# ---- pinned constraints: supersession, never silent truncation --------------


def merge_constraints(
    a: list[PinnedConstraint] | None, b: list[PinnedConstraint] | None
) -> list[PinnedConstraint]:
    """Union by id, latest by seq — supersession is NOT applied here.

    Property testing caught a real algebra bug in the doc pseudocode's
    "merge then drop superseded" shape: removal is destructive at
    intermediate folds, so the supersedes-edge of a retired record vanishes
    and a later fold resurrects what it had retired — breaking associativity
    and re-fold idempotency (a LangGraph resume re-fold would change
    committed state). The reducer therefore stays a pure keyed union
    (superseded records are retained as tombstone-bearing data) and the
    live set is a deterministic READ-TIME view: `live_constraints()`.

    The cardinality bound is also NOT enforced here — silent truncation is
    forbidden (arch §8); the cardinality monitor escalates to HITL at 90%.
    """
    by_id: dict[str, PinnedConstraint] = {}
    for c in _iter(a, b):
        prev = by_id.get(c.id)
        if prev is None or c.seq > prev.seq:
            by_id[c.id] = c
    return list(by_id.values())


def live_constraints(merged: list[PinnedConstraint]) -> list[PinnedConstraint]:
    """The current-truth view used for context injection (arch §8).

    A constraint is retired iff the LATEST record of some id names it in
    `supersedes` — supersession is itself supersedable (re-emitting an ADR
    without the edge withdraws the retirement), deterministically, because
    the reducer retains every id's latest record.
    """
    superseded = {c.supersedes for c in merged if c.supersedes}
    return [c for c in merged if c.id not in superseded]


# ---- gates: attestation-gated, latch-aware, latest-by-Seq per key ------------


def merge_gates(a: list[GateResult] | None, b: list[GateResult] | None) -> list[GateResult]:
    """Latest-by-`seq` per (name, run_against) — with the trust checks.

    NOT pure max-by-seq for judgment verdicts: the negative-verdict latch
    (read from the global cross-fork record set) means a request-changes
    cannot be superseded by re-emitting an older approve. Deterministic-gate
    records are accepted ONLY if runner-attested; judgment verdicts only if
    the binding validates and the nonce isn't consumed by a different
    content identity. Rejected writes are dropped silently by design — an
    unsigned agent write must not be able to wedge the channel either.
    """
    v = _active_verifier()
    m: dict[tuple[str, str], GateResult] = {}
    for g in _iter(a, b):
        if g.name in DETERMINISTIC_GATES and not v.verify_attestation(g):
            continue
        if g.name in JUDGMENT_VERDICTS:
            if not v.verify_judgment_binding(g):
                continue  # forged / replayed / status-flipped binding
            # These two are READS of the cross-fork ledger; the WRITE side is the
            # trusted judgment-commit step `_commit_judgment_ledger` (graph/supervisor.py,
            # H3): after a judgment verdict survives this filter it consumes the
            # verdict's single-use nonce (keyed to content identity) and latches a
            # request-changes. Each verdict carries its OWN per-(node,subject,state)
            # nonce (quality/_common.server_issued_nonce), so the two reviews a run
            # emits don't collide. This reducer stays PURE (read-only); the supervisor
            # holds the records and does the write.
            if v.nonce_consumed_by_other_verdict(g):
                continue  # single-use nonce replayed by a different verdict
            if v.negative_verdict_latched(g):
                continue  # latched request-changes; only a fresh re-review releases
        k = (g.name, g.run_against)
        prev = m.get(k)
        if prev is None or g.seq > prev.seq:
            m[k] = g
    return list(m.values())
