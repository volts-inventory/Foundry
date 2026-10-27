"""Area classification derived from the OwnershipMap.

Back-compat (no digest churn), determinism (pure function of ``owners``), and a
sensible canonical classification.
"""

from __future__ import annotations

import pytest

from foundry.schemas.architecture import (
    CANONICAL_AREAS,
    OwnershipMap,
    classify_area,
    classify_areas,
)
from foundry.state.seq import Seq


def _seq() -> Seq:
    return Seq("run", 0, 0, "principal_architect", 0)


def _map(owners: dict[str, str]) -> OwnershipMap:
    return OwnershipMap(owners=owners, seq=_seq())


# -- back-compat: the record is byte-identical to before (no stored field) ---


def test_ownership_map_serialization_has_no_area_field() -> None:
    """Areas are DERIVED, never stored — model_dump must not gain an area key.
    (``unit_criteria`` is the planner's stored per-unit criterion mapping.)"""
    om = _map({"api/store.py": "store", "web/app.tsx": "ui"})
    dumped = om.model_dump(mode="json")
    assert set(dumped) == {"owners", "unit_criteria", "version", "supersedes", "seq"}
    # content_identity is over the dump, so it cannot have shifted.
    assert "area" not in repr(dumped)


# -- determinism: a pure function of the mapping -----------------------------


def test_classification_is_deterministic() -> None:
    owners = {
        "web/Button.tsx": "ui_team",
        "api/auth/login.py": "auth_team",
        "etl/pipeline.py": "data_team",
        "services/url_shortener.py": "core_team",
    }
    assert classify_areas(owners) == classify_areas(dict(reversed(owners.items())))


# -- classification: each canonical area + the backend default ---------------


@pytest.mark.parametrize(
    ("owner", "units", "expected"),
    [
        ("frontend_team", ["web/components/Nav.tsx"], "frontend"),
        ("auth", ["api/auth/token.py"], "security"),
        ("ml", ["ml/training/model.py"], "data"),
        ("ops", ["infra/terraform/main.tf"], "infra"),
        ("core", ["services/handler.py"], "backend"),  # default
        # ``ai`` (len 2) must match a whole token only — never inside ``domain``.
        ("core", ["src/domain/email_validator.py"], "backend"),
        # Whole-token matching: an area keyword embedded in a larger token must NOT
        # fire (these all classified WRONG under the old len>=4 substring rule).
        ("nlp", ["src/tokenizer.py"], "backend"),  # not 'token'⊂tokenizer
        ("hr", ["src/interview_scheduler.py"], "backend"),  # not 'view'
        ("core", ["src/metadata_loader.py"], "backend"),  # not 'data'
        ("net", ["src/api_client.py"], "backend"),  # 'client' pruned
        ("shop", ["src/storefront.py"], "backend"),  # 'store'/'front' pruned
        ("gfx", ["src/model3d.py"], "backend"),  # not 'model' (and pruned)
        # Precedence, not dict order: security outranks data/frontend.
        ("billing", ["api/auth/session.py", "db/schema.sql"], "security"),
        # frontend outranks data when only those two collide.
        ("dash", ["web/views/Chart.tsx", "analytics/query.sql"], "frontend"),
    ],
)
def test_classify_area(owner: str, units: list[str], expected: str) -> None:
    assert classify_area(owner, units) == expected


def test_empty_owners_map() -> None:
    om = _map({})
    assert om.leads() == ()
    assert om.leads_by_area() == {}


def test_leads_one_per_distinct_owner() -> None:
    om = _map({"a.py": "team1", "b.py": "team1", "c.py": "team2"})
    assert om.leads() == ("team1", "team2")
    assert om.units_of("team1") == ("a.py", "b.py")


def test_leads_by_area_groups_owners() -> None:
    om = _map({
        "data/d.py": "warehouse",
        "web/a.tsx": "ui",
        "web/b.tsx": "ui",
        "api/c.py": "svc",
    })
    by_area = om.leads_by_area()
    assert by_area == {"frontend": ("ui",), "backend": ("svc",), "data": ("warehouse",)}
    assert list(by_area) == [a for a in CANONICAL_AREAS if a in by_area]  # canonical order
