"""The INFRA / IaC domain pack (ROADMAP Phase 6) — plan is a gate, apply never is.

Capability lives in the ``iac-terraform`` verification profile (fmt/validate as
gates — ROADMAP Phase 3) and in the credential typology (target-system
credentials NEVER enter the sandbox; deploy is a credentialed-phase action —
ROADMAP 1b). THIS pack adds the governance floor for infrastructure changes:

* **plan-artifact HITL before apply** (``perceptual_hitl`` reused as the
  recorded-human-review seam over the PLAN evidence bundle): an infra release
  requires a human to review what will change in the real world.
* **approver-of-record** (``approver_of_record``) — release + infra-plan
  artifacts bind a class-4 human approver.
* **egress restriction** (``egress_profile``) — no third-party trace egress for
  runs that carry infrastructure topology (hostnames, account layout).
* **mode policy** (``mode_tier_policy``) — ``autonomous`` hands-off DEPLOY is
  vetoed; gated release with a recorded sign-off is the ceiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.domains.contract import (
    ApproverPolicy,
    BaseDomainPack,
    EgressPolicy,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    VerdictClassPolicy,
)
from foundry.domains.governance_hooks import MODE_AUTONOMOUS
from foundry.schemas.gates import VerdictClass

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

__all__ = ["INFRA_ARTIFACT_CLASSES", "INFRA_CAPABILITIES", "INFRA_CLASS4_KEYS", "InfraPack"]

INFRA_CAPABILITIES: frozenset[str] = frozenset(
    {"infra", "infrastructure", "iac", "terraform", "cloud", "deploy"}
)

INFRA_ARTIFACT_CLASSES: frozenset[str] = frozenset({"release", "infra_plan"})

INFRA_CLASS4_KEYS: frozenset[str] = frozenset({"infra_plan_review", "release_sign_off"})


class InfraPack(BaseDomainPack):
    """Human-reviewed plan, bound approver, no hands-off deploy."""

    name = "infra"
    capabilities = INFRA_CAPABILITIES

    def _engaged(self, request: Mapping[str, Any]) -> bool:
        from foundry.domains.contract import request_capabilities

        return bool(request_capabilities(request) & self.capabilities)

    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        if not self._engaged(request):
            return ModeTierPolicy.identity()
        return ModeTierPolicy(
            vetoed_modes=frozenset({MODE_AUTONOMOUS}),
            force_human_release=True,
        )

    def approver_of_record(self, request: Mapping[str, Any]) -> ApproverPolicy:
        if not self._engaged(request):
            return ApproverPolicy.identity()
        return ApproverPolicy(bound_artifact_classes=INFRA_ARTIFACT_CLASSES)

    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        if not self._engaged(request):
            return VerdictClassPolicy.identity()
        return VerdictClassPolicy(
            classes={key: VerdictClass.HUMAN_WITNESSED for key in INFRA_CLASS4_KEYS}
        )

    def perceptual_hitl(self, request: Mapping[str, Any]) -> PerceptualHITLPolicy:
        """The plan-review HITL: a human reviews the PLAN evidence bundle before
        any apply — reuses the recorded-sign-off seam (never auto-approved)."""
        if not self._engaged(request):
            return PerceptualHITLPolicy.identity()
        return PerceptualHITLPolicy(
            hitl_required_capabilities=self.capabilities,
            require_rendered_evidence=True,
        )

    def egress_profile(self, request: Mapping[str, Any]) -> EgressPolicy:
        if not self._engaged(request):
            return EgressPolicy.identity()
        return EgressPolicy(no_third_party_trace_egress=True)
