"""Checkpoint serializer with Foundry's schema types explicitly allowed.

LangGraph's msgpack serde will block deserialization of unregistered types;
we allowlist exactly our frozen pydantic records rather than disabling the
check (the allowlist is part of the trust posture — checkpoint contents are
load-bearing state).
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
]


def foundry_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=FOUNDRY_MSGPACK_MODULES)
