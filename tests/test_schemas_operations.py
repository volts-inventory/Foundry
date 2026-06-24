"""Conformance tests for the operations / Phase-3 operability artifact schemas.

Contract under test (docs/operations.md; conventions from
foundry/schemas/base.py): every persisted record is a FrozenModel (frozen,
extra="forbid"), carries `seq: Seq` where it is a channel record, has a
content_identity() stable across construction order and sensitive to field
changes. Large bodies are `*_uri` CAS handles (DS9). The load-bearing
liveness distinction is RunStatus PAUSED vs WEDGED. The existing
Pipeline/IaCProposal/DeployPlan/Release records are unchanged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.schemas.base import FrozenModel
from foundry.schemas.operations import (
    SLO,
    Alert,
    AlertSeverity,
    CheckpointHealth,
    FairnessDecision,
    HITLLoad,
    PauseReason,
    QueueEntry,
    ReviewerQueueEntry,
    RunStatus,
    RunStatusRecord,
    SLOTarget,
    TenantQuota,
    UpgradeAction,
    UpgradePlan,
    VersionStamp,
)
from foundry.state.seq import Seq

SEQ = Seq(run_id="r1", emission_epoch=1, superstep_index=3, node_id="operator", local=0)

_TARGET = SLOTarget(name="run_success_rate", objective_micro=995_000, unit="ratio")
_VFROM = VersionStamp(state_schema_ver=1, graph_ver="g1", prompt_ver="p1")
_VTO = VersionStamp(
    state_schema_ver=2, graph_ver="g2", prompt_ver="p1", model_pins={"T1": "opus"}
)

# (model, valid kwargs, field to change, changed value) — one row per seq'd record.
CASES: list[tuple[type[FrozenModel], dict, str, object]] = [
    (
        RunStatusRecord,
        {
            "run_id": "r1",
            "status": RunStatus.PAUSED,
            "current_phase": "implementation",
            "pause_reason": PauseReason.HITL_WAIT,
            "awaiting": "two-person release sign-off",
            "cost_rate_microusd_per_hr": 0,
            "seq": SEQ,
        },
        "awaiting",
        "design approval",
    ),
    (
        SLO,
        {"target": _TARGET, "observed_micro": 990_000, "breaching": True, "seq": SEQ},
        "observed_micro",
        998_000,
    ),
    (
        Alert,
        {
            "name": "store_outage",
            "severity": AlertSeverity.PAGE,
            "monitor": "infra",
            "summary": "Postgres unavailable",
            "seq": SEQ,
        },
        "severity",
        AlertSeverity.WARN,
    ),
    (
        TenantQuota,
        {
            "tenant_id": "acme",
            "cost_rate_cap_microusd_per_hr": 5_000_000,
            "fair_share_micro": 250_000,
            "seq": SEQ,
        },
        "cost_rate_cap_microusd_per_hr",
        6_000_000,
    ),
    (
        QueueEntry,
        {"run_id": "r1", "tenant_id": "acme", "priority": 3, "seq": SEQ},
        "priority",
        7,
    ),
    (
        FairnessDecision,
        {
            "run_id": "r1",
            "admitted": False,
            "reason": "fleet_cost_rate_cap",
            "fleet_cost_rate_cap_microusd_per_hr": 100_000_000,
            "seq": SEQ,
        },
        "admitted",
        True,
    ),
    (
        ReviewerQueueEntry,
        {
            "run_id": "r1",
            "decision_class": "release_signoff",
            "subject_artifact_uri": "cas://release/1",
            "two_person": True,
            "sla_deadline_ms": 3_600_000,
            "seq": SEQ,
        },
        "decision_class",
        "drift_decision",
    ),
    (
        HITLLoad,
        {
            "backlog_depth": 4,
            "oldest_age_ms": 1000,
            "available_reviewer_pairs": 2,
            "per_class_backlog": {"release_signoff": 3, "drift_decision": 1},
            "seq": SEQ,
        },
        "backlog_depth",
        9,
    ),
    (
        CheckpointHealth,
        {
            "run_id": "r1",
            "checkpoint_uri": "cas://ckpt/1",
            "clean": False,
            "quarantined": True,
            "last_clean_checkpoint_uri": "cas://ckpt/0",
            "dangling_handle_uris": ("cas://blob/x",),
            "seq": SEQ,
        },
        "clean",
        True,
    ),
    (
        UpgradePlan,
        {
            "from_version": _VFROM,
            "to_version": _VTO,
            "action": UpgradeAction.MIGRATE,
            "affected_run_ids": ("r1", "r2"),
            "seq": SEQ,
        },
        "action",
        UpgradeAction.DRAIN_BEFORE_UPGRADE,
    ),
]

IDS = [cls.__name__ for cls, *_ in CASES]


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_constructs_and_is_frozen(cls, kwargs, field, changed):
    m = cls(**kwargs)
    assert m.seq == SEQ
    with pytest.raises(ValidationError):
        setattr(m, field, changed)


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_extra_fields_rejected(cls, kwargs, field, changed):
    with pytest.raises(ValidationError):
        cls(**kwargs, not_a_declared_field="boom")


@pytest.mark.parametrize(("cls", "kwargs", "field", "changed"), CASES, ids=IDS)
def test_content_identity_stable_and_field_sensitive(cls, kwargs, field, changed):
    a = cls(**kwargs)
    b = cls(**dict(reversed(list(kwargs.items()))))  # construction order irrelevant
    assert a.content_identity() == b.content_identity()
    assert a.content_identity() == a.content_identity()  # deterministic
    mutated = a.model_copy(update={field: changed})
    assert mutated.content_identity() != a.content_identity()


# ---------------------------------------------------------------------------
# Non-seq sub-records (SLOTarget / VersionStamp) are frozen + extra-forbid too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [SLOTarget, VersionStamp])
def test_subrecords_frozen_and_extra_forbid(cls):
    inst = _TARGET if cls is SLOTarget else _VFROM
    with pytest.raises(ValidationError):
        cls(**inst.model_dump(), not_a_field="boom")
    # frozenness: direct attribute set is rejected
    field = next(iter(type(inst).model_fields))
    with pytest.raises(ValidationError):
        setattr(inst, field, "boom")


def test_subrecords_content_identity_stable():
    assert _TARGET.content_identity() == SLOTarget(
        name="run_success_rate", objective_micro=995_000, unit="ratio"
    ).content_identity()
    assert _VFROM.content_identity() != _VTO.content_identity()


# ---------------------------------------------------------------------------
# Spot checks on the load-bearing invariants.
# ---------------------------------------------------------------------------


def test_run_status_liveness_states_are_distinct():
    """The whole point of §2: PAUSED and WEDGED are DISTINCT states. A paused
    run is a legitimate, non-alerted wait; a wedged run is alerted."""
    assert RunStatus.PAUSED is not RunStatus.WEDGED
    assert RunStatus.PAUSED.value == "paused"
    assert RunStatus.WEDGED.value == "wedged"
    # full liveness taxonomy present
    assert {s.value for s in RunStatus} == {
        "running",
        "paused",
        "wedged",
        "hibernated",
        "done",
        "failed",
        "quarantined",
    }


def test_paused_run_is_not_alertable_wedged_is():
    paused = RunStatusRecord(
        run_id="r1",
        status=RunStatus.PAUSED,
        current_phase="quality",
        pause_reason=PauseReason.TRUSTED_COMPONENT_UNAVAILABLE,
        awaiting="gate runner",
        cost_rate_microusd_per_hr=0,
        seq=SEQ,
    )
    wedged = paused.model_copy(
        update={"status": RunStatus.WEDGED, "pause_reason": None}
    )
    assert paused.is_alertable() is False  # legitimate wait — $0/hr, not stuck
    assert wedged.is_alertable() is True  # no heartbeat past deadline — alert
    assert paused.cost_rate_microusd_per_hr == 0


def test_pause_reason_taxonomy_covers_the_doc_causes():
    """§3 store outage, §4 trusted-component, §7 HITL, §6 capacity, sibling
    latch, operator — each is a first-class, operator-distinguishable reason."""
    values = {r.value for r in PauseReason}
    assert {
        "store_outage",
        "trusted_component_unavailable",
        "hitl_wait",
        "capacity",
        "latched_by_sibling_fork",
        "operator",
    } <= values


def test_checkpoint_health_clean_default_and_poison_pointer():
    clean = CheckpointHealth(run_id="r1", checkpoint_uri="cas://ckpt/1", seq=SEQ)
    assert clean.clean is True and clean.quarantined is False
    poisoned = clean.model_copy(
        update={
            "clean": False,
            "quarantined": True,
            "last_clean_checkpoint_uri": "cas://ckpt/0",
        }
    )
    assert poisoned.last_clean_checkpoint_uri == "cas://ckpt/0"  # fork-to-prior-good
    assert poisoned.content_identity() != clean.content_identity()


def test_hitl_load_mutable_default_is_independent():
    """S1: default_factory dict — two instances don't share one mapping."""
    a = HITLLoad(seq=SEQ)
    b = HITLLoad(seq=SEQ)
    assert a.per_class_backlog == {} and a.per_class_backlog is not b.per_class_backlog


def test_tenant_quota_carries_fleet_cost_rate_cap_in_microusd():
    q = TenantQuota(
        tenant_id="acme", cost_rate_cap_microusd_per_hr=5_000_000, seq=SEQ
    )
    assert isinstance(q.cost_rate_cap_microusd_per_hr, int)  # integer micro-USD


def test_fairness_decision_never_constructs_a_kill():
    """Admission-side throttle: a denied run stays queued (admitted=False),
    it is not killed; the cap is carried for the dashboard."""
    d = FairnessDecision(
        run_id="r1",
        admitted=False,
        reason="fleet_cost_rate_cap",
        fleet_cost_rate_microusd_per_hr=101_000_000,
        fleet_cost_rate_cap_microusd_per_hr=100_000_000,
        seq=SEQ,
    )
    assert d.admitted is False and d.preempts == ()


def test_version_stamp_pins_and_model_map():
    assert _VTO.model_pins == {"T1": "opus"}
    assert _VFROM.model_pins == {}  # S1 default_factory
    plan = UpgradePlan(
        from_version=_VFROM,
        to_version=_VTO,
        action=UpgradeAction.DRAIN_BEFORE_UPGRADE,
        seq=SEQ,
    )
    assert plan.to_version.state_schema_ver == 2
