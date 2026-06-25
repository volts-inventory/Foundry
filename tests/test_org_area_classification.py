"""Wave A — area classification derived from the OwnershipMap (ships dark).

Covers the three things the critic must trust for a behavior-neutral data wave:
back-compat (no digest churn), determinism (pure function of ``owners``), and a
sensible canonical classification (areas materialize only from real work).
"""

from __future__ import annotations

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
    """Areas are DERIVED, never stored — model_dump must not gain a key (the
    ownership_map digest/state_sha stay identical to pre-Wave-A)."""
    om = _map({"api/store.py": "store", "web/app.tsx": "ui"})
    dumped = om.model_dump(mode="json")
    assert set(dumped) == {"owners", "version", "supersedes", "seq"}
    # content_identity is over the dump, so it cannot have shifted.
    assert "area" not in repr(dumped)


def test_content_identity_stable_across_calls() -> None:
    om = _map({"a.py": "x"})
    assert om.content_identity() == _map({"a.py": "x"}).content_identity()


# -- determinism: a pure function of the mapping -----------------------------


def test_classification_is_deterministic() -> None:
    owners = {
        "web/Button.tsx": "ui_team",
        "api/auth/login.py": "auth_team",
        "etl/pipeline.py": "data_team",
        "services/url_shortener.py": "core_team",
    }
    assert classify_areas(owners) == classify_areas(dict(reversed(owners.items())))


def test_leads_by_area_reproducible() -> None:
    om = _map({"web/x.tsx": "ui", "api/y.py": "svc"})
    assert om.leads_by_area() == _map(dict(om.owners)).leads_by_area()


# -- classification: each canonical area + the backend default ---------------


def test_each_area_classifies() -> None:
    assert classify_area("frontend_team", ["web/components/Nav.tsx"]) == "frontend"
    assert classify_area("auth", ["api/auth/token.py"]) == "security"
    assert classify_area("ml", ["ml/training/model.py"]) == "data"
    assert classify_area("ops", ["infra/terraform/main.tf"]) == "infra"
    assert classify_area("core", ["services/handler.py"]) == "backend"  # default


def test_short_keyword_does_not_false_match_substring() -> None:
    """``ai`` (len 2) must match a whole token only — never inside ``domain``."""
    assert classify_area("core", ["src/domain/email_validator.py"]) == "backend"


def test_long_keyword_does_not_false_match_substring() -> None:
    """Whole-token matching: an area keyword embedded in a larger token must NOT
    fire (these all classified WRONG under the old len>=4 substring rule)."""
    assert classify_area("nlp", ["src/tokenizer.py"]) == "backend"  # not 'token'⊂tokenizer
    assert classify_area("hr", ["src/interview_scheduler.py"]) == "backend"  # not 'view'
    assert classify_area("core", ["src/metadata_loader.py"]) == "backend"  # not 'data'
    assert classify_area("net", ["src/api_client.py"]) == "backend"  # 'client' pruned
    assert classify_area("shop", ["src/storefront.py"]) == "backend"  # 'store'/'front' pruned
    assert classify_area("gfx", ["src/model3d.py"]) == "backend"  # not 'model' (and pruned)


def test_empty_owners_map() -> None:
    om = _map({})
    assert om.leads() == ()
    assert om.areas_present() == ()
    assert om.leads_by_area() == {}


def test_precedence_security_beats_other_areas() -> None:
    """A domain touching multiple areas resolves by fixed precedence, not dict
    order: security outranks data/frontend (most-specific first)."""
    # owns both an auth seam (security) and a schema (data) -> security wins.
    assert classify_area("billing", ["api/auth/session.py", "db/schema.sql"]) == "security"
    # frontend outranks data when only those two collide.
    assert classify_area("dash", ["web/views/Chart.tsx", "analytics/query.sql"]) == "frontend"


def test_areas_present_only_materializes_real_work() -> None:
    # A headless service: no frontend, no data — only backend.
    om = _map({"services/a.py": "svc_a", "services/b.py": "svc_b"})
    assert om.areas_present() == ("backend",)
    # A web + api product: frontend and backend, but no data area.
    om2 = _map({"web/app.tsx": "ui", "api/handler.py": "svc"})
    assert om2.areas_present() == ("frontend", "backend")
    assert "data" not in om2.areas_present()


def test_areas_present_is_canonical_order() -> None:
    om = _map({
        "infra/deploy.tf": "ops",
        "web/x.tsx": "ui",
        "api/h.py": "svc",
        "ml/model.py": "ds",
    })
    present = om.areas_present()
    assert present == tuple(a for a in CANONICAL_AREAS if a in present)
    assert present == ("frontend", "backend", "data", "infra")


def test_leads_one_per_distinct_owner() -> None:
    om = _map({"a.py": "team1", "b.py": "team1", "c.py": "team2"})
    assert om.leads() == ("team1", "team2")
    assert om.units_of("team1") == ("a.py", "b.py")


def test_leads_by_area_groups_owners() -> None:
    om = _map({
        "web/a.tsx": "ui",
        "web/b.tsx": "ui",
        "api/c.py": "svc",
        "data/d.py": "warehouse",
    })
    by_area = om.leads_by_area()
    assert by_area == {"frontend": ("ui",), "backend": ("svc",), "data": ("warehouse",)}
