"""Phase-4 EXIT proof — the four adversarial attacks CANNOT reach production.

(docs/roadmap.md Phase 4 EXIT; SPEC.md Phase 4; governance §1c/§4/§5)

The binding exit criterion: *a malicious-test / forged-results / forged-verdict /
fork-divergence attempt CANNOT reach production; a trusted-component outage
degrades to PAUSED, not WEDGED.* This module proves each of the four attacks is
GENUINELY (non-vacuously) blocked from reaching a deploy — each block is driven
by the REAL trust-root component (the discriminator, the hardened two-zone
runner, the deploy-time admission controller, the cross-fork ``global_records``),
NOT a hand-flipped status — and pairs each with a CLEAN control that DOES deploy,
so the blocks are load-bearing rather than a pipeline that never ships.

The four attacks (each: block → no deploy; control → deploy):

1. **Malicious-test** — a gamed-green suite that does not kill a held-out mutant
   makes the REAL Test gate (held-out-mutant discriminator, T-M4-04) FAIL; that
   attested FAIL in the gate-state makes the autonomous emit guard HALT before
   build/admit/deploy. Control: a genuine suite kills the mutant → attested PASS →
   the emit guard proceeds to a deploy.
2. **Forged-results** — a forged/weakened scanner config (or a tampered zone-A
   observation) makes the REAL hardened runner (T-M4-03) sign a FAIL/ERROR, never
   a PASS; that non-PASS in the gate-state halts the emit guard. Control: the
   genuine sealed/pinned run → attested PASS → deploy.
3. **Forged-verdict** — an unsigned/forged gate attestation, or a verdict signed
   for a DIFFERENT tree, makes the REAL :class:`AdmissionController` DENY (the
   signature / input-binding re-verify fails); the :class:`Deployer` refuses a
   non-admitted decision → no deploy. Control: the genuine gate-state → ADMIT →
   the deployer emits exactly one deploy action.
4. **Fork-divergence** — a digest cross-fork DISQUALIFIED (or stable-FAIL latched)
   in ``global_records`` makes ``admission_look`` return DISQUALIFIED, so the REAL
   admission DENYs even with otherwise-valid signatures → no deploy (a laundered
   fork cannot escape). Control: the un-disqualified digest → ADMIT → deploy.

5. **Clean control (full E2E)** — a fully genuine autonomous run (real builder +
   admission ADMIT + deployer) → EXACTLY ONE deploy action, proving 1-4 are real
   blocks and not a pipeline that never deploys.
6. **Trusted-component outage → PAUSED not WEDGED** — a trusted-component
   (signer/runner/proxy) outage degrades the run to
   PAUSED(trusted_component_unavailable), never WEDGED (reuses
   ``trusted_component`` / ``trusted_lifecycle``).

Honesty note (LEARNINGS §0.3, dev-grade roots): the genuine-E2E surface here is
the discriminator + the two-zone runner + ``merge_gates`` admittance + the
admission re-verification + the cross-fork ``GlobalRecords`` + the deployer's
admitted-only refusal + the full compiled autonomous graph (clean control). The
dev-grade stand-ins are the symmetric-HMAC ``ZoneBSigner``/``DevGateWriteVerifier``
(prod: air-gapped signer / keyless OIDC), the constant pinned hermetic build
inputs in :mod:`foundry.graph.factory_graph` (prod: a real source/base-image pin),
and the surrounding required-PASS gate-state in the emit-guard propagation tests,
which are genuine ``ZoneBSigner.with_attestation`` records (mirrors
``test_admission.py``) rather than full materialized-tree gate runs — the BLOCKING
verdict in each attack IS produced by the real component.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import (
    DevGateWriteVerifier,
    GateImpl,
    TestGate,
    TwoZoneGateRunner,
    ZoneAObservation,
    ZoneBSigner,
    observe,
)
from foundry.gates.discriminator import HeldOutMutant, TestSuiteDiscriminator
from foundry.gates.runner import RAN_CONFIG_REV_KEY
from foundry.graph.factory_graph import build_factory_graph
from foundry.graph.supervisor import MODE_AUTONOMOUS
from foundry.operations.trusted_component import (
    AvailabilityModel,
    ComponentStatus,
    TrustedComponent,
    classify_for_dependencies,
    on_recovery,
)
from foundry.operations.trusted_lifecycle import (
    ComponentInstance,
    HAComponent,
    classify_ha,
)
from foundry.policy.scanner_config import (
    PinnedScannerConfig,
    ScannerConfigRecord,
    ScannerRule,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.operability import (
    PauseReason,
    RunStatus,
)
from foundry.schemas.security import SBOMComponent
from foundry.schemas.trust_root import (
    AdmissionVerdict,
    BuildRequest,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq
from foundry.trust_root import AdmissionController, Deployer, DeployRefused, TrustedBuilder

# Import the autonomous E2E harness verbatim (the clean control reuses it whole).
from tests.test_factory_graph_autonomous import (
    BUILDER_ID as AUTO_BUILDER_ID,
)
from tests.test_factory_graph_autonomous import (
    DEPLOY_TARGET as AUTO_DEPLOY_TARGET,
)
from tests.test_factory_graph_autonomous import (
    KEY as AUTO_KEY,
)
from tests.test_factory_graph_autonomous import (
    KEY_ID as AUTO_KEY_ID,
)
from tests.test_factory_graph_autonomous import (
    _build_autonomous,
    _config,
    _deploy_actions,
    _gates_by_name,
    _resolve_blob_factory,
    _src,
    _synthetic_resolve,
)
from tests.test_factory_graph_autonomous import (
    _runner as _auto_runner,
)
from tests.test_factory_graph_autonomous import (
    _runtime as _auto_runtime,
)

KEY = b"phase4-exit-zone-b-key-0123456789abcdef"
KEY_ID = "phase4-exit-zoneB"
SEAL_KEY = b"phase4-exit-zone-a-seal-key-0123456789ab"
BUILDER_ID = "dev-trusted-builder-phase4"
DEPLOY_TARGET = "prod"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"

# The autonomous required release gate set when risk_tier is unset (None / LOW):
# the terminal gates + the always-on assurance floor (Security + Audit).
_REQUIRED_GATES = ("spec", "build", "test", "security", "audit")

# ---- malicious-test fixtures (mirror test_discriminator.py) -----------------
CALC_SRC = "def add(a, b):\n    return a + b\n"
GENUINE_SUITE = (
    "import calc\n"
    "def test_add_basic():\n"
    "    assert calc.add(2, 3) == 5\n"
    "def test_add_commutes():\n"
    "    assert calc.add(7, 4) == calc.add(4, 7)\n"
)
GAMED_SUITE = "import calc\ndef test_vacuous():\n    assert calc is not None\n"
ADD_MUTANT = HeldOutMutant(
    mutant_id="add->sub", target="calc.py", find="a + b", replace="a - b"
)

# A clean material set for a genuine hermetic build (mirrors test_admission.py).
_MAT_A = "sha256:" + "a" * 64
_MAT_B = "sha256:" + "b" * 64
_COMPONENTS = (
    SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT_A, license="MIT"),
    SBOMComponent(name="libbar", version="4.5.6", digest=_MAT_B, license="Apache-2.0"),
)


# ---- shared builders --------------------------------------------------------


def _seq(node: str = "phase4", *, local: int = 0, against: str = "tree") -> Seq:
    return Seq(
        run_id=against, emission_epoch=1, superstep_index=0, node_id=node, local=local
    )


def _records_verifier() -> tuple[GlobalRecords, DevGateWriteVerifier]:
    records = GlobalRecords()
    return records, DevGateWriteVerifier(KEY, KEY_ID, records)


def _runner(records: GlobalRecords, **kw: Any) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(
        ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV, **kw
    )


def _mk_calc_tree(tmp_path: Path, suite: str, src: str = CALC_SRC) -> Path:
    (tmp_path / "calc.py").write_text(src, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(suite, encoding="utf-8")
    return tmp_path


def _signed_pass(signer: ZoneBSigner, name: str, tree_sha: str, *, local: int) -> GateResult:
    """A genuine zone-B-attested PASS verdict bound to ``tree_sha`` (the
    surrounding required gate-state; mirrors test_admission.py)."""
    g = GateResult(
        name=name,
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        results_digest=f"results-{name}",
        run_against=tree_sha,
        seq=_seq("gate", local=local, against=tree_sha),
    )
    return signer.with_attestation(g)


def _all_pass_gate_state(signer: ZoneBSigner, tree_sha: str) -> dict[str, GateResult]:
    """Genuine attested PASS verdicts for EVERY required gate, keyed by name."""
    return {
        name: _signed_pass(signer, name, tree_sha, local=i)
        for i, name in enumerate(_REQUIRED_GATES)
    }


def _feature_diff_handle(tree_sha: str) -> Artifact:
    """A ``feature_diff`` artifact handle the emit guard opens the deploy against
    (newest-by-Seq; the autonomous emit needs a non-None subject)."""
    return Artifact(
        uri="cas://sha256/feature-diff-phase4",
        kind="feature_diff",
        produced_by="senior_engineer",
        task_id="T1",
        seq=_seq("feature_diff", against=tree_sha),
    )


def _emit_state(gate_state: dict[str, GateResult], tree_sha: str) -> FactoryState:
    """A FactoryState the autonomous emit node reads: the trust-filtered gate-state
    on ``gates`` (admitted through the REAL merge_gates) + a feature_diff subject."""
    admitted = merge_gates(list(gate_state.values()), None)
    state: FactoryState = {
        "gates": admitted,
        "artifacts": [_feature_diff_handle(tree_sha)],
        "risk_tier": None,
    }
    return state


def _autonomous_emit_node(records: GlobalRecords, verifier: DevGateWriteVerifier) -> Any:
    """The REAL autonomous emit node, extracted from the compiled factory graph by
    wiring the same trust-root chain the E2E test uses. We invoke just the emit
    node over a hand-assembled gate-state so the BLOCKING verdict is the real
    component's output while the build→admit→deploy chain is exercised verbatim."""
    signer = ZoneBSigner(KEY, KEY_ID)
    app = build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_noop_resolve,
        resolve_blob=_noop_resolve_blob,
        source_dir=".",
        mode=MODE_AUTONOMOUS,
        builder=TrustedBuilder(signer, builder_id=BUILDER_ID),
        admission=AdmissionController(builder_id=BUILDER_ID),
        deployer=Deployer(target=DEPLOY_TARGET),
        gate_verifier=verifier,
    )
    # The compiled graph's emit node is the closure we need; reach it via nodes.
    return app.nodes["emit"]


def _runtime() -> AgentRuntime:
    proxy = ModelProviderProxy(MockTransport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _noop_resolve(handle: Artifact) -> Any:  # pragma: no cover - not reached
    raise AssertionError("resolve not used by the emit node")


def _noop_resolve_blob(uri: str) -> bytes:  # pragma: no cover - not reached
    raise AssertionError("resolve_blob not used by the emit node")


def _emit_config(run_id: str) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id, "state_sha": "tree-integrated"},
        "metadata": {"langgraph_step": 1},
    }


def _run_emit(
    records: GlobalRecords,
    verifier: DevGateWriteVerifier,
    gate_state: dict[str, GateResult],
    tree_sha: str,
    run_id: str,
) -> dict[str, Any]:
    """Drive the REAL autonomous emit node over a gate-state and return its delta
    (``{"phase": ..., "actions": [...]}``)."""
    node = _autonomous_emit_node(records, verifier)
    token = set_gate_write_verifier(verifier)
    try:
        out: dict[str, Any] = node.invoke(
            _emit_state(gate_state, tree_sha), _emit_config(run_id)
        )
    finally:
        reset_gate_write_verifier(token)
    return out


def _deploys(delta: dict[str, Any]) -> list[Any]:
    return [a for a in (delta.get("actions") or ()) if a.kind == "deploy"]


# ===========================================================================
# Control sanity: a genuine all-PASS gate-state deploys via the real emit chain
# ===========================================================================


def test_control_clean_gate_state_deploys_via_emit_chain() -> None:
    """LOAD-BEARING CONTROL: a fully genuine attested all-PASS gate-state drives
    the REAL build → admit → deploy emit chain to EXACTLY ONE deploy action.

    Without this, every "no deploy" assertion below would be vacuous (a pipeline
    that never deploys). The deploy fires off the ADMITTING decision."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    tree = "tree-control"
    delta = _run_emit(
        records, verifier, _all_pass_gate_state(signer, tree), tree, "run-control"
    )
    deploys = _deploys(delta)
    assert delta["phase"] == "done"
    assert len(deploys) == 1
    assert deploys[0].kind == "deploy"
    assert deploys[0].payload_uri.startswith("cas://admission/")


# ===========================================================================
# Attack 1 — MALICIOUS-TEST cannot reach prod (held-out-mutant discriminator)
# ===========================================================================


def test_malicious_test_blocked_genuine_suite_control(tmp_path: Path) -> None:
    """CONTROL: a genuine suite kills the held-out mutant → the REAL Test gate
    attests PASS → that PASS in the gate-state drives the emit chain to a deploy."""
    records, verifier = _records_verifier()
    token = set_gate_write_verifier(verifier)
    try:
        tree_dir = _mk_calc_tree(tmp_path, GENUINE_SUITE)
        disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
        test_verdict = _runner(records).run(
            TestGate(discriminator=disc), "tree-good", _seq("test", against="tree-good"), tree_dir
        )
    finally:
        reset_gate_write_verifier(token)
    assert test_verdict.status is GateStatus.PASS
    assert test_verdict.runner_attestation is not None

    # Splice the genuine PASS Test verdict into an otherwise-genuine gate-state.
    signer = ZoneBSigner(KEY, KEY_ID)
    gate_state = _all_pass_gate_state(signer, "tree-good")
    gate_state["test"] = test_verdict
    delta = _run_emit(records, verifier, gate_state, "tree-good", "run-mt-good")
    assert delta["phase"] == "done"
    assert len(_deploys(delta)) == 1


def test_malicious_test_cannot_reach_prod(tmp_path: Path) -> None:
    """ATTACK: a gamed-green suite passes pytest but does NOT kill the held-out
    mutant → the REAL Test gate (discriminator) attests FAIL. That attested FAIL
    in the gate-state makes the autonomous emit guard HALT — NO build, admit, or
    deploy. The block is genuine: the FAIL is the discriminator's verdict, and the
    control above proves the same chain WOULD deploy on a genuine PASS."""
    records, verifier = _records_verifier()
    token = set_gate_write_verifier(verifier)
    try:
        tree_dir = _mk_calc_tree(tmp_path, GAMED_SUITE)
        disc = TestSuiteDiscriminator(mutants=[ADD_MUTANT])
        test_verdict = _runner(records).run(
            TestGate(discriminator=disc), "tree-evil", _seq("test", against="tree-evil"), tree_dir
        )
    finally:
        reset_gate_write_verifier(token)
    # GENUINE block: the gamed suite is green in pytest but the mutant SURVIVED.
    assert test_verdict.status is GateStatus.FAIL
    assert any("SURVIVED" in f for f in test_verdict.findings)
    assert test_verdict.runner_attestation is not None  # still a real, foldable verdict

    signer = ZoneBSigner(KEY, KEY_ID)
    gate_state = _all_pass_gate_state(signer, "tree-evil")
    gate_state["test"] = test_verdict
    delta = _run_emit(records, verifier, gate_state, "tree-evil", "run-mt-evil")
    assert delta["phase"] == "halted"
    assert _deploys(delta) == []


# ===========================================================================
# Attack 1 (END-TO-END through the COMPILED autonomous graph) — the
# discriminator is wired into the SHIPPING Test gate node, not just the unit test
# ===========================================================================
#
# A-M4-01 fix proof: a DiscriminatorFactory threaded build_factory_graph ->
# build_supervisor_graph -> the build_test_gate node -> run_build_test_gates ->
# the COMPILED TestGate. We drive the FULL autonomous spine (triage -> ... ->
# build_test_gate -> assurance -> autonomous emit) twice with the SAME factory:
#
#  * a GAMED-GREEN suite (clears the adequacy floor — one passing test — but does
#    NOT kill the held-out mutant) -> the compiled Test gate attests FAIL -> the
#    boundary routes fix/halt -> the autonomous emit deploys NOTHING; and
#  * a GENUINE suite (kills the held-out mutant) with the SAME factory wired ->
#    Test PASS -> build -> admit -> EXACTLY ONE deploy (the control).
#
# This proves the malicious-test block is in the SHIPPING path. The held-out
# mutant set is the dev-grade CURATED set the factory supplies (plan §0.3).

# ``app.py`` under test (the autonomous decomposition's single unit). The
# genuine suite kills the held-out mutant; the gamed suite is green-but-vacuous.
_E2E_APP = "def add(a, b):\n    return a + b\n"
# The held-out mutant the factory injects: add -> sub. A genuine suite that
# actually asserts on add() FAILs against the mutated tree (the mutant is killed);
# a vacuous suite stays green (the mutant SURVIVES) -> the discriminator FAILs.
_E2E_MUTANT = HeldOutMutant(
    mutant_id="e2e-add->sub", target="app.py", find="a + b", replace="a - b"
)


def _e2e_discriminator_factory() -> TestSuiteDiscriminator:
    """The injected DiscriminatorFactory — a dev-grade CURATED held-out mutant set
    for the subject under test (``app.py``). Produces a fresh discriminator each
    call (the gate constructs it per run); a real mutation engine is heavier
    (plan §0.3)."""
    return TestSuiteDiscriminator(mutants=[_E2E_MUTANT])


# A clean unified-diff ADDITION: app.py + a GENUINE suite that kills the mutant +
# a lockfile (so the assurance Security/Audit gates PASS). Mirrors the
# StubAdapter hunk shape the autonomous harness uses (first body line '+', rest
# verbatim) so gate_wiring's Phase-1 applier round-trips it.
_GENUINE_E2E_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+def add(a, b):\n    return a + b\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import app\ndef test_add():\n    assert app.add(2, 3) == 5\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)
# A GAMED-GREEN suite: it clears the adequacy floor (one passing test) but
# exercises nothing real, so the add->sub mutant SURVIVES -> the discriminator
# FAILs the Test gate in the compiled graph.
_GAMED_E2E_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+def add(a, b):\n    return a + b\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+import app\ndef test_vacuous():\n    assert app is not None\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
)


def _build_autonomous_with_discriminator(
    records: GlobalRecords, source_dir: str, *, diff_body: str, checkpointer: Any
) -> Any:
    """Wire the autonomous factory graph with the SAME trust-root chain the E2E
    harness uses, PLUS the held-out-mutant DiscriminatorFactory threaded through
    ``build_factory_graph`` (the A-M4-01 seam). The compiled Test gate node then
    consults the discriminator over the materialized integrated tree."""
    signer = ZoneBSigner(AUTO_KEY, AUTO_KEY_ID)
    return build_factory_graph(
        _auto_runtime(),
        runner=_auto_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        checkpointer=checkpointer,
        max_fix_attempts=2,
        mode=MODE_AUTONOMOUS,
        builder=TrustedBuilder(signer, builder_id=AUTO_BUILDER_ID),
        admission=AdmissionController(builder_id=AUTO_BUILDER_ID),
        deployer=Deployer(target=AUTO_DEPLOY_TARGET),
        gate_verifier=DevGateWriteVerifier(AUTO_KEY, AUTO_KEY_ID, records),
        discriminator_factory=_e2e_discriminator_factory,
    )


def test_compiled_autonomous_graph_genuine_suite_deploys_once(tmp_path: Any) -> None:
    """CONTROL (compiled-graph): a GENUINE suite that kills the held-out mutant,
    with the DiscriminatorFactory wired into the SHIPPING Test gate node, drives
    the full autonomous spine to EXACTLY ONE deploy. Proves the wired discriminator
    does NOT block a genuine suite (the block below is load-bearing, not a pipeline
    that never ships)."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(AUTO_KEY, AUTO_KEY_ID, records))
    try:
        app = _build_autonomous_with_discriminator(
            records, _src(tmp_path), diff_body=_GENUINE_E2E_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-disc-genuine")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    # The compiled Test gate ran the discriminator (mutant killed) -> PASS.
    assert statuses.get("test") is GateStatus.PASS
    for name in ("spec", "build", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    assert final["phase"] == "done"
    deploys = _deploy_actions(final)
    assert len(deploys) == 1
    assert deploys[0].kind == "deploy"


def test_compiled_autonomous_graph_gamed_suite_deploys_nothing(tmp_path: Any) -> None:
    """ATTACK (compiled-graph, A-M4-01): a GAMED-GREEN suite clears the adequacy
    floor (one passing test) but does NOT kill the held-out mutant. With the SAME
    DiscriminatorFactory wired into the SHIPPING Test gate node, the COMPILED Test
    gate attests FAIL — the malicious-test defense fires in the autonomous spine,
    not just in a unit test. The boundary halts and the autonomous emit deploys
    NOTHING. WITHOUT the wiring (the A-M4-01 gap) this gamed-green suite would have
    cleared the adequacy floor and shipped."""
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(AUTO_KEY, AUTO_KEY_ID, records))
    try:
        app = _build_autonomous_with_discriminator(
            records, _src(tmp_path), diff_body=_GAMED_E2E_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-disc-gamed")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    # Build PASSes (it compiles) and pytest is GREEN (the adequacy floor is met),
    # but the COMPILED Test gate's discriminator FAILs it: the mutant SURVIVED.
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.FAIL
    test_verdict = next(g for g in final["gates"] if g.name == "test")
    assert test_verdict.runner_attestation is not None  # a real attested FAIL
    assert any("SURVIVED" in f for f in test_verdict.findings)

    # Fail-closed: the Test FAIL halts the boundary -> autonomous emit deploys NOTHING.
    assert _deploy_actions(final) == []
    assert final["phase"] == "halted"


# ===========================================================================
# Attack 2 — FORGED-RESULTS cannot reach prod (hardened two-zone runner)
# ===========================================================================


def _pinned_config() -> PinnedScannerConfig:
    record = ScannerConfigRecord(
        rules=(ScannerRule(id="secret.aws", family="secret", version="1"),),
        severity_floors=(("sast", "high"),),
    )
    return PinnedScannerConfig.pin(record)


class _ConfigPinnedGate:
    """A class-2 gate whose zone-A observation reports the config it ran under
    (mirrors test_runner_hardening.py). ``decide`` PASSes a clean observation, so
    any downgrade is the runner's hardening, not the gate."""

    name = "security"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(self, *, ran_rev: str | None, seal_key: bytes | None = None) -> None:
        self._ran_rev = ran_rev
        self._seal_key = seal_key

    def execute(self, workdir: Path) -> ZoneAObservation:
        structured: dict[str, object] = {}
        if self._ran_rev is not None:
            structured[RAN_CONFIG_REV_KEY] = self._ran_rev
        return observe(
            self.name, exit_codes=(0,), findings=(), structured=structured,
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS


def test_forged_results_blocked_genuine_config_control(tmp_path: Path) -> None:
    """CONTROL: a run whose zone-A config matches the re-derived pin → the REAL
    hardened runner attests PASS → that PASS drives the emit chain to a deploy."""
    records, verifier = _records_verifier()
    pinned = _pinned_config()
    token = set_gate_write_verifier(verifier)
    try:
        runner = _runner(records, pinned_config=pinned)
        gate: GateImpl = _ConfigPinnedGate(ran_rev=pinned.pinned_revision)
        sec = runner.run(gate, "tree-cfg-ok", _seq("security", against="tree-cfg-ok"), tmp_path)
    finally:
        reset_gate_write_verifier(token)
    assert sec.status is GateStatus.PASS

    signer = ZoneBSigner(KEY, KEY_ID)
    gate_state = _all_pass_gate_state(signer, "tree-cfg-ok")
    gate_state["security"] = sec
    delta = _run_emit(records, verifier, gate_state, "tree-cfg-ok", "run-fr-good")
    assert delta["phase"] == "done"
    assert len(_deploys(delta)) == 1


def test_forged_results_cannot_reach_prod(tmp_path: Path) -> None:
    """ATTACK: a forged/weakened scanner config (zone A ran under a config that
    does NOT match the run-immutable pin) → the REAL hardened runner re-derives
    the pin and attests FAIL, never PASS. That non-PASS in the gate-state halts
    the emit guard — no build, admit, or deploy. A forged scanner config cannot
    fabricate a passing attested verdict."""
    records, verifier = _records_verifier()
    pinned = _pinned_config()
    token = set_gate_write_verifier(verifier)
    try:
        runner = _runner(records, pinned_config=pinned)
        gate: GateImpl = _ConfigPinnedGate(ran_rev="weakened-attacker-config")
        sec = runner.run(gate, "tree-cfg-evil", _seq("security", against="tree-cfg-evil"), tmp_path)
    finally:
        reset_gate_write_verifier(token)
    # GENUINE block: the runner re-derived the pin and downgraded to FAIL.
    assert sec.status is GateStatus.FAIL
    assert any("scanner-config mismatch" in f for f in sec.findings)
    assert sec.scanner_config_rev == pinned.pinned_revision  # the re-derived pin

    signer = ZoneBSigner(KEY, KEY_ID)
    gate_state = _all_pass_gate_state(signer, "tree-cfg-evil")
    gate_state["security"] = sec
    delta = _run_emit(records, verifier, gate_state, "tree-cfg-evil", "run-fr-evil")
    assert delta["phase"] == "halted"
    assert _deploys(delta) == []


class _TamperingGate:
    """Zone A produces a SEALED FAIL, then an agent-influenced path swaps it for a
    PASS re-sealed with a key it does not hold (mirrors test_runner_hardening.py).
    Only the seal check can stop the forged PASS."""

    name = "test"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def execute(self, workdir: Path) -> ZoneAObservation:
        honest_fail = observe("test", exit_codes=(1,), findings=("test failed",), seal_key=SEAL_KEY)
        forged_pass = observe(
            "test", exit_codes=(0,), findings=(), seal_key=b"attacker-forged-key-000000000000"
        )
        assert honest_fail.exit_codes == (1,)  # what zone A actually saw
        return forged_pass

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS if obs.exit_codes == (0,) else GateStatus.FAIL


def test_forged_results_tampered_observation_cannot_reach_prod(tmp_path: Path) -> None:
    """ATTACK (sealed channel): a tampered zone-A observation (FAIL swapped for a
    PASS) fails the seal the runner re-derives → the REAL runner attests ERROR,
    never PASS. The non-PASS halts the emit guard — no deploy."""
    records, verifier = _records_verifier()
    token = set_gate_write_verifier(verifier)
    try:
        runner = _runner(records, seal_key=SEAL_KEY)
        gate: GateImpl = _TamperingGate()
        tst = runner.run(gate, "tree-seal-evil", _seq("test", against="tree-seal-evil"), tmp_path)
    finally:
        reset_gate_write_verifier(token)
    assert tst.status is GateStatus.ERROR
    assert any("sealed-channel violation" in f for f in tst.findings)

    signer = ZoneBSigner(KEY, KEY_ID)
    gate_state = _all_pass_gate_state(signer, "tree-seal-evil")
    gate_state["test"] = tst
    delta = _run_emit(records, verifier, gate_state, "tree-seal-evil", "run-fr-seal")
    assert delta["phase"] == "halted"
    assert _deploys(delta) == []


# ===========================================================================
# Attack 3 — FORGED-VERDICT cannot reach prod (deploy-time admission)
# ===========================================================================


def _genuine_build(signer: ZoneBSigner, tree_sha: str) -> Any:
    builder = TrustedBuilder(signer, builder_id=BUILDER_ID)
    request = BuildRequest(
        build_type="dev-build",
        source_uri="git://repo",
        source_digest="sha256:" + "c" * 64,
        tree_sha=tree_sha,
        base_image_digest="sha256:" + "d" * 64,
        build_config_uri="cas://invocation/1",
        material_digests=(_MAT_A, _MAT_B),
        hermetic=True,
        seq=_seq("build", against=tree_sha),
    )
    return builder.build(request, components=_COMPONENTS)


def test_forged_verdict_blocked_admit_control() -> None:
    """CONTROL: a genuine attested gate-state + genuine build → the REAL admission
    ADMITs → the REAL deployer emits EXACTLY ONE deploy action."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fv-ok")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    gates = tuple(_all_pass_gate_state(signer, tree).values())

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=gates, required_gates=_REQUIRED_GATES,
        records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.ADMIT

    _result, action = Deployer(target=DEPLOY_TARGET).deploy(
        decision, admission_decision_uri="cas://admission/ok", seq=_seq("deploy")
    )
    assert action.kind == "deploy"


def test_forged_verdict_unsigned_attestation_cannot_reach_prod() -> None:
    """ATTACK: an agent-forged (unsigned) required gate verdict → the REAL
    admission re-verification fails the signature check → DENY. The deployer then
    REFUSES a non-admitted decision (DeployRefused) → no deploy can be emitted."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fv-unsigned")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    gates = list(_all_pass_gate_state(signer, tree).values())
    # Strip the attestation off the security verdict (an agent-forged PASS).
    gates[_REQUIRED_GATES.index("security")] = gates[
        _REQUIRED_GATES.index("security")
    ].model_copy(update={"runner_attestation": None})

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(gates), required_gates=_REQUIRED_GATES,
        records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_signature_invalid"

    # The deployer fail-closes: a denied decision can never produce a deploy.
    try:
        Deployer(target=DEPLOY_TARGET).deploy(
            decision, admission_decision_uri="cas://admission/denied", seq=_seq("deploy")
        )
        raise AssertionError("deployer should refuse a non-admitted decision")
    except DeployRefused:
        pass


def test_forgery_deny_disqualifies_digest_cross_fork() -> None:
    """The autonomous emit node's cross-fork defense (gov §5, trust-audit F1): a
    forgery/tamper admission DENY disqualifies the digest in the ledger, so a sibling
    fork re-presenting the SAME digest under a fresh run_id is also denied. An honest
    gate FAILURE is NOT a forgery and must NOT poison the digest."""
    from foundry.state.global_records import AdmissionState
    from foundry.trust_root.admission import FORGERY_DENY_REASONS

    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fv-disq")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    gates = list(_all_pass_gate_state(signer, tree).values())
    # Agent-forged (unsigned) security verdict -> signature-invalid forgery deny.
    gates[_REQUIRED_GATES.index("security")] = gates[
        _REQUIRED_GATES.index("security")
    ].model_copy(update={"runner_attestation": None})

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(gates), required_gates=_REQUIRED_GATES,
        records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason in FORGERY_DENY_REASONS              # a forgery, not a benign fail

    # This is what the emit node does on a forgery deny (factory_graph):
    records.disqualify_digest(decision.subject_digest, decision.reason, "run-A")
    # A sibling fork re-presenting the SAME digest now reads DISQUALIFIED.
    assert records.admission_look(digest) is AdmissionState.DISQUALIFIED

    # Honest failure must NOT be treated as forgery (the stable-FAIL latch owns that).
    assert "required_gate_not_pass" not in FORGERY_DENY_REASONS


def test_forged_terminal_gate_across_trees_cannot_reach_prod() -> None:
    """ATTACK (M4-R1-01): the release spans TWO materialized trees — the terminal
    Spec/Build/Test gates on the terminal tree, the assurance gates (security/
    audit) on the provenance/assurance tree (AW2 re-hash). An agent forges the
    TERMINAL ``build`` verdict (strips its attestation). Admission now
    CRYPTOGRAPHICALLY re-verifies EVERY required gate across BOTH trees (pinned by
    ``expected_gate_trees``), so the forged terminal gate fails the signature
    re-check → DENY (previously the terminal gates rested only on the upstream
    emit guard's merge_gates filter, NOT admission). The deployer then refuses the
    non-admitted decision → no deploy."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    assurance_tree = "tree-assur"
    terminal_tree = "tree-terminal"
    # Provenance binds the assurance tree (the build is keyed to it).
    build = _genuine_build(signer, assurance_tree)
    digest = build.result.subject_digest

    # Terminal gates on the terminal tree; assurance gates on the provenance tree.
    terminal = {
        name: _signed_pass(signer, name, terminal_tree, local=i)
        for i, name in enumerate(("spec", "build", "test"))
    }
    assurance = {
        name: _signed_pass(signer, name, assurance_tree, local=10 + i)
        for i, name in enumerate(("security", "audit"))
    }
    expected = {n: terminal_tree for n in terminal} | {n: assurance_tree for n in assurance}
    gate_state = {**terminal, **assurance}
    # FORGE the terminal `build` gate (strip its attestation — agent-minted PASS).
    gate_state["build"] = gate_state["build"].model_copy(update={"runner_attestation": None})

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gate_state.values()),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_signature_invalid"
    with_pytest_refuse(decision)


def test_wrong_tree_terminal_gate_across_trees_cannot_reach_prod() -> None:
    """ATTACK (M4-R1-01): a genuinely-signed TERMINAL ``test`` verdict re-presented
    for the WRONG tree (not its expected terminal tree) across the two-tree
    release → admission's input-binding rejects it against its expected terminal
    tree → DENY → no deploy. Proves admission covers the terminal gates' DISTINCT
    tree, not just the assurance group bound to the provenance."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    assurance_tree = "tree-assur-2"
    terminal_tree = "tree-terminal-2"
    build = _genuine_build(signer, assurance_tree)
    digest = build.result.subject_digest

    terminal = {
        name: _signed_pass(signer, name, terminal_tree, local=i)
        for i, name in enumerate(("spec", "build", "test"))
    }
    assurance = {
        name: _signed_pass(signer, name, assurance_tree, local=10 + i)
        for i, name in enumerate(("security", "audit"))
    }
    expected = {n: terminal_tree for n in terminal} | {n: assurance_tree for n in assurance}
    gate_state = {**terminal, **assurance}
    # The `test` verdict is genuinely signed — but for a THIRD tree.
    gate_state["test"] = _signed_pass(signer, "test", "tree-elsewhere", local=99)

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest,
        provenance=build.provenance,
        build_verdict=build.signed_verdict,
        gate_results=tuple(gate_state.values()),
        required_gates=_REQUIRED_GATES,
        records=records,
        verifier=verifier,
        expected_gate_trees=expected,
        seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_wrong_tree"
    with_pytest_refuse(decision)


def test_forged_verdict_wrong_tree_cannot_reach_prod() -> None:
    """ATTACK (replay from a DIFFERENT tree): a required gate verdict genuinely
    signed but for a DIFFERENT tree/digest → the REAL admission input-binding
    re-verify rejects it (the gate-state must key to the provenance's exact tree)
    → DENY → no deploy."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fv-wrong")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    gates = list(_all_pass_gate_state(signer, tree).values())
    # The `test` verdict is genuinely signed — but for a DIFFERENT tree.
    gates[_REQUIRED_GATES.index("test")] = _signed_pass(
        signer, "test", "some-other-tree", local=99
    )

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(gates), required_gates=_REQUIRED_GATES,
        records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "required_gate_wrong_tree"
    with_pytest_refuse(decision)


def with_pytest_refuse(decision: Any) -> None:
    """The deployer fail-closes on any non-admitted decision."""
    try:
        Deployer(target=DEPLOY_TARGET).deploy(
            decision, admission_decision_uri="cas://admission/denied", seq=_seq("deploy")
        )
        raise AssertionError("deployer should refuse a non-admitted decision")
    except DeployRefused:
        pass


# ===========================================================================
# Attack 4 — FORK-DIVERGENCE cannot reach prod (cross-fork global_records)
# ===========================================================================


def test_fork_divergence_blocked_admissible_control() -> None:
    """CONTROL: an un-disqualified digest with valid signatures → admission_look
    ADMISSIBLE → the REAL admission ADMITs → deploys."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fork-ok")
    digest = build.result.subject_digest
    from foundry.state.global_records import AdmissionState

    assert records.admission_look(digest) is AdmissionState.ADMISSIBLE
    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(_all_pass_gate_state(signer, build.provenance.tree_sha).values()),
        required_gates=_REQUIRED_GATES, records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.ADMIT


def test_fork_divergence_disqualified_digest_cannot_reach_prod() -> None:
    """ATTACK: a digest cross-fork DISQUALIFIED in global_records (a laundered /
    forged fork re-presenting it under a fresh run_id) → admission_look returns
    DISQUALIFIED → the REAL admission DENYs even though the build signature + every
    gate attestation are otherwise VALID → no deploy. The disqualification is keyed
    on the DIGEST (never the run_id), so a new fork cannot launder it."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fork-evil")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    # A DIFFERENT fork disqualified this exact digest.
    assert records.disqualify_digest(digest, "forged-verdict fork", "fork-run-99")
    from foundry.state.global_records import AdmissionState

    assert records.admission_look(digest) is AdmissionState.DISQUALIFIED

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(_all_pass_gate_state(signer, tree).values()),
        required_gates=_REQUIRED_GATES, records=records, verifier=verifier, seq=_seq(),
    )
    # Signatures are valid, gate-state is clean PASS — admission STILL DENYs.
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "stable_fail_latched_or_disqualified"
    assert decision.stable_fail_latched
    with_pytest_refuse(decision)


def test_fork_divergence_stable_fail_latch_cannot_reach_prod() -> None:
    """ATTACK: a digest with a stable-FAIL latch (any gate) → admission_look
    DISQUALIFIED → DENY, even though the live gate-state shows PASS (gov §5: WORM
    holds it latched across forks) → no deploy."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fork-latch")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    records.latch_stable_fail(digest, "security", "run-7")

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(_all_pass_gate_state(signer, tree).values()),
        required_gates=_REQUIRED_GATES, records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "stable_fail_latched_or_disqualified"
    with_pytest_refuse(decision)


def test_stable_fail_latch_on_TREE_namespace_cannot_reach_prod() -> None:
    """ATTACK (H2): the runner latches stable FAIL on the TREE sha (`run_against`) —
    a DIFFERENT namespace from `subject_digest`. A later PASS masks the FAIL in the
    live gate-state, and check #4's subject query can't see a TREE latch. The test
    above latches the SUBJECT digest (so #4 catches it); this one latches the TREE,
    the way production actually does, and proves admission STILL denies via #4b."""
    records, verifier = _records_verifier()
    signer = ZoneBSigner(KEY, KEY_ID)
    build = _genuine_build(signer, "tree-fork-treelatch")
    tree = build.provenance.tree_sha
    digest = build.result.subject_digest
    # Latch on the TREE, not the subject digest (what the runner does on a FAIL).
    records.latch_stable_fail(tree, "security", "run-9")
    from foundry.state.global_records import AdmissionState

    # The namespaces are genuinely distinct: the SUBJECT reads admissible…
    assert records.admission_look(digest) is AdmissionState.ADMISSIBLE
    # …while the TREE is disqualified. Check #4 (subject) alone would miss this.
    assert records.admission_look(tree) is AdmissionState.DISQUALIFIED

    decision = AdmissionController(builder_id=BUILDER_ID).admit(
        digest, provenance=build.provenance, build_verdict=build.signed_verdict,
        gate_results=tuple(_all_pass_gate_state(signer, tree).values()),
        required_gates=_REQUIRED_GATES, records=records, verifier=verifier, seq=_seq(),
    )
    assert decision.verdict is AdmissionVerdict.DENY
    assert decision.reason == "stable_fail_latched_or_disqualified"
    assert decision.stable_fail_latched
    with_pytest_refuse(decision)


# ===========================================================================
# Clean control — full E2E autonomous run deploys EXACTLY ONCE
# ===========================================================================


def test_clean_autonomous_run_deploys_exactly_once(tmp_path: Any) -> None:
    """The fully-genuine E2E control: the REAL compiled autonomous graph (real
    builder + admission ADMIT + deployer) over a clean run → EXACTLY ONE deploy
    action. This proves attacks 1-4 are real, load-bearing blocks — the same
    machinery DOES ship a clean build. Reuses the T-M4-10 autonomous harness."""
    from tests.test_factory_graph_autonomous import _CLEAN_DIFF

    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(
        DevGateWriteVerifier(AUTO_KEY, AUTO_KEY_ID, records)
    )
    try:
        app = _build_autonomous(
            records, _src(tmp_path), diff_body=_CLEAN_DIFF, checkpointer=ckpt.saver
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-phase4-clean")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    for name in ("spec", "build", "test", "security", "audit"):
        assert statuses.get(name) is GateStatus.PASS, name
    assert final["phase"] == "done"
    deploys = _deploy_actions(final)
    assert len(deploys) == 1
    assert deploys[0].kind == "deploy"
    assert deploys[0].gate_bearing is True
    assert AUTO_BUILDER_ID  # harness sanity


# ===========================================================================
# Trusted-component outage → PAUSED, not WEDGED
# ===========================================================================


def test_trusted_component_outage_degrades_to_paused_not_wedged() -> None:
    """EXIT property: a trusted-component (the zone-B signer) outage degrades the
    dependent gate/judgment node to PAUSED(trusted_component_unavailable) — a
    clear $0/hr wait, NOT WEDGED — and recovers to RUNNING. The dependent node
    never fakes a verdict it cannot get trustworthily."""
    outage = AvailabilityModel().with_status(
        ComponentStatus(
            component=TrustedComponent.ZONE_B_SIGNER,
            available=False,
            detail="signer unreachable",
        )
    )
    paused = classify_for_dependencies(
        outage,
        run_id="r1",
        current_phase="quality",
        dependencies=[TrustedComponent.ZONE_B_SIGNER],
        seq=_seq("judge"),
        running_cost_rate_microusd_per_hr=5_000,
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.status is not RunStatus.WEDGED
    assert paused.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    assert paused.cost_rate_microusd_per_hr == 0
    assert paused.is_alertable() is False

    # Recovery resumes (RUNNING) — the round-trip the EXIT property asserts.
    resumed = on_recovery(
        run_id="r1", current_phase="quality", seq=_seq("judge"),
        cost_rate_microusd_per_hr=5_000,
    )
    assert resumed.status is RunStatus.RUNNING
    assert resumed.pause_reason is None


def test_trusted_component_ha_outage_fails_over_not_wedged() -> None:
    """A single-instance outage of a trusted component fails over to a healthy
    replica → RUNNING (not WEDGED); with NO healthy instance it STAYS PAUSED
    (still not WEDGED, fail-closed)."""
    # Primary down, replica healthy → failover → RUNNING.
    ha_one_up = HAComponent(
        component=TrustedComponent.GATE_RUNNER,
        primary="runner-a",
        instances=(
            ComponentInstance(instance_id="runner-a", healthy=False),
            ComponentInstance(instance_id="runner-b", healthy=True),
        ),
    )
    after, rec = classify_ha(
        ha_one_up, run_id="r1", current_phase="gates", seq=_seq("gate"),
        running_cost_rate_microusd_per_hr=4_000,
    )
    assert rec.status is RunStatus.RUNNING
    assert rec.status is not RunStatus.WEDGED
    assert after.primary == "runner-b"

    # All instances down → STAYS PAUSED, never WEDGED.
    ha_all_down = HAComponent(
        component=TrustedComponent.GATE_RUNNER,
        primary="runner-a",
        instances=(
            ComponentInstance(instance_id="runner-a", healthy=False),
            ComponentInstance(instance_id="runner-b", healthy=False),
        ),
    )
    _after2, rec2 = classify_ha(
        ha_all_down, run_id="r1", current_phase="gates", seq=_seq("gate"),
    )
    assert rec2.status is RunStatus.PAUSED
    assert rec2.status is not RunStatus.WEDGED
    assert rec2.pause_reason is PauseReason.TRUSTED_COMPONENT_UNAVAILABLE
    assert rec2.cost_rate_microusd_per_hr == 0
