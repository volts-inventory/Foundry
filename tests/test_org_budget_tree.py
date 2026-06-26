"""Wave D — the org budget tree: per-area lineage + sub-allocation of the global
envelope (org_fanout_design.md). Pure-unit coverage of the allocator and the
task->area mapping; the per-area fail-closed ISOLATION behaviour is exercised
end-to-end in test_subgraph_implementation.py.
"""

from __future__ import annotations

from foundry.agents.implementation.junior_engineer import (
    _area_by_task_id,
    _domain_by_task_id,
    _read_ownership,
)
from foundry.agents.implementation.senior_engineer import OWNERSHIP_KIND, task_id_for_unit
from foundry.execution.work_pool import allocate_budget_tree, allocate_lineage_quotas
from foundry.schemas.architecture import OwnershipMap
from foundry.schemas.base import Artifact
from foundry.state.seq import Seq


def _om(owners: dict[str, str]) -> OwnershipMap:
    return OwnershipMap(owners=owners, seq=Seq("run", 0, 0, "principal_architect", 0))


# -- allocate_lineage_quotas: the budget tree split --------------------------


def test_proportional_split_sums_within_cap() -> None:
    quotas = allocate_lineage_quotas({"data": 1, "backend": 3}, 1_000_000)
    assert quotas == {"data": 250_000, "backend": 750_000}
    assert sum(quotas.values()) <= 1_000_000  # the fail-closed invariant


def test_floor_division_never_over_allocates() -> None:
    # Not evenly divisible: floors leave the remainder as un-allocated global
    # headroom — children's quotas can never exceed the parent envelope.
    quotas = allocate_lineage_quotas({"a": 1, "b": 1, "c": 1}, 1_000_000)
    assert sum(quotas.values()) <= 1_000_000
    assert quotas == {"a": 333_333, "b": 333_333, "c": 333_333}


def test_unlimited_or_no_weight_yields_no_per_lineage_limit() -> None:
    assert allocate_lineage_quotas({"a": 1}, 0) == {}  # 0 == unlimited global
    assert allocate_lineage_quotas({"a": 1}, -5) == {}
    assert allocate_lineage_quotas({}, 1_000_000) == {}
    assert allocate_lineage_quotas({"a": 0}, 1_000_000) == {}  # no positive weight


def test_zero_weight_lineages_get_no_quota() -> None:
    quotas = allocate_lineage_quotas({"data": 2, "backend": 0}, 1_000_000)
    assert quotas == {"data": 1_000_000}  # only present (positive-weight) areas


# -- _area_by_task_id: task -> area (the per-task lineage) --------------------


def test_area_by_task_id_maps_each_task_to_its_owner_area() -> None:
    om = _om({
        "warehouse/etl.py": "data_team",
        "web/Nav.tsx": "ui_team",
        "auth/login.py": "sec_team",
        "services/handler.py": "svc",
    })
    mapping = _area_by_task_id(om)
    assert mapping[task_id_for_unit("warehouse/etl.py")] == "data"
    assert mapping[task_id_for_unit("web/Nav.tsx")] == "frontend"
    assert mapping[task_id_for_unit("auth/login.py")] == "security"
    assert mapping[task_id_for_unit("services/handler.py")] == "backend"


def test_area_by_task_id_groups_units_by_owner_area() -> None:
    # Two units share one owner -> both tasks carry that owner's single area.
    om = _om({"svc/a.py": "svc", "svc/b.py": "svc"})
    mapping = _area_by_task_id(om)
    assert mapping == {
        task_id_for_unit("svc/a.py"): "backend",
        task_id_for_unit("svc/b.py"): "backend",
    }


def test_domain_by_task_id_maps_each_task_to_its_owner() -> None:
    om = _om({"svc/a.py": "svc", "warehouse/etl.py": "data_team"})
    mapping = _domain_by_task_id(om)
    assert mapping == {
        task_id_for_unit("svc/a.py"): "svc",
        task_id_for_unit("warehouse/etl.py"): "data_team",
    }


# -- allocate_budget_tree: two-level global -> area -> domain ----------------


def test_budget_tree_two_levels_sum_within_cap() -> None:
    # data area weight 2, backend area weight 2 (two domains of 1 each). global 4M
    # -> data 2M, backend 2M -> backend splits 1M/1M between its two domains.
    domain_area = {"ds": "data", "svc1": "backend", "svc2": "backend"}
    domain_weight = {"ds": 2, "svc1": 1, "svc2": 1}
    quotas = allocate_budget_tree(domain_area, domain_weight, 4_000_000)
    assert quotas == {"ds": 2_000_000, "svc1": 1_000_000, "svc2": 1_000_000}
    assert sum(quotas.values()) <= 4_000_000


def test_budget_tree_siblings_split_their_area_not_the_global() -> None:
    # Two backend domains share the backend AREA cap, not the whole global cap:
    # heavier domain gets a bigger slice, but Σ stays within the area (and global).
    domain_area = {"light": "backend", "heavy": "backend"}
    domain_weight = {"light": 1, "heavy": 3}
    quotas = allocate_budget_tree(domain_area, domain_weight, 3_600_000)
    assert quotas == {"light": 900_000, "heavy": 2_700_000}  # 1:3 of the 3.6M area
    assert sum(quotas.values()) <= 3_600_000


def test_budget_tree_unlimited_yields_no_limit() -> None:
    assert allocate_budget_tree({"a": "x"}, {"a": 1}, 0) == {}
    assert allocate_budget_tree({}, {}, 1_000_000) == {}


# -- _read_ownership: fail-soft to the "default" bucket ----------------------


def test_read_ownership_none_when_handle_absent() -> None:
    # No ownership handle on the channel -> None -> caller falls back to "default".
    assert _read_ownership({"artifacts": []}, lambda _h: None) is None
    assert _read_ownership({}, lambda _h: None) is None


def test_read_ownership_none_on_wrong_type() -> None:
    handle = Artifact(
        kind=OWNERSHIP_KIND,
        uri="cas://x",
        produced_by="principal_architect",
        seq=Seq("run", 0, 0, "principal_architect", 0),
    )
    # resolve returns a non-OwnershipMap body -> None (fail closed to no per-area).
    assert _read_ownership({"artifacts": [handle]}, lambda _h: "not an ownership map") is None
