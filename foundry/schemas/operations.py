"""Operations artifacts (agents.md Layer 6 — Operations).

DevOps/SRE → `Pipeline`, `IaCProposal` (plan-only), `DeployPlan`;
Release Manager → `Release`. IaC is PROPOSED, never applied, by agents: the
plan diff is gated by policy-as-code and a separate least-privileged
non-agent deployer applies it after admission (governance.md §2/§5).
Bodies are content-addressed handles (DS9); effects go through the outbox.
"""

from __future__ import annotations

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class Pipeline(FrozenModel):
    """CI/CD definition authored/verified by DevOps/SRE (agents.md Layer 6).
    The pipeline is itself a high-privilege artifact reviewed by the
    Supply-Chain Reviewer (agents.md Layer 5)."""

    pipeline_uri: str  # CAS handle to the pipeline definition (DS9)
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class IaCProposal(FrozenModel):
    """IaC `plan` PROPOSAL (agents.md Layer 6 — DevOps/SRE SOP): gated by
    policy-as-code (Conftest/Checkov, Supply-chain gate) and applied only by
    the non-agent deployer after admission (governance.md §2/§5)."""

    plan_uri: str  # CAS handle to the `terraform plan` diff (DS9)
    plan_only: bool = True  # MUST default True — agents never apply
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class DeployPlan(FrozenModel):
    """Deploy plan + rollback + health checks (agents.md Layer 6 — DevOps/SRE
    SOP). Execution belongs to the non-agent deployer, never this artifact."""

    plan_uri: str  # CAS handle to the deploy plan body (DS9)
    rollback_plan_uri: str  # CAS handle to the rollback procedure (DS9)
    health_checks: tuple[str, ...] = ()
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class Release(FrozenModel):
    """Release Manager output (agents.md Layer 6): semver + changelog + the
    independent evidence pack for human sign-off (governance.md §6).

    `server_nonce` is the per-approval server-issued challenge bound into the
    class-4 human attestation tuple, so the Release-gate sign-off cannot be
    minted or replayed by the pipeline (governance.md §6). Idempotent via the
    external-action ledger; never double-tags on resume.
    """

    semver: str
    artifact_digest: str  # the attested digest admission re-verifies (gov §5)
    changelog_uri: str  # CAS handle (DS9)
    evidence_pack_uri: str  # independent, NON-agent-assembled evidence pack (gov §6)
    server_nonce: str  # binds the human Release attestation (nonce binding field)
    seq: Seq
