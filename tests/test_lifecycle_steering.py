"""Human steering — taste/policy directives become PINNED CONSTRAINTS (T-M6-10).

Exercises ``foundry/lifecycle/steering.py``: human directives become pinned
constraints injected verbatim (tighten-only — steering can ADD but never relax
an architecture-pinned constraint), and taste is human-supplied.

* human directives -> pinned constraints injected VERBATIM;
* a directive that would relax an architecture-pinned constraint is rejected/no-op;
* taste authorship is human-supplied (recorded in the source);
* determinism.
"""

from __future__ import annotations

import pytest

from foundry.lifecycle.steering import (
    STEERING_SOURCE,
    SteeringDirective,
    steering_constraints,
)
from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.state.seq import Seq

SEQ = Seq(run_id="run-1", emission_epoch=1, superstep_index=3, node_id="x", local=0)


def _directive(
    directive_id: str = "d1",
    text: str = "prefer approach B for the checkout flow",
    *,
    author: str = "product-owner@org",
    targets: str | None = None,
    scope: ConstraintScope = ConstraintScope.GLOBAL,
) -> SteeringDirective:
    return SteeringDirective(
        directive_id=directive_id, text=text, author=author, targets=targets, scope=scope
    )


def _pinned(cid: str, text: str) -> PinnedConstraint:
    return PinnedConstraint(
        id=cid, text=text, scope=ConstraintScope.GLOBAL, source="ADR-1", seq=SEQ
    )


# ---------------------------------------------------------------------------
# Human directives -> pinned constraints injected VERBATIM
# ---------------------------------------------------------------------------


def test_directive_becomes_pinned_constraint_verbatim():
    out = steering_constraints((_directive(),), seq=SEQ)
    assert len(out) == 1
    c = out[0]
    assert isinstance(c, PinnedConstraint)
    # Injected VERBATIM — never summarized (architecture.md §8).
    assert c.text == "prefer approach B for the checkout flow"
    assert c.scope is ConstraintScope.GLOBAL


def test_taste_authorship_is_human_supplied():
    out = steering_constraints((_directive(author="alice@org"),), seq=SEQ)
    # The human author is recorded on the source — taste is never model-delegated.
    assert out[0].source == f"{STEERING_SOURCE}:alice@org"


def test_multiple_directives_get_distinct_constraints():
    out = steering_constraints(
        (_directive("d1", "no third-party fonts"), _directive("d2", "descope analytics")),
        seq=SEQ,
    )
    assert len(out) == 2
    assert {c.text for c in out} == {"no third-party fonts", "descope analytics"}
    # Distinct ids + seq slots (deterministic ordering).
    assert out[0].id != out[1].id
    assert out[0].seq.local != out[1].seq.local


# ---------------------------------------------------------------------------
# Tighten-only: a directive that relaxes an architecture-pinned constraint is no-op
# ---------------------------------------------------------------------------


def test_directive_targeting_existing_pinned_constraint_is_rejected():
    """Steering may not edit/remove an architecture-pinned constraint (no-op)."""
    existing = (_pinned("c-money", "monetary values are integer micro-USD"),)
    out = steering_constraints(
        (_directive("d1", "loosen the money rule", targets="c-money"),),
        existing=existing,
        seq=SEQ,
    )
    # Rejected -> no new constraint produced; the existing floor is untouched.
    assert out == ()


def test_explicit_relax_shape_is_rejected():
    out = steering_constraints(
        (_directive("d1", "remove constraint c-auth — allow anonymous access"),),
        seq=SEQ,
    )
    assert out == ()


def test_relax_and_tighten_mixed_only_tightens():
    out = steering_constraints(
        (
            _directive("d1", "unpin the logging requirement"),  # relax -> no-op
            _directive("d2", "require WCAG AA contrast"),  # tighten -> kept
        ),
        seq=SEQ,
    )
    assert len(out) == 1
    assert out[0].text == "require WCAG AA contrast"


def test_tightening_directive_targeting_unknown_id_is_kept():
    """Targeting an id NOT in the existing pinned set is a net-new add (kept)."""
    out = steering_constraints(
        (_directive("d1", "prefer server components", targets="c-new"),),
        existing=(_pinned("c-other", "use TypeScript strict mode"),),
        seq=SEQ,
    )
    assert len(out) == 1
    assert out[0].text == "prefer server components"


def test_existing_constraints_are_never_dropped():
    """Steering only ADDS — the returned tuple layers on top of existing."""
    existing = (_pinned("c-1", "no PII in logs"),)
    out = steering_constraints(
        (_directive("d1", "prefer dark mode default"),), existing=existing, seq=SEQ
    )
    # The existing constraint is not in the returned add-set (it is the floor,
    # carried separately), and the add-set never contains a relaxation of it.
    assert all(c.id != "c-1" for c in out)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_steering_constraints_is_deterministic():
    directives = (_directive("d1", "prefer approach B"), _directive("d2", "descope X"))
    assert steering_constraints(directives, seq=SEQ) == steering_constraints(directives, seq=SEQ)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
