"""Phase-5 EXIT integration test — the three exit criteria END-TO-END + the
open/closed (add-a-pack-without-core-edits) invariant (T-M5-09).

This is the BINDING Phase-5 acceptance test (docs/roadmap.md Phase 5 / SPEC.md):

* (a) a REGULATED request makes Autonomous / auto-approve UNREACHABLE and binds a
  human approver-of-record — a hands-off autonomous run deploys NOTHING (no recorded
  HUMAN approver ⇒ no release, fail-closed); a Gated release WITH a recorded human
  approver-of-record proceeds; a self-judge-only release is BLOCKED;
* (b) an ML request keys its model-eval verdict on ``state_sha`` with a class-3
  statistical gate + a compute budget — a data/weight change RE-KEYS the verdict
  (invalidating reuse) and the compute budget fail-closes on overspend;
* (c) a UI request forces a VISUAL HITL — a class-5 perceptual gate that is never
  auto-approved; a self-judge clean UI run is BLOCKED at the release boundary;
* (d) a BRAND-NEW 4th pack (defined inside this test) is added via
  ``registry.register(...)`` and ENGAGES end-to-end — WITHOUT editing any core
  module (contract / registry / supervisor / factory_graph). Adding a pack is a
  contract registration, not a core edit (the open/closed invariant).

Each criterion is proven GENUINELY (non-vacuously): driven end-to-end through the
wired graph where possible, over the SAME end-to-end doubles the Phase-2/4
supervisor + autonomous tests use (LEARNINGS D1/D3/D4/SUP7): a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API key) + a
dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner` admitted through the
REAL ``merge_gates`` path, a :class:`StubAdapter`, in-memory ``resolve`` /
``resolve_blob`` materialization seams, and (for the autonomous case) the REAL
trust-root chain (:class:`TrustedBuilder` / :class:`AdmissionController` /
:class:`Deployer`).

LEARNINGS honesty note (genuine E2E vs dev-grade stand-in):

* GENUINE end-to-end (the real wired graph + the real reducers/runner/trust root):
  the regulated-Autonomous-unreachable + no-hands-off-deploy proof (criterion a),
  the regulated Gated release WITH a recorded human approver-of-record, the UI/ML
  back-compat spine runs, and the 4th-pack engagement through ``build_supervisor_graph``.
* DEV-GRADE STAND-IN (the unit-level mechanism the graph reads, exercised honestly
  but not yet wired as a graph node in Phase 5): the class-3 ``state_sha`` re-key
  (``ml_state_sha`` / ``ml_verdict_rebinds``) and the compute-budget overspend
  fail-close (``ComputeBudgetController``) are proven against the REAL core
  machinery the MLDataPack composes, not a mock; the class-5 perceptual + class-4
  approver REQUIREMENTS are proven via the REAL ``gate_requirements_for`` /
  ``satisfies`` taxonomy. The graph-side enforcement reads exactly these facts
  (``pack_release_admitted`` / ``autonomous_pack_reachable``), so the unit facts and
  the E2E boundary share one code path — no parallel logic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.domains.contract import (
    BaseDomainPack,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    StateInputPolicy,
    VerdictClassPolicy,
)
from foundry.domains.governance_hooks import (
    bind_approver_of_record,
    resolve_effective_mode_tier,
)
from foundry.domains.packs.frontend import PERCEPTUAL_GATE, FrontendPack
from foundry.domains.packs.ml_data import MODEL_EVAL_GATE, RETRAIN_GATE, MLDataPack
from foundry.domains.packs.regulated import RegulatedPack
from foundry.domains.registry import PackRegistry
from foundry.domains.state_sha import (
    ComputeBudget,
    ComputeBudgetController,
    ComputeOverspendError,
    ml_state_sha,
    ml_verdict_rebinds,
)
from foundry.domains.verdict_class import (
    Class3Binding,
    GroundingKind,
    InvalidClass3BindingError,
    assert_class3_binds_ledger,
    autonomous_reachable,
    gate_requirements_for,
    satisfies,
)
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import (
    autonomous_pack_reachable,
    build_factory_graph,
    pack_release_admitted,
)
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    build_supervisor_graph,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.schemas.governance import RiskTier
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

KEY = b"dev-zone-b-phase5-exit-key-0123456789abcdef"
KEY_ID = "dev-key-phase5-exit"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
BUILDER_ID = "dev-trusted-builder-phase5-exit"
DEPLOY_TARGET = "prod"

_DIFF_BODY_URI = "cas://test/diff-body"


# ---- scripted per-node payloads (free-text completions, SOP-keyed) ----------

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user"],
    "unknowns": [],
    "success_criteria": ["a user can add a todo"],
    "must_haves": ["add todo"],
    "nice_to_haves": [],
}
PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos.",
    "required_capabilities": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "normal",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401; negative oracle",
            "security_abuse": True,
        },
    ],
}
CRITIQUE_PAYLOAD: dict[str, Any] = {"missing": [], "security_abuse_omissions": []}
SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "none",
    "done_definition": "all criteria green",
}
DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "design_body": "api -> store",
    "components": ["api"],
    "adrs": [],
    "interfaces": [],
    "ownership": {"app.py": "owner-a"},
    "pinned_constraints": [],
}
DECOMPOSITION_PAYLOAD: dict[str, Any] = {
    "tasks": [{"unit": "app.py", "title": "build app", "criterion_ids": ["AC-001"]}]
}
QA_PAYLOAD: dict[str, Any] = {
    "tests": [{"test_id": "t-1", "criterion_ids": ["AC-001", "AC-002"]}],
    "quarantined": [],
}
SDET_PAYLOAD: dict[str, Any] = {
    "scenarios": [{"scenario_id": "e2e-1", "criterion_ids": ["AC-001"]}],
    "quarantined": [],
}
APPSEC_PAYLOAD: dict[str, Any] = {
    "threat_model_body": "STRIDE model",
    "oracle_bindings": {"S1": "test_auth"},
    "residual_item_ids": [],
    "severity": "low",
    "findings": [],
}
AUDIT_PAYLOAD: dict[str, Any] = {"sbom_body": "sbom", "severity": "low", "findings": []}
SUPPLY_CHAIN_PAYLOAD: dict[str, Any] = {
    "report_body": "supply-chain review",
    "policy_violations": [],
    "trust_root_touched": False,
}

_SCRIPTS: dict[str, str] = {
    "Stakeholder / Client Proxy": json.dumps(INTENT_PAYLOAD),
    "Product Manager": json.dumps(PRD_PAYLOAD),
    "Criteria-Completeness Critic": json.dumps(CRITIQUE_PAYLOAD),
    "Engineering / Program Manager": json.dumps(SPRINT_PAYLOAD),
    "Principal Architect": json.dumps(DESIGN_PAYLOAD),
    "Senior Engineer": json.dumps(DECOMPOSITION_PAYLOAD),
    "QA Engineer": json.dumps(QA_PAYLOAD),
    "SDET": json.dumps(SDET_PAYLOAD),
    "You are the Security / AppSec Engineer": json.dumps(APPSEC_PAYLOAD),
    "You are the Audit / Compliance agent": json.dumps(AUDIT_PAYLOAD),
    "You are the Supply-Chain Reviewer": json.dumps(SUPPLY_CHAIN_PAYLOAD),
}


# ---- in-memory materialization doubles --------------------------------------

# A clean unified-diff ADDITION: app.py + a passing test + a lockfile. Build+Test
# PASS; assurance Security/Audit PASS.
_CLEAN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)


def _synthetic_resolve(handle: Artifact) -> FrozenModel:
    """KIND-dispatching typed resolver (an in-memory infra double; SUP7)."""
    if handle.kind in {SystemDesign.__name__, "system_design"}:
        return SystemDesign(
            title="Todo service",
            design_uri="cas://design/body",
            components=("api",),
            interface_ids=(),
            seq=handle.seq,
        )
    if handle.kind == "ownership_map":
        return OwnershipMap(owners={"app.py": "owner-a"}, seq=handle.seq)
    if handle.kind == "acceptance_criterion":
        cid = f"AC-{handle.seq.local:03d}-{handle.seq.emission_epoch}"
        return AcceptanceCriterion(
            id=cid,
            text=f"Given/When/Then for {cid}",
            criticality=Criticality.HIGH_RISK,
            oracle_hint=f"oracle for {cid}",
            security_abuse=True,
            seq=handle.seq,
        )
    if handle.kind == "feature_diff":
        return FeatureDiff(
            diff_uri=_DIFF_BODY_URI,
            task_id=handle.task_id or "T1",
            branch=f"task/{handle.task_id or 'T1'}",
            base_tree_sha="base-sha",
            seq=handle.seq,
        )
    raise AssertionError(f"unexpected artifact kind for resolve: {handle.kind!r}")


def _resolve_blob_factory(diff_body: str) -> Any:
    def _resolve_blob(uri: str) -> bytes:
        return diff_body.encode("utf-8")

    return _resolve_blob


# ---- harness ----------------------------------------------------------------


def _runtime() -> AgentRuntime:
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": run_id,
            "verdict_nonce": f"N-{run_id}",
            "subject_artifact_uri": _DIFF_BODY_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
        "recursion_limit": 200,
    }


def _seq(node: str = "test") -> Seq:
    return Seq(run_id="r", emission_epoch=1, superstep_index=0, node_id=node, local=0)


def _src(tmp_path: Any) -> str:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    return str(src)


def _build_supervisor(
    records: GlobalRecords,
    source_dir: str,
    *,
    mode: str = MODE_GATED,
    registry: PackRegistry | None = None,
) -> Any:
    return build_supervisor_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(_CLEAN_DIFF),
        source_dir=source_dir,
        mode=mode,
        pack_registry=registry,
    ).compile(checkpointer=MemorySaver())


def _build_autonomous_factory(
    records: GlobalRecords,
    source_dir: str,
    *,
    registry: PackRegistry | None = None,
) -> Any:
    signer = ZoneBSigner(KEY, KEY_ID)
    return build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(_CLEAN_DIFF),
        source_dir=source_dir,
        checkpointer=MemorySaver(),
        mode=MODE_AUTONOMOUS,
        builder=TrustedBuilder(signer, builder_id=BUILDER_ID),
        admission=AdmissionController(builder_id=BUILDER_ID),
        deployer=Deployer(target=DEPLOY_TARGET),
        gate_verifier=DevGateWriteVerifier(KEY, KEY_ID, records),
        pack_registry=registry,
    )


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


def _deploy_actions(state: FactoryState) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "deploy"]


def _run_supervisor(
    tmp_path: Any, *, request: dict[str, Any], run_id: str, **kwargs: Any
) -> FactoryState:
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_supervisor(records, _src(tmp_path), **kwargs)
        return app.invoke(request, _config(run_id))  # type: ignore[no-any-return]
    finally:
        reset_gate_write_verifier(token)


def _release_subject_and_sha(state: FactoryState, config: RunnableConfig) -> tuple[str, str]:
    """The (subject_uri, state_sha) the release-boundary approver-of-record keys on
    — the SAME anchors ``pack_release_admitted`` reads (so a sign-off we record here
    binds the exact decision the boundary checks, not a parallel one)."""
    from foundry.agents.implementation.senior_engineer import DESIGN_KIND
    from foundry.governance.release_anchors import newest_artifact_uri, state_sha

    subject_uri = newest_artifact_uri(state, DESIGN_KIND)
    sha = state_sha(config)
    return subject_uri, sha


# =====================================================================
# Criterion (a): REGULATED → Autonomous unreachable + approver-of-record
# =====================================================================


def test_a_regulated_makes_autonomous_unreachable(tmp_path: Any) -> None:
    """(a-i) A regulated request requested as ``autonomous`` resolves to an effective
    GATED mode with Autonomous UNREACHABLE — the registry-generic mode/tier veto.

    Proven non-vacuously: the SAME request WITHOUT the regulated capability keeps
    Autonomous reachable, so the block is the pack's doing, not a constant."""
    registry = PackRegistry()
    registry.register(RegulatedPack())

    final = _run_supervisor(
        tmp_path,
        request={"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
        run_id="run-a-reg-cap",
        mode=MODE_AUTONOMOUS,
        registry=registry,
    )
    # The veto degraded the requested Autonomous mode to Gated at triage.
    assert final["mode"] == MODE_GATED
    tier = final.get("risk_tier")
    assert tier is not None
    assert tier.tier is RiskTier.HIGH  # regulatory-significance force-escalation
    assert tier.two_person_release is True
    assert tier.relaxes_integrity() is False

    # The registry-generic reachability fact the emit boundary reads: UNREACHABLE.
    assert (
        autonomous_pack_reachable(
            final, registry, requested_mode=MODE_AUTONOMOUS, seq=_seq()
        )
        is False
    )
    # NON-VACUOUS: drop the regulated capability and Autonomous stays reachable
    # (so the block above is the pack's veto, not an unconditional False).
    no_reg = {**final, "required_capabilities": ()}
    assert (
        autonomous_pack_reachable(
            no_reg, registry, requested_mode=MODE_AUTONOMOUS, seq=_seq()
        )
        is True
    )


def test_a_regulated_autonomous_run_deploys_nothing_without_human(tmp_path: Any) -> None:
    """(a-ii) END-TO-END: a regulated request driven through the REAL autonomous
    factory graph CANNOT hands-off deploy — no recorded HUMAN approver-of-record ⇒
    no release (fail-closed). The integrity gates still PASS, but NO deploy fires."""
    records = GlobalRecords()
    registry = PackRegistry()
    registry.register(RegulatedPack())
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous_factory(records, _src(tmp_path), registry=registry)
        final: FactoryState = app.invoke(
            {"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
            _config("run-a-reg-auto"),
        )
    finally:
        reset_gate_write_verifier(token)

    # The regulated change is assured (the integrity floor still passes)...
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    # ...but auto-approve / self-judge is REJECTED: no recorded human approver ⇒ the
    # hands-off emit deploys NOTHING (fail-closed; Autonomous unreachable).
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"


def test_a_self_judge_blocked_but_human_approver_admits(tmp_path: Any) -> None:
    """(a-iii) The approver-of-record hook is enforced at release, fail-closed AND
    non-vacuous: a self-judge-only state is NOT admitted, but recording a NAMED HUMAN
    two-person approver-of-record over the exact decision anchors DOES admit it.

    This proves the block is a missing HUMAN sign-off (not an unconditional refusal):
    the only thing that changes between blocked and admitted is the human record."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    config = _config("run-a-approver")

    # A Gated regulated run records a self-judge release sign-off (not a human).
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_supervisor(
            records,
            _src(tmp_path),
            mode=MODE_AUTONOMOUS,  # degrades to Gated
            registry=registry,
        )
        final: FactoryState = app.invoke(
            {"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
            config,
        )
    finally:
        reset_gate_write_verifier(token)

    # BLOCKED: the recorded self-judge release sign-off is not a HUMAN approver.
    assert (
        pack_release_admitted(final, config, registry, requested_mode=MODE_AUTONOMOUS)
        is False
    )

    # Now bind a NAMED HUMAN two-person approver-of-record over the EXACT anchors the
    # release boundary checks (segregation of duties — distinct identities + roles).
    subject_uri, sha = _release_subject_and_sha(final, config)
    policy = registry.compose(final)
    human_sign_offs = bind_approver_of_record(
        policy.approver,
        artifact_class="release",
        subject_artifact_uri=subject_uri,
        state_sha=sha,
        presented_evidence_hash="ev-hash-irrelevant-for-approver-class",
        server_nonce="human-nonce-1",
        approver="alice@example.com",
        role="release-engineer",
        profile="gated",
        seq=_seq("release"),
        second_approver="bob@example.com",
        second_role="quality-lead",
        second_nonce="human-nonce-2",
        second_seq=_seq("release2"),
    )
    assert len(human_sign_offs) == 2  # two-person recorded
    with_human = {**final, "sign_offs": (*(final.get("sign_offs") or ()), *human_sign_offs)}

    # ADMITTED: with the recorded human two-person approver-of-record the regulated
    # release proceeds (no human ⇒ no release; WITH a human ⇒ release).
    assert (
        pack_release_admitted(with_human, config, registry, requested_mode=MODE_GATED)
        is True
    )


def test_a_regulated_class4_unreachable_by_machine_gate() -> None:
    """(a-iv) The regulated release key is class-4 (human-witnessed): Autonomous /
    auto-approve is UNREACHABLE for it, and a green class-2 MACHINE gate can NEVER
    satisfy it (the verdict-class taxonomy is the registry-generic mechanism)."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    policy = registry.compose({"required_capabilities": ["regulated"]})

    assigned = policy.verdict_class.classes.get("release")
    assert assigned is VerdictClass.HUMAN_WITNESSED
    req = gate_requirements_for(assigned)
    assert req.grounding is GroundingKind.APPROVER_OF_RECORD
    assert req.requires_approver is True
    assert req.auto_approvable is False
    assert autonomous_reachable(VerdictClass.HUMAN_WITNESSED) is False
    # A green machine (class-2) gate can NEVER satisfy a class-4 key.
    assert satisfies(VerdictClass.HUMAN_WITNESSED, VerdictClass.EXECUTION_DETERMINISTIC) is False


# =====================================================================
# Criterion (b): ML → class-3 state_sha gate + compute budget
# =====================================================================


def test_b_ml_model_eval_is_class3_keyed_on_state_sha() -> None:
    """(b-i) An ML request engages MLDataPack → the model-eval gate is class-3
    (statistical), whose gate requirement is ALPHA_STATE_SHA: a frozen alpha look +
    a ``state_sha``. A class-3 verdict that cites neither is INVALID (fail-closed)."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    policy = registry.compose({"required_capabilities": ["ml"]})

    assigned = policy.verdict_class.classes.get(MODEL_EVAL_GATE)
    assert assigned is VerdictClass.STATISTICAL
    req = gate_requirements_for(assigned)
    assert req.grounding is GroundingKind.ALPHA_STATE_SHA
    assert req.requires_alpha_state_sha is True

    # Genuine fail-closed grounding over the REAL alpha-look ledger: a verdict that
    # cites an UNDRAWN alpha look is rejected; only a FROZEN (drawn) look + a
    # state_sha is admitted.
    records = GlobalRecords()
    binding = Class3Binding(
        eval_set_identity="evalset-1", decision_id="dec-1", state_sha="ml-sha-D-W"
    )
    with pytest.raises(InvalidClass3BindingError):
        assert_class3_binds_ledger(binding, records)  # not yet drawn → invalid
    records.record_alpha_look("evalset-1", "dec-1", "ml-sha-D-W", "run-b")
    assert assert_class3_binds_ledger(binding, records) is binding  # frozen → valid


def test_b_data_or_weight_change_rekeys_the_verdict() -> None:
    """(b-ii) The model-eval verdict is KEYED on the ml ``state_sha``: a data, weight,
    or feature change RE-KEYS it (invalidating reuse → a forced re-draw). Identical
    refs are a legitimate cache hit; ANY change re-keys (proven on every axis)."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    base = "base-tree-sha-source-only"

    # The pack folds the request's declared data/weight/feature refs into state_sha.
    data_v1 = registry.compose({"required_capabilities": ["ml"], "data_refs": ["D@v1"]})
    data_v2 = registry.compose({"required_capabilities": ["ml"], "data_refs": ["D@v2"]})
    weight_v2 = registry.compose(
        {"required_capabilities": ["ml"], "data_refs": ["D@v1"], "weight_refs": ["W@v2"]}
    )

    key_v1 = ml_state_sha(base, data_refs=data_v1.state_inputs.data_refs)
    key_v1_again = ml_state_sha(base, data_refs=data_v1.state_inputs.data_refs)
    key_v2 = ml_state_sha(base, data_refs=data_v2.state_inputs.data_refs)

    # Identical refs → identical key (a legitimate cache hit / verdict reuse).
    assert key_v1 == key_v1_again
    # A DATA change re-keys (cached verdict invalidated — a re-draw, not a hit).
    assert key_v1 != key_v2
    assert ml_verdict_rebinds(
        base, old=data_v1.state_inputs, new=data_v2.state_inputs
    )
    # A WEIGHT change (same data) ALSO re-keys.
    assert ml_verdict_rebinds(
        base, old=data_v1.state_inputs, new=weight_v2.state_inputs
    )
    # NON-VACUOUS: the SAME refs do NOT re-key (so re-key ⇒ a genuine change).
    assert not ml_verdict_rebinds(
        base, old=data_v1.state_inputs, new=data_v1.state_inputs
    )


def test_b_compute_budget_fail_closes_on_overspend() -> None:
    """(b-iii) The ML compute-cost budget is a fail-closed dimension: the expensive
    model-eval / retrain gates are metered, and a charge that would cross the cap
    PAUSES / escalates rather than silently overspending compute (it raises)."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    policy = registry.compose({"required_capabilities": ["ml"]})

    # The pack declares model-eval + retrain EXPENSIVE (behind a proxy + confirm).
    assert policy.compute.expensive_gates == frozenset({MODEL_EVAL_GATE, RETRAIN_GATE})
    assert policy.compute.require_proxy is True
    assert policy.compute.confirm_escalation is True

    # Meter the expensive gates against a small compute cap (cap = 1 unit).
    ctrl = ComputeBudgetController(
        ComputeBudget(cap_units=1, default_gate_units=1)
    )
    # The first expensive gate fits under the cap and is booked.
    first = ctrl.charge(MODEL_EVAL_GATE)
    assert first.allowed is True
    assert ctrl.spent_units == 1
    # A SECOND expensive run would cross the cap → fail-closed (raises, books nothing).
    assert ctrl.authorize(RETRAIN_GATE).allowed is False
    with pytest.raises(ComputeOverspendError):
        ctrl.charge(RETRAIN_GATE)
    assert ctrl.spent_units == 1  # nothing was overspent


def test_b_ml_request_runs_the_gated_spine_back_compat(tmp_path: Any) -> None:
    """(b-iv) END-TO-END back-compat: an ML request runs the gated spine with
    MLDataPack engaged and a NON-relaxing tier — the integrity boundaries still
    PASS (the pack tightens; it never relaxes the integrity floor)."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    final = _run_supervisor(
        tmp_path,
        request={"request": "train a classifier", "required_capabilities": ["ml"]},
        run_id="run-b-ml-spine",
        mode=MODE_GATED,
        registry=registry,
    )
    assert final["mode"] == MODE_GATED
    tier = final.get("risk_tier")
    assert tier is not None and tier.relaxes_integrity() is False
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test"):
        assert statuses.get(name) is GateStatus.PASS, name


# =====================================================================
# Criterion (c): UI → mandatory visual HITL (class 5, never auto-approved)
# =====================================================================


def test_c_ui_perceptual_gate_is_class5_never_auto_approved() -> None:
    """(c-i) A UI request engages FrontendPack → the perceptual gate is class-5
    (PERCEPTUAL): mandatory HITL, never auto-approvable, and NO machine gate (even a
    green class-2 one) can satisfy the class-5 perceptual key (taste is not delegated)."""
    registry = PackRegistry()
    registry.register(FrontendPack())
    policy = registry.compose({"required_capabilities": ["ui"]})

    assigned = policy.verdict_class.classes.get(PERCEPTUAL_GATE)
    assert assigned is VerdictClass.PERCEPTUAL
    req = gate_requirements_for(assigned)
    assert req.grounding is GroundingKind.PERCEPTUAL_HITL
    assert req.requires_hitl is True
    assert req.auto_approvable is False
    assert autonomous_reachable(VerdictClass.PERCEPTUAL) is False
    # A green machine gate can NEVER satisfy the class-5 perceptual key.
    assert satisfies(VerdictClass.PERCEPTUAL, VerdictClass.EXECUTION_DETERMINISTIC) is False
    # The composed perceptual-HITL policy demands the rendered-evidence bundle on the
    # ui capability (the visual HITL surface).
    assert "ui" in policy.perceptual_hitl.hitl_required_capabilities
    assert policy.perceptual_hitl.require_rendered_evidence is True


def test_c_ui_clean_run_blocked_at_release_no_human_hitl(tmp_path: Any) -> None:
    """(c-ii) END-TO-END: a UI request's self-judge clean run is BLOCKED at the
    release boundary — the composed perceptual-HITL hook requires a recorded HUMAN
    visual sign-off, which a self-judge run never produces ⇒ not admitted.

    NON-VACUOUS: the SAME final state with NO frontend pack engaged admits, so the
    block is the FrontendPack's class-5 HITL demand, not a constant refusal."""
    registry = PackRegistry()
    registry.register(FrontendPack())
    config = _config("run-c-ui-hitl")
    final = _run_supervisor(
        tmp_path,
        request={"request": "build a dashboard UI", "required_capabilities": ["ui"]},
        run_id="run-c-ui-hitl",
        mode=MODE_GATED,
        registry=registry,
    )
    # No recorded human visual sign-off ⇒ the mandatory perceptual HITL is unmet.
    assert (
        pack_release_admitted(final, config, registry, requested_mode=MODE_GATED)
        is False
    )
    # NON-VACUOUS: with NO pack engaged the SAME state admits generically.
    assert (
        pack_release_admitted(final, config, None, requested_mode=MODE_GATED)
        is True
    )


# =====================================================================
# Criterion (d): a BRAND-NEW 4th pack engages WITHOUT any core edit
# =====================================================================


class _CryptoPack(BaseDomainPack):
    """A BRAND-NEW 4th pack the core has NEVER seen — defined entirely in THIS test.

    It is a stub "crypto / key-management" pack that TIGHTENS multiple axes the core
    enforces generically: it VETOES Autonomous + auto-approve, force-escalates the
    tier to HIGH, binds a class-4 human approver-of-record on its ``release`` class,
    demands a perceptual HITL over its own capability, and folds a key-material ref
    into ``state_sha``. Registering it is the WHOLE integration — the registry is the
    seam, so NO edit to contract / registry / supervisor / factory_graph is needed.
    """

    name = "crypto_keymgmt"
    capabilities = frozenset({"crypto"})

    def mode_tier_policy(self, request: Any) -> ModeTierPolicy:
        return ModeTierPolicy(
            vetoed_modes=frozenset({MODE_AUTONOMOUS, MODE_ASSISTANT}),
            min_tier=RiskTier.HIGH,
            force_human_release=True,
            two_person_release=True,
        )

    def assign_verdict_class(self, request: Any) -> VerdictClassPolicy:
        # Its release key is human-witnessed (class 4) like a regulated artifact.
        return VerdictClassPolicy(classes={"release": VerdictClass.HUMAN_WITNESSED})

    def perceptual_hitl(self, request: Any) -> PerceptualHITLPolicy:
        return PerceptualHITLPolicy(
            hitl_required_capabilities=frozenset({"crypto"}),
            require_rendered_evidence=True,
        )

    def state_inputs(self, request: Any) -> StateInputPolicy:
        return StateInputPolicy(weight_refs=frozenset({"keymaterial@kms-v1"}))


def test_d_fresh_pack_engages_end_to_end_without_core_edit(tmp_path: Any) -> None:
    """(d-i) END-TO-END open/closed: a pack the core has NEVER seen engages purely via
    ``register()``. Triage caps the requested Autonomous → Gated + raises the tier;
    the release boundary enforces its approver-of-record + perceptual-HITL demand
    fail-closed — NONE of this required an edit to any core module."""
    registry = PackRegistry()
    registry.register(_CryptoPack())
    config = _config("run-d-crypto")

    final = _run_supervisor(
        tmp_path,
        request={"request": "build a key-management service", "required_capabilities": ["crypto"]},
        run_id="run-d-crypto",
        mode=MODE_AUTONOMOUS,
        registry=registry,
    )
    # The new pack's veto degraded Autonomous → Gated + raised the tier to HIGH.
    assert final["mode"] == MODE_GATED
    tier = final.get("risk_tier")
    assert tier is not None and tier.tier is RiskTier.HIGH
    assert tier.two_person_release is True

    # Its release demands (approver-of-record + perceptual HITL) BLOCK a self-judge
    # release — the core enforced a pack it had no prior knowledge of.
    assert (
        pack_release_admitted(final, config, registry, requested_mode=MODE_AUTONOMOUS)
        is False
    )
    # And hands-off Autonomous is UNREACHABLE for the new pack's request.
    assert (
        autonomous_pack_reachable(
            final, registry, requested_mode=MODE_AUTONOMOUS, seq=_seq()
        )
        is False
    )


def test_d_fresh_pack_composes_with_first_party_packs() -> None:
    """(d-ii) The 4th pack COMPOSES strictest-wins with the three first-party packs
    via the generic registry fold — a crypto+ml+ui request engages all four and the
    composed policy carries every pack's tightening (no pack-by-pack core wiring)."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    registry.register(MLDataPack())
    registry.register(FrontendPack())
    registry.register(_CryptoPack())  # the brand-new 4th pack

    request = {"required_capabilities": ["crypto", "ml", "ui"], "data_refs": ["D@v1"]}
    selected = {p.name for p in registry.select_packs(request)}
    assert selected == {"crypto_keymgmt", "ml_data", "frontend"}  # regulated not matched

    policy = registry.compose(request)
    # crypto + frontend HITL caps both present (union); rendered evidence demanded.
    assert {"crypto", "ui"} <= policy.perceptual_hitl.hitl_required_capabilities
    assert policy.perceptual_hitl.require_rendered_evidence is True
    # ml class-3 model-eval, crypto class-4 release, ui class-5 perceptual all present.
    assert policy.verdict_class.classes.get(MODEL_EVAL_GATE) is VerdictClass.STATISTICAL
    assert policy.verdict_class.classes.get("release") is VerdictClass.HUMAN_WITNESSED
    assert policy.verdict_class.classes.get(PERCEPTUAL_GATE) is VerdictClass.PERCEPTUAL
    # crypto's veto + HIGH tier + two-person survive the strictest-wins join.
    eff = resolve_effective_mode_tier(
        policy.mode_tier,
        requested_mode=MODE_AUTONOMOUS,
        request=request,
        capabilities=["crypto", "ml", "ui"],
        seq=_seq(),
    )
    assert eff.autonomous_reachable is False
    assert eff.effective_mode == MODE_GATED
    assert eff.effective_tier is RiskTier.HIGH
    assert eff.two_person_release is True
    # crypto's key-material ref folded into state_sha (the state_inputs union).
    assert "keymaterial@kms-v1" in policy.state_inputs.weight_refs


# =====================================================================
# M5-R1-01: the autonomous approver/HITL check is UNCONDITIONAL
# =====================================================================


class _ApproverOnlyPack(BaseDomainPack):
    """A FUTURE-shaped pack that binds a class-4 ``release`` approver-of-record but
    does NOT veto Autonomous (it leaves ``mode_tier_policy`` at the identity).

    This is exactly the M5-R1-01 hazard: under the OLD AND-coupling the autonomous
    emit halted only if (Autonomous vetoed) AND (pack reqs unmet). This pack keeps
    Autonomous REACHABLE (``autonomous_pack_reachable`` is True) while binding a
    human approver-of-record — so the AND would short-circuit and SKIP the approver
    check on the hands-off deploy path. The fixed OR-coupling enforces the approver
    check UNCONDITIONALLY, so the emit halts (no recorded human ⇒ no deploy)."""

    name = "approver_only"
    capabilities = frozenset({"approveronly"})

    def approver_of_record(self, request: Any) -> Any:
        from foundry.domains.contract import ApproverPolicy

        return ApproverPolicy(bound_artifact_classes=frozenset({"release"}))


def test_m5r101_approver_binding_pack_halts_autonomous_even_when_reachable(
    tmp_path: Any,
) -> None:
    """M5-R1-01: a pack that binds a class-4 approver-of-record WITHOUT vetoing
    Autonomous still HALTS the hands-off autonomous emit — the approver/HITL check
    is enforced UNCONDITIONALLY (no recorded HUMAN approver ⇒ NO deploy), exactly as
    the gated/assistant paths already do.

    Proven non-vacuously: Autonomous IS reachable (the pack does not veto it) and the
    integrity gates all PASS — the ONLY thing blocking deploy is the now-unconditional
    approver-of-record check. Compare with the clean control (no pack), which deploys."""
    records = GlobalRecords()
    registry = PackRegistry()
    registry.register(_ApproverOnlyPack())
    config = _config("run-m5r101-approver")
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous_factory(records, _src(tmp_path), registry=registry)
        final: FactoryState = app.invoke(
            {"request": "ship a release", "required_capabilities": ["approveronly"]},
            config,
        )
    finally:
        reset_gate_write_verifier(token)

    # The integrity floor still passes — the change is fully assured...
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name

    # ...and Autonomous IS reachable (the pack binds an approver but does NOT veto):
    # so the OLD AND-coupling would have SHORT-CIRCUITED and deployed. The fixed
    # unconditional check halts because no recorded HUMAN approver-of-record exists.
    assert (
        autonomous_pack_reachable(
            final, registry, requested_mode=MODE_AUTONOMOUS, seq=_seq()
        )
        is True
    )
    assert (
        pack_release_admitted(final, config, registry, requested_mode=MODE_AUTONOMOUS)
        is False
    )

    # The hands-off deploy is HALTED — the approver check fired unconditionally.
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"
