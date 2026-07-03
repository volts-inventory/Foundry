"""The GAME domain pack (ROADMAP Phase 6) — playability is perceptual.

Capability lives in the factory (the agentic adapters, the game verification
profiles with their runtime SMOKE gate — ROADMAP Phases 1-3); THIS pack adds
what only governance can: a game "works" when a human says it is *playable*,
and a runtime-boot gate key must be part of the boundary's grounding.

Tighten-only hooks (composes strictest-wins with any other engaged pack):

* **class-5 playability HITL** (``perceptual_hitl`` + ``assign_verdict_class``)
  — modeled directly on the frontend pack's visual HITL: a game release is
  perceptual (feel / pacing / controls) and is NEVER auto-approved; a recorded
  human sign-off over the rendered/smoke evidence is mandatory.
* **class-2 runtime gate key** (``assign_verdict_class``) — the profile's smoke
  ("boots and survives N ticks") verdict is named as an execution-deterministic
  key so a boundary that never ran the game cannot claim the grounding.
* **mode veto** (``mode_tier_policy``) — ``autonomous`` (hands-off deploy) is
  vetoed: a game whose playability gate is class-5 human cannot hands-off
  release; ``assistant``/``gated`` stay reachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.domains.contract import (
    BaseDomainPack,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    VerdictClassPolicy,
)
from foundry.domains.governance_hooks import MODE_AUTONOMOUS
from foundry.schemas.gates import VerdictClass

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

__all__ = ["GAME_CAPABILITIES", "PLAYABILITY_GATE", "RUNTIME_SMOKE_GATE", "GamePack"]

GAME_CAPABILITIES: frozenset[str] = frozenset({"game", "gameplay", "interactive"})

# Gate keys this pack assigns classes to (consumed by verdict_class routing).
PLAYABILITY_GATE = "game_playability"  # class 5 — human plays it; never auto-approved
RUNTIME_SMOKE_GATE = "smoke"  # class 2 — the profile's boot-and-run verdict


class GamePack(BaseDomainPack):
    """Playability HITL + runtime-gate grounding + autonomous-release veto."""

    name = "game"
    capabilities = GAME_CAPABILITIES

    def _engaged(self, request: Mapping[str, Any]) -> bool:
        from foundry.domains.contract import request_capabilities

        return bool(request_capabilities(request) & self.capabilities)

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        """A game release requires a recorded human PLAYABILITY sign-off (class 5)
        over rendered/smoke evidence — taste and feel are never auto-approved."""
        if not self._engaged(request):
            return PerceptualHITLPolicy.identity()
        return PerceptualHITLPolicy(
            hitl_required_capabilities=self.capabilities,
            require_rendered_evidence=True,
        )

    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        if not self._engaged(request):
            return VerdictClassPolicy.identity()
        return VerdictClassPolicy(
            classes={
                PLAYABILITY_GATE: VerdictClass.PERCEPTUAL,  # class 5 — human plays it
                RUNTIME_SMOKE_GATE: VerdictClass.EXECUTION_DETERMINISTIC,  # class 2
            }
        )

    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        """VETO hands-off release: a class-5 playability gate cannot be satisfied
        by a machine, so `autonomous` is unreachable for game requests."""
        if not self._engaged(request):
            return ModeTierPolicy.identity()
        return ModeTierPolicy(vetoed_modes=frozenset({MODE_AUTONOMOUS}))
