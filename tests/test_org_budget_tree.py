"""The run's budget split: ``allocate_lineage_quotas`` sub-allocates the global
envelope across lineages (the ticket path's specialties)."""

from __future__ import annotations

import pytest

from foundry.execution.work_pool import allocate_lineage_quotas


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        ({"data": 1, "backend": 3}, {"data": 250_000, "backend": 750_000}),
        # Not evenly divisible: floors leave the remainder as un-allocated global
        # headroom — children's quotas can never exceed the parent envelope.
        ({"a": 1, "b": 1, "c": 1}, {"a": 333_333, "b": 333_333, "c": 333_333}),
        # Only positive-weight lineages get a quota.
        ({"data": 2, "backend": 0}, {"data": 1_000_000}),
    ],
)
def test_allocate_lineage_quotas_split(weights: dict[str, int], expected: dict[str, int]) -> None:
    quotas = allocate_lineage_quotas(weights, 1_000_000)
    assert quotas == expected
    assert sum(quotas.values()) <= 1_000_000  # the fail-closed invariant


def test_unlimited_or_no_weight_yields_no_per_lineage_limit() -> None:
    assert allocate_lineage_quotas({"a": 1}, 0) == {}  # 0 == unlimited global
    assert allocate_lineage_quotas({"a": 1}, -5) == {}
    assert allocate_lineage_quotas({}, 1_000_000) == {}
    assert allocate_lineage_quotas({"a": 0}, 1_000_000) == {}  # no positive weight
