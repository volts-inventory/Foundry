"""Governance / meta artifacts (agents.md Layer 7 — Governance / Meta).

Orchestrator/Triage → `TriageDecision`; Guardian → `GuardianDecision`;
Budget Controller → `BudgetDecision`; Human Liaison → `HumanDecision`
(recorded in `human_decisions` and the WORM audit store).
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class RequestRoute(StrEnum):
    """Triage classification of the incoming request (agents.md Layer 7)."""

    GREENFIELD = "greenfield"
    EVOLUTION = "evolution"
    BUGFIX = "bugfix"  # routes to MINSPEC
    SPIKE = "spike"  # timeboxed Architect research; throwaway, never released


class ChangeRiskTier(StrEnum):
    """Change-risk tier (governance.md §1e): scales assurance depth and HITL,
    never the integrity guarantees (admission stays fail-closed)."""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"  # build/CI/IaC, deps, scanner-config, auth/crypto, trust root


class TriageDecision(FrozenModel):
    """Orchestrator/Triage output (agents.md Layer 7): route + change-risk
    tier (a first-class, validated output — governance.md §1e) + the seeded
    default per-route budget cap (refined later by the Eng/Program Manager).
    Monetary values are integer micro-USD, never floats (architecture.md §8)."""

    route: RequestRoute
    tier: ChangeRiskTier
    usd_cap_microusd: int  # seeded default per-route cap (governance.md §7)
    seq: Seq


class GuardianAction(StrEnum):
    HALT = "halt"
    PAUSE = "pause"
    ESCALATE = "escalate"  # to HITL


class GuardianDecision(FrozenModel):
    """Guardian output (agents.md Layer 7). Deterministic-monitor trips are
    load-bearing (`advisory=False`); the toolless sandboxed analyzer only
    flags advisorily (`advisory=True`) — it cannot approve work or override
    a deterministic trip."""

    action: GuardianAction
    advisory: bool = False  # True only for analyzer color, never for monitor trips
    reason: str  # e.g. the loop signature (criterion_id/task, gate, failure_signature)
    seq: Seq


class BudgetAction(StrEnum):
    DOWNGRADE = "downgrade"  # tier downgrade — T2/T3 only; T1 NEVER auto-downgrades
    ESCALATE = "escalate"  # T1 pressure escalates (governance.md §7)
    PAUSE = "pause"  # hard breach: graceful checkpoint-and-pause


class BudgetDecision(FrozenModel):
    """Budget Controller output (agents.md Layer 7): per-node/per-loop cap
    enforcement. Monetary values in integer micro-USD, never floats
    (architecture.md §8)."""

    action: BudgetAction
    node_id: str  # the pressured node
    spend_microusd: int
    cap_microusd: int
    seq: Seq


class HumanDecision(FrozenModel):
    """Human Liaison record of a HITL decision (agents.md Layer 7): design
    approval, release sign-off, retry-cap breaches, monitor flags.

    `approver` is the approver-of-record (a named human identity — admission
    rejects when it intersects the author set, governance.md §1e);
    `server_nonce` is the per-approval server-issued challenge bound into the
    class-4 attestation so an approval cannot be minted or replayed
    (governance.md §6). Resumes from checkpoint without re-firing effects.
    """

    subject_artifact_uri: str  # what was approved/rejected
    approver: str  # approver-of-record
    approved: bool
    server_nonce: str  # per-approval server-issued challenge (governance.md §6)
    note: str = ""
    seq: Seq
