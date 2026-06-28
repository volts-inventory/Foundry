"""Tests for the platform composition layer (foundry.platform).

The planner/contracts/stitch are pure + deterministic, so they're tested directly.
The end-to-end build runs on the keyless ``mock`` backend (skeletons) — it proves
the ORCHESTRATION + STITCHING, not per-service code quality (that needs a real
backend).
"""

from __future__ import annotations

import json
import os

from foundry.platform import build_platform, plan_platform
from foundry.platform.contracts import build_contract, contract_spec_text
from foundry.platform.schema import ROLES, PlatformPlan


def _roles(plan: PlatformPlan) -> list[str]:
    return [s.name for s in plan.services]


# ---- planner: request shape -> service decomposition -----------------------

def test_data_backed_site_gets_full_spine() -> None:
    plan = plan_platform("a website for a company that stores users and forums and posts")
    assert _roles(plan) == ["web", "api", "backend", "data"]
    web = plan.service("web")
    assert web is not None and "api" in web.depends_on


def test_self_contained_game_is_single_game_service_in_lua() -> None:
    plan = plan_platform("a lua based farming game like stardew valley")
    assert _roles(plan) == ["game"]
    game = plan.service("game")
    assert game is not None
    assert "LÖVE" in game.request or "Love2D" in game.request
    assert game.depends_on == ()


def test_online_game_adds_api_and_data() -> None:
    plan = plan_platform("a snake game with an online leaderboard")
    assert _roles(plan) == ["game", "api", "data"]
    assert plan.service("game").depends_on == ("api",)  # type: ignore[union-attr]


def test_engine_inferred_from_request() -> None:
    assert "pygame" in plan_platform("a pygame space shooter").service("game").request  # type: ignore[union-attr]
    assert "Godot" in plan_platform("a godot 2d platformer").service("game").request  # type: ignore[union-attr]


def test_plain_web_app_has_no_backend() -> None:
    assert _roles(plan_platform("a markdown editor web app")) == ["web"]


def test_only_filter_restricts_roles() -> None:
    plan = plan_platform("a saas app", only=["web", "api"])
    assert set(_roles(plan)) <= {"web", "api"}


def test_every_request_carries_the_depth_directive() -> None:
    plan = plan_platform("a website that stores users and posts")
    for svc in plan.services:
        assert "near-complete" in svc.request
        assert "TECHNICAL ONLY" in svc.request
        assert svc.role in ROLES


# ---- contract --------------------------------------------------------------

def test_contract_surface_and_spec_text() -> None:
    plan = plan_platform("a forum site with users")
    contract = build_contract(plan)
    assert "/users" in contract["paths"]  # type: ignore[operator]
    text = contract_spec_text(plan, contract)
    assert "Shared platform contract" in text
    for svc in plan.services:
        assert svc.name in text


# ---- end-to-end (mock backend) ---------------------------------------------

def test_build_platform_stitches_a_monorepo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = str(tmp_path / "repo")
    result = build_platform(
        "a website for a company that stores users and forums",
        out_dir=out, backend="mock",
    )
    assert result.passed
    assert result.built == len(result.services) >= 3

    # the stitched monorepo has the expected wiring
    for rel in ("README.md", "docker-compose.yml", "PLATFORM-MANIFEST.json",
                "Makefile", "packages/contracts/openapi.json",
                ".github/workflows/ci.yml"):
        assert os.path.isfile(os.path.join(out, rel)), rel
    for svc in result.services:
        assert os.path.isdir(os.path.join(out, "services", svc.name))

    with open(os.path.join(out, "PLATFORM-MANIFEST.json")) as fh:
        manifest = json.load(fh)
    assert manifest["request"]
    assert {s["name"] for s in manifest["services"]} == {s.name for s in result.services}


def test_deepening_passes_build_on_prior_tree(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = str(tmp_path / "deep")
    result = build_platform(
        "a tiny REST api", out_dir=out, backend="mock", only=["api"], passes=2,
    )
    assert result.passed
    # pass 1 lands in <name>.p1, the final pass in <name>; both exist in staging
    staging = os.path.join(out, ".platform-build")
    assert os.path.isdir(os.path.join(staging, "api.p1"))   # first (greenfield) pass
    assert os.path.isdir(os.path.join(staging, "api"))      # final (deepened) pass
    assert os.path.isdir(os.path.join(out, "services", "api"))


def test_auto_passes_converge_below_the_failsafe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = str(tmp_path / "auto")
    result = build_platform(
        "a tiny REST api", out_dir=out, backend="mock", only=["api"],
        passes="auto", max_passes=6,
    )
    svc = result.services[0]
    assert result.passed
    assert 1 <= svc.passes < 6          # converged on "done", did NOT hit the failsafe
    assert os.path.isdir(os.path.join(out, "services", "api"))


def test_game_platform_skips_compose_service(tmp_path) -> None:  # type: ignore[no-untyped-def]
    out = str(tmp_path / "game")
    result = build_platform("a lua based farming game like stardew", out_dir=out, backend="mock")
    assert result.passed
    assert [s.name for s in result.services] == ["game"]
    with open(os.path.join(out, "docker-compose.yml")) as fh:
        compose = fh.read()
    assert "build: ./services/game" not in compose       # a game is not a container
    with open(os.path.join(out, "README.md")) as fh:
        assert "love services/game" in fh.read()
