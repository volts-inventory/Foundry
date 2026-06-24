"""Wave D — the org budget tree: per-area lineage + sub-allocation of the global
envelope (org_fanout_design.md). Pure-unit coverage of the allocator and the
task->area mapping; the per-area fail-closed ISOLATION behaviour is exercised
end-to-end in test_subgraph_implementation.py.
"""

from __future__ import annotations

from foundry.agents.implementation.junior_engineer import _area_by_task_id
from foundry.agents.implementation.senior_engineer import task_id_for_unit
from foundry.execution.work_pool import allocate_lineage_quotas
from foundry.schemas.architecture import OwnershipMap
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
