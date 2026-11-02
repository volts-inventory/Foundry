"""Governance / meta artifacts (agents.md Layer 7 — Governance / Meta).

Budget Controller → `BudgetDecision`; change-risk / scope tiering →
`TierDecision` / `ScopeDecision`; release sign-off → `SignOffRecord`.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.state.seq import Seq


class ChangeRiskTier(StrEnum):
    """Change-risk tier (governance.md §1e): scales assurance depth and HITL,
    never the integrity guarantees (admission stays fail-closed)."""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"  # build/CI/IaC, deps, scanner-config, auth/crypto, trust root


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


# --- Phase-2 (Gated mode) assurance + governance artifacts ------------------
# Change-risk tiering scales assurance DEPTH and HITL load, never the integrity
# guarantees (admission stays fail-closed, verdicts always runner-signed —
# governance.md §1e). RiskTier is an alias of the existing ChangeRiskTier so the
# tiering wave and triage share ONE enum (no parallel verdict type, L3).
RiskTier = ChangeRiskTier


class TierDecision(FrozenModel):
    """Change-risk tier decision (governance.md §1e): the first-class, validated
    record of WHICH assurance depth + HITL load a change is scaled to.

    The tier scales assurance DEPTH and human load ONLY — it NEVER relaxes the
    integrity guarantees (admission stays fail-closed, verdicts stay
    runner-signed). `forced_high` records a security-relevant/trust-root path or
    a domain-pack regulatory-significance escalation that pins HIGH regardless of
    diff size; `gate_set` is the (advisory) scoped gate family this tier runs.
    Mis-tiering is caught the same way criteria/config edits are — by
    deterministic pattern checks + HITL — so this is a triage record, not a
    bypass of any gate.
    """

    tier: RiskTier
    rationale: str  # why this tier (the deterministic-classifier explanation)
    run_against: str  # tree_sha / design ref the tiering was computed against
    gate_set: tuple[str, ...] = ()  # scoped gate family this tier runs (advisory)
    two_person_release: bool = False  # HIGH / regulated → distinct-identity sign-off
    forced_high: bool = False  # security-relevant/trust-root/regulated force-escalation
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class ScopeTier(StrEnum):
    """How much system the request is actually asking for.

    ``STANDARD`` is the historical behaviour — every prompt and cap is exactly
    what the factory did before scope tiering existed, so a STANDARD run is
    byte-identical to a pre-tiering run. ``DEMO`` is the only tier that changes
    anything: it caps how much the product/architecture layers may PROPOSE.
    """

    DEMO = "demo"
    STANDARD = "standard"


class ScopeDecision(FrozenModel):
    """Scope-tier decision: the record of how much system a request wants.

    Sibling of :class:`TierDecision` — that one scales assurance DEPTH off the
    change's risk, this one scales the proposed SYSTEM off the request's stated
    ambition. Both are triage records, neither is a bypass: this one is read only
    by agent PROMPTS (the PM's criteria cap, the architect's component /
    interface / ownership caps). No gate, router, or admission path reads it.

    Honest limit on that: the gate SET is tier-independent, but acceptance
    criteria are the specification those gates verify against, so capping their
    count does thin what gets checked. A DEMO build is a smaller build passing
    the same gates — not an equally-assured one. What the caps may never do is
    drop a criterion a gate structurally REQUIRES (see the directives in
    ``foundry.agents._common``), which would fail the run rather than shrink it.

    The model supplies ``tier``/``rationale``/``signals`` only; the concrete caps
    are a deterministic table in ``foundry.agents._common`` (agents propose
    judgment, code applies policy — the same split as the gate two-zone rule).
    """

    tier: ScopeTier
    rationale: str  # why this tier (carries "pinned by the operator" when flagged)
    signals: tuple[str, ...] = ()  # the request phrases that triggered DEMO
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class SignOffRecord(FrozenModel):
    """Approver-of-record sign-off for a Gated release / HITL decision
    (governance.md §1e/§6, domains.md §1 class-4 human-witnessed).

    `approver`/`role` are the named accountable identity + its distinct role
    (admission REJECTS when the approver intersects the author set or lacks the
    required role — segregation of duties is an enforced predicate, gov §1e).
    `server_nonce` is the per-approval server-issued challenge bound into the
    class-4 attestation so a sign-off cannot be minted or replayed (gov §6);
    `presented_evidence_hash` is the content hash of the EXACT evidence pack
    shown to the signer, so the human signs over what was presented.

    `judgment_provenance` is the self-judge stamp for the CHOSEN profile
    (governance.md §1e solo-lineage profile): SELF when a same-lineage judge
    issued the residual judgment verdict (recorded, never hidden — the scorecard
    MUST NOT claim independent validation for it), INDEPENDENT otherwise. The
    MODEL never supplies it; the proxy/runner stamps it from the active profile.
    """

    subject_artifact_uri: str  # what was signed off (the attested artifact)
    state_sha: str  # the integrated state the sign-off binds to
    approver: str  # approver-of-record — named accountable human identity
    role: str  # the distinct required role (segregation of duties, gov §1e)
    approved: bool
    server_nonce: str  # per-approval server-issued challenge (gov §6)
    presented_evidence_hash: str  # hash of the exact evidence pack shown (gov §6)
    judgment_provenance: JudgmentProvenance  # self-judge profile stamp (gov §1e)
    meaning_of_signing: str = ""  # what the signature attests (gov §1c class-4)
    note: str = ""
    seq: Seq
