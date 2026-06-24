"""Domain-pack engagement at triage + composed-policy enforcement at release (T-M5-08).

Drives the SINGLE graph-core seam that makes the three first-party domain packs
(Regulated / ML-data / Frontend) take effect END-TO-END: ``build_supervisor_graph``
/ ``build_factory_graph`` accept an OPTIONAL ``pack_registry``; when provided the
TRIAGE node routes the request's ``required_capabilities`` through the registry →
a strictest-wins ``ComposedPolicy``, applies the mode/tier VETO
(:func:`resolve_effective_mode_tier`), and stamps the EFFECTIVE mode/risk_tier on
state; the RELEASE/emit boundary RE-DERIVES the composed policy (a pure function of
the request capabilities) and enforces it fail-closed.

Asserts (the T-M5-08 exit criteria — docs/roadmap.md Phase 5):

* a REGULATED request with ``mode=autonomous`` + the RegulatedPack → effective mode
  is Gated, Autonomous UNREACHABLE; a hands-off autonomous run CANNOT deploy (no
  recorded human approver-of-record ⇒ no release, fail-closed);
* an ML request + MLDataPack → the model-eval verdict is class-3 keyed on
  ``state_sha`` (the gate requirement is ``ALPHA_STATE_SHA``);
* a UI request + FrontendPack → a mandatory perceptual HITL (class-5; never
  auto-approved) — a self-judge clean run is BLOCKED at the release boundary;
* registry=None / a NO-matching-pack request → exact prior behavior (back-compat);
* (open/closed) a freshly-registered STUB pack engages without editing this wiring.

Reuses the SAME end-to-end doubles the Phase-2/4 supervisor tests use (a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport`, a dev
:class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner` admitted through the REAL
``merge_gates`` path, a :class:`StubAdapter`, in-memory materialization seams, the
REAL trust-root chain for the autonomous case).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.domains.contract import (
    BaseDomainPack,
    ModeTierPolicy,
    PerceptualHITLPolicy,
    VerdictClassPolicy,
)
from foundry.domains.governance_hooks import resolve_effective_mode_tier
from foundry.domains.packs.frontend import PERCEPTUAL_GATE, FrontendPack
from foundry.domains.packs.ml_data import MODEL_EVAL_GATE, MLDataPack
from foundry.domains.packs.regulated import RegulatedPack
from foundry.domains.registry import PackRegistry
from foundry.domains.verdict_class import (
    GroundingKind,
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
from foundry.schemas.gates import GateStatus, JudgmentProvenance, VerdictClass
from foundry.schemas.governance import RiskTier
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from foundry.state.seq import Seq
from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

KEY = b"dev-zone-b-supervisor-packs-key-0123456789"
KEY_ID = "dev-key-supervisor-packs"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
BUILDER_ID = "dev-trusted-builder-packs"
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

_CLEAN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)


def _synthetic_resolve(handle: Artifact) -> FrozenModel:
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
    if handle.kind == "criteria_findings":
        # The Criteria-Completeness Critic finding the Spec gate now consumes.
        # No security/abuse omission here -> does not block the clean run.
        return CriteriaCritique(
            prd_uri="cas://design/prd",
            security_abuse_omissions=(),
            judgment_provenance=JudgmentProvenance.SELF,
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


# =====================================================================
# 1. REGULATED + mode=autonomous → effective mode Gated, Autonomous UNREACHABLE
# =====================================================================


def test_regulated_request_degrades_autonomous_to_gated_at_triage(tmp_path: Any) -> None:
    """A regulated request requested as Autonomous is CAPPED by the pack veto: the
    triage node stamps the EFFECTIVE mode Gated + a HIGH risk_tier (tighten-only)."""
    registry = PackRegistry()
    registry.register(RegulatedPack())

    final = _run_supervisor(
        tmp_path,
        request={"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
        run_id="run-reg-autocap",
        mode=MODE_AUTONOMOUS,
        registry=registry,
    )

    # The veto degraded the requested Autonomous mode to Gated (Autonomous + the
    # auto-approve Assistant are both vetoed for a regulated request).
    assert final["mode"] == MODE_GATED
    # The tier was force-escalated to HIGH (regulatory significance, tighten-only);
    # two-person Release is forced on; integrity is never relaxed.
    tier = final.get("risk_tier")
    assert tier is not None
    assert tier.tier is RiskTier.HIGH
    assert tier.two_person_release is True
    assert tier.relaxes_integrity() is False


def test_regulated_resolver_makes_autonomous_unreachable() -> None:
    """The composed RegulatedPack mode/tier policy makes hands-off Autonomous
    UNREACHABLE (the unit fact the emit boundary reads, registry-generic)."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    policy = registry.compose({"required_capabilities": ["regulated"]})

    eff = resolve_effective_mode_tier(
        policy.mode_tier,
        requested_mode=MODE_AUTONOMOUS,
        request={"required_capabilities": ["regulated"]},
        capabilities=["regulated"],
        seq=_seq(),
    )
    assert eff.autonomous_reachable is False
    assert eff.effective_mode == MODE_GATED


def test_regulated_autonomous_run_cannot_hands_off_deploy(tmp_path: Any) -> None:
    """END-TO-END: a regulated request driven as Autonomous CANNOT hands-off
    deploy — the trust-root emit fail-closes (no recorded human approver-of-record
    ⇒ no release), so NO deploy action is produced."""
    records = GlobalRecords()
    registry = PackRegistry()
    registry.register(RegulatedPack())
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_autonomous_factory(records, _src(tmp_path), registry=registry)
        final: FactoryState = app.invoke(
            {"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
            _config("run-reg-auto-deploy"),
        )
    finally:
        reset_gate_write_verifier(token)

    # The integrity gates still PASS (the regulated change compiles + is assured)...
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    # ...but the hands-off deploy is BLOCKED: no recorded human approver-of-record,
    # so the autonomous emit deploys NOTHING (fail-closed; Autonomous unreachable).
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"


def test_regulated_pack_release_admitted_requires_human_approver(tmp_path: Any) -> None:
    """The composed approver-of-record hook is enforced at the release boundary: a
    self-judge-only state is NOT admitted (no recorded HUMAN approver ⇒ no release)."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    final = _run_supervisor(
        tmp_path,
        request={"request": "build a fintech ledger", "required_capabilities": ["regulated"]},
        run_id="run-reg-approver",
        mode=MODE_AUTONOMOUS,  # degrades to Gated → records a self-judge release sign-off
        registry=registry,
    )
    # A self-judge release-approver record exists, but it is NOT a human approver,
    # so the regulated pack's approver-of-record hook BLOCKS the release.
    assert (
        pack_release_admitted(
            final, _config("run-reg-approver"), registry, requested_mode=MODE_AUTONOMOUS
        )
        is False
    )
    # The autonomous veto is also unreachable for this state.
    assert (
        autonomous_pack_reachable(
            final, registry, requested_mode=MODE_AUTONOMOUS, seq=_seq()
        )
        is False
    )


# =====================================================================
# 2. ML + MLDataPack → model-eval verdict class 3, keyed on state_sha
# =====================================================================


def test_ml_pack_assigns_class3_state_sha_grounding() -> None:
    """An ML request engages MLDataPack → the model-eval gate is class-3 (statistical),
    whose gate requirement is ALPHA_STATE_SHA (a frozen alpha look + a state_sha)."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    policy = registry.compose({"required_capabilities": ["ml"]})

    assigned = policy.verdict_class.classes.get(MODEL_EVAL_GATE)
    assert assigned is VerdictClass.STATISTICAL
    req = gate_requirements_for(assigned)
    assert req.grounding is GroundingKind.ALPHA_STATE_SHA
    assert req.requires_alpha_state_sha is True


def test_ml_request_triage_tiers_without_relaxing(tmp_path: Any) -> None:
    """An ML request runs the gated spine with MLDataPack engaged and a non-relaxing
    tier; back-compat: the run completes through the integrity boundaries."""
    registry = PackRegistry()
    registry.register(MLDataPack())
    final = _run_supervisor(
        tmp_path,
        request={"request": "train a classifier", "required_capabilities": ["ml"]},
        run_id="run-ml-triage",
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
# 3. UI + FrontendPack → mandatory perceptual HITL (class 5, never auto-approved)
# =====================================================================


def test_ui_pack_requires_non_auto_approvable_perceptual_hitl() -> None:
    """A UI request engages FrontendPack → the perceptual gate is class-5 (never
    auto-approvable; mandatory HITL over a rendered-evidence bundle)."""
    registry = PackRegistry()
    registry.register(FrontendPack())
    policy = registry.compose({"required_capabilities": ["ui"]})

    assigned = policy.verdict_class.classes.get(PERCEPTUAL_GATE)
    assert assigned is VerdictClass.PERCEPTUAL
    req = gate_requirements_for(assigned)
    assert req.requires_hitl is True
    assert req.auto_approvable is False
    # A green machine gate can NEVER satisfy the class-5 perceptual key.
    assert satisfies(VerdictClass.PERCEPTUAL, VerdictClass.EXECUTION_DETERMINISTIC) is False


def test_ui_request_clean_run_blocked_at_release_no_human_hitl(tmp_path: Any) -> None:
    """A UI request's self-judge clean run is BLOCKED at the release boundary: the
    composed perceptual-HITL hook requires a recorded HUMAN visual sign-off, which a
    self-judge run never produces — so the release is NOT admitted (never auto-approved)."""
    registry = PackRegistry()
    registry.register(FrontendPack())
    final = _run_supervisor(
        tmp_path,
        request={"request": "build a dashboard UI", "required_capabilities": ["ui"]},
        run_id="run-ui-hitl",
        mode=MODE_GATED,
        registry=registry,
    )
    # No recorded human visual sign-off ⇒ the perceptual HITL is unmet ⇒ blocked.
    assert (
        pack_release_admitted(
            final, _config("run-ui-hitl"), registry, requested_mode=MODE_GATED
        )
        is False
    )


# =====================================================================
# 4. Back-compat: registry=None / no matching pack → exact prior behavior
# =====================================================================


def test_registry_none_is_exact_prior_behavior(tmp_path: Any) -> None:
    """registry=None engages NO packs: the run is byte-for-byte the prior Gated
    flow (mode Gated, a deterministic tier, both M1 boundaries PASS)."""
    final = _run_supervisor(
        tmp_path,
        request={"request": "build a todo app"},
        run_id="run-none",
        mode=MODE_GATED,
        registry=None,
    )
    assert final["mode"] == MODE_GATED
    assert final.get("risk_tier") is not None
    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test"):
        assert statuses.get(name) is GateStatus.PASS, name
    # No pack engaged: the release boundary admits generically (no extra tightening).
    assert (
        pack_release_admitted(final, _config("run-none"), None, requested_mode=MODE_GATED)
        is True
    )


def test_no_matching_pack_is_back_compat(tmp_path: Any) -> None:
    """A registry WITH packs but a request whose capabilities match NONE engages
    nothing — the composed policy is identity, the release admits, mode is unchanged."""
    registry = PackRegistry()
    registry.register(RegulatedPack())
    registry.register(MLDataPack())
    registry.register(FrontendPack())
    final = _run_supervisor(
        tmp_path,
        request={"request": "build a todo app", "required_capabilities": ["unrelated"]},
        run_id="run-nomatch",
        mode=MODE_GATED,
        registry=registry,
    )
    assert final["mode"] == MODE_GATED
    assert (
        pack_release_admitted(
            final, _config("run-nomatch"), registry, requested_mode=MODE_GATED
        )
        is True
    )


# =====================================================================
# 5. Open/closed: a freshly-registered STUB pack engages without editing the wiring
# =====================================================================


class _StubPack(BaseDomainPack):
    """A brand-new pack added ONLY via register() — it vetoes Autonomous, raises
    the tier to HIGH, demands a perceptual HITL on its own capability, and assigns
    a class-5 key. None of this required a wiring edit (the registry is the seam)."""

    name = "stub_widget"
    capabilities = frozenset({"widget"})

    def mode_tier_policy(self, request: Any) -> ModeTierPolicy:
        return ModeTierPolicy(
            vetoed_modes=frozenset({MODE_AUTONOMOUS, MODE_ASSISTANT}),
            min_tier=RiskTier.HIGH,
            force_human_release=True,
        )

    def perceptual_hitl(self, request: Any) -> PerceptualHITLPolicy:
        return PerceptualHITLPolicy(
            hitl_required_capabilities=frozenset({"widget"}),
            require_rendered_evidence=True,
        )

    def assign_verdict_class(self, request: Any) -> VerdictClassPolicy:
        return VerdictClassPolicy(classes={"widget.review": VerdictClass.PERCEPTUAL})


def test_fresh_stub_pack_engages_without_wiring_edit(tmp_path: Any) -> None:
    """A pack the core has NEVER seen engages purely via register(): triage caps the
    requested Autonomous to Gated + raises the tier, and the release boundary enforces
    its perceptual-HITL demand fail-closed — NO edit to supervisor/factory_graph."""
    registry = PackRegistry()
    registry.register(_StubPack())

    final = _run_supervisor(
        tmp_path,
        request={"request": "build a widget", "required_capabilities": ["widget"]},
        run_id="run-stub",
        mode=MODE_AUTONOMOUS,
        registry=registry,
    )
    # The stub pack's veto degraded Autonomous → Gated + raised the tier to HIGH.
    assert final["mode"] == MODE_GATED
    tier = final.get("risk_tier")
    assert tier is not None and tier.tier is RiskTier.HIGH
    # Its perceptual-HITL demand is enforced at release (no human HITL ⇒ blocked).
    assert (
        pack_release_admitted(final, _config("run-stub"), registry, requested_mode=MODE_AUTONOMOUS)
        is False
    )
