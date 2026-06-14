"""Pinned-constraint injection: verbatim, bounded, scoped. (architecture.md §8)

Contract:

- **Globals always injected.** `scope: global` constraints are injected
  VERBATIM into every node regardless of relevance scoring — they have the
  weakest call/def-graph signal, so they are never subject to a scoping miss.
- **Locals are relevance-scoped** to the node's task scope under the per-node
  injection token budget. The scorer is deterministic (token-set overlap, no
  embeddings) and pluggable via the `Scorer` protocol — this is the measured
  seam: any scorer that plugs in here must clear the labeled
  (task, relevant-constraint) **recall floor of evaluation.md §4** before it
  gates real runs. The default scorer is the dev-grade baseline, not the bar.
- **Misclassification check (AI-R10-F1).** Any local constraint whose text
  matches the deterministic cross-cutting patterns
  (`PinnedConstraint.misclassified_local()`) is escalated to HITL *and*
  fail-safe injected as if global — the blast radius of an Architect scoping
  error is bounded to narrowly-local constraints.
- **Cardinality monitor.** Token use is estimated with the documented
  `len(text) // 4` heuristic (`estimate_tokens`). At >= 90% of the budget an
  escalation of kind ``"constraint-cardinality"`` is emitted. The set is
  NEVER silently truncated: when locals do not fit, the lowest-scored are
  excluded and **every exclusion is recorded in `warnings` with the
  constraint id**; globals always fit or escalate hard (injected anyway —
  truncating a global invariant is never an option).
- **Supersession is a read-time view**: `live_constraints()` is applied
  first, so retired constraints are never injected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.state.reducers import live_constraints

# Fraction of the per-node budget at which the cardinality monitor escalates.
CARDINALITY_ESCALATION_FRACTION = 0.9

_WORD = re.compile(r"\w+")


def estimate_tokens(text: str) -> int:
    """Documented heuristic: ~4 chars/token (`len(text) // 4`).

    Deliberately crude — the monitor escalates at 90% precisely so the
    estimate's error margin is absorbed by the headroom, and exclusions are
    visible (warnings), never silent.
    """
    return len(text) // 4


class NeedContext(Exception):
    """Typed "insufficient context" escape. (architecture.md §8)

    A node returns (or raises) this instead of hallucinating over a partial
    view; the orchestrator re-retrieves with `query` or escalates. Retrieval
    failures are detected, not silently tolerated.
    """

    def __init__(self, query: str) -> None:
        super().__init__(query)
        self.query = query


def need_context(query: str) -> NeedContext:
    """Build the typed escape a node can `return` (or `raise`)."""
    return NeedContext(query)


class Scorer(Protocol):
    """Pluggable relevance scorer for LOCAL constraints.

    The measured-recall seam (evaluation.md §4): a replacement scorer
    (lexical, graph-aware, embedding-backed, ...) must be deterministic for
    a given input and must clear the labeled recall floor before deployment.
    Higher score == more relevant to the node's task scope.
    """

    def score(self, constraint: PinnedConstraint, task_scope: tuple[str, ...]) -> float: ...


class TokenOverlapScorer:
    """Deterministic token-set overlap between constraint text/source and scope.

    Jaccard overlap of lowercase word tokens — no embeddings, no model calls,
    bitwise reproducible. This is the dev-grade baseline behind the `Scorer`
    protocol; it must still be measured against the eval §4 recall floor.
    """

    @staticmethod
    def _tokens(*texts: str) -> frozenset[str]:
        out: set[str] = set()
        for t in texts:
            out.update(m.group(0).lower() for m in _WORD.finditer(t))
        return frozenset(out)

    def score(self, constraint: PinnedConstraint, task_scope: tuple[str, ...]) -> float:
        c_tokens = self._tokens(constraint.text, constraint.source)
        s_tokens = self._tokens(*task_scope)
        if not c_tokens or not s_tokens:
            return 0.0
        union = c_tokens | s_tokens
        return len(c_tokens & s_tokens) / len(union)


@dataclass(frozen=True)
class Escalation:
    """HITL escalation entry emitted by the injector."""

    kind: str  # "constraint-cardinality" | "misclassified-local"
    constraint_id: str | None
    detail: str


@dataclass(frozen=True)
class InjectionResult:
    """What a node actually receives, plus the visibility channels."""

    global_block: str  # VERBATIM concatenation of all effective-global constraints
    local_block: str  # relevance-scoped locals under the remaining budget
    warnings: list[str] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)


def _ordered(constraints: list[PinnedConstraint]) -> list[PinnedConstraint]:
    """Deterministic injection order: causal order (`Seq`), id tie-break."""
    return sorted(constraints, key=lambda c: (c.seq, c.id))


class ConstraintInjector:
    """Assembles the pinned-constraint blocks for one node. (arch §8)"""

    def __init__(self, token_budget_per_node: int, scorer: Scorer | None = None) -> None:
        if token_budget_per_node <= 0:
            raise ValueError("token_budget_per_node must be positive")
        self.token_budget_per_node = token_budget_per_node
        self.scorer: Scorer = scorer if scorer is not None else TokenOverlapScorer()

    def assemble(
        self,
        merged_constraints: list[PinnedConstraint],
        node_task_scope: tuple[str, ...] | list[str],
    ) -> InjectionResult:
        scope_strings = tuple(node_task_scope)
        warnings: list[str] = []
        escalations: list[Escalation] = []

        # 1. Supersession is a read-time view — apply it first.
        live = live_constraints(merged_constraints)

        # 2. Partition. Misclassified locals (AI-R10-F1) escalate AND are
        #    fail-safe injected as if global.
        effective_global: list[PinnedConstraint] = []
        locals_: list[PinnedConstraint] = []
        for c in live:
            if c.scope is ConstraintScope.GLOBAL:
                effective_global.append(c)
            elif c.misclassified_local():
                effective_global.append(c)
                escalations.append(
                    Escalation(
                        kind="misclassified-local",
                        constraint_id=c.id,
                        detail=(
                            f"constraint {c.id!r} matches cross-cutting patterns but is "
                            "scoped local; injected as global (fail-safe), HITL review "
                            "required (AI-R10-F1)"
                        ),
                    )
                )
            else:
                locals_.append(c)

        # 3. Globals: ALWAYS injected verbatim, regardless of scoring. They
        #    fit or escalate hard — never truncated.
        effective_global = _ordered(effective_global)
        global_block = "\n\n".join(c.text for c in effective_global)
        global_tokens = sum(estimate_tokens(c.text) for c in effective_global)
        if global_tokens > self.token_budget_per_node:
            escalations.append(
                Escalation(
                    kind="constraint-cardinality",
                    constraint_id=None,
                    detail=(
                        f"GLOBAL constraints alone ({global_tokens} est. tokens) exceed the "
                        f"per-node budget ({self.token_budget_per_node}); injected anyway — "
                        "globals are never truncated — HITL must re-bound the set"
                    ),
                )
            )

        # 4. Locals: deterministic relevance order, greedy under the
        #    remaining budget. Every exclusion is visible, never silent.
        remaining = max(0, self.token_budget_per_node - global_tokens)
        ranked = sorted(
            locals_,
            key=lambda c: (-self.scorer.score(c, scope_strings), c.seq, c.id),
        )
        included: list[PinnedConstraint] = []
        used = 0
        for c in ranked:
            cost = estimate_tokens(c.text)
            if used + cost <= remaining:
                included.append(c)
                used += cost
            else:
                warnings.append(
                    f"excluded local constraint {c.id!r} "
                    f"(score={self.scorer.score(c, scope_strings):.4f}, "
                    f"est_tokens={cost}): per-node injection budget exhausted"
                )
        local_block = "\n\n".join(c.text for c in _ordered(included))

        # 5. Cardinality monitor: escalate when the injected set approaches
        #    (>= 90% of) the budget.
        injected_tokens = global_tokens + used
        threshold = CARDINALITY_ESCALATION_FRACTION * self.token_budget_per_node
        if injected_tokens >= threshold:
            escalations.append(
                Escalation(
                    kind="constraint-cardinality",
                    constraint_id=None,
                    detail=(
                        f"injected constraint set at {injected_tokens} est. tokens — "
                        f">= 90% of the per-node budget ({self.token_budget_per_node}); "
                        "HITL should re-scope or raise the budget before silent pressure "
                        "starts excluding relevant locals"
                    ),
                )
            )

        return InjectionResult(
            global_block=global_block,
            local_block=local_block,
            warnings=warnings,
            escalations=escalations,
        )
