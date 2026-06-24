"""Trusted-component availability + per-lineage capacity reservation
(operations.md §4/§8).

Two Day-2 concerns about the *trusted* (non-agent) chokepoints — the
model-provider proxy (the single egress + trust SPOF, LEARNINGS Operability),
the zone-B signer, and the two-zone gate runner:

1. **Availability ≠ compromise (fail-closed, not stuck).** governance covers
   *compromise*; this module covers *outage*. A trusted-component outage must
   degrade the affected runs to **PAUSED(reason=trusted_component_unavailable)**
   — a clear, machine-readable, ``$0/hr`` wait — and **never to WEDGED**. The
   dependent judgment/gate node does NOT proceed (a verdict it cannot get
   trustworthily is never faked); it pauses-and-escalates, exactly the
   discipline the proxy already applies when it raises ``ProxyUnavailable``
   (see :mod:`foundry.proxy.model_provider_proxy`). The EXIT property is the
   round-trip: outage → PAUSED; recovery → resume (RUNNING).

2. **Per-lineage capacity reservation (§4).** T1 failover + always-on high-risk
   judging shift load onto non-primary lineages. The stack provisions ≥3
   lineages so decorrelation-preserving failover survives a single-lineage
   outage, which means failover redistributes across whichever lineages
   *survive* — so headroom is reserved on EVERY non-primary lineage, sized for
   the worst-case redistribution of any one failed lineage. The reservation
   model here makes that concrete: each lineage gets a RESERVED capacity slice,
   and admission for a lineage draws against THAT lineage's reserved slice plus
   any free shared burst pool — never against another lineage's reservation. So
   one lineage saturating demand can never starve another's reserved quota.

Everything here is **pure and deterministic**: no clock, no I/O, no LLM. The
classification reuses the §2 run-status vocabulary
(:class:`~foundry.schemas.operations.RunStatus` /
:class:`~foundry.schemas.operations.PauseReason`) so the operator surface reads
one taxonomy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from foundry.schemas.operations import (
    PauseReason,
    RunStatus,
    RunStatusRecord,
)
from foundry.state.seq import Seq

# ===========================================================================
# Trusted-component availability
# ===========================================================================


class TrustedComponent(StrEnum):
    """The trust-critical non-agent chokepoints whose *outage* (not compromise)
    this module models. Mandatory single points on the trust path — there is no
    agent bypass — so their availability is a fleet-wide concern (§4)."""

    MODEL_PROVIDER_PROXY = "model_provider_proxy"  # single egress + trust SPOF
    ZONE_B_SIGNER = "zone_b_signer"  # judgment-binding signer
    GATE_RUNNER = "gate_runner"  # two-zone gate runner / attestor


@dataclass(frozen=True)
class ComponentStatus:
    """Availability of one trusted component (§4).

    ``available`` False is an *outage*, not a compromise — it degrades dependent
    runs to PAUSED, never WEDGED, and burns no budget. ``detail`` is an
    operator-facing reason; ``runbook_uri`` links the per-component runbook
    (§1/§4). Deliberately carries no clock — availability is an observed input,
    not derived here.
    """

    component: TrustedComponent
    available: bool
    detail: str = ""
    runbook_uri: str | None = None


@dataclass(frozen=True)
class AvailabilityModel:
    """A snapshot of trusted-component availability across the fleet (§4).

    Components ABSENT from ``statuses`` are treated as available (defense in
    depth defaults to *not* spuriously pausing a run on an unmodeled component;
    the dependent node still fails closed at its own egress, e.g. the proxy's
    ``ProxyUnavailable``). The model is immutable: ``with_status`` returns a new
    model (supersession, not mutation — LEARNINGS L4).
    """

    statuses: Mapping[TrustedComponent, ComponentStatus] = field(
        default_factory=dict
    )

    def component_status(self, component: TrustedComponent) -> ComponentStatus:
        """Availability of ``component`` (defaults to available-if-unmodeled)."""
        existing = self.statuses.get(component)
        if existing is not None:
            return existing
        return ComponentStatus(component=component, available=True)

    def is_available(self, component: TrustedComponent) -> bool:
        return self.component_status(component).available

    def unavailable(self) -> tuple[TrustedComponent, ...]:
        """The components currently in outage (deterministic enum order)."""
        return tuple(c for c in TrustedComponent if not self.is_available(c))

    def all_available(self, components: Iterable[TrustedComponent]) -> bool:
        """True iff every dependency in ``components`` is available — the
        precondition a judgment/gate node checks before proceeding."""
        return all(self.is_available(c) for c in components)

    def with_status(self, status: ComponentStatus) -> AvailabilityModel:
        """Return a new model with ``status`` recorded (immutable update)."""
        merged = dict(self.statuses)
        merged[status.component] = status
        return AvailabilityModel(statuses=merged)


def on_unavailable(
    *,
    run_id: str,
    current_phase: str,
    component: TrustedComponent,
    seq: Seq,
    awaiting: str = "",
    last_progress_seq: Seq | None = None,
    last_progress_ts_ms: int = 0,
    state_footprint_bytes: int = 0,
    expected_recovery_deadline_ms: int = 0,
) -> RunStatusRecord:
    """A trusted-component outage degrades the affected run to **PAUSED**.

    Fail-closed, NOT stuck: the dependent judgment/gate node does not proceed —
    it pauses-and-escalates — and the run is classified
    ``PAUSED(reason=trusted_component_unavailable)``, never ``WEDGED`` (§4). A
    paused run is alive, ``$0/hr`` by construction, and is NOT alerted; the
    *operator* alert for the outage itself is a separate fleet-level concern
    (§1). ``awaiting`` defaults to a clear "<component> recovery" string so the
    operator surface reads who/what the run waits on. Pure: no clock, no I/O.
    """
    waiting_on = awaiting or f"{component.value} recovery"
    return RunStatusRecord(
        run_id=run_id,
        status=RunStatus.PAUSED,
        current_phase=current_phase,
        pause_reason=PauseReason.TRUSTED_COMPONENT_UNAVAILABLE,
        awaiting=waiting_on,
        last_progress_seq=last_progress_seq,
        last_progress_ts_ms=last_progress_ts_ms,
        expected_next_transition_deadline_ms=expected_recovery_deadline_ms,
        cost_rate_microusd_per_hr=0,  # paused ⇒ $0/hr (§2/§4), not budget-burning
        state_footprint_bytes=state_footprint_bytes,
        seq=seq,
    )


def on_recovery(
    *,
    run_id: str,
    current_phase: str,
    seq: Seq,
    cost_rate_microusd_per_hr: int = 0,
    last_progress_seq: Seq | None = None,
    last_progress_ts_ms: int = 0,
    state_footprint_bytes: int = 0,
) -> RunStatusRecord:
    """The recovery half of the EXIT property: an outage that recovers resumes.

    Returns a **RUNNING** record (no pause reason, ``$0/hr`` no longer forced) —
    the queue-and-drain-on-recovery semantics of §4. Calling
    :func:`on_unavailable` then :func:`on_recovery` is the round-trip the EXIT
    property asserts: outage → PAUSED → (component back) → RUNNING.
    """
    return RunStatusRecord(
        run_id=run_id,
        status=RunStatus.RUNNING,
        current_phase=current_phase,
        pause_reason=None,
        awaiting="",
        last_progress_seq=last_progress_seq,
        last_progress_ts_ms=last_progress_ts_ms,
        cost_rate_microusd_per_hr=cost_rate_microusd_per_hr,
        state_footprint_bytes=state_footprint_bytes,
        seq=seq,
    )


def classify_for_dependencies(
    model: AvailabilityModel,
    *,
    run_id: str,
    current_phase: str,
    dependencies: Iterable[TrustedComponent],
    seq: Seq,
    running_cost_rate_microusd_per_hr: int = 0,
    last_progress_seq: Seq | None = None,
    last_progress_ts_ms: int = 0,
    state_footprint_bytes: int = 0,
) -> RunStatusRecord:
    """End-to-end classification for a node with trusted ``dependencies``.

    If ANY dependency is in outage → PAUSED (fail-closed, §4) on the FIRST
    unavailable dependency in deterministic enum order; otherwise RUNNING. This
    is the single derivation a gate/judgment node uses to decide whether it may
    proceed — it never proceeds while a dependency is unavailable, and it is
    never misclassified as WEDGED for waiting on one.
    """
    deps = tuple(dependencies)
    for component in TrustedComponent:
        if component in deps and not model.is_available(component):
            status = model.component_status(component)
            return on_unavailable(
                run_id=run_id,
                current_phase=current_phase,
                component=component,
                seq=seq,
                awaiting=status.detail or f"{component.value} recovery",
                last_progress_seq=last_progress_seq,
                last_progress_ts_ms=last_progress_ts_ms,
                state_footprint_bytes=state_footprint_bytes,
            )
    return on_recovery(
        run_id=run_id,
        current_phase=current_phase,
        seq=seq,
        cost_rate_microusd_per_hr=running_cost_rate_microusd_per_hr,
        last_progress_seq=last_progress_seq,
        last_progress_ts_ms=last_progress_ts_ms,
        state_footprint_bytes=state_footprint_bytes,
    )


# ===========================================================================
# Per-lineage capacity reservation
# ===========================================================================


@dataclass(frozen=True)
class LineageReservation:
    """A reserved capacity slice for one model lineage/family (§4).

    ``reserved`` is the lineage's GUARANTEED slice (concurrent cross-family
    consumers it can always admit: always-on high-risk judging + worst-case
    failover fan-in redistributed onto it + flake re-judges + mutant-rotation
    re-grounding — §4(a)-(d)). ``used`` is current in-flight demand on the
    lineage. Capacity is integer units (concurrent attestations / requests),
    never a float. Immutable: admission returns a NEW reservation.
    """

    lineage: str
    reserved: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.reserved < 0:
            raise ValueError(f"reserved must be >= 0, got {self.reserved}")
        if self.used < 0:
            raise ValueError(f"used must be >= 0, got {self.used}")

    @property
    def available(self) -> int:
        """Headroom remaining in the lineage's OWN reserved slice."""
        return max(0, self.reserved - self.used)

    def admit(self, amount: int = 1) -> LineageReservation:
        """Reserve ``amount`` against this lineage's slice (immutable update)."""
        if amount < 0:
            raise ValueError(f"amount must be >= 0, got {amount}")
        return LineageReservation(
            lineage=self.lineage, reserved=self.reserved, used=self.used + amount
        )


@dataclass(frozen=True)
class CapacityReservation:
    """Per-lineage reservation table + an optional shared burst pool (§4/§6).

    Each lineage's ``reserved`` slice is protected: ``admit_for_lineage`` for
    lineage X draws ONLY against X's own reserved headroom plus the shared
    ``burst`` pool — NEVER against another lineage's reservation. So one
    lineage's burst can exhaust the shared pool and its own slice but can never
    consume the reserved quota of another lineage (the anti-starvation
    invariant). ``burst`` is the un-reserved shared headroom (0 = strictly
    partitioned). Immutable: ``reserve`` and ``admit_for_lineage`` return new
    tables (supersession, not mutation — L4).
    """

    reservations: Mapping[str, LineageReservation] = field(default_factory=dict)
    burst: int = 0
    burst_used: int = 0

    def __post_init__(self) -> None:
        if self.burst < 0:
            raise ValueError(f"burst must be >= 0, got {self.burst}")
        if self.burst_used < 0:
            raise ValueError(f"burst_used must be >= 0, got {self.burst_used}")

    @property
    def burst_available(self) -> int:
        """Headroom remaining in the SHARED burst pool (not reserved to anyone)."""
        return max(0, self.burst - self.burst_used)

    def reserve(self, lineage: str, reserved: int) -> CapacityReservation:
        """Declare/replace lineage ``lineage``'s reserved capacity slice.

        Preserves any in-flight ``used`` on an existing reservation (a
        re-declaration changes the guarantee, not what is already admitted).
        Returns a new table.
        """
        if reserved < 0:
            raise ValueError(f"reserved must be >= 0, got {reserved}")
        prior = self.reservations.get(lineage)
        used = prior.used if prior is not None else 0
        merged = dict(self.reservations)
        merged[lineage] = LineageReservation(
            lineage=lineage, reserved=reserved, used=used
        )
        return CapacityReservation(
            reservations=merged, burst=self.burst, burst_used=self.burst_used
        )

    def for_lineage(self, lineage: str) -> LineageReservation:
        """The reservation for ``lineage`` (an empty, zero-reserved slice if the
        lineage has none declared — an undeclared lineage has no protected
        quota and can only ride the shared burst pool)."""
        existing = self.reservations.get(lineage)
        if existing is not None:
            return existing
        return LineageReservation(lineage=lineage, reserved=0, used=0)

    def can_admit(self, lineage: str, amount: int = 1) -> bool:
        """True iff ``amount`` fits in ``lineage``'s OWN reserved headroom plus
        the shared burst pool — and NOTHING else. Other lineages' reserved
        slices are invisible here, which is exactly the protection."""
        if amount < 0:
            raise ValueError(f"amount must be >= 0, got {amount}")
        own = self.for_lineage(lineage).available
        return amount <= own + self.burst_available

    def admit_for_lineage(
        self, lineage: str, amount: int = 1
    ) -> tuple[CapacityReservation, bool]:
        """Try to admit ``amount`` units of demand for ``lineage``.

        Draws from the lineage's OWN reserved slice FIRST, then spills the
        remainder onto the shared burst pool. Returns ``(new_table, admitted)``;
        on a refusal (``admitted`` False) the table is unchanged — the run
        queues against this lineage's reserved capacity rather than mounting an
        unbounded retry storm (§4/§6), and CRUCIALLY it never reaches into
        another lineage's reserved quota. Deterministic.
        """
        if amount < 0:
            raise ValueError(f"amount must be >= 0, got {amount}")
        slice_ = self.for_lineage(lineage)
        from_reserved = min(amount, slice_.available)
        from_burst = amount - from_reserved
        if from_burst > self.burst_available:
            return self, False  # does not fit own slice + shared burst → queue

        merged = dict(self.reservations)
        merged[lineage] = LineageReservation(
            lineage=lineage,
            reserved=slice_.reserved,
            used=slice_.used + from_reserved,
        )
        return (
            CapacityReservation(
                reservations=merged,
                burst=self.burst,
                burst_used=self.burst_used + from_burst,
            ),
            True,
        )


__all__ = [
    "AvailabilityModel",
    "CapacityReservation",
    "ComponentStatus",
    "LineageReservation",
    "TrustedComponent",
    "classify_for_dependencies",
    "on_recovery",
    "on_unavailable",
]
