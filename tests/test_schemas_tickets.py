"""Ticket-driven schema: the shared contract the planner, scheduler, BDD gate and
Workshop all build against."""

from __future__ import annotations

import pytest

from foundry.durability.serde import foundry_serde
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import ChangeRiskTier, SignOffRecord, TierDecision
from foundry.schemas.implementation import (
    BDD_REPORT_ENV,
    Difficulty,
    Specialty,
    StackDecision,
    Task,
    Ticket,
    Toolchain,
)
from foundry.schemas.product import AcceptanceCriterion
from foundry.schemas.security import (
    AuditFinding,
    SBOMRef,
    SecurityTriage,
    Severity,
    SupplyChainReport,
    ThreatModel,
)
from foundry.state.seq import Seq

SEQ = Seq("r", 1, 1, "planner", 0)
SHA = "c" * 40

RECORDS: list[FrozenModel] = [
    Ticket(id="T-1", title="x", specialty=Specialty.TEST, difficulty=Difficulty.TRIVIAL,
           depends_on=("T-0",), seq=SEQ),
    Ticket(id="T-1", title="x", specialty=Specialty.BACKEND, reads=("src/a.ts",), seq=SEQ),
    AcceptanceCriterion(id="AC-1", text="Given a When b Then c", given=("a",), when=("b",),
                        then=("c",), ticket_id="T-1", seq=SEQ),
    Task(id="t", spec_uri="u", criterion_ids=("AC-1",), branch="task/t", ticket_id="T-1",
         specialty=Specialty.TEST, tier="T3", seq=SEQ),
    StackDecision(language="typescript", package_manager="npm", install="npm ci",
                  typecheck="npx tsc --noEmit",
                  bdd=f"npx cucumber-js --format message:${BDD_REPORT_ENV}"),
    Toolchain(language="go", binaries=("go",), version="1.24"),
    InterfaceContract(name="TodoStore.add", spec_uri="cas://c", kind="function",
                      signature="add(text: string): Todo", seq=SEQ),
    # Checkpoint-compat records: no longer written, but an old checkpoint carrying
    # them (risk_tier / sign_offs channels, security bodies) must still load typed.
    TierDecision(tier=ChangeRiskTier.HIGH, rationale="r", run_against=SHA,
                 gate_set=("build",), forced_high=True, seq=SEQ),
    SignOffRecord(subject_artifact_uri="cas://release/1", state_sha=SHA, approver="alice",
                  role="release-manager", approved=True, server_nonce="sn-1",
                  presented_evidence_hash="sha256:" + "a" * 64,
                  judgment_provenance=JudgmentProvenance.SELF, seq=SEQ),
    ThreatModel(threat_model_uri="cas://tm/1", run_against=SHA,
                oracle_bindings={"TM-1": "cas://oracle/3"}, residual_item_ids=("TM-2",),
                seq=SEQ),
    SecurityTriage(run_against=SHA, severity=Severity.HIGH, findings=("f",), seq=SEQ),
    SupplyChainReport(base_tree_sha="a" * 40, tree_sha=SHA, report_uri="cas://sc/1", seq=SEQ),
    SBOMRef(sbom_uri="cas://sbom/1", tree_sha=SHA, seq=SEQ),
    AuditFinding(run_against=SHA, sbom_uri="cas://sbom/1", severity=Severity.LOW, seq=SEQ),
]


@pytest.mark.parametrize("obj", RECORDS, ids=lambda o: type(o).__name__)
def test_records_round_trip_the_checkpoint_serde(obj: FrozenModel) -> None:
    serde = foundry_serde()
    back = serde.loads_typed(serde.dumps_typed(obj))
    assert type(back) is type(obj) and back == obj
