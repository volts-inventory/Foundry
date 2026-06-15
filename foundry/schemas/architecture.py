"""Architecture-layer artifacts (Phase 0 subset: pinned constraints).

The full SystemDesign/ADR/InterfaceContract models land with the
architecture agents (Phase 1); `PinnedConstraint` is here now because the
state backbone's `_merge_constraints` reducer is typed against it.
"""

from __future__ import annotations

import re
from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class ConstraintScope(StrEnum):
    GLOBAL = "global"  # always injected verbatim into every node (arch §8)
    LOCAL = "local"  # relevance-scoped under the per-node injection budget


# Deterministic cross-cutting pattern check: a constraint matching these that
# is NOT marked global is a validated-classification mismatch -> HITL trip
# (arch §8, AI-R10-F1). Patterns are deliberately broad; false positives cost
# one HITL look, false negatives cost a scoping miss on a security invariant.
_CROSS_CUTTING = re.compile(
    r"\b(money|monetary|cents|currency|PII|personal data|logging|logs?|auth(entication|orization)?|"
    r"crypto(graphy)?|secret|credential|token)\b",
    re.IGNORECASE,
)


class PinnedConstraint(FrozenModel):
    """Injected VERBATIM, never summarized; superseded, never edited."""

    id: str
    text: str
    scope: ConstraintScope
    source: str  # ADR / criterion / ownership-map ref
    supersedes: str | None = None
    seq: Seq

    def misclassified_local(self) -> bool:
        """True when text matches cross-cutting patterns but scope is local."""
        return self.scope is ConstraintScope.LOCAL and bool(_CROSS_CUTTING.search(self.text))
