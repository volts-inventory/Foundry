"""The CRYPTO domain pack (ROADMAP Phase 6) — never ship a hand-rolled cipher.

Capability lives in the crypto verification profile (KAT vectors, round-trip +
tamper properties, vetted-primitives contract — ROADMAP Phase 3); THIS pack
adds the governance floor for anything exporting a cryptographic API:

* **mode veto + human release** (``mode_tier_policy``) — ``autonomous`` AND
  auto-approvable ``assistant`` are vetoed: cryptographic releases only run
  Gated, min tier HIGH, with a forced human release sign-off (a subtle nonce
  reuse is not Low-risk because the diff is small).
* **security approver-of-record** (``approver_of_record``) — the release and
  crypto-design artifacts require a recorded HUMAN approver (class 4); a green
  machine gate can never satisfy them.
* **class-4 release keys** (``assign_verdict_class``) — the crypto review /
  release keys are human-witnessed; the KAT/property suite stays the class-2
  machine floor underneath (from the profile), not a replacement for review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.domains.contract import (
    AIJudgePolicy,
    ApproverPolicy,
    BaseDomainPack,
    ModeTierPolicy,
    VerdictClassPolicy,
    request_is_autonomous,
)
from foundry.domains.governance_hooks import MODE_ASSISTANT
from foundry.schemas.gates import VerdictClass
from foundry.schemas.governance import RiskTier

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

__all__ = [
    "CRYPTO_ARTIFACT_CLASSES",
    "CRYPTO_CAPABILITIES",
    "CRYPTO_CLASS4_KEYS",
    "CryptoPack",
]

CRYPTO_CAPABILITIES: frozenset[str] = frozenset(
    {"crypto", "cryptography", "encryption", "key_management", "secrets"}
)

# Artifact classes requiring a class-4 human approver-of-record.
CRYPTO_ARTIFACT_CLASSES: frozenset[str] = frozenset({"release", "crypto_design"})

# Gate keys forced to class 4 (human-witnessed).
CRYPTO_CLASS4_KEYS: frozenset[str] = frozenset({"crypto_review", "release_sign_off"})


class CryptoPack(BaseDomainPack):
    """Gated-only, human-reviewed release for cryptographic surfaces."""

    name = "crypto"
    capabilities = CRYPTO_CAPABILITIES

    def _engaged(self, request: Mapping[str, Any]) -> bool:
        from foundry.domains.contract import request_capabilities

        return bool(request_capabilities(request) & self.capabilities)

    def mode_tier_policy(self, request: Mapping[str, Any]) -> ModeTierPolicy:
        """Gated-only among the HUMAN modes (assistant's auto-approve is vetoed);
        AUTONOMOUS is reachable by operator choice — the HIGH-tier floor still
        applies there, and the AI crypto reviewer judges the release
        (see ai_judgment)."""
        if not self._engaged(request):
            return ModeTierPolicy.identity()
        if request_is_autonomous(request):
            return ModeTierPolicy(min_tier=RiskTier.HIGH)
        return ModeTierPolicy(
            vetoed_modes=frozenset({MODE_ASSISTANT}),
            min_tier=RiskTier.HIGH,
            force_human_release=True,
        )

    def ai_judgment(self, request: Mapping[str, Any]) -> AIJudgePolicy:
        """The AUTONOMOUS stand-in for the human crypto review: an AI
        cryptography reviewer judges the release evidence."""
        if not self._engaged(request) or not request_is_autonomous(request):
            return AIJudgePolicy.identity()
        return AIJudgePolicy(rubrics={
            "crypto_review": (
                "Judge this release as a cryptography reviewer. Approve ONLY if "
                "the evidence shows standard, well-reviewed primitives and modes "
                "(no home-rolled ciphers, no ECB, no static IVs/nonces, no "
                "hardcoded keys or secrets in the tree), keys sourced from "
                "config/KMS, and the gates exercised the crypto paths. Refuse on "
                "any invented primitive or key-handling smell."
            ),
        })

    def approver_of_record(self, request: Mapping[str, Any]) -> ApproverPolicy:
        if not self._engaged(request) or request_is_autonomous(request):
            return ApproverPolicy.identity()
        return ApproverPolicy(bound_artifact_classes=CRYPTO_ARTIFACT_CLASSES)

    def assign_verdict_class(self, request: Mapping[str, Any]) -> VerdictClassPolicy:
        if not self._engaged(request):
            return VerdictClassPolicy.identity()
        return VerdictClassPolicy(
            classes={key: VerdictClass.HUMAN_WITNESSED for key in CRYPTO_CLASS4_KEYS}
        )
