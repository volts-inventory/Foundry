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


class SystemDesign(FrozenModel):
    """Principal Architect output (agents.md Layer 2): the chosen architecture
    and component decomposition. The full design body (diagrams, data model,
    cross-cutting concerns) is content-addressed (DS9); only the component
    seam names and the body handle live in state. Superseded, never edited."""

    title: str
    design_uri: str  # CAS handle to the full design body (DS9)
    components: tuple[str, ...] = ()  # component decomposition (named seams)
    adr_ids: tuple[str, ...] = ()  # decisions this design rests on
    interface_ids: tuple[str, ...] = ()  # interface contracts this design defines
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class ADR(FrozenModel):
    """Architecture Decision Record (agents.md Layer 2 — Architect SOP writes
    ADRs WITH rejected alternatives). The decision narrative body is
    content-addressed (DS9); `rejected_alternatives` and their rationale are
    pinned downstream verbatim (architecture.md §8), so they stay in state."""

    id: str
    title: str
    decision: str  # the decision in one line; full narrative lives at body_uri
    body_uri: str  # CAS handle to the full ADR body / rationale (DS9)
    rejected_alternatives: tuple[str, ...] = ()  # alternatives considered + rejected
    status: str = "accepted"  # proposed | accepted | superseded
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class InterfaceContract(FrozenModel):
    """A stable interface seam definition (agents.md Layer 2): the API/interface
    contract downstream implementation branches code against. The signature/spec
    body is content-addressed (DS9). Frozen + versioned: a contract change
    arrives only as a supersession, never an in-place edit."""

    name: str  # the seam's stable identity (e.g. "TodoStore.add")
    spec_uri: str  # CAS handle to the full signature / contract body (DS9)
    consumers: tuple[str, ...] = ()  # component/interface ids that depend on this seam
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class OwnershipMap(FrozenModel):
    """File/interface → owner mapping (agents.md Layer 2/3): the Architect
    assigns each path/seam an owner so the Senior Engineer can resolve
    integration conflicts and derive a DETERMINISTIC merge order for
    branch-per-task implementation. Frozen + versioned; superseded, never
    edited. The merge order is a pure function of the mapping (no process
    state), so a LangGraph resume re-derives the identical order."""

    owners: dict[str, str]  # path / interface name -> owner (task or agent id)
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def owner_of(self, unit: str) -> str | None:
        """Owner of a file/interface unit, or None if unowned."""
        return self.owners.get(unit)

    def merge_order(self) -> tuple[str, ...]:
        """Ownership units in a deterministic merge order.

        Ordered by (owner, unit) so units sharing an owner merge together and
        the sequence is a stable, total function of the mapping alone — the
        Senior Engineer's integration order is reproducible across resume
        (agents.md Layer 3; no reliance on dict insertion or process state)."""
        return tuple(sorted(self.owners, key=lambda unit: (self.owners[unit], unit)))
