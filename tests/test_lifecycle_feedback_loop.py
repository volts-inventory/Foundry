"""Product-feedback loop — production signal RE-ENTERS via maintenance (T-M6-10).

Exercises ``foundry/lifecycle/feedback_loop.py``: a production signal becomes a
backlog item that re-enters the factory as a MAINTAINED change (linked to the
released artifact's provenance), and a regression re-enters via the maintenance
re-gate path — never bypassing the gates.

* a production signal -> a backlog item linked to the released artifact's provenance;
* a regression re-enters via the maintenance re-gate path (doesn't bypass gates);
* every kind routes to maintenance with re_gate_required True (fail-closed);
* determinism.
"""

from __future__ import annotations

import pytest

from foundry.lifecycle.feedback_loop import (
    MAINTENANCE_ROUTE,
    BacklogItem,
    ProductSignal,
    SignalKind,
    feedback_to_backlog,
)


def _signal(kind: SignalKind = SignalKind.DEFECT) -> ProductSignal:
    return ProductSignal(
        signal_id="sig-1",
        kind=kind,
        released_subject_uri="cas://sha256/v1",
        provenance_uri="cas://prov/v1",
        summary="checkout 500s under load",
        detail_uri="cas://telemetry/1",
    )


# ---------------------------------------------------------------------------
# A production signal -> a backlog item linked to the released artifact's provenance
# ---------------------------------------------------------------------------


def test_signal_becomes_backlog_item_linked_to_provenance():
    item = feedback_to_backlog(_signal())
    assert isinstance(item, BacklogItem)
    # Linked to the affected released artifact's provenance (the maintenance-chain
    # anchor) so the maintained change re-enters against the right prior version.
    assert item.prior_subject_uri == "cas://sha256/v1"
    assert item.provenance_uri == "cas://prov/v1"
    assert item.origin_signal_id == "sig-1"
    assert item.kind is SignalKind.DEFECT
    assert item.summary == "checkout 500s under load"
    assert item.detail_uri == "cas://telemetry/1"


def test_backlog_item_reenters_via_maintenance_route():
    item = feedback_to_backlog(_signal())
    # The closed loop: it re-enters via the maintenance route, never a side door.
    assert item.route == MAINTENANCE_ROUTE
    assert item.re_gate_required is True
    assert item.reenters_via_regate()


# ---------------------------------------------------------------------------
# A regression re-enters via the maintenance re-gate path (doesn't bypass gates)
# ---------------------------------------------------------------------------


def test_regression_reenters_via_regate_path():
    item = feedback_to_backlog(_signal(SignalKind.REGRESSION))
    assert item.kind is SignalKind.REGRESSION
    assert item.is_regression is True
    # A regression is the canonical "re-prove it" trigger — it MUST re-gate, not
    # bypass the gates.
    assert item.re_gate_required is True
    assert item.route == MAINTENANCE_ROUTE
    assert item.reenters_via_regate()


@pytest.mark.parametrize("kind", list(SignalKind))
def test_every_signal_kind_requires_the_regate(kind: SignalKind):
    """No signal kind bypasses the gates — re_gate_required is universal."""
    item = feedback_to_backlog(_signal(kind))
    assert item.re_gate_required is True
    assert item.route == MAINTENANCE_ROUTE
    assert item.reenters_via_regate()
    # Only an actual regression is marked is_regression.
    assert item.is_regression == (kind is SignalKind.REGRESSION)


def test_non_regression_is_not_marked_regression():
    item = feedback_to_backlog(_signal(SignalKind.FEATURE_REQUEST))
    assert item.is_regression is False
    assert item.re_gate_required is True  # still re-gates


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_feedback_to_backlog_is_deterministic():
    sig = _signal(SignalKind.REGRESSION)
    assert feedback_to_backlog(sig) == feedback_to_backlog(sig)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
