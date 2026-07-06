"""The GAME domain pack (ROADMAP Phase 6) — playability is perceptual.

Capability lives in the factory (the agentic adapters, the game verification
profiles with their runtime SMOKE gate — ROADMAP Phases 1-3); THIS pack adds
what only governance can: a game "works" when a human says it is *playable*,
and a runtime-boot gate key must be part of the boundary's grounding.

Tighten-only hooks (composes strictest-wins with any other engaged pack):

* **class-5 playability HITL** (``perceptual_hitl`` + ``assign_verdict_class``)
  — modeled directly on the frontend pack's visual HITL: a game release is
  perceptual (feel / pacing / controls) and is NEVER auto-approved; a recorded
  human sign-off over the rendered/smoke evidence is mandatory in the
  human-in-the-loop modes (assistant/gated). An AUTONOMOUS run has no human to
  sign, so by operator choice the sign-off does not bind there — the class-2
  runtime smoke gate is the hands-off release floor.
* **class-2 runtime gate key** (``assign_verdict_class``) — the profile's smoke
  ("boots and survives N ticks") verdict is named as an execution-deterministic
  key so a boundary that never ran the game cannot claim the grounding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.domains.contract import (
    BaseDomainPack,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    VerdictClassPolicy,
    request_is_autonomous,
)
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
    """Playability HITL (human modes) + runtime-gate grounding."""

    name = "game"
    capabilities = GAME_CAPABILITIES

    def _engaged(self, request: Mapping[str, Any]) -> bool:
        from foundry.domains.contract import request_capabilities

        return bool(request_capabilities(request) & self.capabilities)

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        """A game release requires a recorded human PLAYABILITY sign-off (class 5)
        over rendered/smoke evidence — taste and feel are never auto-approved.

        AUTONOMOUS runs are exempt by operator choice: hands-off mode has no human
        to sign, so the class-2 runtime smoke gate is the release floor there and
        playability judgment moves to whoever plays the released build."""
        if not self._engaged(request) or request_is_autonomous(request):
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
        """No mode veto: `autonomous` is reachable for game requests. The class-5
        playability sign-off binds HUMAN-in-the-loop modes (see perceptual_hitl);
        a hands-off run releases on the machine gates (build/tests/smoke) alone."""
        return ModeTierPolicy.identity()
