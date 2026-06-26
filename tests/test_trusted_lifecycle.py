"""EXIT proof for trusted-component lifecycle — key/component rotation + HA
(operations.md §8 lifecycle/rotation, §4 availability;
foundry/operations/trusted_lifecycle.py).

Load-bearing properties:

* **Rotation WITH OVERLAP.** A verdict signed under the OLD key is still
  verifiable during the grace window, and NOT after retirement.
* **Fail-closed on orphan.** A rotation/retirement that would invalidate an
  in-flight, still-valid verdict is REFUSED and PAUSES the run (never silently
  breaks trust).
* **Monotonic.** Key versions only advance.
* **HA failover.** A single-instance outage → PAUSED (not WEDGED) + failover to
  a healthy instance; no healthy instance → STAYS PAUSED (still not WEDGED).
* **Determinism.**
"""

from __future__ import annotations

import pytest

from foundry.operations.trusted_component import TrustedComponent
from foundry.operations.trusted_lifecycle import (
    ComponentInstance,
    HAComponent,
    KeyVersion,
    NonMonotonicRotationError,
    OrphanedVerdictError,
    RotationState,
    classify_ha,
    pause_for_rotation,
)
from foundry.schemas.operability import (
    PauseReason,
    RunStatus,
)
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="judge", local=0)


def _signer_rotation(*, grace_ms: int) -> RotationState:
    return RotationState(
        component=TrustedComponent.ZONE_B_SIGNER,
        versions=(
            KeyVersion(key_id="zoneB-v1", version=1, activated_ms=1_000),
        ),
        grace_ms=grace_ms,
    )


# ---------------------------------------------------------------------------
# Rotation with overlap: old key verifiable during grace, not after retirement
# ---------------------------------------------------------------------------


def test_old_key_verifiable_during_grace_not_after_retirement() -> None:
    state = _signer_rotation(grace_ms=5_000)
    # Rotate at t=10_000 — old key retires at 10_000 + 5_000 = 15_000.
    rotated = state.rotate_key(new_key_id="zoneB-v2", now_ms=10_000)

    # New key is active and verifiable.
    assert rotated.active.key_id == "zoneB-v2"
    assert rotated.verifiable_under("zoneB-v2", now_ms=10_000) is True

    # Old key: still verifiable DURING the grace window...
    assert rotated.verifiable_under("zoneB-v1", now_ms=12_000) is True
    assert rotated.verifiable_under("zoneB-v1", now_ms=14_999) is True
    # ...and NOT at/after retirement.
    assert rotated.verifiable_under("zoneB-v1", now_ms=15_000) is False
    assert rotated.verifiable_under("zoneB-v1", now_ms=20_000) is False


def test_unknown_key_never_verifiable_fail_closed() -> None:
    state = _signer_rotation(grace_ms=5_000)
    assert state.verifiable_under("never-issued", now_ms=10_000) is False


def test_explicit_retire_makes_old_key_non_verifiable() -> None:
    state = _signer_rotation(grace_ms=5_000).rotate_key(
        new_key_id="zoneB-v2", now_ms=10_000
    )
    # Operator retires the old key early at t=11_000 (no in-flight verdicts).
    retired = state.retire(key_id="zoneB-v1", now_ms=11_000)
    assert retired.verifiable_under("zoneB-v1", now_ms=10_999) is True
    assert retired.verifiable_under("zoneB-v1", now_ms=11_000) is False


# ---------------------------------------------------------------------------
# Fail-closed on orphan: never silently invalidate an in-flight verdict
# ---------------------------------------------------------------------------


def test_zero_grace_rotation_orphaning_in_flight_verdict_is_refused() -> None:
    """A zero-grace rotation with a verdict still in flight under the old key
    would orphan it — refuse, fail-closed."""
    state = _signer_rotation(grace_ms=0)
    with pytest.raises(OrphanedVerdictError):
        state.rotate_key(
            new_key_id="zoneB-v2",
            now_ms=10_000,
            in_flight_key_ids=["zoneB-v1"],
        )


def test_grace_rotation_does_not_orphan_in_flight_verdict() -> None:
    """With a grace window the prior key stays verifiable, so the SAME in-flight
    verdict does NOT orphan — rotation proceeds."""
    state = _signer_rotation(grace_ms=5_000)
    rotated = state.rotate_key(
        new_key_id="zoneB-v2",
        now_ms=10_000,
        in_flight_key_ids=["zoneB-v1"],
    )
    # The in-flight verdict's key remains verifiable inside the grace window.
    assert rotated.verifiable_under("zoneB-v1", now_ms=12_000) is True


def test_retiring_key_with_in_flight_verdict_is_refused() -> None:
    state = _signer_rotation(grace_ms=5_000).rotate_key(
        new_key_id="zoneB-v2", now_ms=10_000
    )
    with pytest.raises(OrphanedVerdictError):
        state.retire(
            key_id="zoneB-v1",
            now_ms=11_000,
            in_flight_key_ids=["zoneB-v1"],
        )


def test_orphaning_rotation_pauses_run_not_wedged() -> None:
    """The fail-closed escape hatch: instead of breaking trust, the run PAUSES
    (trusted_component_unavailable), never WEDGED, $0/hr, not alerted."""
    rec = pause_for_rotation(
        run_id="r1",
        current_phase="quality",
        component=TrustedComponent.ZONE_B_SIGNER,
        seq=SEQ,
    )
    assert rec.status is RunStatus.PAUSED
    assert rec.status is not RunStatus.WEDGED
    assert rec.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    assert rec.cost_rate_microusd_per_hr == 0
    assert rec.is_alertable() is False
    assert "zone_b_signer" in rec.awaiting


# ---------------------------------------------------------------------------
# Monotonic: key versions only advance
# ---------------------------------------------------------------------------


def test_rotation_is_monotonic_versions_advance() -> None:
    state = _signer_rotation(grace_ms=5_000)
    assert state.active.version == 1
    r1 = state.rotate_key(new_key_id="zoneB-v2", now_ms=10_000)
    assert r1.active.version == 2
    r2 = r1.rotate_key(new_key_id="zoneB-v3", now_ms=20_000)
    assert r2.active.version == 3
    # Strictly increasing history.
    versions = [v.version for v in r2.versions]
    assert versions == sorted(versions) == [1, 2, 3]


def test_rotating_to_the_active_key_id_is_rejected() -> None:
    state = _signer_rotation(grace_ms=5_000)
    with pytest.raises(NonMonotonicRotationError):
        state.rotate_key(new_key_id="zoneB-v1", now_ms=10_000)


def test_non_monotonic_version_history_rejected() -> None:
    with pytest.raises(ValueError, match="monotonic"):
        RotationState(
            component=TrustedComponent.ZONE_B_SIGNER,
            versions=(
                KeyVersion(key_id="a", version=2, activated_ms=1),
                KeyVersion(key_id="b", version=1, activated_ms=2),
            ),
        )


def test_cannot_retire_active_key() -> None:
    state = _signer_rotation(grace_ms=5_000)
    with pytest.raises(ValueError, match="active"):
        state.retire(key_id="zoneB-v1", now_ms=10_000)


def test_rotation_also_models_lineage_map_and_scanner_config() -> None:
    """Rotation applies to the other trust-critical component revisions too
    (lineage_map_revision, scanner_config_rev) — same overlap logic."""
    for component in (
        TrustedComponent.GATE_RUNNER,
        TrustedComponent.MODEL_PROVIDER_PROXY,
    ):
        state = RotationState(
            component=component,
            versions=(KeyVersion(key_id="rev1", version=1, activated_ms=0),),
            grace_ms=1_000,
        )
        rotated = state.rotate_key(new_key_id="rev2", now_ms=5_000)
        assert rotated.active.key_id == "rev2"
        assert rotated.verifiable_under("rev1", now_ms=5_500) is True
        assert rotated.verifiable_under("rev1", now_ms=6_000) is False


# ---------------------------------------------------------------------------
# HA: single-instance outage → PAUSED + failover; none healthy → stays PAUSED
# ---------------------------------------------------------------------------


def _ha(*, primary_healthy: bool, secondary_healthy: bool) -> HAComponent:
    return HAComponent(
        component=TrustedComponent.ZONE_B_SIGNER,
        primary="signer-a",
        instances=(
            ComponentInstance(instance_id="signer-a", healthy=primary_healthy),
            ComponentInstance(instance_id="signer-b", healthy=secondary_healthy),
        ),
    )


def test_single_instance_outage_fails_over_to_healthy_instance() -> None:
    ha = _ha(primary_healthy=False, secondary_healthy=True)
    after, serving = ha.failover()
    assert serving is not None
    assert serving.instance_id == "signer-b"
    assert after.primary == "signer-b"  # failed over

    # End-to-end: the run RESUMES (RUNNING) on the surviving instance — the
    # single-instance outage did NOT wedge it.
    after2, rec = classify_ha(
        ha,
        run_id="r1",
        current_phase="quality",
        seq=SEQ,
        running_cost_rate_microusd_per_hr=5_000,
    )
    assert rec.status is RunStatus.RUNNING
    assert rec.status is not RunStatus.WEDGED
    assert rec.pause_reason is None
    assert rec.cost_rate_microusd_per_hr == 5_000
    assert after2.primary == "signer-b"


def test_no_healthy_instance_stays_paused_not_wedged() -> None:
    ha = _ha(primary_healthy=False, secondary_healthy=False)
    after, serving = ha.failover()
    assert serving is None
    assert after is ha  # unchanged — nothing to fail over to

    _after2, rec = classify_ha(
        ha,
        run_id="r1",
        current_phase="quality",
        seq=SEQ,
        running_cost_rate_microusd_per_hr=5_000,
    )
    # Fail-closed: PAUSED, NOT WEDGED, $0/hr, not alerted.
    assert rec.status is RunStatus.PAUSED
    assert rec.status is not RunStatus.WEDGED
    assert rec.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    assert rec.cost_rate_microusd_per_hr == 0
    assert rec.is_alertable() is False
    assert "zone_b_signer" in rec.awaiting


def test_healthy_primary_keeps_serving_no_failover() -> None:
    ha = _ha(primary_healthy=True, secondary_healthy=True)
    after, serving = ha.failover()
    assert serving is not None
    assert serving.instance_id == "signer-a"
    assert after.primary == "signer-a"  # no needless failover
    assert ha.healthy_instance() is not None
    assert ha.healthy_instance().instance_id == "signer-a"


def test_healthy_instance_returns_none_when_all_down() -> None:
    ha = _ha(primary_healthy=False, secondary_healthy=False)
    assert ha.healthy_instance() is None


def test_mark_records_instance_health_immutably() -> None:
    ha = _ha(primary_healthy=True, secondary_healthy=True)
    downed = ha.mark("signer-a", healthy=False)
    assert ha.primary_healthy is True  # original unchanged (immutable)
    assert downed.primary_healthy is False
    # Failover off the marked-down primary.
    _, serving = downed.failover()
    assert serving is not None
    assert serving.instance_id == "signer-b"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_rotation_is_deterministic() -> None:
    a = _signer_rotation(grace_ms=5_000).rotate_key(
        new_key_id="zoneB-v2", now_ms=10_000
    )
    b = _signer_rotation(grace_ms=5_000).rotate_key(
        new_key_id="zoneB-v2", now_ms=10_000
    )
    assert a == b
    assert a.versions == b.versions


def test_failover_is_deterministic() -> None:
    a, sa = _ha(primary_healthy=False, secondary_healthy=True).failover()
    b, sb = _ha(primary_healthy=False, secondary_healthy=True).failover()
    assert a == b
    assert sa == sb


def test_classify_ha_is_deterministic() -> None:
    ha = _ha(primary_healthy=False, secondary_healthy=False)
    _, r1 = classify_ha(ha, run_id="r1", current_phase="quality", seq=SEQ)
    _, r2 = classify_ha(ha, run_id="r1", current_phase="quality", seq=SEQ)
    assert r1 == r2
