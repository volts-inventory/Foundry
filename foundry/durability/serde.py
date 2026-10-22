"""Checkpoint + artifact-body serializer with Foundry's schema types allowed.

LangGraph's msgpack serde will block deserialization of unregistered types;
we allowlist exactly our frozen pydantic records rather than disabling the
check (the allowlist is part of the trust posture — checkpoint contents AND the
out-of-band artifact bodies the ``ArtifactStore`` (de)serializes are load-bearing
state). A type NOT on this list degrades to a ``dict`` on load, which the store
rejects fail-closed (DECISIONS D6) — so every typed artifact BODY a node emits
must appear here to round-trip back to its frozen model.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

FOUNDRY_MSGPACK_MODULES: list[tuple[str, str]] = [
    ("foundry.schemas.base", "Artifact"),
    ("foundry.schemas.base", "LogRef"),
    ("foundry.schemas.base", "ExternalAction"),
    ("foundry.schemas.base", "IdempotencyPath"),
    ("foundry.schemas.base", "BudgetLedger"),
    ("foundry.schemas.gates", "GateResult"),
    ("foundry.schemas.gates", "GateTriage"),
    ("foundry.schemas.gates", "GateStatus"),
    ("foundry.schemas.gates", "VerdictClass"),
    ("foundry.schemas.gates", "JudgmentProvenance"),
    # Advisory `agent_journal` channel record (plan [11]) — small, rides the
    # channel directly (like GateTriage), so it must round-trip the checkpoint.
    ("foundry.schemas.journal", "JournalEntry"),
    # Human-steering `checkpoint_decisions` channel records (plan [10]) — small,
    # ride the channel directly, so they must round-trip the checkpoint. NOT a
    # trust input: a checkpoint approve can never turn a gate FAIL into a pass.
    ("foundry.schemas.checkpoint", "CheckpointDecision"),
    ("foundry.schemas.checkpoint", "CheckpointVerdict"),
    ("foundry.schemas.checkpoint", "CheckpointBoundary"),
    # Phase-2 channel records that ride `risk_tier` / `sign_offs` directly. Both
    # are read as TYPED objects after a resume — `assurance_gate_set` reads
    # `tier.tier` and the release boundary filters `s.role` — so a type missing
    # here degrades the record to a dict and raises AttributeError mid-run.
    # ChangeRiskTier is the class behind the `RiskTier` alias TierDecision carries.
    ("foundry.schemas.governance", "TierDecision"),
    ("foundry.schemas.governance", "ChangeRiskTier"),
    ("foundry.schemas.governance", "SignOffRecord"),
    # `scope_tier` single-writer channel record — rides the channel directly
    # (like GateTriage), so it must round-trip the checkpoint or a resumed run
    # loses the tier and silently reverts to STANDARD prompts mid-build. NOT a
    # trust input: no gate reads it (ScopeDecision.relaxes_integrity()).
    ("foundry.schemas.governance", "ScopeDecision"),
    ("foundry.schemas.governance", "ScopeTier"),
    ("foundry.schemas.architecture", "PinnedConstraint"),
    ("foundry.schemas.architecture", "ConstraintScope"),
    ("foundry.state.seq", "Seq"),
    # Out-of-band artifact BODIES the ArtifactStore (de)serializes (D6). Every
    # typed body a node emits via emit_artifact(..., store=rt.cas), plus the
    # FeatureDiff body gate_wiring resolves — and the enums those bodies carry.
    ("foundry.schemas.product", "IntentBrief"),
    ("foundry.schemas.product", "PRD"),
    ("foundry.schemas.product", "AcceptanceCriterion"),
    ("foundry.schemas.product", "CriteriaCritique"),
    ("foundry.schemas.product", "SprintPlan"),
    ("foundry.schemas.product", "Criticality"),
    ("foundry.schemas.architecture", "SystemDesign"),
    ("foundry.schemas.architecture", "ADR"),
    ("foundry.schemas.architecture", "InterfaceContract"),
    ("foundry.schemas.architecture", "OwnershipMap"),
    ("foundry.schemas.implementation", "Task"),
    ("foundry.schemas.implementation", "FeatureDiff"),
    ("foundry.schemas.implementation", "ConflictResolution"),
    ("foundry.schemas.implementation", "TaskStatus"),
    ("foundry.schemas.implementation", "Ticket"),
    ("foundry.schemas.implementation", "Specialty"),
    ("foundry.schemas.implementation", "Difficulty"),
    ("foundry.schemas.quality", "TestPlan"),
    ("foundry.schemas.quality", "OracleBinding"),
    ("foundry.schemas.quality", "TestReport"),
    ("foundry.schemas.quality", "E2EReport"),
    ("foundry.schemas.quality", "OracleKind"),
    ("foundry.schemas.security", "ThreatModel"),
    ("foundry.schemas.security", "SecurityTriage"),
    ("foundry.schemas.security", "SupplyChainReport"),
    ("foundry.schemas.security", "SBOMRef"),
    ("foundry.schemas.security", "AuditFinding"),
    ("foundry.schemas.security", "Severity"),
    # Upstream narrative body (PM/architect prose) persisted via put_body_at.
    # (The removed release-doc bodies — Runbook etc. — were CAS-only and are never
    # read back, so dropping them here cannot break loading an old checkpoint.)
    ("foundry.schemas.docs", "ProseBody"),
]


def foundry_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=FOUNDRY_MSGPACK_MODULES)
