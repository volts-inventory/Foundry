"""Phase-1.5 EXIT integration test — the WHOLE decision gate, end-to-end (T-M15-11).

This module is the milestone witness for the docs/roadmap.md **Phase 1.5 EXIT**
criteria: it drives the entire decision-gate methodology end-to-end on a synthetic
:class:`~foundry.evaluation.task_fixtures.TaskFrame` and asserts the methodology is
both COHERENT and HONEST. It wires together every shipped decision-gate module:

    generate_synthetic_frame
        -> run_pilot           (variance/base-rate, DISJOINT pilot slice, read-only)
        -> compute_power_rung  (a FUNDED rung off the pilot)
        -> freeze_preregistration (coherent hypothesis+rung; ONE alpha look)
        -> run_ab              (scored slice only; cites the frozen prereg; ITT)
        -> decide              (the §2.5 branch point -> a DecisionClass)
        -> run_ablation        (do roles earn their place?)
        -> build_scorecard     (render() / honesty_banner() — the dry-run banner)

HONESTY (load-bearing): this is a METHODOLOGY DRY-RUN on synthetic fixtures with
SIMULATED baseline/ablation arms. The test ASSERTS the dry-run/synthetic banner so
the harness can NEVER be mistaken for real Foundry-vs-baseline evidence (criterion
7). The honest audit map of which exit criteria are proven end-to-end vs which are
simulated stand-ins lives in build/LEARNINGS.md (EV35).

The FOUNDRY arm of :mod:`foundry.evaluation.harness` wraps the SHIPPED pipeline
(``foundry.cli.run`` keyless via the test doubles) — genuinely heavy — so the
full-pipeline exercises keep the frames TINY (small ``n_tasks`` + ``k_repeats=1``).
"""

from __future__ import annotations

import pytest

from foundry.evaluation.ab_runner import (
    AB_ARMS,
    ABResult,
    ArmReport,
    BootstrapComparison,
    ScoredSliceContaminationError,
    SecondaryComparison,
    run_ab,
)
from foundry.evaluation.ablation import run_ablation
from foundry.evaluation.decision_table import DecisionRecord, decide
from foundry.evaluation.hypothesis import (
    Arm,
    CentralHypothesis,
    DecisionClass,
    Provenance,
)
from foundry.evaluation.pilot import PilotResult, run_pilot
from foundry.evaluation.power import FundedPowerRung, compute_power_rung, is_funded
from foundry.evaluation.preregistration import (
    AlphaAlreadyDrawnError,
    PreRegistration,
    TamperedPreRegistrationError,
    freeze_preregistration,
)
from foundry.evaluation.scorecard import Scorecard, build_scorecard
from foundry.evaluation.task_fixtures import (
    TaskFrame,
    generate_synthetic_frame,
)
from foundry.state.global_records import GlobalRecords

# Pre-committed (BEFORE the scored run) decision parameters (§2 BLOCKER).
_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
# A small per-task-repeat cost + a big budget so the power calc returns a FUNDED
# rung for the exercise (the rung's n is large; the cost stays affordable).
_REPEAT_COST = 1_000  # micro-USD per task-repeat per arm
_BIG_BUDGET = 10_000_000_000  # 10k USD — funds the rung; never binds


# ---------------------------------------------------------------------------
# Shared end-to-end driver: the WHOLE gate on a synthetic frame.
# ---------------------------------------------------------------------------


def _sizing_pilot(live: PilotResult) -> PilotResult:
    """Return a NON-DEGENERATE pilot summary the power calc can size a funded rung off.

    HONEST MAP (load-bearing): the LIVE pilot (``run_pilot``) genuinely runs the
    variance/base-rate stage end-to-end on the synthetic frame and IS asserted (its
    provenance/disjointness/read-only contract). But on a TINY dry-run frame the
    SIMULATED arms saturate (e.g. ``control_base_rate == 1.0``, zero between-task
    variance), which makes ``compute_power_rung`` size a degenerate ``required_n == 0``
    rung — an artifact of the synthetic frame, not the methodology. So for the power
    calc we substitute a representative pilot summary (the same non-degenerate values
    every shipped power/prereg test uses) carrying the LIVE pilot's bound
    ``eval_set_identity``. The power-sizing arithmetic is genuinely exercised; only
    the variance/base-rate *inputs* are the representative dry-run stand-in.
    """
    return live.model_copy(
        update={
            "control_base_rate": 0.2,
            "effect_size_variance": 0.01,
            "control_resolution_variance": 0.01,
        }
    )


def _funded_rung(pilot: PilotResult) -> FundedPowerRung:
    """Size the FUNDED rung off the pilot via the REAL power calc (never by hand)."""
    return compute_power_rung(
        _sizing_pilot(pilot),
        mde=_MDE,
        alpha=_ALPHA,
        target_power=_TARGET_POWER,
        budget_usd_microusd=_BIG_BUDGET,
        per_task_repeat_cost_microusd=_REPEAT_COST,
    )


def _coherent_scored_prereg(
    frame: TaskFrame, rung: FundedPowerRung, ledger: GlobalRecords, *, decision_id: str
) -> PreRegistration:
    """Freeze a coherent SCORED prereg bound to ``frame``'s scored slice.

    The embedded hypothesis pins the SAME ``mde``/``alpha``/``funded_power_rung`` as
    the rung being frozen (the §2/§8 coherence guard), and is bound to the SCORED
    slice identity (EV7) so ``run_ab`` accepts it.
    """
    hyp = CentralHypothesis(
        mde=_MDE,
        funded_power_rung=rung,
        provenance=Provenance.SCORED,
        dry_run=False,
    )
    return freeze_preregistration(
        hyp,
        rung,
        frame.scored_slice.identity,
        decision_id=decision_id,
        ledger=ledger,
    )


def _drive_full_gate(
    frame: TaskFrame, *, decision_id: str = "phase1.5-exit"
) -> tuple[PilotResult, FundedPowerRung, PreRegistration, ABResult, DecisionRecord, Scorecard]:
    """Run the WHOLE Phase-1.5 decision gate end-to-end on ``frame``.

    Returns every intermediate artifact so the criterion tests can assert on each
    stage. Uses a fresh :class:`GlobalRecords` ledger so the alpha look starts unspent.
    """
    # 1. PILOT FIRST — on the DISJOINT pilot slice (default arms include FOUNDRY,
    #    which runs the SHIPPED pipeline keyless; k_repeats=1 keeps it cheap).
    pilot = run_pilot(frame, k_repeats=1)

    # 2. POWER CALC — a FUNDED rung off the read-only pilot summary.
    rung = _funded_rung(pilot)
    assert rung.funded and not rung.underpowered, "the exercise needs a FUNDED rung"
    assert is_funded(rung, _BIG_BUDGET)

    # 3. PRE-REGISTRATION — coherent hypothesis+rung; records exactly ONE alpha look.
    ledger = GlobalRecords()
    prereg = _coherent_scored_prereg(frame, rung, ledger, decision_id=decision_id)
    assert ledger.alpha_looks_spent(frame.scored_slice.identity, decision_id) == 1

    # 4. A/B RUN — scored slice ONLY, citing the frozen prereg, ITT.
    ab = run_ab(frame, prereg)

    # 5. DECISION — the §2.5 branch point.
    decision = decide(ab, prereg)

    # 6. ABLATION — do roles earn their place?
    ablation = run_ablation(frame, seed=0, k_repeats=1)

    # 7. SCORECARD — presentation + honesty banner.
    scorecard = build_scorecard(ab, decision, ablation=ablation, prereg=prereg)
    return pilot, rung, prereg, ab, decision, scorecard


# A tiny frame for the heavy full-pipeline runs (FOUNDRY arm = the real graph).
def _tiny_frame(*, n_tasks: int = 4, seed: int = 1, pilot_fraction: float = 0.5) -> TaskFrame:
    return generate_synthetic_frame(n_tasks=n_tasks, seed=seed, pilot_fraction=pilot_fraction)


# ---------------------------------------------------------------------------
# Criterion 1 — the FULL pipeline runs end-to-end and produces a DecisionClass.
# ---------------------------------------------------------------------------


def test_exit_1_full_pipeline_completes_and_produces_a_decision_class() -> None:
    """Generate -> pilot -> power -> prereg -> A/B -> decide -> ablation -> scorecard.

    The whole gate completes on a synthetic frame and yields a DecisionClass, with
    every intermediate artifact carrying its expected shape.
    """
    frame = _tiny_frame()
    pilot, rung, prereg, ab, decision, scorecard = _drive_full_gate(frame)

    # Pilot is the read-only variance/base-rate summary (NOT a scored arm).
    assert pilot.provenance is Provenance.PILOT and pilot.scored is False
    assert 0.0 <= pilot.control_base_rate <= 1.0

    # Funded rung carries a concrete (n, k) allocation at the COMMITTED mde/alpha.
    assert rung.mde == _MDE and rung.alpha == _ALPHA
    assert rung.n_tasks is not None and rung.k_repeats is not None

    # The frozen manifest binds the scored slice + decision.
    assert prereg.scored_eval_set_identity == frame.scored_slice.identity
    assert prereg.registration_identity()

    # The A/B reports ALL THREE pre-registered arms over the scored slice.
    assert {r.arm for r in ab.arm_reports} == set(AB_ARMS)
    assert ab.registration_identity == prereg.registration_identity()

    # The gate produced a real DecisionClass.
    assert isinstance(decision.decision, DecisionClass)
    assert decision.decision in set(DecisionClass)

    # The scorecard presents that verdict verbatim.
    assert scorecard.decision is decision.decision
    assert isinstance(scorecard.render(), str) and scorecard.render()


# ---------------------------------------------------------------------------
# Criterion 2 — pilot-first + DISJOINT + read-only.
# ---------------------------------------------------------------------------


def test_exit_2_pilot_slice_is_disjoint_from_scored_slice() -> None:
    """The pilot slice and the scored A/B slice share NO task (drawn from one frame)."""
    frame = _tiny_frame(n_tasks=12, seed=2, pilot_fraction=0.4)
    pilot_ids = set(frame.pilot_slice.task_ids)
    scored_ids = set(frame.scored_slice.task_ids)
    assert pilot_ids and scored_ids
    assert pilot_ids & scored_ids == set()  # DISJOINT
    # Exhaustive partition of one frame permutation (base rate + variance transfer).
    assert pilot_ids | scored_ids == {f.task_id for f in frame.fixtures}


def test_exit_2_pilot_is_read_only_rerun_cannot_redraw_the_scored_partition() -> None:
    """Re-running the pilot does NOT alter the scored partition (it only re-estimates).

    The partition is a deterministic seed-fixed prefix-cut of one frame permutation,
    so the scored slice identity is UNCHANGED across pilot runs (the pilot is
    read-only into the power calc — it never selects scored tasks).
    """
    frame = _tiny_frame(n_tasks=12, seed=7, pilot_fraction=0.4)
    scored_before = frame.scored_slice.identity

    p1 = run_pilot(frame, k_repeats=1)
    after_first = frame.scored_slice.identity
    p2 = run_pilot(frame, k_repeats=1)
    after_second = frame.scored_slice.identity

    # The scored partition identity never moves; the pilot is deterministic + read-only.
    assert scored_before == after_first == after_second
    assert p1 == p2  # same (frame, arms, k, seed, budget) -> identical PilotResult
    # The pilot summary is bound to the PILOT slice, not the scored one.
    assert p1.eval_set_identity == frame.pilot_slice.identity
    assert p1.eval_set_identity != frame.scored_slice.identity


# ---------------------------------------------------------------------------
# Criterion 3 — alpha drawn EXACTLY ONCE (a second freeze of the same pair raises).
# ---------------------------------------------------------------------------


def test_exit_3_alpha_drawn_exactly_once_second_freeze_of_same_pair_raises() -> None:
    """After freezing, the (eval_set_identity, decision_id) look is SPENT; re-freeze raises.

    A fork/retrain re-presenting the SAME scored slice + decision cannot re-draw the
    alpha budget (the ledger is keyed on the PAIR, NOT state_sha) — fail closed.
    """
    frame = _tiny_frame(n_tasks=8, seed=3, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)
    ledger = GlobalRecords()
    decision_id = "dec-exit-3"

    assert ledger.alpha_looks_spent(frame.scored_slice.identity, decision_id) == 0
    _coherent_scored_prereg(frame, rung, ledger, decision_id=decision_id)
    # EXACTLY one look spent.
    assert ledger.alpha_looks_spent(frame.scored_slice.identity, decision_id) == 1

    # A second freeze of the SAME pair against the SAME ledger raises (no re-draw).
    with pytest.raises(AlphaAlreadyDrawnError):
        _coherent_scored_prereg(frame, rung, ledger, decision_id=decision_id)
    # Still exactly one look — the rejected freeze never appended a second.
    assert ledger.alpha_looks_spent(frame.scored_slice.identity, decision_id) == 1

    # A DIFFERENT decision_id draws its OWN budget (allowed).
    _coherent_scored_prereg(frame, rung, ledger, decision_id="dec-exit-3-other")
    assert ledger.alpha_looks_spent(frame.scored_slice.identity, "dec-exit-3-other") == 1


# ---------------------------------------------------------------------------
# Criterion 4 — the decision CITES the frozen prereg (tampering makes it raise).
# ---------------------------------------------------------------------------


def _tamper(prereg: PreRegistration) -> PreRegistration:
    """Reconstruct a manifest with an edited top-level MDE mirror (a tamper vector).

    A frozen model can't be mutated in place; the practical tamper is rebuilding the
    manifest with a divergent mde/alpha mirror and presenting it as the original.
    ``model_construct`` skips validation so the diverged mirror survives construction
    — exactly the post-hoc edit ``assert_unmodified`` must catch.
    """
    return prereg.model_construct(**{**prereg.__dict__, "mde": prereg.mde + 0.05})


def test_exit_4_run_ab_rejects_a_tampered_prereg() -> None:
    """``run_ab`` fails closed on a tampered manifest BEFORE any arm runs."""
    frame = _tiny_frame(n_tasks=8, seed=4, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)
    prereg = _coherent_scored_prereg(
        frame, rung, GlobalRecords(), decision_id="dec-exit-4a"
    )

    tampered = _tamper(prereg)
    with pytest.raises(TamperedPreRegistrationError):
        run_ab(frame, tampered)


def test_exit_4_decide_rejects_a_tampered_prereg() -> None:
    """``decide`` fails closed on a tampered manifest BEFORE any verdict is computed."""
    frame = _tiny_frame(n_tasks=8, seed=5, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)
    prereg = _coherent_scored_prereg(
        frame, rung, GlobalRecords(), decision_id="dec-exit-4b"
    )
    ab = run_ab(frame, prereg)  # a clean A/B against the UNMODIFIED manifest

    tampered = _tamper(prereg)
    with pytest.raises(TamperedPreRegistrationError):
        decide(ab, tampered)


def test_exit_4_scored_slice_must_match_the_cited_manifest() -> None:
    """A prereg bound to a DIFFERENT frame's scored slice is rejected by ``run_ab``.

    The manifest binds the run to a specific frame/partition (EV7); citing it against
    a frame whose scored slice identity differs fails closed.
    """
    frame_a = _tiny_frame(n_tasks=8, seed=6, pilot_fraction=0.5)
    frame_b = _tiny_frame(n_tasks=8, seed=7, pilot_fraction=0.5)
    assert frame_a.scored_slice.identity != frame_b.scored_slice.identity

    pilot = run_pilot(frame_a, k_repeats=1)
    rung = _funded_rung(pilot)
    # Prereg bound to frame_a's scored slice, but run against frame_b.
    prereg = _coherent_scored_prereg(
        frame_a, rung, GlobalRecords(), decision_id="dec-exit-4c"
    )
    with pytest.raises(ScoredSliceContaminationError):
        run_ab(frame_b, prereg)


# ---------------------------------------------------------------------------
# Criterion 5 — ITT denominator is UNCONDITIONAL (attempted == n*k, never shrunk).
# ---------------------------------------------------------------------------


def test_exit_5_scorecard_itt_denominator_is_unconditional_attempted() -> None:
    """Each arm's scorecard ITT denominator == attempted (n_scored_tasks * k), not shrunk."""
    frame = _tiny_frame(n_tasks=6, seed=8, pilot_fraction=0.5)
    _pilot, rung, _prereg, ab, _decision, scorecard = _drive_full_gate(
        frame, decision_id="dec-exit-5"
    )

    n_scored = len(frame.scored_slice.task_ids)
    k = rung.k_repeats
    assert k is not None
    expected_attempted = n_scored * k

    for arm in AB_ARMS:
        arm_score = scorecard.arm_score(arm)
        ab_report = ab.report(arm)
        # The denominator is the UNCONDITIONAL attempted count (all initiated tasks),
        # copied verbatim from the ArmReport — never conditioned on completion.
        assert arm_score.attempted == expected_attempted
        assert arm_score.attempted == ab_report.attempted
        # The rate is resolved / attempted (the ITT ratio), never resolved / completed.
        assert arm_score.resolved_count <= arm_score.attempted
        assert arm_score.itt_pass_rate == pytest.approx(
            arm_score.resolved_count / arm_score.attempted
        )
        assert arm_score.itt_pass_rate == ab_report.itt_pass_rate


# ---------------------------------------------------------------------------
# Criterion 6 — BOTH branch outcomes reachable (PROCEED and HARD_KILL).
# ---------------------------------------------------------------------------


def _ab_with_known_primary(
    prereg: PreRegistration,
    *,
    significant: bool,
    meets_mde: bool,
    point: float,
    foundry_cost: int,
    baseline_cost: int,
) -> ABResult:
    """A hand-built ABResult citing ``prereg`` with the exact primary bits + costs.

    This drives the gate's REAL branch logic (``decide``) with a controlled scored
    read — the only deterministic way to exercise the PROCEED branch, because the
    harness's SIMULATED FOUNDRY arm always costs MORE than the baseline stand-in
    (1.2M vs 0.8M micro-USD/attempt), so the equal-USD cost test — part of the
    PROCEED predicate — can never hold on a naturally-run dry-run frame. The
    ``registration_identity`` is the cited manifest's, so ``decide``'s
    assert_unmodified gate still passes.
    """
    primary = BootstrapComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=point,
        ci_low=point - 0.05,
        ci_high=point + 0.05,
        alpha=prereg.alpha,
        mde=prereg.mde,
        n_tasks=8,
        resamples=2000,
        significant_at_alpha=significant,
        meets_mde=meets_mde,
    )
    secondary = SecondaryComparison(
        name="foundry_vs_baseline_efficiency",
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=0.0,
        raw_p=0.5,
        holm_adjusted_p=1.0,
        significant=False,
    )

    def _report(arm: Arm, *, rate: float, cost: int, simulated: bool) -> ArmReport:
        attempted = 10
        resolved = round(rate * attempted)
        return ArmReport(
            arm=arm,
            itt_pass_rate=resolved / attempted,
            resolved_count=resolved,
            attempted=attempted,
            total_cost_microusd=cost,
            simulated=simulated,
        )

    return ABResult(
        registration_identity=prereg.registration_identity(),
        scored_eval_set_identity=prereg.scored_eval_set_identity,
        seed=0,
        k_repeats=1,
        n_tasks=8,
        arm_reports=(
            _report(Arm.FOUNDRY, rate=0.6, cost=foundry_cost, simulated=False),
            _report(Arm.BASELINE_SINGLE_AGENT, rate=0.4, cost=baseline_cost, simulated=True),
            _report(Arm.SINGLE_AGENT_PLUS_GATES, rate=0.45, cost=baseline_cost, simulated=True),
        ),
        primary=primary,
        secondaries=(secondary,),
        provenance=prereg.hypothesis.provenance,
        dry_run=prereg.hypothesis.provenance is not Provenance.SCORED,
    )


def test_exit_6_proceed_branch_is_reachable() -> None:
    """A significant, meets-MDE, cost<=baseline primary routes the gate to PROCEED.

    Proves the gate is a REAL branch (not hard-wired to a kill): with the equal-USD
    frame satisfied (FOUNDRY cost <= BASELINE) the §2.5 row-1 predicate fires.
    """
    frame = _tiny_frame(n_tasks=8, seed=9, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)
    prereg = _coherent_scored_prereg(
        frame, rung, GlobalRecords(), decision_id="dec-exit-6-proceed"
    )

    ab = _ab_with_known_primary(
        prereg,
        significant=True,
        meets_mde=True,
        point=_MDE + 0.05,  # >= committed MDE
        foundry_cost=800_000,  # FOUNDRY <= BASELINE -> equal-USD frame holds
        baseline_cost=900_000,
    )
    decision = decide(ab, prereg)
    assert decision.decision is DecisionClass.PROCEED
    assert decision.primary_significant_at_alpha
    assert decision.primary_meets_mde
    assert decision.cost.at_most_equal

    # The scorecard renders the PROCEED verbatim.
    scorecard = build_scorecard(ab, decision)
    assert scorecard.decision is DecisionClass.PROCEED
    assert "PROCEED" in scorecard.render()


def test_exit_6_hard_kill_branch_is_reachable_via_the_real_pipeline() -> None:
    """The genuinely-run full pipeline yields HARD_KILL on the dry-run frame.

    The SIMULATED FOUNDRY arm costs MORE than the baseline stand-in, so the
    equal-USD cost test fails and the USD-primary is a null at the funded MDE with
    no supplied positive token reading -> the conservative HARD_KILL (§2.5 row 2).
    Together with the PROCEED test this proves the gate is a real branch.
    """
    frame = _tiny_frame(n_tasks=6, seed=10, pilot_fraction=0.5)
    _pilot, _rung, _prereg, _ab, decision, scorecard = _drive_full_gate(
        frame, decision_id="dec-exit-6-kill"
    )

    assert decision.decision is DecisionClass.HARD_KILL
    # The kill is terminal: no re-test / re-power field is exposed on the record.
    for forbidden in ("repower", "re_power", "re_test", "retest"):
        assert forbidden not in DecisionRecord.model_fields
    # FOUNDRY genuinely costs more than BASELINE here (equal-USD frame violated),
    # OR the primary is otherwise null — either way it is NOT a PROCEED.
    assert not (
        decision.primary_significant_at_alpha
        and decision.primary_meets_mde
        and decision.cost.at_most_equal
    )
    # The scorecard renders the KILL verbatim (no proceed-spin).
    assert "HARD_KILL" in scorecard.render()


def test_exit_6_both_verdicts_are_distinct_decision_classes() -> None:
    """PROCEED and HARD_KILL are distinct verdicts of the SAME gate logic."""
    frame = _tiny_frame(n_tasks=8, seed=11, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)

    # PROCEED via the controlled scored read.
    prereg_p = _coherent_scored_prereg(
        frame, rung, GlobalRecords(), decision_id="dec-6-both-p"
    )
    proceed = decide(
        _ab_with_known_primary(
            prereg_p,
            significant=True,
            meets_mde=True,
            point=_MDE + 0.05,
            foundry_cost=700_000,
            baseline_cost=800_000,
        ),
        prereg_p,
    )

    # HARD_KILL via a USD-null (cost-worse win) — the SAME gate logic.
    prereg_k = _coherent_scored_prereg(
        frame, rung, GlobalRecords(), decision_id="dec-6-both-k"
    )
    hard_kill = decide(
        _ab_with_known_primary(
            prereg_k,
            significant=True,
            meets_mde=True,
            point=_MDE + 0.05,
            foundry_cost=2_000_000,  # FOUNDRY > BASELINE -> cost-worse -> NOT proceed
            baseline_cost=800_000,
        ),
        prereg_k,
    )

    assert proceed.decision is DecisionClass.PROCEED
    assert hard_kill.decision is DecisionClass.HARD_KILL
    assert proceed.decision is not hard_kill.decision


# ---------------------------------------------------------------------------
# Criterion 7 — HONESTY: the dry-run / synthetic / simulated-arms banner.
# ---------------------------------------------------------------------------


def test_exit_7_scorecard_banner_discloses_dry_run_synthetic_simulated() -> None:
    """``honesty_banner()`` + ``render()`` disclose DRY-RUN / synthetic / simulated arms.

    A SCORED-provenance prereg still flows DRY_RUN==False through to the scorecard,
    but the banner is non-empty either way and the rendered scorecard prints it
    FIRST so the exercise can NEVER be read as real evidence.
    """
    # A pure DRY-RUN scorecard (default provenance) — the methodology rehearsal.
    frame = _tiny_frame(n_tasks=4, seed=12, pilot_fraction=0.5)
    pilot = run_pilot(frame, k_repeats=1)
    rung = _funded_rung(pilot)
    # Build a DRY_RUN hypothesis/prereg (the default provenance — a rehearsal).
    hyp = CentralHypothesis(mde=_MDE, funded_power_rung=rung)  # provenance=DRY_RUN
    prereg = freeze_preregistration(
        hyp,
        rung,
        frame.scored_slice.identity,
        decision_id="dec-exit-7",
        ledger=GlobalRecords(),
    )
    ab = run_ab(frame, prereg)
    decision = decide(ab, prereg)
    ablation = run_ablation(frame, seed=0, k_repeats=1)
    scorecard = build_scorecard(ab, decision, ablation=ablation)

    assert scorecard.dry_run is True
    assert scorecard.provenance is Provenance.DRY_RUN

    banner = scorecard.honesty_banner()
    assert banner  # non-empty (mandatory)
    upper = banner.upper()
    assert "DRY-RUN" in upper
    assert "SYNTHETIC" in upper
    assert "SIMULATED" in upper
    # The disclosure says it asserts NOTHING about real evidence.
    assert "NOT" in upper and "REAL" in upper

    rendered = scorecard.render()
    # The banner is the FIRST line — a reader cannot miss it.
    assert rendered.split("\n", 1)[0] == banner
    # The ablation summary is flagged simulated too (a contribution model).
    assert scorecard.ablation_simulated is True


def test_exit_7_banner_non_empty_for_scored_provenance_too() -> None:
    """Even a SCORED-provenance scorecard renders a mandatory, non-empty banner FIRST.

    The full-gate driver uses a SCORED-provenance prereg; its banner is the SCORED
    confirmatory disclosure (oracle-scored primary; no self-judge as independent
    validation; unconditional ITT). It is NEVER empty, and is still rendered first.
    """
    frame = _tiny_frame(n_tasks=4, seed=13, pilot_fraction=0.5)
    _pilot, _rung, _prereg, _ab, _decision, scorecard = _drive_full_gate(
        frame, decision_id="dec-exit-7-scored"
    )
    assert scorecard.dry_run is False
    assert scorecard.provenance is Provenance.SCORED

    banner = scorecard.honesty_banner()
    assert banner  # non-empty either way (mandatory)
    upper = banner.upper()
    # The SCORED banner still discloses the oracle-scored / no-self-judge posture
    # and the unconditional ITT denominator.
    assert "ORACLE-SCORED" in upper
    assert "UNCONDITIONAL" in upper
    # Rendered FIRST.
    assert scorecard.render().split("\n", 1)[0] == banner
