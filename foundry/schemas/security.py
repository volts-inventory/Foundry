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


class AuditFinding(FrozenModel):
    """Audit/Compliance ADVISORY triage (agents.md Layer 5; gov §1c).

    The Audit/Compliance agent's SOP emits findings (disallowed-license /
    critical-CVE / control-mapping gaps), a license report, and a compliance
    map — all ADVISORY. The authoritative Audit verdict (SBOM/CVE/license) is
    re-derived and signed by the trusted runner, never claimed here. Bodies
    (license report, compliance/control map) are content-addressed handles
    (DS9); this record is the agent-side reference + triage severity.
    """

    run_against: str  # tree_sha the audit triaged
    sbom_uri: str  # CAS handle to the agent-side SBOM reference (DS9)
    severity: Severity  # highest severity among findings — advisory, not a verdict
    findings: tuple[str, ...] = ()  # disallowed license / critical CVE / control gap
    license_report_uri: str | None = None  # CAS handle (DS9)
    compliance_map_uri: str | None = None  # control-mapping body (DS9)
    version: int = 1
    supersedes: str | None = None
    seq: Seq


# ===========================================================================
# Phase-4 (Trust root) artifact schemas. (docs/governance.md §4–§5,
# docs/implementation-plan.md §6 + §0.3)
#
# These records are the input-bound, content-addressed evidence the supply-chain
# trust root produces and the deploy-time admission controller re-verifies:
# the authoritative re-derivable SBOM (fuller than the agent-side `SBOMRef` handle).
# They follow the same conventions as the records above: FrozenModel (frozen +
# extra="forbid"), `seq: Seq` on every persisted/channel record, bodies as
# content-addressed `*_uri` handles (DS9/L4), `default_factory` for mutable
# defaults (S1), reuse of `Severity`.
#
# DEV-GRADE-ROOTS HONESTY (implementation-plan.md §0.3): these records, their
# input-binding, and the re-verification logic that consumes them are REAL and
# CORRECT software. Their cryptographic ROOT is dev-grade — the builder id /
# OIDC identity / signing key custody / hermetic isolation are local-dev stand-ins
# (real KMS / SLSA-L3 hardware root / air-gapped builders are an ops deployment
# task), NOT a production root of trust. The discipline is real; only the root is
# dev-grade.
# ===========================================================================


class SBOMComponent(FrozenModel):
    """One resolved component in the authoritative SBOM (governance.md §4 SBOM
    pass criteria: full resolution, no unpinned components).

    `name` + `version` + `digest` (its content address) + `license` (SPDX id) are
    the fields the Audit gate's deterministic SBOM/CVE/license re-derivation reads.
    A component with no pinned `digest` is an unpinned component the Audit gate
    fails on (gov §4)."""

    name: str
    version: str
    digest: str  # content address — "sha256:<hex>" (no unpinned components, §4)
    license: str | None = None  # SPDX id — license-compatibility input (§4)
    purl: str | None = None  # package URL (ecosystem coordinate)


class SBOM(FrozenModel):
    """The AUTHORITATIVE, fuller SBOM record (governance.md §4) — distinct from
    the agent-side `SBOMRef` handle (which is just a CAS pointer the trusted CI
    builder re-derives from).

    Carries the resolved component inventory inline (`components` — names,
    versions, digests, licenses) so the Audit gate's deterministic re-derivation
    (no disallowed-license / critical-CVE deps; full resolution, no unpinned
    components — gov §4) has a content-addressed, re-checkable input. `tree_sha`
    is the tree the SBOM was generated for; `sbom_uri` keeps the out-of-band body
    handle for the large canonical document (DS9).

    DEV-GRADE ROOT (plan §0.3): the inventory + binding are real; the trusted
    builder that AUTHORITATIVELY re-derives it in a hermetic environment is an ops
    deployment concern.
    """

    tree_sha: str  # the tree the SBOM was generated for
    sbom_uri: str  # CAS handle to the full canonical SBOM body (DS9)
    components: tuple[SBOMComponent, ...] = ()  # resolved component inventory (§4)
    format: str = "cyclonedx-1.5"  # the SBOM document format
    fully_resolved: bool = True  # no unpinned components (§4 pass criterion)
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def unpinned_components(self) -> tuple[str, ...]:
        """Component names lacking a pinned digest — the Audit gate fails on any
        non-empty result (full resolution / no unpinned components, gov §4)."""
        return tuple(c.name for c in self.components if not c.digest)
