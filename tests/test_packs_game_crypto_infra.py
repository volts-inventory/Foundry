"""Phase 6 (ROADMAP): the game / crypto / infra domain packs.

Each pack is tighten-only governance over capability that lives in Phases 1-3:
playability HITL + autonomous veto (game); gated-only human-reviewed release
(crypto); plan-review HITL + no hands-off deploy + trace-egress restriction
(infra). Off-capability requests get the no-op identity on every hook.
"""

from __future__ import annotations

from foundry.domains.governance_hooks import MODE_ASSISTANT, MODE_AUTONOMOUS
from foundry.domains.packs.crypto import CryptoPack
from foundry.domains.packs.game import GamePack, PLAYABILITY_GATE, RUNTIME_SMOKE_GATE
from foundry.domains.packs.infra import InfraPack
from foundry.schemas.gates import VerdictClass

GAME_REQ = {"required_capabilities": ["game"]}
CRYPTO_REQ = {"required_capabilities": ["encryption"]}
INFRA_REQ = {"required_capabilities": ["terraform"]}
PLAIN_REQ = {"required_capabilities": ["backend"]}


# ---- game -----------------------------------------------------------------------


def test_game_pack_requires_playability_hitl_and_vetoes_autonomous() -> None:
    pack = GamePack()
    hitl = pack.perceptual_hitl(GAME_REQ)
    assert "game" in hitl.hitl_required_capabilities
    assert hitl.require_rendered_evidence is True
    classes = pack.assign_verdict_class(GAME_REQ).classes
    assert classes[PLAYABILITY_GATE] is VerdictClass.PERCEPTUAL  # never auto-approved
    assert classes[RUNTIME_SMOKE_GATE] is VerdictClass.EXECUTION_DETERMINISTIC
    assert MODE_AUTONOMOUS in pack.mode_tier_policy(GAME_REQ).vetoed_modes
    assert MODE_ASSISTANT not in pack.mode_tier_policy(GAME_REQ).vetoed_modes


def test_game_pack_is_identity_off_capability() -> None:
    pack = GamePack()
    assert pack.perceptual_hitl(PLAIN_REQ).hitl_required_capabilities == frozenset()
    assert pack.mode_tier_policy(PLAIN_REQ).vetoed_modes == frozenset()


# ---- crypto ----------------------------------------------------------------------


def test_crypto_pack_forces_gated_human_release() -> None:
    pack = CryptoPack()
    policy = pack.mode_tier_policy(CRYPTO_REQ)
    assert {MODE_AUTONOMOUS, MODE_ASSISTANT} <= set(policy.vetoed_modes)
    assert policy.force_human_release is True
    assert "release" in pack.approver_of_record(CRYPTO_REQ).bound_artifact_classes
    classes = pack.assign_verdict_class(CRYPTO_REQ).classes
    assert classes["crypto_review"] is VerdictClass.HUMAN_WITNESSED


def test_crypto_pack_is_identity_off_capability() -> None:
    pack = CryptoPack()
    assert pack.mode_tier_policy(PLAIN_REQ).vetoed_modes == frozenset()
    assert pack.approver_of_record(PLAIN_REQ).bound_artifact_classes == frozenset()


# ---- infra -----------------------------------------------------------------------


def test_infra_pack_plan_review_and_no_handsoff_deploy() -> None:
    pack = InfraPack()
    assert MODE_AUTONOMOUS in pack.mode_tier_policy(INFRA_REQ).vetoed_modes
    assert pack.mode_tier_policy(INFRA_REQ).force_human_release is True
    hitl = pack.perceptual_hitl(INFRA_REQ)
    assert hitl.require_rendered_evidence is True  # the PLAN evidence bundle
    assert "infra_plan" in pack.approver_of_record(INFRA_REQ).bound_artifact_classes
    assert pack.egress_profile(INFRA_REQ).no_third_party_trace_egress is True


# ---- registry / CLI integration ----------------------------------------------------


def test_all_three_register_and_compose() -> None:
    from foundry.domains.registry import PackRegistry

    registry = PackRegistry()
    for pack in (GamePack(), CryptoPack(), InfraPack()):
        registry.register(pack)
    assert {"game", "crypto", "infra"} <= {p.name for p in registry.packs()}


def test_cli_offers_the_new_packs() -> None:
    from foundry.cli import _pack_factories

    names = set(_pack_factories())
    assert {"game", "crypto", "infra", "regulated", "ml_data", "frontend"} <= names
