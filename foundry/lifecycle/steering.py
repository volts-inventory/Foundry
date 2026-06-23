"""Human steering — taste/policy directives become PINNED CONSTRAINTS. (lifecycle.md §2)

In the pipeline humans appear only as gate-approvers; real software needs humans
*steering*. A human injects a directive ("prefer approach B", "descope X",
"no third-party fonts") and it becomes a :class:`PinnedConstraint` injected
VERBATIM (architecture.md §8 — pinned constraints are injected verbatim, never
summarized) that flows into every downstream node.

Two invariants this module enforces:

* **Tighten-only.** Steering can ADD a constraint; it can NEVER remove or relax
  an architecture-pinned one. A directive that contradicts / relaxes an existing
  pinned constraint is REJECTED (a no-op for that directive) — steering tightens
  the constraint set, it does not loosen it (fail-closed). The architecture-pinned
  set is the floor; steering only raises it.
* **Taste is a human input, never model-delegated.** The directives this module
  turns into constraints are HUMAN-authored (the Stakeholder/Client Proxy LLM
  stand-in does NOT supply them). The caller passes human directives; we never
  synthesize taste.

Determinism (L7): :func:`steering_constraints` is a pure function of the
directives + the existing pinned set + a caller-supplied :class:`Seq` (no clock,
no network, no agent code) — equal inputs yield an equal constraint tuple.
"""

from __future__ import annotations

from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq

__all__ = [
    "SteeringDirective",
    "steering_constraints",
]

# Source tag stamped on a steering-derived constraint so its human authorship is
# legible in the pinned set (lifecycle.md §2: steering is authorship).
STEERING_SOURCE = "human-steering"

# A directive whose text RELAXES rather than tightens is a no-op. We detect the
# "remove / drop / allow-again" shape deterministically; the conservative default
# is to ADMIT (tighten) — a false "tighten" costs one extra pinned constraint, a
# false "relax" would silently loosen the floor, so we only reject the explicit
# relax shapes.
_RELAX_VERBS = (
    "remove constraint",
    "drop constraint",
    "relax constraint",
    "unpin",
    "lift the requirement",
    "no longer require",
    "allow again",
)


class SteeringDirective(FrozenModel):
    """A single human steering directive (lifecycle.md §2).

    ``author`` is the HUMAN who injected it (taste is not model-delegated — the
    author is a person, recorded for the author-set / segregation-of-duties
    check). ``targets`` optionally names the pinned-constraint id this directive
    speaks to; a directive that targets an existing pinned constraint AND relaxes
    it is rejected (tighten-only).

    ``scope`` controls how the derived constraint is injected (GLOBAL = every
    node verbatim; LOCAL = relevance-scoped) and defaults to GLOBAL because a
    taste/policy directive is usually cross-cutting.
    """

    directive_id: str  # stable identity of this directive
    text: str  # the human-authored directive, pinned VERBATIM
    author: str  # the HUMAN author (taste is never model-delegated)
    scope: ConstraintScope = ConstraintScope.GLOBAL
    targets: str | None = None  # an existing pinned-constraint id it speaks to


def _is_relaxing(text: str) -> bool:
    """True iff the directive text has an explicit RELAX shape (tighten-only)."""
    low = text.lower()
    return any(verb in low for verb in _RELAX_VERBS)


def steering_constraints(
    directives: tuple[SteeringDirective, ...],
    *,
    existing: tuple[PinnedConstraint, ...] = (),
    seq: Seq,
) -> tuple[PinnedConstraint, ...]:
    """Turn human steering directives into PINNED CONSTRAINTS — tighten-only (§2).

    Each admitted directive becomes a :class:`PinnedConstraint` whose ``text`` is
    the directive's text VERBATIM (architecture.md §8) and whose ``source`` is
    tagged :data:`STEERING_SOURCE` (human authorship is legible). The constraints
    are returned in directive order, each with a distinct ``local`` seq slot.

    A directive is REJECTED (a no-op — it produces no constraint) when it would
    RELAX the floor:

    * it has an explicit relax shape ("remove constraint", "unpin", ...), OR
    * it ``targets`` an id present in ``existing`` (steering may not edit/remove
      an architecture-pinned constraint — pinned constraints are superseded by
      the Architect, never relaxed by a steering directive).

    Steering can only ADD to the pinned set, never subtract — so the returned
    tuple is a (possibly empty) set of NEW constraints layered ON TOP of
    ``existing``; ``existing`` is never mutated or dropped. Equal inputs yield an
    equal tuple (L7).
    """
    existing_ids = {c.id for c in existing}
    out: list[PinnedConstraint] = []
    for i, d in enumerate(directives):
        # Tighten-only: reject an explicit relax, or a directive that targets an
        # already-pinned constraint (it would edit/relax the floor — no-op).
        if _is_relaxing(d.text):
            continue
        if d.targets is not None and d.targets in existing_ids:
            continue
        out.append(
            PinnedConstraint(
                id=f"steer:{d.directive_id}",
                text=d.text,  # injected VERBATIM, never summarized (arch §8)
                scope=d.scope,
                source=f"{STEERING_SOURCE}:{d.author}",  # human authorship legible
                seq=seq._replace(node_id="steering", local=i),
            )
        )
    return tuple(out)
