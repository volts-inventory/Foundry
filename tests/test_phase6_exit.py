"""Phase-6 EXIT integration test — the FINAL milestone witness (T-M6-12).

This is the binding acceptance test for the docs/roadmap.md Phase-6 EXIT criteria.
It COMPOSES the shipped M6 surface (it adds NO core code — it OWNS only this file
plus a LEARNINGS note) and proves each exit criterion GENUINELY: every claim has a
CONTROL that flips, so none is vacuous.

The roadmap Phase-6 EXIT criteria, and where each is proven:

1. **Scorecard published + STABLE.** ``build_headline_scorecard`` over a VALID
   ``FactoryEvalResult`` publishes; ``stable_digest()`` is byte-identical across
   two builds (the "stable" bit); a NON-valid eval (integrity/safeties dirty)
   refuses to publish (``NotPublishableError`` — the fail-closed control).
2. **Phase-6 A/B reproduces the Phase-1.5 result WITHOUT re-spending alpha.** The
   replication ``reproduces_phase15`` the Phase-1.5 PROCEED AND the alpha look-count
   is IDENTICAL before/after (delta 0); a DISCONFIRMING replication leaves the
   Phase-1.5 decision/ledger UNTOUCHED (the control — a Phase-6 disconfirmation
   cannot un-spend the sunk cost and is never a second gate, §6.5).
3. **Optimization Pareto-improves / non-Pareto rejected.** A genuine Pareto
   improvement → ADOPT; a candidate that regresses ANY of
   {escaped-defects, cost, wall-clock, policy-compliance} → REJECT (named); a TIE
   → REJECT (the controls).
4. **Product-feedback + maintenance/provenance loop.** A production signal → a
   backlog item that RE-ENTERS via the maintenance RE-GATE path (no bypass); the
   maintained release has a COMPLETE, tamper-evident provenance trace (a
   tampered/dropped link breaks it — control); a released artifact's license is
   provenance-backed (no provenance → no clean license — control).
5. **(bonus) Red-team all-defended.** ``run_red_team()`` reports every probe
   DEFENDED on the real defenses; the weakened-defense control reports UNDEFENDED.

HONESTY (mapped in the LEARNINGS note): the eval/optimization/replication/red-team
layers run as a METHODOLOGY DRY-RUN on SYNTHETIC fixtures (the logic is real, the
inputs synthetic — NO funded live study); the maintenance/provenance/licensing
loop runs against the REAL trust seams (the two-zone runner, the admission
controller, the evidence-pack hash-chain) over a real on-disk workdir — GENUINE
E2E. See the per-test docstrings + the LEARNINGS map for the exact line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.evaluation.ab_runner import (
    ABResult,
    ArmReport,
    BootstrapComparison,
    SecondaryComparison,
)

# --- M6 evaluation surface ---------------------------------------------------
from foundry.evaluation.agent_validity import AgentValidityReport, RoleValidity, ValidityRole
from foundry.evaluation.decision_table import DecisionRecord, decide
from foundry.evaluation.factory_eval import FactoryEvalResult, run_factory_eval
from foundry.evaluation.hypothesis import Arm, CentralHypothesis, DecisionClass, Provenance
from foundry.evaluation.integrity import AgentRuntimeSurface
from foundry.evaluation.optimization import (
    MetricVector,
    evaluate_optimization,
    pareto_improves,
)
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import PreRegistration, freeze_preregistration
from foundry.evaluation.red_team import RedTeamHarness
from foundry.evaluation.replication_ab import (
    ReplicationResult,
    reproduces_phase15,
    run_replication_ab,
)
from foundry.evaluation.scorecard import (
    HeadlineScorecard,
    NotPublishableError,
    SecondaryReading,
    build_headline_scorecard,
)
from foundry.evaluation.task_fixtures import TaskFrame, generate_synthetic_frame

# --- M6 lifecycle surface (REAL trust seams) ---------------------------------
from foundry.gates import (
    AuditGate,
    BuildGate,
    DevGateWriteVerifier,
    SecurityGate,
    SpecGate,
    TestGate,
    TwoZoneGateRunner,
    ZoneBSigner,
)
from foundry.graph.reverification import changed_scope
from foundry.lifecycle.feedback_loop import (
    MAINTENANCE_ROUTE,
    ProductSignal,
    SignalKind,
    feedback_to_backlog,
)
from foundry.lifecycle.licensing import UnlicensableArtifactError, license_for
from foundry.lifecycle.maintenance import maintain, plan_maintenance
from foundry.lifecycle.provenance import (
    ProvenanceRecord,
    trace,
)
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateStatus
from foundry.schemas.security import SBOMComponent
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha
from foundry.trust_root import AdmissionController, TrustedBuilder

# ===========================================================================
# Shared synthetic-eval builders (the proven M6 fixture-assembly patterns).
# ===========================================================================

_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000
_DECISION_ID = "dec-phase15"


def _frame(*, n_tasks: int = 4, seed: int = 0) -> TaskFrame:
    # Small frame keeps the FOUNDRY-arm pipeline runs cheap + deterministic.
    return generate_synthetic_frame(n_tasks=n_tasks, seed=seed, pilot_fraction=0.5)


def _funded_rung() -> FundedPowerRung:
    return compute_power_rung(
        PilotResult(
            control_base_rate=0.2,
            effect_size_variance=0.01,
            control_resolution_variance=0.01,
            pilot_n=8,
            k_repeats=2,
            consumed_budget_microusd=10_000,
            eval_set_identity="pilot-eval-set",
        ),
        mde=_MDE,
        alpha=_ALPHA,
        target_power=_TARGET_POWER,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )


def _frozen_prereg(frame: TaskFrame, *, ledger: GlobalRecords) -> PreRegistration:
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE, funded_power_rung=rung, provenance=Provenance.SCORED, dry_run=False
    )
    return freeze_preregistration(
        hyp,
        rung,
        frame.scored_slice.identity,  # BIND to the scored slice
        decision_id=_DECISION_ID,
        ledger=ledger,
    )


def _valid_validity() -> AgentValidityReport:
    """A precomputed fully-valid §4 report (injected to avoid slow agent runs)."""
    return AgentValidityReport(
        roles=tuple(
            RoleValidity(role=r, valid=True, planted_defect="planted", observed="caught")
            for r in ValidityRole
        )
    )


def _clean_eval(frame: TaskFrame, **overrides: object) -> FactoryEvalResult:
    kwargs: dict[str, object] = {"agent_validity": _valid_validity()}
    kwargs.update(overrides)
    return run_factory_eval(frame, **kwargs)  # type: ignore[arg-type]


def _ab_result(prereg: PreRegistration, *, win: bool) -> ABResult:
    """A hand-built ABResult with a known FOUNDRY-vs-baseline read."""
    point = 0.18 if win else 0.0
    primary = BootstrapComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=point,
        ci_low=point - 0.05,
        ci_high=point + 0.05,
        alpha=_ALPHA,
        mde=_MDE,
        n_tasks=8,
        resamples=2000,
        significant_at_alpha=win,
        meets_mde=win,
    )
    secondary = SecondaryComparison(
        name="foundry_vs_baseline_efficiency",
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=0.0,
        raw_p=0.9,
        holm_adjusted_p=0.9,
        significant=False,
    )

    def _arm(arm: Arm, *, rate: float, cost: int, sim: bool) -> ArmReport:
        attempted = 10
        resolved = round(rate * attempted)
        return ArmReport(
            arm=arm,
            itt_pass_rate=resolved / attempted,
            resolved_count=resolved,
            attempted=attempted,
            total_cost_microusd=cost,
            simulated=sim,
        )

    return ABResult(
        registration_identity=prereg.registration_identity(),
        scored_eval_set_identity=prereg.scored_eval_set_identity,
        seed=0,
        k_repeats=1,
        n_tasks=8,
        arm_reports=(
            _arm(Arm.FOUNDRY, rate=0.6, cost=900, sim=False),
            _arm(Arm.BASELINE_SINGLE_AGENT, rate=0.3, cost=1_000, sim=True),
            _arm(Arm.SINGLE_AGENT_PLUS_GATES, rate=0.4, cost=900, sim=True),
        ),
        primary=primary,
        secondaries=(secondary,),
        provenance=Provenance.SCORED,
        dry_run=False,
    )


def _phase15_decision(prereg: PreRegistration, *, win: bool) -> DecisionRecord:
    """A REAL Phase-1.5 DecisionRecord via ``decide`` (PROCEED on a win else KILL)."""
    return decide(_ab_result(prereg, win=win), prereg)


def _winning_core(frame: TaskFrame, prereg: PreRegistration, seed: int) -> ABResult:
    """An estimation core returning a FOUNDRY-beats-baseline read (a positive)."""
    return _ab_result(prereg, win=True)


def _null_core(frame: TaskFrame, prereg: PreRegistration, seed: int) -> ABResult:
    """An estimation core returning a NULL read (no FOUNDRY-vs-baseline signal)."""
    return _ab_result(prereg, win=False)


def _secondaries() -> tuple[SecondaryReading, ...]:
    # Three secondaries (alpha=0.05, m=3): Holm rejects only the smallest p, so the
    # two 0.04 endpoints are RAW-significant but Holm-NON-significant.
    return (
        SecondaryReading(name="novel_suite_resolution", point_estimate=0.2, raw_p=0.001),
        SecondaryReading(name="hidden_test_pass_rate", point_estimate=0.05, raw_p=0.04),
        SecondaryReading(name="handoff_fidelity", point_estimate=0.03, raw_p=0.04),
    )


def _dirty_eval(frame: TaskFrame) -> FactoryEvalResult:
    """A NON-valid eval: a reachable hidden test trips integrity (§6) → not valid."""
    scored = tuple(sorted(frame.scored_slice.task_ids))
    victim = scored[0]
    oracle = frame.fixture(victim).oracle
    surfaces = {tid: AgentRuntimeSurface(task_id=tid) for tid in scored}
    surfaces[victim] = AgentRuntimeSurface(
        task_id=victim,
        reachable_refs=frozenset({oracle.hidden_test_id, oracle.expected_output}),
    )
    return _clean_eval(frame, surfaces=surfaces)


# ===========================================================================
# CRITERION 1 — Scorecard published + STABLE (fail-closed control flips).
# ===========================================================================


def test_exit_c1_scorecard_published_and_byte_stable() -> None:
    """A VALID eval → a PUBLISHED headline scorecard whose digest is STABLE.

    Mechanism: ``build_headline_scorecard`` over a VALID ``FactoryEvalResult`` (+ the
    Phase-1.5 arbiter read + the Phase-6 replication overlay) yields a publishable
    ``HeadlineScorecard``; two builds from the SAME inputs are byte-identical, so
    ``stable_digest()`` coincides. This is the roadmap "scorecard published + stable".
    """
    frame = _frame()
    ledger = GlobalRecords()
    prereg = _frozen_prereg(frame, ledger=ledger)
    decision = _phase15_decision(prereg, win=True)
    replication = run_replication_ab(
        frame, prereg, decision, seed=0, estimation_core=_winning_core
    )
    eval_result = _clean_eval(frame)
    assert eval_result.valid is True  # the published headline rests on a VALID eval

    first = build_headline_scorecard(
        eval_result, replication=replication, decision=decision, secondaries=_secondaries()
    )
    second = build_headline_scorecard(
        eval_result, replication=replication, decision=decision, secondaries=_secondaries()
    )
    assert isinstance(first, HeadlineScorecard)
    assert first.publishable is True
    # PUBLISHED + STABLE: byte-identical across two builds (the roadmap "stable" bit).
    assert first == second
    assert first.stable_digest() == second.stable_digest()
    assert first.render() == second.render()
    # The stable digest is surfaced in the published render (it IS the published bit).
    assert first.stable_digest() in first.render()


def test_exit_c1_control_non_valid_eval_refuses_to_publish() -> None:
    """CONTROL (fail-closed): a NON-valid eval refuses to publish (the digest never
    stabilises around a tripped eval). Same frame, only the invalidating input added,
    so the refusal is non-vacuous against the published case above."""
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg, win=True)

    dirty = _dirty_eval(frame)
    assert dirty.valid is False  # fail-closed invalidation upstream
    with pytest.raises(NotPublishableError):
        build_headline_scorecard(dirty, decision=decision)


# ===========================================================================
# CRITERION 2 — Phase-6 A/B reproduces Phase-1.5 WITHOUT re-spending alpha.
# ===========================================================================


def test_exit_c2_replication_reproduces_phase15_and_spends_no_alpha() -> None:
    """The Phase-6 replication REPRODUCES the Phase-1.5 PROCEED with alpha-delta 0.

    Mechanism: the Phase-1.5 ``freeze_preregistration`` DREW exactly one alpha look;
    the confirmatory ``decide`` returned PROCEED. The Phase-6 ``run_replication_ab``
    is estimation-only — it reproduces that PROCEED yet the ledger look-count is
    IDENTICAL before/after (delta 0). This is the roadmap "reproduces the Phase-1.5
    result (alpha NOT re-spent)".
    """
    ledger = GlobalRecords()
    frame = _frame(n_tasks=8, seed=1)
    prereg = _frozen_prereg(frame, ledger=ledger)  # DREW the Phase-1.5 alpha look
    decision = _phase15_decision(prereg, win=True)
    assert decision.decision is DecisionClass.PROCEED

    looks_before = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    assert looks_before == 1  # Phase-1.5 spent exactly one look

    result = run_replication_ab(
        frame, prereg, decision, seed=0, ledger=ledger, estimation_core=_winning_core
    )
    looks_after = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)

    assert isinstance(result, ReplicationResult)
    # REPRODUCES the Phase-1.5 decision (both the method + the module-level alias).
    assert result.reproduces is True
    assert result.reproduces_phase15(decision) is True
    assert reproduces_phase15(result, decision) is True
    # ALPHA NOT RE-SPENT: the look-count is IDENTICAL before/after (delta 0).
    assert looks_after == looks_before == 1
    assert result.alpha_looks_before == 1
    assert result.alpha_looks_after == 1
    assert result.alpha_spent_delta == 0
    assert result.is_estimation_only is True


def test_exit_c2_control_disconfirming_replication_leaves_phase15_untouched() -> None:
    """CONTROL: a DISCONFIRMING (null) replication does NOT reproduce the PROCEED —
    and CANNOT alter the Phase-1.5 decision/ledger (§6.5: not a second gate).

    The flip vs the criterion above: same PROCEED decision, but a null estimation
    core → ``reproduces is False``, while the decision object stays PROCEED and the
    sunk alpha is neither un-spent nor re-spent (delta still 0)."""
    ledger = GlobalRecords()
    frame = _frame(n_tasks=8, seed=2)
    prereg = _frozen_prereg(frame, ledger=ledger)
    decision = _phase15_decision(prereg, win=True)
    assert decision.decision is DecisionClass.PROCEED

    looks_before = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    result = run_replication_ab(
        frame, prereg, decision, seed=0, ledger=ledger, estimation_core=_null_core
    )
    looks_after = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)

    # A Phase-6 DISCONFIRMATION is recorded...
    assert result.reproduces is False
    assert result.reproduces_phase15(decision) is False
    # ...but the Phase-1.5 decision/ledger is UNCHANGED — a disconfirmation never
    # un-spends the sunk cost and is never a fresh verdict (§6.5).
    assert decision.decision is DecisionClass.PROCEED
    assert looks_before == looks_after
    assert result.alpha_spent_delta == 0


# ===========================================================================
# CRITERION 3 — Optimization Pareto-improves ≥1 / non-Pareto rejected.
# ===========================================================================


def _opt_baseline() -> MetricVector:
    return MetricVector(
        resolution_rate=0.50,
        policy_compliance=0.90,
        escaped_defects=10,
        cost_microusd=1_000_000,
        wall_clock_ms=60_000,
    )


def test_exit_c3_genuine_pareto_improvement_is_adopted() -> None:
    """A genuine Pareto improvement (≥1 headline metric up, NO regression) → ADOPT.

    Mechanism: ``evaluate_optimization`` adopts iff the candidate is >= baseline on
    EVERY constrained metric and strictly > on >= 1. This is the roadmap
    "Pareto-improves >= 1 headline metric with no regression".
    """
    base = _opt_baseline()
    # One strict win (resolution up), every constrained metric held → Pareto.
    cand = base.model_copy(update={"resolution_rate": 0.60})
    assert pareto_improves(base, cand) is True

    decision = evaluate_optimization(base, cand)
    assert decision.adopted is True
    assert "resolution_rate" in decision.improved_metrics
    assert decision.regressed_metrics == ()
    assert "ADOPT" in decision.rationale


@pytest.mark.parametrize(
    ("update", "regressed"),
    [
        ({"resolution_rate": 0.70, "escaped_defects": 15}, "escaped_defects"),
        ({"resolution_rate": 0.70, "cost_microusd": 1_500_000}, "cost_microusd"),
        ({"resolution_rate": 0.70, "wall_clock_ms": 90_000}, "wall_clock_ms"),
        ({"resolution_rate": 0.70, "policy_compliance": 0.80}, "policy_compliance"),
    ],
)
def test_exit_c3_control_regression_in_any_constrained_metric_is_rejected(
    update: dict[str, object], regressed: str
) -> None:
    """CONTROL (non-Pareto rejected): a win bought with a regression in ANY of
    {escaped-defects, cost, wall-clock, policy-compliance} → REJECT, metric NAMED.

    Each case improves resolution but regresses ONE constrained metric — the exact
    four the roadmap names as "no regression" guards — and the gate fails closed."""
    base = _opt_baseline()
    cand = base.model_copy(update=update)
    assert pareto_improves(base, cand) is False

    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert regressed in decision.regressed_metrics
    assert regressed in decision.rationale  # the regressed metric is NAMED
    assert "REJECT" in decision.rationale


def test_exit_c3_control_tie_is_rejected() -> None:
    """CONTROL: a TIE (no strict improvement, no regression) → REJECT (no progress).
    A non-improvement is NOT an adoption — the gate demands a real win."""
    base = _opt_baseline()
    cand = base.model_copy()  # identical
    assert pareto_improves(base, cand) is False

    decision = evaluate_optimization(base, cand)
    assert decision.adopted is False
    assert decision.improved_metrics == ()
    assert decision.regressed_metrics == ()
    assert "TIE" in decision.rationale


# ===========================================================================
# CRITERION 4 — Product-feedback + maintenance/provenance loop.
#
# REAL trust seams: a real two-zone runner, a real hermetic build, the real
# admission controller, the real evidence-pack hash-chain. Over a real on-disk
# workdir — GENUINE E2E.
# ===========================================================================

_KEY = b"zone-b-dev-key-0123456789abcdef"
_KEY_ID = "zoneB-dev"
_SCANNER_REV = "scanner-rev-1"
_LINEAGE_REV = "lineage-rev-1"
_RUN_ID = "p6-maint-run-1"
_TOOLCHAIN = "foundry-dev-toolchain-v1"
_BUILDER_ID = "dev-trusted-builder-1"

_SRC = "sha256:" + "c" * 64
_BASE = "sha256:" + "d" * 64
_MAT_A = "sha256:" + "a" * 64

_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'
_SECRET_MODULE = 'API_KEY = "AKIAIOSFODNN7EXAMPLEsecrettoken1234567890"\n'

_CRITERIA = [
    {"id": "c1", "text": "do a thing", "oracle": "test_c1", "security_abuse": False},
    {"id": "c2", "text": "no abuse", "oracle": "test_c2", "security_abuse": True},
]

_GATES = (
    SpecGate(),
    BuildGate(),
    TestGate(min_passed=1),
    AuditGate(),
    SecurityGate(),
)
_REQUIRED = tuple(g.name for g in _GATES)
_COMPONENTS = (SBOMComponent(name="libfoo", version="1.2.3", digest=_MAT_A, license="MIT"),)


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(_KEY, _KEY_ID), records, _SCANNER_REV, _LINEAGE_REV)


def _seq(node: str = "maintain", epoch: int = 1) -> Seq:
    return Seq(run_id=_RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _materialize(dest: Path, files: dict[str, str]) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "criteria.json").write_text(json.dumps(_CRITERIA), encoding="utf-8")
    for rel, body in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tree_sha(dest, toolchain_digest=_TOOLCHAIN, prefer_git=False)


def _run_all(runner: TwoZoneGateRunner, workdir: Path, sha: str):
    return [runner.run(g, sha, _seq(f"{g.name}_gate"), workdir) for g in _GATES]


def _clean_files() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


def _with_criteria(files: dict[str, str]) -> dict[str, str]:
    return files | {"criteria.json": json.dumps(_CRITERIA)}


def _build_provenance(signer: ZoneBSigner, *, tree: str):
    """A genuine hermetic build keyed to the maintained tree_sha."""
    from foundry.schemas.trust_root import BuildRequest

    builder = TrustedBuilder(signer, builder_id=_BUILDER_ID)
    request = BuildRequest(
        build_type="dev-build",
        source_uri="git://repo",
        source_digest=_SRC,
        tree_sha=tree,
        base_image_digest=_BASE,
        build_config_uri="cas://invocation/1",
        material_digests=(_MAT_A,),
        hermetic=True,
        seq=_seq("build"),
    )
    return builder.build(request, components=_COMPONENTS)


def _feedback_signal(kind: SignalKind = SignalKind.REGRESSION) -> ProductSignal:
    return ProductSignal(
        signal_id="sig-p6",
        kind=kind,
        released_subject_uri="cas://artifact/v1",
        provenance_uri="cas://prov/v1",
        summary="checkout 500s under load",
        detail_uri="cas://telemetry/1",
    )


def test_exit_c4_feedback_reenters_via_maintenance_regate_and_releases(tmp_path: Path) -> None:
    """A production signal → a backlog item that RE-ENTERS via the maintenance
    RE-GATE path (no bypass), is re-verified change-proportionally, RE-PASSES the
    gates, and RE-ADMITS — a released maintained version.

    Mechanism: ``feedback_to_backlog`` routes EVERY signal to the maintenance route
    with ``re_gate_required`` (no side door); ``plan_maintenance`` re-runs only the
    touched scope (the rest REUSE the prior attested verdict); ``maintain`` re-runs
    the real gates + re-admits via the REAL admission controller. This is the
    roadmap "product-feedback + maintenance ... loop works".
    """
    # FEEDBACK: a production regression becomes a backlog item that re-enters via the
    # maintenance re-gate path — NEVER a side door (the closed loop).
    item = feedback_to_backlog(_feedback_signal(SignalKind.REGRESSION))
    assert item.route == MAINTENANCE_ROUTE
    assert item.re_gate_required is True
    assert item.reenters_via_regate()
    assert item.prior_subject_uri == "cas://artifact/v1"

    # MAINTENANCE: the backlog item drives a maintained change through the REAL seams.
    records = GlobalRecords()
    signer = ZoneBSigner(_KEY, _KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(_KEY, _KEY_ID, records)
    admission = AdmissionController(builder_id=_BUILDER_ID)

    old_dir = tmp_path / "old"
    old_files = _clean_files()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": "VALUE = 43\n"}  # a benign maintained tweak
    new_sha = _materialize(new_dir, new_files)
    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=_TOOLCHAIN, new_toolchain_digest=_TOOLCHAIN,
        old_scanner_config_rev=_SCANNER_REV, new_scanner_config_rev=_SCANNER_REV,
    )
    assert scope.changed_files == {"app.py"}

    plan = plan_maintenance(
        prior, scope,
        prior_subject_uri=item.prior_subject_uri,  # the maintenance-chain anchor
        gates=_GATES, runner=runner, new_state_sha=new_sha, scanner_config_rev=_SCANNER_REV,
    )
    # CHANGE-PROPORTIONAL: only the touched scope re-runs (no full re-freight).
    assert set(plan.rerun_gates) < set(_REQUIRED)
    assert {"spec", "audit"} <= set(plan.reuse_gates)

    build = _build_provenance(signer, tree=new_sha)
    outcome = maintain(
        plan, gates=_GATES, runner=runner, workdir=new_dir,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier, seq=_seq(),
    )
    # RE-PASSED the gates + RE-ADMITTED → released; the chain links to the prior.
    assert outcome.released
    assert outcome.admission_admitted
    assert outcome.prior_subject_uri == item.prior_subject_uri
    for g in outcome.gate_results:
        assert g.status is GateStatus.PASS
        assert g.runner_attestation is not None


def test_exit_c4_control_failing_maintained_change_is_not_released(tmp_path: Path) -> None:
    """CONTROL (no gate bypass): a maintained change that FAILS a gate is NOT
    released. The flip vs the criterion above: the SAME re-gate path, but a
    hardcoded secret trips the Security gate → not released, admission DENIES."""
    records = GlobalRecords()
    signer = ZoneBSigner(_KEY, _KEY_ID)
    runner = _runner(records)
    verifier = DevGateWriteVerifier(_KEY, _KEY_ID, records)
    admission = AdmissionController(builder_id=_BUILDER_ID)

    old_dir = tmp_path / "old"
    old_files = _clean_files()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": _SECRET_MODULE}  # trips Security
    new_sha = _materialize(new_dir, new_files)
    scope = changed_scope(
        _with_criteria(old_files), _with_criteria(new_files),
        old_state_sha=old_sha, new_state_sha=new_sha,
        old_toolchain_digest=_TOOLCHAIN, new_toolchain_digest=_TOOLCHAIN,
        old_scanner_config_rev=_SCANNER_REV, new_scanner_config_rev=_SCANNER_REV,
    )
    plan = plan_maintenance(
        prior, scope, prior_subject_uri="cas://artifact/v1",
        gates=_GATES, runner=runner, new_state_sha=new_sha, scanner_config_rev=_SCANNER_REV,
    )
    assert "security" in plan.rerun_gates

    build = _build_provenance(signer, tree=new_sha)
    outcome = maintain(
        plan, gates=_GATES, runner=runner, workdir=new_dir,
        new_subject_digest=build.result.subject_digest,
        provenance=build.provenance, build_verdict=build.signed_verdict,
        admission=admission, records=records, verifier=verifier, seq=_seq(),
    )
    sec = {g.name: g for g in outcome.gate_results}["security"]
    assert sec.status is GateStatus.FAIL
    assert not outcome.released  # FAIL-CLOSED: maintenance never bypasses the gates
    assert not outcome.admission_admitted


# --- provenance trace (tamper-evident) + provenance-backed licensing ----------


def _provenance_record(
    *,
    tamper_gate: bool = False,
    drop_gate: bool = False,
) -> ProvenanceRecord:
    """A complete, intact provenance record over a REAL evidence pack (+ optional
    tamper/drop controls that break the hash-chain)."""
    from foundry.durability.evidence_pack import build_evidence_pack, pack_evidence_hash
    from foundry.schemas.gates import GateResult, JudgmentProvenance, VerdictClass
    from foundry.schemas.governance import SignOffRecord
    from foundry.schemas.security import Material, SLSAProvenance

    run_against = "d" * 64
    seq = Seq(run_id=_RUN_ID, emission_epoch=1, superstep_index=5, node_id="release", local=0)

    def _gate(name: str) -> GateResult:
        return GateResult(
            name=name,
            status=GateStatus.PASS,
            decision_deterministic=True,
            execution_deterministic=True,
            verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
            runner_attestation="zoneB-sig",
            run_against=run_against,
            results_digest="r" * 64,
            seq=seq,
        )

    art = Artifact(kind="release", uri="cas://sha256/rc", produced_by="release", seq=seq)
    gate_results = (_gate("build"), _gate("test"))

    presented = build_evidence_pack(
        run_against=run_against, gate_results=gate_results,
        artifacts=(art,), sign_offs=(), seq=seq,
    )
    sign_off = SignOffRecord(
        subject_artifact_uri=art.uri,
        state_sha=run_against,
        approver="alice@org",
        role="release-manager",
        approved=True,
        server_nonce="nonce-1",
        presented_evidence_hash=pack_evidence_hash(presented),
        judgment_provenance=JudgmentProvenance.SELF,
        seq=seq,
    )
    pack = build_evidence_pack(
        run_against=run_against, gate_results=gate_results,
        artifacts=(art,), sign_offs=(sign_off,), seq=seq,
    )
    slsa = SLSAProvenance(
        build_type="dev-build", builder_id=_BUILDER_ID, invocation_uri="cas://invocation/1",
        source_uri="git://repo", source_digest=_SRC, tree_sha=run_against,
        subject_name="rc", subject_digest=_MAT_A,
        materials=(Material(uri="git://repo", digest="sha256:" + "b" * 64),), seq=seq,
    )

    recorded = gate_results
    if tamper_gate:
        recorded = tuple(
            g.model_copy(update={"status": GateStatus.FAIL}) if g.name == "build" else g
            for g in gate_results
        )
    elif drop_gate:
        recorded = gate_results[:1]

    return ProvenanceRecord(
        subject_artifact=art,
        run_id=_RUN_ID,
        run_against=run_against,
        evidence_pack=pack,
        gate_results=recorded,
        sign_offs=(sign_off,),
        slsa_provenance=slsa,
        seq=seq,
    )


def test_exit_c4_released_artifact_has_complete_tamper_evident_provenance() -> None:
    """The maintained release TRACES to a COMPLETE, tamper-evident provenance chain.

    Mechanism: ``trace`` walks the run + attested gate verdicts + evidence pack +
    sign-offs + SLSA predicate and re-derives the evidence-pack hash-chain; an
    intact record is ``complete`` + ``tamper_evident`` + ``trusted_as_released()``.
    """
    record = _provenance_record()
    chain = trace(record)
    assert chain.complete
    assert chain.tamper_evident
    assert chain.trusted_as_released()
    assert chain.all_gates_pass
    assert chain.flags == ()


@pytest.mark.parametrize("which", ["tamper", "drop"])
def test_exit_c4_control_tampered_or_dropped_link_breaks_the_trace(which: str) -> None:
    """CONTROL: a tampered OR dropped gate verdict breaks the hash-chain → the trace
    is no longer complete/tamper-evident, and the release is NOT trusted. The flip
    vs the criterion above: the same record, one link altered/removed."""
    record = _provenance_record(tamper_gate=(which == "tamper"), drop_gate=(which == "drop"))
    chain = trace(record)
    assert not chain.complete
    assert not chain.tamper_evident
    assert not chain.trusted_as_released()


def test_exit_c4_license_is_provenance_backed() -> None:
    """A released artifact's license is PROVENANCE-BACKED: a clean provenance chain
    → a clean license with a real liability anchor (the human sign-off).

    Mechanism: ``license_for`` folds the artifact's ``ProvenanceRecord`` through the
    SAME ``trace`` and issues a clean license IFF the chain is complete.
    """
    record = _provenance_record()
    art = record.subject_artifact
    lic = license_for(art, record, provenance_uri="cas://prov/rc")
    assert lic.clean is True
    assert lic.is_clean()
    assert lic.license_id  # a real SPDX id, not ""
    assert lic.accountable_party == "alice@org"  # the liability anchor (human sign-off)
    assert lic.ai_generated is True
    assert lic.flags == ()


def test_exit_c4_control_no_provenance_means_no_clean_license() -> None:
    """CONTROL: an artifact with NO provenance gets NO clean license (fail-closed).
    You cannot license code whose origin you cannot prove. ``strict`` raises."""
    record = _provenance_record()
    art = record.subject_artifact

    # No provenance → not clean, no license id, the ground named.
    none_lic = license_for(art, None)
    assert none_lic.clean is False
    assert none_lic.license_id == ""
    assert "no_provenance" in none_lic.flags

    # And a BROKEN provenance (tampered chain) is not clean either — the underlying
    # provenance flags are carried through legibly.
    broken = _provenance_record(tamper_gate=True)
    broken_lic = license_for(broken.subject_artifact, broken)
    assert broken_lic.clean is False
    assert broken_lic.flags  # provenance grounds named

    # strict=True fails closed by RAISING rather than returning a non-clean record.
    with pytest.raises(UnlicensableArtifactError):
        license_for(art, None, strict=True)


# ===========================================================================
# CRITERION 5 (bonus) — Red-team all-defended (weakened control flips).
# ===========================================================================


def test_exit_c5_red_team_all_probes_defended_on_real_system() -> None:
    """Every red-team probe HOLDS on the REAL, unmodified defenses.

    Mechanism: ``run_red_team()`` runs the seven adversarial probes (prompt-
    injection, gate-evasion, forged-results, forged-verdict, fork-divergence,
    reward-hacking, eval-lie) against the REAL shipped defenses and reports each
    DEFENDED + conclusive, with no critical findings.
    """
    report = RedTeamHarness().run_red_team()
    assert report.all_defended
    assert report.critical_findings == ()
    assert len(report.probes) == 7
    for p in report.probes:
        assert p.defended, f"{p.name}: {p.detail}"
        assert p.conclusive
        assert not p.critical_finding


def test_exit_c5_control_weakened_defenses_report_undefended() -> None:
    """CONTROL (non-vacuity): the SAME suite wired to DELIBERATELY-WEAKENED defenses
    reports UNDEFENDED on every probe — proving the DEFENDED verdict tracks the real
    defense, not an always-green constant."""
    report = RedTeamHarness().run_red_team(weaken=True)
    assert not report.all_defended
    assert len(report.critical_findings) == 7
    for p in report.probes:
        assert not p.defended, f"{p.name} should be UNDEFENDED when weakened"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
