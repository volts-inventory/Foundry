"""Security & Compliance layer (agents.md Layer 5) — node factories.

Three ``make_<agent>_node(rt: AgentRuntime) -> NodeFn`` factories, all ADVISORY
only (LEARNINGS L5): each authors a content-addressed body handle on
``artifacts`` and an advisory ``GateTriage`` on ``gate_triage`` — NONE writes a
``GateResult`` to the ``gates`` channel. The trusted two-zone gate runner
computes + signs the Security / Audit / Supply-chain verdicts later in the
assurance fan-out (governance §1c); the agents only triage.

* :func:`make_appsec_engineer_node` — authors a ``ThreatModel`` (STRIDE, per-item
  oracle bindings, residual items -> Threat-review sub-gate) and an advisory
  ``security`` ``GateTriage`` that flags any HIGH/CRITICAL finding as BLOCKING.
* :func:`make_audit_compliance_node` — authors an ``AuditFinding`` (SBOM /
  license / compliance advisory) and an advisory ``audit`` ``GateTriage``.
* :func:`make_supply_chain_reviewer_node` — authors a ``SupplyChainReport``
  (CI/IaC/lockfile policy advisory) and an advisory ``supply_chain``
  ``GateTriage`` (policy violation / trust-root change -> BLOCKING/HITL).

Every node follows the shared seam (AR1-AR4): mint a ``Seq`` via ``rt.seq``,
reach the single egress through ``rt.proxy.complete`` (L6), content-address
bodies behind ``cas://`` handles (DS9 / L4). No node imports ``anthropic``.
"""

from __future__ import annotations

from foundry.agents.security.appsec_engineer import make_appsec_engineer_node
from foundry.agents.security.audit_compliance import make_audit_compliance_node
from foundry.agents.security.supply_chain_reviewer import (
    make_supply_chain_reviewer_node,
)

__all__ = [
    "make_appsec_engineer_node",
    "make_audit_compliance_node",
    "make_supply_chain_reviewer_node",
]
