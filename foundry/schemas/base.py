"""Shared channel record types. (architecture.md §3)

Design rule (DS9): large bodies NEVER live in state. `Artifact` is a
content-addressed *handle*; the bytes live in the CAS / object store. This is
what keeps checkpoint size bounded and Postgres write-amplification linear in
new data, not total trajectory length.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from foundry.canonical import content_digest
from foundry.state.seq import Seq


class FrozenModel(BaseModel):
    """Immutable record base: artifacts are superseded, never mutated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def content_identity(self) -> str:
        """Content digest over the full canonical record (immutable, cacheable).

        Set/frozenset fields serialize to a list in hash-seed-dependent order, which
        would make the digest non-deterministic across processes (breaking CAS
        addressing + the evidence/WORM chain). They're sorted here so EVERY set-valued
        field is stable by default — a subclass no longer has to remember (GateResult
        historically hand-sorted `served_family`; this generalizes that guard)."""
        d = self.model_dump(mode="json")
        for name, value in self:
            if isinstance(value, (set, frozenset)) and isinstance(d.get(name), list):
                d[name] = sorted(d[name])
        return content_digest(d)


class Artifact(FrozenModel):
    kind: str
    uri: str  # content-addressed store ref — globally unique identity
    produced_by: str
    task_id: str | None = None
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class LogRef(FrozenModel):
    """Handle to out-of-band append-only data (history / large artifacts)."""

    uri: str
    count: int = 0
    head_seq: int = 0


class IdempotencyPath(StrEnum):
    """The three external-effect idempotency paths, in priority order. (arch §5)"""

    CLIENT_KEY = "client_key"  # external system stores the key; resume re-queries
    EMBEDDED_MARKER = "embedded_marker"  # key embedded (branch/tag/PR marker); re-derive
    FOUNDRY_DEDUP = "foundry_dedup"  # local dedup row; ONLY for tolerable effects


class ExternalAction(FrozenModel):
    """A requested external effect; the outbox is the only durable-effect surface."""

    idempotency_key: str
    kind: str  # "git_push" | "open_pr" | "tag" | "deploy" | "notify" | ...
    payload_uri: str  # CAS handle to the payload (bodies out-of-band)
    path: IdempotencyPath
    # Path-3 is reserved for duplicate-or-omission-tolerable effects, never
    # anything a gate/release/deploy depends on (arch §5, DS-R9-F4):
    gate_bearing: bool = False
    seq: Seq


class BudgetLedger(FrozenModel):
    """Single-writer (Budget Controller). Meters token AND compute cost.

    Monetary values in integer micro-USD — never floats (a pinned global
    constraint class; architecture.md §8).
    """

    tokens_in: int = 0
    tokens_out: int = 0
    compute_ms: int = 0
    spend_microusd: int = 0
    cap_microusd: int = 0
    fix_attempts_since_all_green: int = 0  # global convergence bound (arch §7)
    fix_attempts_total: int = 0  # build-side fixes this run (fix_cap); a resumed rerun starts at 0
