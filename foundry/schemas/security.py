"""Security & compliance artifacts (agents.md Layer 5 — Security & Compliance).

Security/AppSec → `ThreatModel` (per-item oracle bindings) + `SecurityTriage`;
Audit/Compliance → `SBOMRef`; Supply-Chain Reviewer → `SupplyChainReport`.
None of these is a verdict: Security/Audit/Supply-chain verdicts are computed
and signed by the trusted runner (governance.md §1c); agents only triage.
Bodies are content-addressed handles (DS9).
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ThreatModel(FrozenModel):
    """AppSec output (agents.md Layer 5): STRIDE threat model whose items are
    each bound to a concrete runner-evaluable oracle at the Design seam.

    Items that cannot be reduced to an oracle are flagged residual and go to
    the Threat-review judgment sub-gate (independent review + HITL), NOT into
    the runner-signed Security verdict (governance.md §1c/§1d).
    """

    threat_model_uri: str  # CAS handle to the STRIDE body (DS9)
    run_against: str  # tree_sha / design ref
    oracle_bindings: dict[str, str]  # threat item id -> runner-evaluable oracle uri
    residual_item_ids: tuple[str, ...] = ()  # no oracle -> Threat-review sub-gate
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def has_residual(self) -> bool:
        """True when judgment items exist that the runner-signed Security
        verdict must NOT absorb (governance.md §1d)."""
        return bool(self.residual_item_ids)


class SecurityTriage(FrozenModel):
    """AppSec manual-review triage (agents.md Layer 5): which items to add,
    severity, remediation — advisory only, NEVER the verdict bit (the Security
    verdict is computed & signed by the trusted runner, governance.md §1c)."""

    run_against: str  # tree_sha
    severity: Severity  # highest severity among findings
    findings: tuple[str, ...] = ()
    seq: Seq


class SupplyChainReport(FrozenModel):
    """Supply-Chain Reviewer output (agents.md Layer 5): the agent-authored
    build is reviewed as a high-privilege artifact — CI/IaC/Dockerfile/
    lockfile/manifest/scanner-config diffed against baseline."""

    base_tree_sha: str  # the reviewed baseline
    tree_sha: str  # the proposed tree
    report_uri: str  # CAS handle to the full report body (DS9)
    policy_violations: tuple[str, ...] = ()  # build-time network/hooks, egress widening...
    trust_root_touched: bool = False  # signing/OIDC-trust changes — HITL required (gov §1e)
    seq: Seq


class SBOMRef(FrozenModel):
    """Audit/Compliance SBOM handle (agents.md Layer 5) — content-addressed;
    the authoritative SBOM is re-derived by the trusted CI builder, this is
    the agent-side reference into the CAS (DS9)."""

    sbom_uri: str
    tree_sha: str  # the tree the SBOM was generated for
    seq: Seq
