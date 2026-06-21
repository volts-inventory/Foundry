"""EXIT proof for trusted-component availability + per-lineage capacity
reservation (operations.md §4; foundry/operations/trusted_component.py).

Two load-bearing properties:

* **Outage → PAUSED, not WEDGED (fail-closed).** A trusted-component outage
  degrades the affected run to PAUSED(reason=trusted_component_unavailable) —
  asserted NOT WEDGED, not alertable, $0/hr — and the dependent judgment/gate
  does NOT proceed. Recovery resumes (RUNNING). This is the EXIT round-trip.
* **Per-lineage reservation protects against starvation.** One lineage
  saturating demand cannot consume another lineage's reserved capacity slice.

Plus determinism: the same inputs yield byte-identical records/tables.
"""

from __future__ import annotations

import pytest

from foundry.operations.trusted_component import (
    AvailabilityModel,
    CapacityReservation,
    ComponentStatus,
    LineageReservation,
    TrustedComponent,
    classify_for_dependencies,
    on_recovery,
    on_unavailable,
)
from foundry.schemas.operations import PauseReason, RunStatus
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="judge", local=0)


# ---------------------------------------------------------------------------
# Outage → PAUSED, not WEDGED (fail-closed) + recovery resumes
# ---------------------------------------------------------------------------


def test_outage_degrades_to_paused_not_wedged() -> None:
    rec = on_unavailable(
        run_id="r1",
        current_phase="quality",
        component=TrustedComponent.MODEL_PROVIDER_PROXY,
        seq=SEQ,
    )
    # Fail-closed, not stuck: PAUSED with the §4 reason, explicitly NOT WEDGED.
    assert rec.status is RunStatus.PAUSED
    assert rec.status is not RunStatus.WEDGED
    assert rec.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    # A paused run is alive, $0/hr, and NOT alerted.
    assert rec.cost_rate_microusd_per_hr == 0
    assert rec.is_alertable() is False
    # It names who/what it waits on so the operator can read it.
    assert "model_provider_proxy" in rec.awaiting


def test_dependent_node_does_not_proceed_while_component_unavailable() -> None:
    """The dependent judgment/gate node must NOT proceed on outage — it pauses
    (fail-closed), and the precondition check reflects that."""
    model = AvailabilityModel().with_status(
        ComponentStatus(
            component=TrustedComponent.ZONE_B_SIGNER,
            available=False,
            detail="signer outage",
        )
    )
    deps = (TrustedComponent.MODEL_PROVIDER_PROXY, TrustedComponent.ZONE_B_SIGNER)
    # The node's go/no-go precondition: a dependency is down → may NOT proceed.
    assert model.all_available(deps) is False

    rec = classify_for_dependencies(
        model,
        run_id="r1",
        current_phase="quality",
        dependencies=deps,
        seq=SEQ,
        running_cost_rate_microusd_per_hr=5_000,
    )
    # Did NOT proceed (would have been RUNNING): it is PAUSED, fail-closed.
    assert rec.status is RunStatus.PAUSED
    assert rec.status is not RunStatus.WEDGED
    assert rec.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    assert rec.cost_rate_microusd_per_hr == 0  # paused ⇒ $0/hr despite a running rate
    assert "signer outage" in rec.awaiting


def test_outage_then_recovery_resumes() -> None:
    """EXIT property round-trip: outage → PAUSED → recovery → RUNNING."""
    deps = (TrustedComponent.GATE_RUNNER,)
    outage = AvailabilityModel().with_status(
        ComponentStatus(component=TrustedComponent.GATE_RUNNER, available=False)
    )
    paused = classify_for_dependencies(
        outage, run_id="r1", current_phase="build_test_gate", dependencies=deps, seq=SEQ
    )
    assert paused.status is RunStatus.PAUSED

    # Component recovers: queue-and-drain on recovery → RUNNING.
    recovered = AvailabilityModel().with_status(
        ComponentStatus(component=TrustedComponent.GATE_RUNNER, available=True)
    )
    assert recovered.all_available(deps) is True
    resumed = classify_for_dependencies(
        recovered,
        run_id="r1",
        current_phase="build_test_gate",
        dependencies=deps,
        seq=SEQ,
        running_cost_rate_microusd_per_hr=7_000,
    )
    assert resumed.status is RunStatus.RUNNING
    assert resumed.status is not RunStatus.WEDGED
    assert resumed.pause_reason is None
    assert resumed.cost_rate_microusd_per_hr == 7_000


def test_on_recovery_returns_running() -> None:
    rec = on_recovery(
        run_id="r1", current_phase="quality", seq=SEQ, cost_rate_microusd_per_hr=1_000
    )
    assert rec.status is RunStatus.RUNNING
    assert rec.pause_reason is None
    assert rec.is_alertable() is False


def test_unmodeled_component_defaults_available() -> None:
    model = AvailabilityModel()
    assert model.is_available(TrustedComponent.MODEL_PROVIDER_PROXY) is True
    assert model.unavailable() == ()


def test_unavailable_lists_outages_in_enum_order() -> None:
    model = (
        AvailabilityModel()
        .with_status(
            ComponentStatus(component=TrustedComponent.GATE_RUNNER, available=False)
        )
        .with_status(
            ComponentStatus(
                component=TrustedComponent.MODEL_PROVIDER_PROXY, available=False
            )
        )
    )
    # Deterministic enum order, regardless of insertion order.
    assert model.unavailable() == (
        TrustedComponent.MODEL_PROVIDER_PROXY,
        TrustedComponent.GATE_RUNNER,
    )


# ---------------------------------------------------------------------------
# Per-lineage reservation protects against starvation
# ---------------------------------------------------------------------------


def test_one_lineage_cannot_consume_another_lineages_reserved_slice() -> None:
    """The anti-starvation invariant: lineage-2 saturating demand cannot reach
    into lineage-3's reserved capacity."""
    table = (
        CapacityReservation()
        .reserve("anthropic-family-2", reserved=2)
        .reserve("anthropic-family-3", reserved=2)
    )  # no shared burst pool ⇒ strictly partitioned

    # Family-2 bursts hard: saturates its OWN slice (2 admits succeed).
    admitted = 0
    for _ in range(5):
        table, ok = table.admit_for_lineage("anthropic-family-2", 1)
        if ok:
            admitted += 1
    assert admitted == 2  # exactly its reserved slice, no more
    # Its slice is now exhausted; further demand is refused (would queue).
    assert table.can_admit("anthropic-family-2", 1) is False

    # Family-3's RESERVED slice is fully intact — protected from family-2's burst.
    assert table.for_lineage("anthropic-family-3").available == 2
    assert table.can_admit("anthropic-family-3", 2) is True
    table, ok = table.admit_for_lineage("anthropic-family-3", 2)
    assert ok is True
    assert table.for_lineage("anthropic-family-3").available == 0


def test_refused_admission_leaves_table_unchanged() -> None:
    table = CapacityReservation().reserve("fam-2", reserved=1)
    table, ok = table.admit_for_lineage("fam-2", 1)
    assert ok is True
    before = table
    after, ok2 = table.admit_for_lineage("fam-2", 1)  # over the slice, no burst
    assert ok2 is False
    assert after is before  # unchanged on refusal — queue, not a partial draw


def test_shared_burst_pool_is_shared_but_reserved_slices_are_not() -> None:
    """Burst is a SHARED un-reserved pool any lineage may draw; reserved slices
    stay private. A lineage spills onto burst only after its own slice."""
    table = CapacityReservation(burst=3).reserve("fam-2", reserved=1).reserve(
        "fam-3", reserved=1
    )
    # fam-2 draws its 1 reserved, then 2 from the shared burst (total 3).
    table, ok = table.admit_for_lineage("fam-2", 3)
    assert ok is True
    assert table.for_lineage("fam-2").available == 0
    assert table.burst_available == 1  # 3 burst - 2 spilled = 1 left
    # fam-3's reserved slice is untouched by fam-2's burst spill.
    assert table.for_lineage("fam-3").available == 1
    # fam-3 can still take its reserved 1 + the last shared burst unit.
    assert table.can_admit("fam-3", 2) is True
    table, ok = table.admit_for_lineage("fam-3", 2)
    assert ok is True
    assert table.burst_available == 0


def test_admit_draws_reserved_before_burst() -> None:
    table = CapacityReservation(burst=2).reserve("fam-2", reserved=2)
    table, ok = table.admit_for_lineage("fam-2", 1)
    assert ok is True
    # Drew from the reserved slice first, not the shared burst.
    assert table.for_lineage("fam-2").available == 1
    assert table.burst_available == 2


def test_undeclared_lineage_has_no_reserved_quota_only_burst() -> None:
    table = CapacityReservation(burst=1)
    # An undeclared lineage has zero protected quota — only the shared burst.
    assert table.for_lineage("unknown").reserved == 0
    assert table.can_admit("unknown", 1) is True
    assert table.can_admit("unknown", 2) is False


def test_reserve_preserves_in_flight_used() -> None:
    table = CapacityReservation().reserve("fam-2", reserved=3)
    table, ok = table.admit_for_lineage("fam-2", 2)
    assert ok is True
    # Re-declaring the slice changes the guarantee, not what is already in flight.
    table = table.reserve("fam-2", reserved=5)
    assert table.for_lineage("fam-2").used == 2
    assert table.for_lineage("fam-2").available == 3


# ---------------------------------------------------------------------------
# Validation + determinism
# ---------------------------------------------------------------------------


def test_negative_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        LineageReservation(lineage="fam-2", reserved=-1)
    with pytest.raises(ValueError):
        CapacityReservation(burst=-1)
    with pytest.raises(ValueError):
        CapacityReservation().reserve("fam-2", -1)
    with pytest.raises(ValueError):
        CapacityReservation().admit_for_lineage("fam-2", -1)


def test_determinism_status_and_capacity() -> None:
    a = on_unavailable(
        run_id="r1",
        current_phase="quality",
        component=TrustedComponent.MODEL_PROVIDER_PROXY,
        seq=SEQ,
    )
    b = on_unavailable(
        run_id="r1",
        current_phase="quality",
        component=TrustedComponent.MODEL_PROVIDER_PROXY,
        seq=SEQ,
    )
    assert a == b
    assert a.content_identity() == b.content_identity()

    t1 = CapacityReservation().reserve("fam-2", 2).reserve("fam-3", 2)
    t2 = CapacityReservation().reserve("fam-3", 2).reserve("fam-2", 2)
    # Insertion order does not affect admission outcomes (per-lineage keyed).
    t1, ok1 = t1.admit_for_lineage("fam-2", 2)
    t2, ok2 = t2.admit_for_lineage("fam-2", 2)
    assert ok1 == ok2 is True
    assert t1.for_lineage("fam-2").available == t2.for_lineage("fam-2").available == 0
    assert t1.for_lineage("fam-3").available == t2.for_lineage("fam-3").available == 2
