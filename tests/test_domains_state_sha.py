"""ML `state_sha` data/weight hooks + the compute-cost budget (T-M5-03, domains.md §2-3).

Contract under test:

* :func:`ml_state_sha` RE-KEYS the verdict when ``data_refs`` / ``weight_refs`` /
  ``feature_defs`` change (each invalidates a cached verdict → forces a re-draw);
  SAME inputs → SAME key (reproducible / deterministic); caller order + duplication
  do not perturb the key (it reuses ``tree_sha.state_sha``'s sort+dedup support).
* Fail-closed: an ML verdict that binds NO data/weight/feature ref into its key is
  rejected (``UnboundMLStateError``) — a verdict can't be reused across a data/weight
  change. ``ml_verdict_rebinds`` detects re-key vs cache-hit.
* The compute-cost budget: charging accumulates; exceeding the cap fail-closes
  (``authorize`` denies, ``charge`` raises ``ComputeOverspendError`` — no silent
  overspend); determinism (same charges → same booked total).
"""

from __future__ import annotations

import pytest

from foundry.domains.contract import StateInputPolicy
from foundry.domains.state_sha import (
    ComputeBudget,
    ComputeBudgetController,
    ComputeOverspendError,
    UnboundMLStateError,
    ml_state_sha,
    ml_state_sha_from_policy,
    ml_verdict_rebinds,
)

BASE = "0" * 64  # a stand-in source-only tree_sha


# --------------------------------------------------------------------------- #
# state_sha data/weight hooks
# --------------------------------------------------------------------------- #


def test_same_inputs_same_key_reproducible() -> None:
    a = ml_state_sha(BASE, data_refs=["d1", "d2"], weight_refs=["w1"], feature_defs=["f1"])
    b = ml_state_sha(BASE, data_refs=["d1", "d2"], weight_refs=["w1"], feature_defs=["f1"])
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_order_and_duplication_do_not_perturb_key() -> None:
    a = ml_state_sha(BASE, data_refs=["d1", "d2"], weight_refs=["w1"])
    b = ml_state_sha(BASE, data_refs=["d2", "d1", "d1"], weight_refs=["w1"])
    assert a == b


def test_data_ref_change_rekeys_verdict() -> None:
    a = ml_state_sha(BASE, data_refs=["d1"], weight_refs=["w1"])
    b = ml_state_sha(BASE, data_refs=["d2"], weight_refs=["w1"])
    assert a != b  # a data change invalidates the cached verdict (re-draw)


def test_weight_ref_change_rekeys_verdict() -> None:
    a = ml_state_sha(BASE, data_refs=["d1"], weight_refs=["w1"])
    b = ml_state_sha(BASE, data_refs=["d1"], weight_refs=["w2"])
    assert a != b  # a retrain / weight change re-keys → re-draw


def test_feature_def_change_rekeys_verdict() -> None:
    a = ml_state_sha(BASE, data_refs=["d1"], feature_defs=["f1"])
    b = ml_state_sha(BASE, data_refs=["d1"], feature_defs=["f2"])
    assert a != b


def test_base_tree_sha_change_rekeys_verdict() -> None:
    a = ml_state_sha(BASE, data_refs=["d1"])
    b = ml_state_sha("1" * 64, data_refs=["d1"])
    assert a != b  # a source/lockfile/toolchain change (folded in base) still re-keys


def test_unbound_ml_verdict_is_rejected_fail_closed() -> None:
    with pytest.raises(UnboundMLStateError):
        ml_state_sha(BASE)  # no data/weight/feature ref → not reusable across data change
    with pytest.raises(UnboundMLStateError):
        ml_state_sha(BASE, data_refs=[], weight_refs=[], feature_defs=[])


def test_non_ml_source_only_special_case_allowed() -> None:
    # require_binding=False is the explicit source-only escape hatch.
    key = ml_state_sha(BASE, require_binding=False)
    assert len(key) == 64


def test_from_policy_matches_explicit_refs() -> None:
    policy = StateInputPolicy(
        data_refs=frozenset({"d1"}),
        weight_refs=frozenset({"w1"}),
        feature_defs=frozenset({"f1"}),
    )
    assert ml_state_sha_from_policy(BASE, policy) == ml_state_sha(
        BASE, data_refs=["d1"], weight_refs=["w1"], feature_defs=["f1"]
    )


def test_from_policy_fail_closed_on_empty_policy() -> None:
    with pytest.raises(UnboundMLStateError):
        ml_state_sha_from_policy(BASE, StateInputPolicy.identity())


def test_rebinds_true_on_weight_change_false_on_identical() -> None:
    base_refs = StateInputPolicy(data_refs=frozenset({"d1"}), weight_refs=frozenset({"w1"}))
    retrained = StateInputPolicy(data_refs=frozenset({"d1"}), weight_refs=frozenset({"w2"}))
    assert ml_verdict_rebinds(BASE, old=base_refs, new=retrained) is True  # re-draw
    assert ml_verdict_rebinds(BASE, old=base_refs, new=base_refs) is False  # cache hit


# --------------------------------------------------------------------------- #
# compute-cost budget dimension
# --------------------------------------------------------------------------- #


def _controller(cap: int = 10, **per_gate: int) -> ComputeBudgetController:
    return ComputeBudgetController(
        ComputeBudget(cap_units=cap, per_gate_units=dict(per_gate), default_gate_units=1)
    )


def test_charging_accumulates() -> None:
    c = _controller(cap=10, retrain=4, device_matrix=3)
    c.charge("retrain")
    assert c.spent_units == 4
    assert c.remaining_units == 6
    c.charge("device_matrix")
    assert c.spent_units == 7
    assert c.remaining_units == 3


def test_default_gate_charge_applies() -> None:
    c = _controller(cap=5)  # no per-gate entry → default 1
    c.charge("unmapped_gate")
    assert c.spent_units == 1


def test_authorize_denies_before_overspend_no_commit() -> None:
    c = _controller(cap=5, retrain=4)
    c.charge("retrain")  # spent 4, remaining 1
    decision = c.authorize("retrain")  # 4 + 4 = 8 > 5
    assert decision.allowed is False
    assert "exceeds compute cap" in decision.reason
    # authorize does NOT commit:
    assert c.spent_units == 4


def test_charge_fail_closes_on_overspend_no_silent_spend() -> None:
    c = _controller(cap=5, retrain=4)
    c.charge("retrain")  # spent 4
    with pytest.raises(ComputeOverspendError):
        c.charge("retrain")  # would be 8 > 5
    # the failed charge booked NOTHING — no silent overspend:
    assert c.spent_units == 4
    assert c.remaining_units == 1


def test_charge_exactly_to_cap_is_allowed() -> None:
    c = _controller(cap=5, retrain=5)
    decision = c.charge("retrain")
    assert decision.allowed is True
    assert c.spent_units == 5
    assert c.remaining_units == 0
    # one more (default 1) now fail-closes:
    with pytest.raises(ComputeOverspendError):
        c.charge("tiny")


def test_compute_budget_determinism() -> None:
    def run() -> int:
        c = _controller(cap=20, a=3, b=5, c=2)
        c.charge("a")
        c.charge("b")
        c.charge("c")
        return c.spent_units

    assert run() == run() == 10


def test_negative_cap_rejected() -> None:
    with pytest.raises(ValueError):
        ComputeBudgetController(ComputeBudget(cap_units=-1))


def test_compute_budget_distinct_from_usd_dimension() -> None:
    # The compute budget is integer UNITS — there is no micro-USD field on it;
    # it is a dimension distinct from foundry.governance.budget_controller's USD.
    budget = ComputeBudget(cap_units=8, per_gate_units={"retrain": 6})
    assert budget.charge_for("retrain") == 6
    assert budget.charge_for("unknown") == 1  # default
    assert "microusd" not in ComputeBudget.model_fields
