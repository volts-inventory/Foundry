"""`JournalEntry` — strictly ADVISORY per-agent work journal. (plan [11])

The journal is **observability only**. A node records a short, structured note
about how its turn went (what it decided, what it assumed, what worried it) for
an operator reading the run's event log.

HARD CONSTRAINT (the whole point of [11]): a ``JournalEntry`` is NEVER a trust
input. It lives on its OWN state channel (``agent_journal``), folded by a plain
union reducer — it is read by no gate, by ``merge_gates``, by ``admission``, by
``route_after_gates``, or by any verdict path. Agents propose; only signed gates
decide (LEARNINGS L5). This is the SAME posture as :class:`GateTriage` (advisory
remediation notes), but the journal is even weaker: triage at least names a gate
it advises on; the journal names nothing a verdict can key off. Structurally,
nothing downstream of the journal channel can turn an entry into a decision.

Like every channel record it is a frozen, content-addressed model carrying a
causal-order :class:`Seq`; the union reducer keys on a stable per-entry identity
so a LangGraph resume re-fold coalesces the re-emission (idempotent, L7 — the
key derives from ``content_digest``, never builtin ``hash()``).
"""

from __future__ import annotations

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class JournalEntry(FrozenModel):
    """An agent's advisory note about its turn. NOT a trust input (see module doc).

    All narrative fields are derived from data the node already has (no extra
    model call by default — token cost is zero); a richer narrative is opt-in
    and cost-gated by the caller, never on by default.
    """

    node_id: str  # which agent recorded this (e.g. "principal_architect")
    role: str  # the agent's human role label (e.g. "Principal Architect")
    summary: str  # one-line rationale / what the turn did
    assumptions: tuple[str, ...] = ()  # what the agent took for granted
    concerns: tuple[str, ...] = ()  # what worried it (advisory only)
    confidence: float | None = None  # optional self-reported confidence in [0,1]
    seq: Seq

    def union_key(self) -> tuple[str, str]:
        """Stable per-entry identity for the advisory ``agent_journal`` union.

        Keyed on ``(node_id, content_identity())``: distinct notes from a node
        coexist (additive), while a byte-identical re-emission on resume folds to
        a single record (idempotent re-fold). ``content_identity()`` spans the
        whole canonical record — including ``seq`` — so two notes that differ
        only by ``seq`` (a genuine re-run with a fresh epoch) are distinct
        identities and BOTH retained, which is correct for an append-only
        observability log.
        """
        return (self.node_id, self.content_identity())
