"""Phase 6 (ROADMAP): the game / crypto / infra domain packs.

Each pack is tighten-only governance over capability that lives in Phases 1-3,
binding its HUMAN requirements in the human-in-the-loop modes: playability HITL
(game); assistant-vetoed human-reviewed release (crypto); plan-review HITL +
trace-egress restriction (infra). A declared-AUTONOMOUS run is reachable for all
three by operator choice — only the regulated pack still vetoes hands-off — and
releases on the machine gates alone. Off-capability requests get the no-op
identity on every hook.
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
GAME_REQ_AUTO = {"required_capabilities": ["game"], "mode": "autonomous"}
CRYPTO_REQ_AUTO = {"required_capabilities": ["encryption"], "mode": "autonomous"}
INFRA_REQ_AUTO = {"required_capabilities": ["terraform"], "mode": "autonomous"}


# ---- game -----------------------------------------------------------------------


def test_game_pack_requires_playability_hitl_in_human_modes() -> None:
    pack = GamePack()
    hitl = pack.perceptual_hitl(GAME_REQ)
    assert "game" in hitl.hitl_required_capabilities
    assert hitl.require_rendered_evidence is True
    classes = pack.assign_verdict_class(GAME_REQ).classes
    assert classes[PLAYABILITY_GATE] is VerdictClass.PERCEPTUAL  # never auto-approved
    assert classes[RUNTIME_SMOKE_GATE] is VerdictClass.EXECUTION_DETERMINISTIC
    # autonomous is reachable (no mode veto); assistant/gated too
    assert pack.mode_tier_policy(GAME_REQ).vetoed_modes == frozenset()
    assert MODE_ASSISTANT not in pack.mode_tier_policy(GAME_REQ).vetoed_modes


def test_game_pack_autonomous_run_skips_the_playability_signoff() -> None:
    """A declared-autonomous run has no human to sign: the playability HITL does
    not bind, so the class-2 smoke gate is the hands-off release floor."""
    pack = GamePack()
    assert pack.perceptual_hitl(GAME_REQ_AUTO).hitl_required_capabilities == frozenset()
    assert pack.mode_tier_policy(GAME_REQ_AUTO).vetoed_modes == frozenset()


def test_game_pack_is_identity_off_capability() -> None:
    pack = GamePack()
    assert pack.perceptual_hitl(PLAIN_REQ).hitl_required_capabilities == frozenset()
    assert pack.mode_tier_policy(PLAIN_REQ).vetoed_modes == frozenset()


# ---- crypto ----------------------------------------------------------------------


def test_crypto_pack_forces_gated_human_release_in_human_modes() -> None:
    pack = CryptoPack()
    policy = pack.mode_tier_policy(CRYPTO_REQ)
    assert MODE_ASSISTANT in policy.vetoed_modes      # auto-approve is vetoed
    assert MODE_AUTONOMOUS not in policy.vetoed_modes  # hands-off reachable
    assert policy.force_human_release is True
    assert "release" in pack.approver_of_record(CRYPTO_REQ).bound_artifact_classes
    classes = pack.assign_verdict_class(CRYPTO_REQ).classes
    assert classes["crypto_review"] is VerdictClass.HUMAN_WITNESSED


def test_crypto_pack_autonomous_keeps_tier_floor_drops_human_release() -> None:
    """Autonomous stays HIGH-tier (machine-side rigor) but the human approver
    requirements do not bind — release rides the machine gates."""
    from foundry.schemas.governance import RiskTier

    pack = CryptoPack()
    policy = pack.mode_tier_policy(CRYPTO_REQ_AUTO)
    assert policy.vetoed_modes == frozenset()
    assert policy.min_tier is RiskTier.HIGH
    assert policy.force_human_release is False
    assert pack.approver_of_record(CRYPTO_REQ_AUTO).bound_artifact_classes == frozenset()


def test_crypto_pack_is_identity_off_capability() -> None:
    pack = CryptoPack()
    assert pack.mode_tier_policy(PLAIN_REQ).vetoed_modes == frozenset()
    assert pack.approver_of_record(PLAIN_REQ).bound_artifact_classes == frozenset()


# ---- infra -----------------------------------------------------------------------


def test_infra_pack_plan_review_in_human_modes() -> None:
    pack = InfraPack()
    assert pack.mode_tier_policy(INFRA_REQ).vetoed_modes == frozenset()
    assert pack.mode_tier_policy(INFRA_REQ).force_human_release is True
    hitl = pack.perceptual_hitl(INFRA_REQ)
    assert hitl.require_rendered_evidence is True  # the PLAN evidence bundle
    assert "infra_plan" in pack.approver_of_record(INFRA_REQ).bound_artifact_classes
    assert pack.egress_profile(INFRA_REQ).no_third_party_trace_egress is True


def test_infra_pack_autonomous_drops_human_requirements() -> None:
    pack = InfraPack()
    assert pack.mode_tier_policy(INFRA_REQ_AUTO).force_human_release is False
    assert pack.approver_of_record(INFRA_REQ_AUTO).bound_artifact_classes == frozenset()
    assert pack.assign_verdict_class(INFRA_REQ_AUTO).classes == {}


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
