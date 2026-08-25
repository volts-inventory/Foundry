"""Security / AppSec Engineer node (agents.md Layer 5 — T1).

SOP (agents.md Layer 5): threat-model the integrated tree (STRIDE) -> bind each
threat-model item to a concrete runner-evaluable oracle at the Design seam ->
flag items that cannot be reduced to an oracle as residual (they go to the
Threat-review judgment sub-gate, not the runner-signed verdict, governance §1d)
-> manual review of auth/crypto/input handling, filing advisory triage (which
items to add, severity, remediation) — **never the verdict bit**.

Output: a content-addressed :class:`ThreatModel` handle (with per-item oracle
bindings) on ``artifacts``, AND an advisory :class:`GateTriage` on the
``gate_triage`` channel naming the ``security`` gate. When AppSec surfaces a
HIGH (or CRITICAL) severity finding the triage explanation is marked BLOCKING —
but AppSec NEVER mints a ``GateResult`` (LEARNINGS L5): the Security gate runner
computes and signs the actual verdict from the bound oracles + scans
(governance §1c). The agent also content-addresses a :class:`SecurityTriage`
body as the manual-review record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import skip_advisory_in_autonomous
from foundry.agents.security._common import (
    blocking_triage,
    cas_uri,
    emit_artifact,
    run_against,
)
from foundry.schemas.security import Severity, ThreatModel

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

import os as _os  # noqa: E402

_NODE_ID = "appsec_engineer"
# Security review defaults to Opus (T1) — don't cheap out on the security floor.
# Default Sonnet (T2): the ThreatModel is advisory/build-dead — the Security gate
# re-scans the materialized tree and reads no agent analysis — so Opus buys no gate
# quality here. FOUNDRY_SECURITY_TIER=T1 restores Opus when a human relies on the
# threat model at a checkpoint.
_TIER = _os.environ.get("FOUNDRY_SECURITY_TIER", "T2").strip() or "T2"
_SECURITY_GATE = "security"

SOP = (
    "You are the Security / AppSec Engineer (Layer 5). Threat-model the "
    "integrated tree. Follow this SOP, in order:\n"
    "1. Enumerate STRIDE threats; give each a stable item id.\n"
    "2. Bind each item to a concrete runner-evaluable oracle (a test, a "
    "SAST/Semgrep rule, or a negative-oracle) — describe the oracle per item.\n"
    "3. Items that cannot be reduced to an oracle are RESIDUAL (they go to the "
    "Threat-review judgment sub-gate, not the runner-signed verdict).\n"
    "4. Manually review auth/crypto/input handling and file findings with a "
    "severity (low/medium/high/critical) and remediation.\n"
    "Respond in the constrained JSON schema. You produce a threat model + "
    "advisory triage only; you do NOT decide the gate verdict."
)

# The model fills the threat content; the node owns the Seq, the body CAS
# handles, and the run_against ref, so those are excluded from the schema.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "threat_model_body": {"type": "string"},
        "oracle_bindings": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "residual_item_ids": {"type": "array", "items": {"type": "string"}},
        "severity": {
            "type": "string",
            "enum": [s.value for s in Severity],
        },
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["threat_model_body", "oracle_bindings", "severity", "findings"],
    "additionalProperties": False,
}


def make_appsec_engineer_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the AppSec Engineer node (LEARNINGS AR3).

    Emits a ``ThreatModel`` handle + an advisory ``GateTriage`` flagging any
    high-severity finding as BLOCKING — never a ``GateResult`` (L5)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        if skip_advisory_in_autonomous(state):
            return {}  # build-dead in a headless run; the Security gate re-scans the tree
        tm_seq = rt.seq(config, _NODE_ID, "artifact:threat_model")
        triage_seq = rt.seq(config, _NODE_ID, "gate_triage:security")
        run_ref = run_against(config)

        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": state.get("request", "")}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        oracle_bindings = {
            str(k): str(v) for k, v in (payload.get("oracle_bindings") or {}).items()
        }
        threat_model = ThreatModel(
            threat_model_uri=cas_uri(str(payload.get("threat_model_body", ""))),
            run_against=run_ref,
            oracle_bindings=oracle_bindings,
            residual_item_ids=tuple(
                str(i) for i in payload.get("residual_item_ids", ())
            ),
            seq=tm_seq,
        )
        handle = emit_artifact(
            threat_model, "threat_model", _NODE_ID, tm_seq, store=rt.cas
        )

        severity = Severity(str(payload.get("severity", Severity.LOW.value)))
        findings = tuple(str(f) for f in payload.get("findings", ()))
        triage = blocking_triage(
            name=_SECURITY_GATE,
            severity=severity,
            findings=findings,
            run_against=run_ref,
            seq=triage_seq,
            produced_by=_NODE_ID,
            noun="security",
            blocking_suffix=(
                "The Security gate runner must confirm the bound oracles "
                "before admission."
            ),
            empty="No security findings surfaced by manual review.",
        )

        # Agents write GateTriage (advisory), NEVER a GateResult — the Security
        # gate runner computes + signs the verdict from the bound oracles (L5).
        return {"artifacts": [handle], "gate_triage": [triage]}

    return _node
