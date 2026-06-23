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
    # Agent-authored release-doc bodies (foundry/docs/writers.py), emitted as
    # content-addressed artifacts like every other stage's output.
    ("foundry.schemas.docs", "Runbook"),
    ("foundry.schemas.docs", "KnownIssue"),
    ("foundry.schemas.docs", "KnownIssues"),
    ("foundry.schemas.docs", "UpgradeGuide"),
    ("foundry.schemas.docs", "PerfCostReport"),
]


def foundry_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=FOUNDRY_MSGPACK_MODULES)
