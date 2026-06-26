"""Upgrade & migration with runs in flight (operations.md §5/§8; T-M3-08).

Contract under test (foundry/operations/upgrade.py): a version upgrade must NOT
corrupt or strand an in-flight run. Per in-flight run the planner decides
DRAIN (complete-then-upgrade on the pinned version), MIGRATE (an additive
checkpoint migration), or — fail-closed — PAUSE (the run halts to the operator
with its checkpoint intact; never a silent lossy upgrade). Plus: the
version-stamp compatibility check, the serde-allowlist guard (an upgrade that
changes a model must keep old checkpoints loadable or migrate them), and
determinism.
"""

from __future__ import annotations

import pytest

from foundry.durability.serde import FOUNDRY_MSGPACK_MODULES
from foundry.operations.upgrade import (
    InFlightRun,
    MigrationError,
    compatible,
    migrate_checkpoint,
    plan_fleet_upgrade,
    plan_upgrade,
    schema_compatible,
    serde_loadable,
)
from foundry.schemas.operability import (
    UpgradeAction,
    UpgradePlan,
    VersionStamp,
)
from foundry.state.seq import Seq


def _seq(run_id: str, epoch: int = 1) -> Seq:
    return Seq(
        run_id=run_id,
        emission_epoch=epoch,
        superstep_index=1,
        node_id="upgrade",
        local=0,
    )


V1 = VersionStamp(state_schema_ver=1, graph_ver="g1", prompt_ver="p1")
V1_NEWGRAPH = VersionStamp(state_schema_ver=1, graph_ver="g2", prompt_ver="p2")
V2 = VersionStamp(state_schema_ver=2, graph_ver="g2", prompt_ver="p1")
V1_PINNED_T1A = VersionStamp(
    state_schema_ver=1, graph_ver="g1", prompt_ver="p1", model_pins={"T1": "opus-a"}
)
V1_PINNED_T1B = VersionStamp(
    state_schema_ver=1, graph_ver="g1", prompt_ver="p1", model_pins={"T1": "opus-b"}
)

# A real allowlisted model type (so a checkpoint carrying it is loadable).
_GOOD_TYPE = FOUNDRY_MSGPACK_MODULES[0][1]


# --- version-stamp compatibility check -------------------------------------


def test_same_schema_is_compatible() -> None:
    assert schema_compatible(V1, V1_NEWGRAPH)
    # graph/prompt bump does NOT break compatibility: resume on pinned version.
    assert compatible(V1, V1_NEWGRAPH)


def test_schema_bump_is_not_compatible() -> None:
    assert not schema_compatible(V1, V2)
    assert not compatible(V1, V2)


def test_pin_drift_breaks_compatibility() -> None:
    # Same role pinned to a different id mid-flight = drift = incompatible.
    assert not compatible(V1_PINNED_T1A, V1_PINNED_T1B)
    # Target ADDING a pin the run never had is not a conflict (new runs re-pin).
    assert compatible(V1, V1_PINNED_T1A)
    # Same id on both sides: no drift.
    assert compatible(V1_PINNED_T1A, V1_PINNED_T1A)


# --- DRAIN: compatible run completes on its pinned version ------------------


def test_compatible_run_drains_not_corrupted() -> None:
    run = InFlightRun(run_id="r1", stamp=V1, seq=_seq("r1"))
    d = plan_upgrade(V1, V1_NEWGRAPH, run)
    assert d.action is UpgradeAction.DRAIN_BEFORE_UPGRADE
    assert not d.stranded  # not stranded
    assert not d.paused  # not corrupted/paused
    assert d.plan.from_version == V1
    assert d.plan.affected_run_ids == ("r1",)


def test_breaking_change_drains_when_run_can_complete_on_old() -> None:
    run = InFlightRun(
        run_id="r1",
        stamp=V1,
        seq=_seq("r1"),
        can_complete_on_old=True,
        checkpoint_model_types=(_GOOD_TYPE,),
    )
    # No migration function for the breaking bump -> drain on old, not pause.
    d = plan_upgrade(V1, V2, run)
    assert d.action is UpgradeAction.DRAIN_BEFORE_UPGRADE
    assert not d.stranded


# --- MIGRATE: additive schema bump with a migration function ---------------


def test_additive_bump_with_migration_migrates() -> None:
    run = InFlightRun(
        run_id="r1", stamp=V1, seq=_seq("r1"), checkpoint_model_types=(_GOOD_TYPE,)
    )
    d = plan_upgrade(V1, V2, run, migration_uri="cas://migrations/v1_to_v2")
    assert d.action is UpgradeAction.MIGRATE
    assert not d.stranded
    assert d.plan.migration_uri == "cas://migrations/v1_to_v2"


# --- PAUSE (fail-closed): unmigratable / unloadable checkpoint -------------


def test_unloadable_checkpoint_pauses_not_corrupts() -> None:
    # A model type NOT on the serde allowlist => the checkpoint cannot round-trip
    # under the target; the run PAUSES (flagged for operator), never corrupts.
    run = InFlightRun(
        run_id="r1",
        stamp=V1,
        seq=_seq("r1"),
        can_complete_on_old=False,
        checkpoint_model_types=("TotallyUnknownModel",),
    )
    d = plan_upgrade(V1, V2, run, migration_uri="cas://migrations/v1_to_v2")
    assert d.action is UpgradeAction.ABORT
    assert d.stranded
    assert d.paused
    assert "non-allowlisted" in d.reason


def test_breaking_change_no_drain_no_migration_pauses() -> None:
    run = InFlightRun(
        run_id="r1",
        stamp=V1,
        seq=_seq("r1"),
        can_complete_on_old=False,
        checkpoint_model_types=(_GOOD_TYPE,),
    )
    d = plan_upgrade(V1, V2, run)  # no migration_uri
    assert d.action is UpgradeAction.ABORT
    assert d.stranded


def test_downgrade_pauses() -> None:
    run = InFlightRun(
        run_id="r1",
        stamp=V2,
        seq=_seq("r1"),
        can_complete_on_old=False,
        checkpoint_model_types=(_GOOD_TYPE,),
    )
    d = plan_upgrade(V2, V1, run, migration_uri="cas://migrations/down")
    assert d.action is UpgradeAction.ABORT
    assert d.stranded


# --- migrate_checkpoint seam ------------------------------------------------


def test_migrate_checkpoint_forward_ok() -> None:
    out = migrate_checkpoint(
        V1, V2, model_types=(_GOOD_TYPE,), migration_uri="cas://m"
    )
    assert out == V2


def test_migrate_checkpoint_rejects_non_allowlisted() -> None:
    with pytest.raises(MigrationError):
        migrate_checkpoint(V1, V2, model_types=("Nope",), migration_uri="cas://m")


def test_migrate_checkpoint_rejects_downgrade() -> None:
    with pytest.raises(MigrationError):
        migrate_checkpoint(V2, V1, model_types=(_GOOD_TYPE,))


def test_migrate_checkpoint_rejects_breaking_without_function() -> None:
    with pytest.raises(MigrationError):
        migrate_checkpoint(V1, V2, model_types=(_GOOD_TYPE,))  # no migration_uri


def test_serde_loadable() -> None:
    assert serde_loadable((_GOOD_TYPE,))
    assert serde_loadable(())  # nothing to load
    assert not serde_loadable((_GOOD_TYPE, "MadeUpModel"))


# --- no in-flight run is dropped, and the plan is deterministic -------------


def test_fleet_plan_covers_every_run_no_strand() -> None:
    runs = [
        # already on the target schema -> compatible -> drain on pinned version.
        InFlightRun(run_id="r1", stamp=V2, seq=_seq("r1")),
        InFlightRun(  # additive -> migrate
            run_id="r2",
            stamp=V1,
            seq=_seq("r2"),
            checkpoint_model_types=(_GOOD_TYPE,),
        ),
        InFlightRun(  # unloadable -> pause
            run_id="r3",
            stamp=V1,
            seq=_seq("r3"),
            can_complete_on_old=False,
            checkpoint_model_types=("BadModel",),
        ),
    ]
    decisions = plan_fleet_upgrade(
        V1, V2, runs, migration_uri="cas://migrations/v1_to_v2"
    )
    # Exactly one decision per in-flight run: none silently dropped/stranded.
    assert len(decisions) == len(runs)
    by_run = {d.run_id: d for d in decisions}
    assert by_run["r1"].action is UpgradeAction.DRAIN_BEFORE_UPGRADE
    assert by_run["r2"].action is UpgradeAction.MIGRATE
    assert by_run["r3"].action is UpgradeAction.ABORT
    # Only the PAUSE outcome is stranded/flagged for the operator.
    assert not by_run["r1"].stranded
    assert not by_run["r2"].stranded
    assert by_run["r3"].stranded
    # Every decision carries a typed channel record.
    assert all(isinstance(d.plan, UpgradePlan) for d in decisions)


def test_fleet_plan_is_deterministic_and_order_independent() -> None:
    runs = [
        InFlightRun(run_id="rb", stamp=V1, seq=_seq("rb", epoch=2)),
        InFlightRun(run_id="ra", stamp=V1, seq=_seq("ra", epoch=1)),
    ]
    a = plan_fleet_upgrade(V1, V1_NEWGRAPH, runs)
    b = plan_fleet_upgrade(V1, V1_NEWGRAPH, list(reversed(runs)))
    # Same plan regardless of input order (sorted by seq), and repeatable.
    assert [d.run_id for d in a] == [d.run_id for d in b]
    assert [d.plan.content_identity() for d in a] == [
        d.plan.content_identity() for d in b
    ]


def test_single_plan_is_pure() -> None:
    run = InFlightRun(
        run_id="r1", stamp=V1, seq=_seq("r1"), checkpoint_model_types=(_GOOD_TYPE,)
    )
    d1 = plan_upgrade(V1, V2, run, migration_uri="cas://m")
    d2 = plan_upgrade(V1, V2, run, migration_uri="cas://m")
    assert d1.plan.content_identity() == d2.plan.content_identity()
    assert d1.action is d2.action is UpgradeAction.MIGRATE
