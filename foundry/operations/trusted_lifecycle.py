"""Trusted-component lifecycle — key/component rotation + HA
(operations.md §8 lifecycle/rotation, §4 availability; governance.md §5).

The SIBLING of :mod:`foundry.operations.trusted_component`. That module covers a
trusted-component *outage* (degrade to PAUSED, never WEDGED) and per-lineage
capacity reservation. This module covers the two remaining Day-2 lifecycle
concerns about the trust-critical chokepoints — the model-provider proxy, the
zone-B signer, and the gate runner (LEARNINGS Operability):

1. **Key / component rotation WITH OVERLAP (§8).** The zone-B signing key (and
   the other trust-critical component versions whose rotation invalidates cached
   verdicts — ``lineage_map_revision``, ``scanner_config_rev``) rotate on a
   cadence. A rotation is NEVER a hard cut-over: the OLD key remains *verifiable*
   during a bounded grace window so an in-flight verdict signed under it stays
   trustworthy while it drains, then the old key is RETIRED. A verdict's
   attestation records WHICH ``key_id`` signed it (see
   :mod:`foundry.gates.attestation` — the signature string is
   ``"{scheme}:{key_id}:{mac}"``), so ``verifiable_under(key_id, now)`` is the
   grace-window check the verifier consults. **FAIL-CLOSED on orphan:** a
   rotation/retirement that would invalidate an in-flight, still-valid
   attestation (retire the key before its in-flight verdicts drain) is refused —
   the affected run PAUSES (``trusted_component_unavailable``), exactly as an
   outage does, rather than silently breaking trust. Rotation is MONOTONIC: key
   versions only advance (a re-issue of an already-superseded version is
   rejected).

2. **HA / failover (§4).** A trusted component runs as redundant *instances*
   sharing one signing identity (the attestor can be replicated, §4). A
   single-instance outage degrades to PAUSED — never WEDGED — and FAILS OVER to a
   healthy instance when one exists; with NO healthy instance it STAYS PAUSED
   (still not WEDGED, fail-closed). This reuses the
   :mod:`foundry.operations.trusted_component` availability vocabulary
   (``on_unavailable`` / ``on_recovery`` / ``TrustedComponent``) verbatim — the
   failover is a recovery onto a surviving instance, the no-healthy case is the
   same PAUSED record the outage path already mints.

Dev-grade roots honesty (implementation-plan.md §0.3): the rotation/overlap/
retirement LOGIC here is real and correct; the key *custody* is dev-grade
(symmetric HMAC keys held locally, per :mod:`foundry.gates.attestation`).
Production hardening swaps the custody (air-gapped signer, keyless OIDC), not
this logic.

Everything is **pure and deterministic** with the clock INJECTED (``now_ms`` is
an argument, never read here) — no I/O, no LLM, no wall-clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from foundry.operations.trusted_component import (
    TrustedComponent,
    on_recovery,
    on_unavailable,
)
from foundry.schemas.operations import RunStatusRecord
from foundry.state.seq import Seq

# ===========================================================================
# Key / component rotation with overlap
# ===========================================================================


class OrphanedVerdictError(Exception):
    """A rotation/retirement would invalidate an in-flight, still-valid
    attestation — refused, fail-closed. The affected run PAUSES instead (see
    :func:`pause_for_rotation`); trust is never silently broken (§8)."""


class NonMonotonicRotationError(Exception):
    """A rotation tried to (re-)issue a version that does not strictly advance
    the active version. Key versions only move forward (§8) — a re-issue of an
    already-superseded version is rejected."""


@dataclass(frozen=True)
class KeyVersion:
    """One version of a trust-critical signing key / component revision (§8).

    ``key_id`` is the identity stamped into every signature/attestation (the
    ``key_id`` of :class:`foundry.gates.attestation.ZoneBSigner`, or a
    ``lineage_map_revision`` / ``scanner_config_rev``). ``version`` is the
    monotonic ordinal (advances on every rotation). ``activated_ms`` is when it
    began signing. ``retired_ms`` is when it stops being *verifiable* — None
    while live or still inside its grace window; once set, a verdict signed under
    this key is no longer verifiable AT/AFTER that instant. Immutable: rotation /
    retirement return NEW versions (supersession, not mutation — LEARNINGS L4).
    """

    key_id: str
    version: int
    activated_ms: int
    retired_ms: int | None = None

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError(f"version must be >= 0, got {self.version}")
        if self.retired_ms is not None and self.retired_ms < self.activated_ms:
            raise ValueError(
                f"retired_ms {self.retired_ms} precedes activated_ms "
                f"{self.activated_ms}"
            )

    def verifiable_at(self, now_ms: int) -> bool:
        """True iff a verdict signed under this key is STILL verifiable at
        ``now_ms``: live (not retired) OR retired but ``now_ms`` is strictly
        before the retirement instant (the grace window). Fail-closed at the
        boundary: AT/after ``retired_ms`` it is no longer verifiable."""
        if self.retired_ms is None:
            return True
        return now_ms < self.retired_ms


@dataclass(frozen=True)
class RotationState:
    """The rotation history of ONE trust-critical component, with overlap (§8).

    ``component`` names which trusted component this key belongs to (the signer,
    the lineage map, the scanner config). ``versions`` is the full history in
    monotonic ``version`` order; the LAST entry is the currently-active signing
    key. ``grace_ms`` is the overlap window: when a new version is rotated in, the
    prior key stays verifiable for ``grace_ms`` so in-flight verdicts drain
    before it is retired. Immutable — every operation returns a NEW state.
    """

    component: TrustedComponent
    versions: tuple[KeyVersion, ...]
    grace_ms: int = 0

    def __post_init__(self) -> None:
        if not self.versions:
            raise ValueError("RotationState requires at least one key version")
        if self.grace_ms < 0:
            raise ValueError(f"grace_ms must be >= 0, got {self.grace_ms}")
        ordered = [v.version for v in self.versions]
        if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
            raise ValueError(
                f"versions must be strictly monotonic, got {ordered}"
            )

    @property
    def active(self) -> KeyVersion:
        """The currently-active signing key (highest version)."""
        return self.versions[-1]

    def _by_id(self, key_id: str) -> KeyVersion | None:
        # Last-wins so a re-used key_id resolves to its newest record (defensive;
        # key_ids are expected unique per version).
        found: KeyVersion | None = None
        for v in self.versions:
            if v.key_id == key_id:
                found = v
        return found

    def verifiable_under(self, key_id: str, now_ms: int) -> bool:
        """The grace-window check the verifier consults: is a verdict signed
        under ``key_id`` still verifiable at ``now_ms``?

        Fail-closed: an UNKNOWN ``key_id`` (never rotated through this component)
        is NOT verifiable — the verifier never trusts a signature from a key it
        cannot place in the rotation history. A known key is verifiable iff live
        or still inside its grace window (:meth:`KeyVersion.verifiable_at`).
        """
        version = self._by_id(key_id)
        if version is None:
            return False
        return version.verifiable_at(now_ms)

    def rotate_key(
        self,
        *,
        new_key_id: str,
        now_ms: int,
        in_flight_key_ids: Iterable[str] = (),
    ) -> RotationState:
        """Rotate in ``new_key_id`` as the active key, WITH OVERLAP (§8).

        The prior active key is scheduled to retire at ``now_ms + grace_ms`` —
        it stays verifiable for the grace window so in-flight verdicts signed
        under it drain before retirement. Monotonic: ``version`` advances by one
        (a stale re-issue raises :class:`NonMonotonicRotationError`).

        **FAIL-CLOSED on orphan.** If any ``in_flight_key_ids`` would be
        invalidated by THIS rotation's retirement schedule — i.e. a still-active
        verdict whose key the grace window would not actually cover (grace 0, so
        the prior key retires AT ``now_ms`` while a verdict under it is still in
        flight) — the rotation is REFUSED with :class:`OrphanedVerdictError`. The
        caller pauses the affected run (:func:`pause_for_rotation`) rather than
        silently breaking that verdict's trust. A grace_ms > 0 keeps the prior
        key verifiable, so a normal overlapped rotation never orphans.
        """
        prior = self.active
        if new_key_id == prior.key_id:
            raise NonMonotonicRotationError(
                f"new_key_id {new_key_id!r} equals the active key — a rotation "
                "must advance to a fresh key id"
            )
        retire_at = now_ms + self.grace_ms
        # Orphan guard: any in-flight verdict on a key that would NOT be
        # verifiable for the whole grace window post-retirement is an orphan.
        # With grace_ms == 0 the prior key retires AT now_ms, so an in-flight
        # verdict under it is orphaned at the instant of rotation — refuse.
        in_flight = set(in_flight_key_ids)
        if in_flight and self.grace_ms == 0 and prior.key_id in in_flight:
            raise OrphanedVerdictError(
                f"rotating away from {prior.key_id!r} with zero grace would "
                f"orphan an in-flight verdict signed under it — refuse, pause "
                "the run instead (fail-closed)"
            )
        retired_prior = replace(prior, retired_ms=retire_at)
        new_version = KeyVersion(
            key_id=new_key_id,
            version=prior.version + 1,
            activated_ms=now_ms,
        )
        return RotationState(
            component=self.component,
            versions=(*self.versions[:-1], retired_prior, new_version),
            grace_ms=self.grace_ms,
        )

    def retire(
        self,
        *,
        key_id: str,
        now_ms: int,
        in_flight_key_ids: Iterable[str] = (),
    ) -> RotationState:
        """Retire ``key_id`` AT ``now_ms`` — it becomes non-verifiable from then.

        **FAIL-CLOSED on orphan.** Retiring a key that still has an in-flight
        verdict under it would orphan that verdict — refused with
        :class:`OrphanedVerdictError`; pause the run instead. The active signing
        key may not be retired (there must always be a live signer).
        """
        version = self._by_id(key_id)
        if version is None:
            raise KeyError(f"unknown key_id {key_id!r}")
        if version.version == self.active.version:
            raise ValueError(
                f"cannot retire the active signing key {key_id!r} — rotate a "
                "successor in first"
            )
        if key_id in set(in_flight_key_ids):
            raise OrphanedVerdictError(
                f"retiring {key_id!r} would orphan an in-flight verdict signed "
                "under it — refuse, pause the run instead (fail-closed)"
            )
        retired = replace(version, retired_ms=now_ms)
        merged = tuple(
            retired if v.version == version.version else v for v in self.versions
        )
        return RotationState(
            component=self.component, versions=merged, grace_ms=self.grace_ms
        )


def pause_for_rotation(
    *,
    run_id: str,
    current_phase: str,
    component: TrustedComponent,
    seq: Seq,
    awaiting: str = "",
    last_progress_seq: Seq | None = None,
    last_progress_ts_ms: int = 0,
    state_footprint_bytes: int = 0,
) -> RunStatusRecord:
    """The fail-closed escape hatch when a rotation WOULD orphan an in-flight
    verdict: degrade the affected run to **PAUSED**, never WEDGED.

    Reuses :func:`foundry.operations.trusted_component.on_unavailable` verbatim
    — an orphaning rotation is, operationally, the trust component being
    unavailable to that run until the rotation resolves (the key drains or the
    operator extends grace). Pure: no clock.
    """
    return on_unavailable(
        run_id=run_id,
        current_phase=current_phase,
        component=component,
        seq=seq,
        awaiting=awaiting or f"{component.value} key rotation grace",
        last_progress_seq=last_progress_seq,
        last_progress_ts_ms=last_progress_ts_ms,
        state_footprint_bytes=state_footprint_bytes,
    )


# ===========================================================================
# HA / failover
# ===========================================================================


@dataclass(frozen=True)
class ComponentInstance:
    """One replicated instance of a trusted component (§4 HA).

    The attestor/runner can run as redundant instances sharing one signing
    identity, so a single-instance outage is survivable by failover. ``healthy``
    False is an instance outage. Immutable.
    """

    instance_id: str
    healthy: bool = True


@dataclass(frozen=True)
class HAComponent:
    """A trusted component with redundant instances + failover (§4).

    ``primary`` is the instance currently serving; ``instances`` is the full
    replica set (including the primary). A single-instance outage degrades to
    PAUSED (never WEDGED) and FAILS OVER to a healthy instance when one exists;
    with none healthy it STAYS PAUSED, fail-closed. Immutable — failover returns
    a NEW component.
    """

    component: TrustedComponent
    primary: str
    instances: tuple[ComponentInstance, ...]

    def __post_init__(self) -> None:
        if not self.instances:
            raise ValueError("HAComponent requires at least one instance")
        ids = [i.instance_id for i in self.instances]
        if len(set(ids)) != len(ids):
            raise ValueError(f"instance ids must be unique, got {ids}")
        if self.primary not in ids:
            raise ValueError(
                f"primary {self.primary!r} is not among instances {ids}"
            )

    def _by_id(self, instance_id: str) -> ComponentInstance:
        for i in self.instances:
            if i.instance_id == instance_id:
                return i
        raise KeyError(f"unknown instance {instance_id!r}")

    @property
    def primary_healthy(self) -> bool:
        return self._by_id(self.primary).healthy

    def healthy_instance(self) -> ComponentInstance | None:
        """A healthy instance to serve from, preferring the current primary;
        deterministic instance order otherwise. None ⇒ no healthy instance
        (the run STAYS PAUSED, fail-closed — never WEDGED)."""
        if self.primary_healthy:
            return self._by_id(self.primary)
        for i in self.instances:
            if i.healthy:
                return i
        return None

    def mark(self, instance_id: str, *, healthy: bool) -> HAComponent:
        """Record an instance's health (immutable update). An observed input —
        carries no clock."""
        updated = tuple(
            replace(i, healthy=healthy) if i.instance_id == instance_id else i
            for i in self.instances
        )
        if not any(i.instance_id == instance_id for i in updated):
            raise KeyError(f"unknown instance {instance_id!r}")
        return HAComponent(
            component=self.component, primary=self.primary, instances=updated
        )

    def failover(self) -> tuple[HAComponent, ComponentInstance | None]:
        """Fail over to a healthy instance when the primary is unhealthy.

        Returns ``(new_component, serving_instance)``. If the primary is already
        healthy nothing changes (it keeps serving). If the primary is down and a
        healthy instance exists, that instance becomes the new primary. If NO
        instance is healthy, the component is unchanged and ``serving_instance``
        is None — the caller keeps the run PAUSED (never WEDGED). Deterministic.
        """
        if self.primary_healthy:
            return self, self._by_id(self.primary)
        target = self.healthy_instance()
        if target is None:
            return self, None  # no healthy instance ⇒ stay PAUSED, fail-closed
        return (
            HAComponent(
                component=self.component,
                primary=target.instance_id,
                instances=self.instances,
            ),
            target,
        )


def classify_ha(
    ha: HAComponent,
    *,
    run_id: str,
    current_phase: str,
    seq: Seq,
    running_cost_rate_microusd_per_hr: int = 0,
    last_progress_seq: Seq | None = None,
    last_progress_ts_ms: int = 0,
    state_footprint_bytes: int = 0,
) -> tuple[HAComponent, RunStatusRecord]:
    """End-to-end HA classification for a node depending on ``ha`` (§4).

    Performs failover, then classifies the run:

    * primary healthy OR failover found a healthy instance → RUNNING on the
      serving instance (:func:`~foundry.operations.trusted_component.on_recovery`);
    * NO healthy instance → PAUSED(``trusted_component_unavailable``), never
      WEDGED (:func:`~foundry.operations.trusted_component.on_unavailable`),
      $0/hr, not alerted.

    Returns ``(component_after_failover, status_record)``. Reuses the
    trusted_component availability vocabulary verbatim — a single-instance
    outage with a surviving replica is, to the run, a recovery onto that replica;
    a total outage is the same PAUSED record an outage already mints. Pure.
    """
    after, serving = ha.failover()
    if serving is not None:
        return after, on_recovery(
            run_id=run_id,
            current_phase=current_phase,
            seq=seq,
            cost_rate_microusd_per_hr=running_cost_rate_microusd_per_hr,
            last_progress_seq=last_progress_seq,
            last_progress_ts_ms=last_progress_ts_ms,
            state_footprint_bytes=state_footprint_bytes,
        )
    return after, on_unavailable(
        run_id=run_id,
        current_phase=current_phase,
        component=ha.component,
        seq=seq,
        awaiting=f"{ha.component.value} healthy instance",
        last_progress_seq=last_progress_seq,
        last_progress_ts_ms=last_progress_ts_ms,
        state_footprint_bytes=state_footprint_bytes,
    )


__all__ = [
    "ComponentInstance",
    "HAComponent",
    "KeyVersion",
    "NonMonotonicRotationError",
    "OrphanedVerdictError",
    "RotationState",
    "classify_ha",
    "pause_for_rotation",
]
