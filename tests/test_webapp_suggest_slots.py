"""webapp.server._suggest_slots — the advisory 'suggest N ICs' heuristic.

Pure mapping from a request string to a parallel-IC count = the distinct
CANONICAL_AREAS the work likely touches. Advisory only; the real split is Wave A.
"""
from __future__ import annotations

from foundry.schemas.architecture import CANONICAL_AREAS
from webapp.server import _suggest_slots


def test_tiny_request_is_backend_baseline() -> None:
    n, areas = _suggest_slots("a hello service")
    assert n == 1 and areas == ["backend"]  # backend is the baseline for any build


def test_multi_area_request_counts_distinct_areas() -> None:
    n, areas = _suggest_slots("a web dashboard UI with a REST api and a postgres database")
    assert set(areas) == {"frontend", "backend", "data"}
    assert n == 3


def test_areas_are_ordered_canonically_and_capped() -> None:
    n, areas = _suggest_slots(
        "frontend ui, backend api, data pipeline, infra deploy, security audit"
    )
    assert areas == [a for a in CANONICAL_AREAS if a in areas]  # canonical order
    assert n == len(CANONICAL_AREAS)  # all five, capped at the canonical count


def test_count_is_at_least_one() -> None:
    n, _ = _suggest_slots("")
    assert n >= 1
