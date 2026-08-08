"""Governance / meta artifacts (agents.md Layer 7 — Governance / Meta).

Orchestrator/Triage → `TriageDecision`; Guardian → `GuardianDecision`;
Budget Controller → `BudgetDecision`; Human Liaison → `HumanDecision`
(recorded in `human_decisions` and the WORM audit store).
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
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

    def relaxes_integrity(self) -> bool:
        """Tiering NEVER relaxes admission/integrity — always False by design.

        Present so callers can assert the invariant in routing/admission rather
        than rely on a comment (governance.md §1e: depth/HITL only)."""
        return False


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
    ambition. Both are triage records, neither is a bypass: this one is read
    only by agent PROMPTS (the PM's criteria cap, the architect's component /
    interface / ownership caps). No gate, router, or admission path reads it, so
    it can lower what agents propose but never what the signed gates require.

    The model supplies ``tier``/``rationale``/``signals`` only; the concrete caps
    are a deterministic table in ``foundry.agents._common`` (agents propose
    judgment, code applies policy — the same split as the gate two-zone rule).
    """

    tier: ScopeTier
    rationale: str  # why this tier
    signals: tuple[str, ...] = ()  # the request phrases that triggered DEMO
    source: str = "judge"  # "judge" (model-classified) | "flag" (operator-pinned)
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def relaxes_integrity(self) -> bool:
        """Scope tiering NEVER relaxes admission/integrity — always False.

        Mirrors :meth:`TierDecision.relaxes_integrity` so callers can assert the
        invariant rather than rely on a comment. A DEMO tier shrinks what the
        agents propose; the gates it must still pass are unchanged."""
        return False


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


class EvidencePack(FrozenModel):
    """Independent, non-agent-assembled release evidence pack (governance.md §6,
    SOP agents.md Layer 6 Release Manager).

    Travels with a Gated release: a HASH-CHAINED set of content-addressed
    references to the WORM-anchored gate results + artifacts + sign-offs keyed to
    one integrated `tree_sha`. Bodies are out-of-band CAS handles (DS9, L4); this
    record carries only the handles + the chain head so the chain is
    tamper-evident. `pack_uri` is the content-addressed handle of the assembled
    pack itself — its hash is what a `SignOffRecord.presented_evidence_hash`
    binds to, closing the what-you-see-is-what-you-sign gap (gov §6).
    """

    pack_uri: str  # CAS handle of the assembled pack body (DS9)
    run_against: str  # the integrated tree_sha this evidence pack is keyed to
    chain_head: str  # hash-chain head over the ordered references (tamper-evident)
    gate_result_uris: tuple[str, ...] = ()  # WORM-anchored attested verdicts (gov §4)
    artifact_uris: tuple[str, ...] = ()  # content-addressed artifact handles
    sign_off_uris: tuple[str, ...] = ()  # SignOffRecord handles (approvers of record)
    scanner_config_rev: str | None = None  # pinned external scanner-config revision (gov §2)
    provenance_predicate_uri: str | None = None  # SLSA/in-toto predicate handle (gov §4)
    version: int = 1
    supersedes: str | None = None
    seq: Seq
