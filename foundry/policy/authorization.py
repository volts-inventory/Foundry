"""Segregation of duties + fail-closed authorization. (governance §1e, §1c)

"Author != approver" is only safe if **enforced** (governance §1e): the system
records the author identity(ies) per artifact, and admission/release **REJECTS
when an approver identity intersects the author set** or lacks a required
distinct role. This mirrors the two-zone split (governance §1c): the zone that
*produces* (executes agent-influenced code) must not be the zone that *signs /
admits* — an agent can never sign its own verdict.

This module is the policy check for that invariant. It is **fail-closed**: an
:class:`AuthorizationDecision` is ``allow`` only on an explicit, satisfied
rule; ambiguity, a self-produce-and-sign attempt, a shared zone, or a missing
distinct role all deny.

DEV-GRADE caveat (implementation-plan §0.3): production binds signer
identities + roles into the WORM record / admission predicate so the
distinctness check is re-derivable at admission, not relayed from agent state.
Here it is a pure predicate over an explicit :class:`DutyRequest`.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel

__all__ = [
    "AuthorizationDecision",
    "Decision",
    "DutyRequest",
    "Zone",
    "authorize_segregation_of_duties",
]


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Zone(StrEnum):
    """The two-zone trust split (governance §1c). Producing and signing/admitting
    must happen in DIFFERENT zones — zone A executes agent code and holds no
    signing identity; zone B signs/admits and runs no agent code."""

    A_PRODUCER = "zone-a-producer"  # executes agent-influenced code; no signing key
    B_ATTESTOR = "zone-b-attestor"  # signs/admits; runs no agent code


class DutyRequest(FrozenModel):
    """A request to let ``approver`` sign/admit an artifact produced by
    ``authors`` — the segregation-of-duties subject the policy evaluates."""

    artifact_uri: str
    authors: tuple[str, ...]  # recorded author identity(ies) of the artifact
    approver: str  # the identity attempting to sign/admit
    approver_zone: Zone
    approver_role: str  # the approver's role
    required_distinct_role: str | None = None  # role the approver MUST hold


class AuthorizationDecision(FrozenModel):
    """A fail-closed authorization verdict: ``allow`` only on a satisfied rule."""

    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(decision=Decision.DENY, reason=reason)


def authorize_segregation_of_duties(req: DutyRequest) -> AuthorizationDecision:
    """Decide whether ``req.approver`` may sign/admit ``req.artifact_uri``.

    Fail-closed deny rules (any one trips → DENY), mirroring governance §1e/§1c:

    1. The approver must NOT be in the author set (an agent cannot sign its own
       verdict; author != approver).
    2. The approver must sign/admit from the attestor zone (zone B) — a producer
       (zone A, executes agent code, holds no signing key) cannot also sign.
    3. If a distinct role is required, the approver must hold exactly it.
    4. The author set must be non-empty and the approver identity non-empty
       (a missing/ambiguous identity is denied, not waved through).

    Only when no deny rule fires is the decision ``allow``.
    """
    if not req.approver:
        return _deny("empty approver identity (fail-closed)")
    if not req.authors:
        return _deny("no recorded author identity (fail-closed)")
    if req.approver in req.authors:
        return _deny(
            f"segregation-of-duties violation: approver {req.approver!r} is in the "
            f"author set (an actor cannot both produce and sign/admit)"
        )
    if req.approver_zone is not Zone.B_ATTESTOR:
        return _deny(
            f"approver zone {req.approver_zone.value!r} is not the attestor zone "
            f"(producing zone cannot sign/admit — two-zone split)"
        )
    if req.required_distinct_role is not None and req.approver_role != req.required_distinct_role:
        return _deny(
            f"approver role {req.approver_role!r} != required distinct role "
            f"{req.required_distinct_role!r}"
        )
    return AuthorizationDecision(
        decision=Decision.ALLOW,
        reason=f"approver {req.approver!r} is distinct from authors and holds the required role",
    )
