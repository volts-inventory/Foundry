"""Gate records: verdicts (trusted infra) vs triage (agents). (gov §1c, arch §3)

The hard rule: **agents write `GateTriage`, never `GateResult`.** Deterministic
verdicts are computed and signed in zone B; judgment verdicts carry a binding
stamped by the proxy OUTSIDE the implementing adapter. `_merge_gates` enforces
both at fold time — an unsigned write is dropped, not errored, so a malicious
or buggy agent write can never supersede a trusted verdict.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import field_validator

from foundry.canonical import content_digest
from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # infra failure — NOT a verdict (DS7); joins handle it separately


class VerdictClass(StrEnum):
    """domains.md §1 — correctness is not one shape."""

    CONFIG_EVALUABLE = "config-evaluable"  # 1
    EXECUTION_DETERMINISTIC = "execution-deterministic"  # 2
    STATISTICAL = "statistical"  # 3 (alpha look-ledger applies)
    HUMAN_WITNESSED = "human-witnessed"  # 4 (server nonce)
    PERCEPTUAL = "perceptual"  # 5 (judgment / HITL)


class JudgmentProvenance(StrEnum):
    """Self-judge profile stamp (governance §1e, implementation-plan §0.2)."""

    SELF = "self"
    INDEPENDENT = "independent"


class GateResult(FrozenModel):
    name: str
    status: GateStatus
    decision_deterministic: bool
    execution_deterministic: bool
    verdict_class: VerdictClass
    # Zone-B signature over (name, run_against, scanner_config_rev,
    # lineage_map_revision, status, results_digest) — deterministic gates only:
    runner_attestation: str | None = None
    scanner_config_rev: str | None = None
    lineage_map_revision: str | None = None
    # Judgment verdicts only — binding stamped by the proxy, outside the adapter,
    # over (node_id, served_family set, status, verdict_nonce,
    # lineage_map_revision, state_sha=run_against, subject_artifact_uri):
    verdict_nonce: str | None = None
    judgment_binding: str | None = None
    served_family: frozenset[str] = frozenset()  # proxy-OBSERVED, never self-reported
    subject_artifact_uri: str | None = None
    judgment_provenance: JudgmentProvenance | None = None
    results_digest: str | None = None
    findings: tuple[str, ...] = ()
    retries: int = 0
    run_against: str  # per-branch "task/<id>" | integrated state_sha (arch §4)
    seq: Seq

    @field_validator("findings", mode="before")
    @classmethod
    def _coerce_findings(cls, v: object) -> object:
        return tuple(v) if isinstance(v, list) else v

    def content_identity(self) -> str:
        """Identity for nonce-consumption keying: re-folding the SAME verdict is
        idempotent; a different verdict re-using the nonce is replay. Excludes
        `seq` and `retries` so a re-emitted identical verdict (resume) coalesces
        instead of being treated as a replay of its own nonce. (DS-R22-F1)
        """
        d = self.model_dump(mode="json", exclude={"seq", "retries"})
        d["served_family"] = sorted(self.served_family)
        return content_digest(d)

    def latch_key(self) -> tuple[str, str, str]:
        """Cross-fork-stable negative-verdict latch key. (arch §3)"""
        return (self.seq.node_id, self.subject_artifact_uri or "", self.run_against)


class GateTriage(FrozenModel):
    """Agents write THIS — advisory remediation notes, never a verdict."""

    name: str
    run_against: str
    explanation: str
    produced_by: str
    seq: Seq

    def union_key(self) -> tuple[str, str, str]:
        return (self.name, self.run_against, self.produced_by)
