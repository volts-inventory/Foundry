"""The ML/data domain pack (T-M5-06, domains.md §5 "ML / data systems").

Contract under test:

* The model-eval gate is assigned **class 3 (statistical)** — its grounding is the
  ALPHA_STATE_SHA requirement (a frozen alpha look + a ``state_sha``). A class-3
  verdict that does NOT bind BOTH is INVALID (fail-closed via
  ``assert_class3_binds_ledger``).
* The class-3 gate genuinely KEYS on ``state_sha``: a verdict for data D / weights W
  cannot be reused for D' / W' — a data/weight/feature change RE-KEYS the verdict
  (``ml_state_sha`` over the composed ``state_inputs``), so the old key differs.
* A second alpha look on the SAME ``(eval_set_identity, decision_id)`` is rejected
  (no re-draw — the ledger is reused, not rebuilt).
* The compute budget fail-closes on overspend (``ComputeBudgetController``).
* The pack REGISTERS via ``PackRegistry.register`` and ROUTES for an ML capability —
  NO core edit (a fresh registry routes to it by capability).
* Determinism: composition is order-independent.
"""

from __future__ import annotations

import pytest

from foundry.domains import PackRegistry
from foundry.domains.contract import StateInputPolicy
from foundry.domains.packs.ml_data import (
    MODEL_EVAL_GATE,
    RETRAIN_GATE,
    MLDataPack,
)
from foundry.domains.state_sha import (
    ComputeBudget,
    ComputeBudgetController,
    ComputeOverspendError,
    ml_state_sha_from_policy,
    ml_verdict_rebinds,
)
from foundry.domains.verdict_class import (
    Class3Binding,
    GroundingKind,
    InvalidClass3BindingError,
    assert_class3_binds_ledger,
    gate_requirements_for,
)
from foundry.schemas.gates import VerdictClass
from foundry.state.global_records import GlobalRecords

BASE = "a" * 64  # a stand-in source-only tree_sha

# An advisory ML request view: ML capability + declared data/weight/feature refs.
_ML_REQUEST = {
    "required_capabilities": ["ml", "model"],
    "data_refs": ["dataset@v1"],
    "weight_refs": ["weights@v1"],
    "feature_defs": ["fstore@v1"],
}


# --------------------------------------------------------------------------- #
# routing + registration (open/closed — no core edit)
# --------------------------------------------------------------------------- #


def test_pack_registers_and_routes_by_ml_capability() -> None:
    reg = PackRegistry()
    pack = MLDataPack()
    reg.register(pack)
    # Routes for an ML capability; not for an unrelated one.
    assert reg.select_packs(_ML_REQUEST) == (pack,)
    assert reg.select_packs({"required_capabilities": ["frontend"]}) == ()
    # All five declared capabilities route to it.
    for cap in ("ml", "data", "model", "training", "ai"):
        assert reg.select_packs([cap]) == (pack,)


def test_register_is_idempotent_and_conflict_safe() -> None:
    reg = PackRegistry()
    pack = MLDataPack()
    reg.register(pack)
    reg.register(pack)  # idempotent re-registration of the SAME instance
    assert reg.packs() == (pack,)


def test_composition_is_order_independent() -> None:
    # The same pack composed alone yields a deterministic policy regardless of how
    # many no-op base packs are present / their order.
    from foundry.domains.contract import BaseDomainPack

    reg_a = PackRegistry()
    reg_a.register(MLDataPack())
    reg_a.register(BaseDomainPack())

    reg_b = PackRegistry()
    other = BaseDomainPack()
    reg_b.register(other)
    reg_b.register(MLDataPack())

    assert reg_a.compose(_ML_REQUEST) == reg_b.compose(_ML_REQUEST)


# --------------------------------------------------------------------------- #
# the model-eval gate is class 3 (statistical), bound to alpha + state_sha
# --------------------------------------------------------------------------- #


def test_model_eval_gate_is_class3_statistical() -> None:
    pack = MLDataPack()
    policy = pack.assign_verdict_class(_ML_REQUEST)
    assert policy.classes[MODEL_EVAL_GATE] is VerdictClass.STATISTICAL
    req = gate_requirements_for(policy.classes[MODEL_EVAL_GATE])
    assert req.grounding is GroundingKind.ALPHA_STATE_SHA
    assert req.requires_alpha_state_sha is True


def test_non_ml_request_is_not_forced_onto_statistical_class() -> None:
    # A misrouted / non-ML request gets the identity — no spurious class-3 force.
    pack = MLDataPack()
    policy = pack.assign_verdict_class({"required_capabilities": ["frontend"]})
    assert policy == policy.identity()


def test_class3_verdict_must_bind_alpha_look_and_state_sha() -> None:
    pack = MLDataPack()
    records = GlobalRecords()
    state = ml_state_sha_from_policy(BASE, pack.state_inputs(_ML_REQUEST))

    # No look drawn yet → an UNDRAWN binding is rejected fail-closed.
    undrawn = Class3Binding(
        eval_set_identity="eval@v1", decision_id="dec-1", state_sha=state
    )
    with pytest.raises(InvalidClass3BindingError):
        assert_class3_binds_ledger(undrawn, records)

    # A binding missing state_sha is rejected even after the look is drawn.
    records.try_first_alpha_look("eval@v1", "dec-1", state, run_id="r1")
    no_state = Class3Binding(
        eval_set_identity="eval@v1", decision_id="dec-1", state_sha=""
    )
    with pytest.raises(InvalidClass3BindingError):
        assert_class3_binds_ledger(no_state, records)

    # Binds BOTH a frozen look + a state_sha → valid.
    bound = Class3Binding(
        eval_set_identity="eval@v1", decision_id="dec-1", state_sha=state
    )
    assert assert_class3_binds_ledger(bound, records) is bound


def test_missing_binding_is_rejected() -> None:
    with pytest.raises(InvalidClass3BindingError):
        assert_class3_binds_ledger(None, GlobalRecords())


# --------------------------------------------------------------------------- #
# the class-3 gate genuinely keys on state_sha — a data/weight change RE-KEYS
# --------------------------------------------------------------------------- #


def test_weight_change_rekeys_the_verdict() -> None:
    pack = MLDataPack()
    old = pack.state_inputs(_ML_REQUEST)
    new = pack.state_inputs({**_ML_REQUEST, "weight_refs": ["weights@v2"]})

    old_key = ml_state_sha_from_policy(BASE, old)
    new_key = ml_state_sha_from_policy(BASE, new)
    # The old verdict's key cannot be reused for the new weights — it re-draws.
    assert old_key != new_key
    assert ml_verdict_rebinds(BASE, old=old, new=new) is True


def test_data_change_rekeys_the_verdict() -> None:
    pack = MLDataPack()
    old = pack.state_inputs(_ML_REQUEST)
    new = pack.state_inputs({**_ML_REQUEST, "data_refs": ["dataset@v2"]})
    assert ml_state_sha_from_policy(BASE, old) != ml_state_sha_from_policy(BASE, new)
    assert ml_verdict_rebinds(BASE, old=old, new=new) is True


def test_same_data_weights_is_a_cache_hit_not_a_redraw() -> None:
    pack = MLDataPack()
    a = pack.state_inputs(_ML_REQUEST)
    b = pack.state_inputs(dict(_ML_REQUEST))
    assert ml_state_sha_from_policy(BASE, a) == ml_state_sha_from_policy(BASE, b)
    assert ml_verdict_rebinds(BASE, old=a, new=b) is False


def test_class3_binding_state_sha_tracks_the_data_weights() -> None:
    # The Class3Binding carries the state_sha; a weight change yields a binding that
    # cannot collide with the prior data/weights' binding (re-key).
    pack = MLDataPack()
    s_v1 = ml_state_sha_from_policy(BASE, pack.state_inputs(_ML_REQUEST))
    s_v2 = ml_state_sha_from_policy(
        BASE, pack.state_inputs({**_ML_REQUEST, "weight_refs": ["weights@v2"]})
    )
    b1 = Class3Binding(eval_set_identity="e", decision_id="d", state_sha=s_v1)
    b2 = Class3Binding(eval_set_identity="e", decision_id="d", state_sha=s_v2)
    assert b1.state_sha != b2.state_sha


# --------------------------------------------------------------------------- #
# the alpha ledger is REUSED — a second look on the same key is rejected
# --------------------------------------------------------------------------- #


def test_second_alpha_look_on_same_key_is_rejected_no_redraw() -> None:
    records = GlobalRecords()
    pack = MLDataPack()
    state = ml_state_sha_from_policy(BASE, pack.state_inputs(_ML_REQUEST))

    first = records.try_first_alpha_look("eval@v1", "dec-1", state, run_id="r1")
    assert first is True
    # A second draw on the SAME (eval_set_identity, decision_id) is rejected — the
    # ledger is reused, not re-drawn (forking/retraining cannot reset alpha).
    second = records.try_first_alpha_look("eval@v1", "dec-1", state, run_id="r2")
    assert second is False
    assert records.alpha_looks_spent("eval@v1", "dec-1") == 1


def test_distinct_decision_id_draws_independently() -> None:
    records = GlobalRecords()
    assert records.try_first_alpha_look("eval@v1", "dec-1", BASE, run_id="r1") is True
    assert records.try_first_alpha_look("eval@v1", "dec-2", BASE, run_id="r1") is True


# --------------------------------------------------------------------------- #
# the compute budget fail-closes on overspend
# --------------------------------------------------------------------------- #


def _budget_from_pack() -> ComputeBudget:
    pack = MLDataPack()
    policy = pack.compute_budget(_ML_REQUEST)
    assert policy.expensive_gates == frozenset({MODEL_EVAL_GATE, RETRAIN_GATE})
    assert policy.require_proxy is True
    assert policy.confirm_escalation is True
    cap = policy.per_fix_compute_cap_units
    assert cap is not None
    # Map the per-fix compute cap onto a ComputeBudget with a deliberate per-gate cost.
    return ComputeBudget(
        cap_units=cap,
        per_gate_units={MODEL_EVAL_GATE: cap, RETRAIN_GATE: cap},
        default_gate_units=1,
    )


def test_compute_budget_fail_closes_on_overspend() -> None:
    ctrl = ComputeBudgetController(_budget_from_pack())
    # The first expensive model-eval fits exactly (charge == cap).
    ctrl.charge(MODEL_EVAL_GATE)
    assert ctrl.remaining_units == 0
    # A second expensive gate would overspend — authorize denies, charge raises.
    assert ctrl.authorize(RETRAIN_GATE).allowed is False
    with pytest.raises(ComputeOverspendError):
        ctrl.charge(RETRAIN_GATE)
    # Nothing was booked past the cap (fail-closed: the gate pauses/escalates).
    assert ctrl.spent_units == _budget_from_pack().cap_units


def test_compute_budget_accounting_is_deterministic() -> None:
    b = ComputeBudget(cap_units=10, per_gate_units={MODEL_EVAL_GATE: 3})
    c1 = ComputeBudgetController(b)
    c2 = ComputeBudgetController(b)
    for _ in range(3):
        c1.charge(MODEL_EVAL_GATE)
        c2.charge(MODEL_EVAL_GATE)
    assert c1.spent_units == c2.spent_units == 9


# --------------------------------------------------------------------------- #
# composed-policy end-to-end through the registry (no core edit)
# --------------------------------------------------------------------------- #


def test_composed_policy_carries_the_ml_tightenings() -> None:
    reg = PackRegistry()
    reg.register(MLDataPack())
    composed = reg.compose(_ML_REQUEST)

    # verdict class: model_eval is statistical (class 3).
    assert composed.verdict_class.classes[MODEL_EVAL_GATE] is VerdictClass.STATISTICAL
    # state inputs: data/weight/feature refs folded in (so state_sha re-keys).
    assert composed.state_inputs == StateInputPolicy(
        data_refs=frozenset({"dataset@v1"}),
        feature_defs=frozenset({"fstore@v1"}),
        weight_refs=frozenset({"weights@v1"}),
    )
    # compute: expensive gates declared, proxy + confirm escalation on.
    assert composed.compute.expensive_gates == frozenset({MODEL_EVAL_GATE, RETRAIN_GATE})
    assert composed.compute.require_proxy is True
    # egress hint: no third-party trace egress.
    assert composed.egress.no_third_party_trace_egress is True
