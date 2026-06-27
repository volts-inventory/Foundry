"""Audit / Compliance node (agents.md Layer 5 — T2).

SOP (agents.md Layer 5): generate an SBOM reference -> check license
compatibility + a critical-CVE floor -> map findings to applicable controls
(domain-pack dependent) -> file ADVISORY findings. The authoritative Audit
verdict (SBOM/CVE/license) is re-derived and signed by the trusted CI builder /
runner, never claimed here (governance §1c); signing/SLSA attestation is NOT
this agent's job.

Output: an :class:`AuditFinding` (advisory triage with SBOM/license/compliance
handles) carried as a content-addressed handle on ``artifacts``, AND an advisory
:class:`GateTriage` on the ``gate_triage`` channel naming the ``audit`` gate.
The agent NEVER mints a ``GateResult`` (LEARNINGS L5) and NEVER writes to the
``gates`` channel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.security._common import (
    blocking_triage,
    cas_uri,
    emit_artifact,
    run_against,
)
from foundry.schemas.security import AuditFinding, Severity

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "audit_compliance"
_TIER = "T2"
_AUDIT_GATE = "audit"

SOP = (
    "You are the Audit / Compliance agent (Layer 5). Follow this SOP, in order:\n"
    "1. Generate an SBOM for the tree.\n"
    "2. Check license compatibility (flag disallowed licenses) and a "
    "critical-CVE floor (flag any critical CVE).\n"
    "3. Map findings to applicable controls (note any control-mapping gap).\n"
    "4. Summarize a license report and a compliance/control map.\n"
    "Set severity to the highest among the findings. Respond in the constrained "
    "JSON schema. You produce ADVISORY findings only; the authoritative Audit "
    "verdict is re-derived and signed by the trusted runner — you do NOT decide "
    "the gate verdict."
)

# The model fills the audit content; the node owns the Seq, the body CAS
# handles, and the run_against ref.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sbom_body": {"type": "string"},
        "license_report_body": {"type": "string"},
        "compliance_map_body": {"type": "string"},
        "severity": {"type": "string", "enum": [s.value for s in Severity]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sbom_body", "severity", "findings"],
    "additionalProperties": False,
}


def make_audit_compliance_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Audit / Compliance node (LEARNINGS AR3).

    Emits an ``AuditFinding`` handle + an advisory ``GateTriage`` — never a
    ``GateResult`` (L5)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        finding_seq = rt.seq(config, _NODE_ID, "artifact:audit_finding")
        triage_seq = rt.seq(config, _NODE_ID, "gate_triage:audit")
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

        severity = Severity(str(payload.get("severity", Severity.LOW.value)))
        findings = tuple(str(f) for f in payload.get("findings", ()))
        license_body = payload.get("license_report_body")
        compliance_body = payload.get("compliance_map_body")

        finding = AuditFinding(
            run_against=run_ref,
            sbom_uri=cas_uri(str(payload.get("sbom_body", ""))),
            severity=severity,
            findings=findings,
            license_report_uri=cas_uri(str(license_body)) if license_body else None,
            compliance_map_uri=(
                cas_uri(str(compliance_body)) if compliance_body else None
            ),
            seq=finding_seq,
        )
        handle = emit_artifact(
            finding, "audit_finding", _NODE_ID, finding_seq, store=rt.cas
        )
        triage = blocking_triage(
            name=_AUDIT_GATE,
            severity=severity,
            findings=findings,
            run_against=run_ref,
            seq=triage_seq,
            produced_by=_NODE_ID,
            noun="audit",
            blocking_suffix=(
                "The Audit gate runner must confirm SBOM/license/CVE "
                "before admission."
            ),
            empty="No license/CVE/control-mapping findings.",
        )

        # ADVISORY only — never a GateResult; the trusted runner re-derives and
        # signs the Audit verdict (LEARNINGS L5).
        return {"artifacts": [handle], "gate_triage": [triage]}

    return _node
