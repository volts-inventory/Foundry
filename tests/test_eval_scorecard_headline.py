"""Conformance tests for the FULL headline scorecard (T-M6-07).

Contract (docs/evaluation.md §7; ``build_headline_scorecard`` PRESENTS the FULL
published headline — it recomputes no statistic except the Holm adjustment over
the supplied secondary family, which reuses the ab_runner helper):

* ONE confirmatory PRIMARY, labeled 1.5-arbiter (SPENT alpha) + 6-replication
  (ESTIMATION only — NO alpha; a disconfirmation is an investigation trigger, NOT
  a second gate — §6.5).
* SECONDARIES under Holm–Bonferroni: a secondary that is raw-significant but
  Holm-NON-significant is reported NOT significant, and a secondary is NEVER
  promoted to a primary claim (``is_primary`` structurally False).
* The per-feature wall-clock + cost line (off the §8 CostBreakdown).
* UNCONDITIONAL per-arm ITT (denominator = attempted, never shrunk).
* PUBLISHED + STABLE: ``stable_digest()`` is identical across two builds from the
  SAME inputs.
* FAIL-CLOSED: a NON-VALID FactoryEvalResult refuses to publish
  (:class:`NotPublishableError`).
* The honesty banner is present, non-empty, and FIRST in ``render()``.
* The existing M1.5 ``build_scorecard`` stays green (backward-compatible).

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.agent_validity import AgentValidityReport, RoleValidity, ValidityRole
from foundry.evaluation.decision_table import DecisionRecord, decide
from foundry.evaluation.factory_eval import FactoryEvalResult, run_factory_eval
from foundry.evaluation.hypothesis import (
    Arm,
    CentralHypothesis,
    Provenance,
)
from foundry.evaluation.integrity import AgentRuntimeSurface
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import PreRegistration, freeze_preregistration
from foundry.evaluation.replication_ab import ReplicationResult, run_replication_ab
from foundry.evaluation.scorecard import (
    HeadlinePrimary,
    HeadlineScorecard,
    HeadlineSecondary,
    NotPublishableError,
    SecondaryReading,
    build_headline_scorecard,
)
from foundry.evaluation.task_fixtures import TaskFrame, generate_synthetic_frame
from foundry.state.global_records import GlobalRecords

_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000
_DECISION_ID = "dec-phase15"


# ---------------------------------------------------------------------------
# Builders — a frame, a frozen Phase-1.5 prereg, a valid FactoryEvalResult, a
# real Phase-1.5 DecisionRecord, and a Phase-6 ReplicationResult.
# ---------------------------------------------------------------------------


def _frame() -> TaskFrame:
    # Small frame keeps the FOUNDRY-arm pipeline runs cheap + deterministic.
    return generate_synthetic_frame(n_tasks=4, seed=0, pilot_fraction=0.5)


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


def _phase15_decision(prereg: PreRegistration) -> DecisionRecord:
    """A REAL Phase-1.5 DecisionRecord via ``decide`` over a known-winning read."""
    from foundry.evaluation.ab_runner import (
        ABResult,
        ArmReport,
        BootstrapComparison,
        SecondaryComparison,
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

    primary = BootstrapComparison(
        treatment_arm=Arm.FOUNDRY,
        control_arm=Arm.BASELINE_SINGLE_AGENT,
        point_estimate=0.3,
        ci_low=0.1,
        ci_high=0.5,
        alpha=_ALPHA,
        mde=_MDE,
        n_tasks=8,
        resamples=2000,
        significant_at_alpha=True,
        meets_mde=True,
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
    ab = ABResult(
        registration_identity=prereg.content_identity(),
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
    return decide(ab, prereg)


def _replication(
    frame: TaskFrame, prereg: PreRegistration, decision: DecisionRecord
) -> ReplicationResult:
    return run_replication_ab(frame, prereg, decision, seed=0)


# Three secondaries (alpha=0.05, m=3). Step-down Holm sorts ascending and rejects
# the run of smallest p's until the FIRST failure:
#   rank0  p=0.001  thr=alpha/3=0.0167  -> reject (Holm-significant)
#   rank1  p=0.04   thr=alpha/2=0.025   -> 0.04 > 0.025 FAIL -> stop
#   rank2  p=0.04   -> not rejected
# So the two 0.04 endpoints are RAW-significant (0.04 < alpha=0.05) but
# Holm-NON-significant -> reported NOT significant. This is the whole point of the
# family-wise correction, and it CANNOT happen with only m<=2 endpoints (the
# largest endpoint's Holm threshold is exactly alpha).
def _secondaries() -> tuple[SecondaryReading, ...]:
    return (
        SecondaryReading(name="novel_suite_resolution", point_estimate=0.2, raw_p=0.001),
        SecondaryReading(name="hidden_test_pass_rate", point_estimate=0.05, raw_p=0.04),
        SecondaryReading(name="handoff_fidelity", point_estimate=0.03, raw_p=0.04),
    )


# ---------------------------------------------------------------------------
# A valid eval + reproduced replication -> a published headline scorecard
# ---------------------------------------------------------------------------


def test_published_headline_one_primary_labeled_1_5_and_6() -> None:
    frame = _frame()
    ledger = GlobalRecords()
    prereg = _frozen_prereg(frame, ledger=ledger)
    decision = _phase15_decision(prereg)
    replication = _replication(frame, prereg, decision)
    eval_result = _clean_eval(frame)

    card = build_headline_scorecard(
        eval_result, replication=replication, decision=decision, secondaries=_secondaries()
    )
    assert isinstance(card, HeadlineScorecard)
    assert card.publishable is True

    # ONE confirmatory primary, off the Phase-1.5 arbiter that SPENT alpha.
    p = card.primary
    assert isinstance(p, HeadlinePrimary)
    assert p.treatment_arm is Arm.FOUNDRY
    assert p.control_arm is Arm.BASELINE_SINGLE_AGENT
    assert p.decision is decision.decision
    assert p.effect == decision.observed_effect
    assert p.significant_at_alpha == decision.primary_significant_at_alpha
    assert p.meets_mde == decision.primary_meets_mde
    assert p.cost_at_most_equal == decision.cost.at_most_equal
    assert p.oracle_scored is True
    assert p.alpha_source == "phase_1.5_arbiter"  # the run that SPENT alpha

    # The Phase-6 replication is the ESTIMATION overlay (no alpha) — labeled.
    assert p.replication_present is True
    assert p.replication_effect == replication.point_estimate
    assert p.replication_reproduces == replication.reproduces
    assert p.estimation_source == "phase_6_replication"

    # render() labels BOTH reads distinctly.
    rendered = card.render()
    assert "1.5 arbiter" in rendered
    assert "SPENT alpha" in rendered
    assert "6 replication" in rendered
    assert "ESTIMATION only" in rendered


def test_secondaries_holm_adjusted_raw_significant_but_not_holm_significant() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    eval_result = _clean_eval(frame)

    card = build_headline_scorecard(eval_result, decision=decision, secondaries=_secondaries())
    by_name = {s.name: s for s in card.secondaries}

    strong = by_name["novel_suite_resolution"]
    weak = by_name["hidden_test_pass_rate"]

    # The strong endpoint clears Holm.
    assert strong.raw_significant is True
    assert strong.significant is True

    # The weak endpoint is RAW-significant (0.03 < 0.05) but Holm-NON-significant
    # -> reported NOT significant (the whole point of the family-wise correction).
    assert weak.raw_p < _ALPHA
    assert weak.raw_significant is True
    assert weak.significant is False
    assert weak.holm_adjusted_p >= _ALPHA

    # A secondary is NEVER promoted to a primary claim.
    for s in card.secondaries:
        assert isinstance(s, HeadlineSecondary)
        assert s.is_primary is False


def test_secondary_can_never_be_constructed_as_a_primary_claim() -> None:
    with pytest.raises(ValueError, match="NEVER promoted to a primary"):
        HeadlineSecondary(
            name="x",
            point_estimate=0.1,
            raw_p=0.01,
            holm_adjusted_p=0.01,
            raw_significant=True,
            significant=True,
            is_primary=True,  # forbidden
        )


def test_cost_and_wall_clock_line_present() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    eval_result = _clean_eval(frame)

    card = build_headline_scorecard(eval_result, decision=decision)

    # A/B-scale cost line off the §8 CostBreakdown (full breakdown, not tokens).
    c = card.ab_scale_cost
    assert c.total_microusd == eval_result.ab_scale_cost.total_microusd
    assert c.wall_clock_ms == eval_result.ab_scale_cost.wall_clock_ms
    assert c.non_token_microusd == eval_result.ab_scale_cost.non_token_microusd()
    assert c.non_token_dominates == eval_result.ab_scale_cost.non_token_dominates()

    # Per-arm cost lines, one per arm, faithfully copied.
    assert len(card.cost_lines) == len(eval_result.arms)
    by_arm = {cl.arm: cl for cl in card.cost_lines}
    for arm_eval in eval_result.arms:
        cl = by_arm[arm_eval.arm]
        assert cl.total_microusd == arm_eval.cost.total_microusd
        assert cl.wall_clock_ms == arm_eval.cost.wall_clock_ms

    rendered = card.render()
    assert "wall-clock" in rendered.lower()
    assert "micro-USD" in rendered


def test_unconditional_itt_per_arm() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    eval_result = _clean_eval(frame)

    card = build_headline_scorecard(eval_result, decision=decision)
    assert len(card.arm_scores) == len(eval_result.arms)
    for arm_eval in eval_result.arms:
        score = card.arm_score(arm_eval.arm)
        # The rate is the ArmEval's UNCONDITIONAL outcome resolution rate.
        assert score.itt_pass_rate == arm_eval.outcome.resolution_rate
        # Denominator = attempted (n_tasks * k_repeats), never shrunk to completed.
        assert score.attempted == arm_eval.outcome.attempted
        assert score.resolved_count == arm_eval.outcome.resolved
        # And it IS the unconditional rate (resolved / attempted).
        assert score.itt_pass_rate == arm_eval.outcome.resolved / arm_eval.outcome.attempted


def test_agent_validity_trajectory_integrity_safeties_summary() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    eval_result = _clean_eval(frame)

    card = build_headline_scorecard(eval_result, decision=decision)
    assert card.agent_validity_score == eval_result.agent_validity.validity_score
    assert card.agent_validity_all_valid == eval_result.agent_validity.all_valid
    assert card.integrity_clean is True
    assert card.safeties_clean is True
    assert len(card.arm_summaries) == len(eval_result.arms)
    by_arm = {a.arm: a for a in card.arm_summaries}
    for arm_eval in eval_result.arms:
        a = by_arm[arm_eval.arm]
        assert a.trajectory_score == arm_eval.trajectory.score
        assert a.trajectory_converged == arm_eval.trajectory.converged
        assert a.safety_clean == arm_eval.safety_clean


# ---------------------------------------------------------------------------
# PUBLISHED + STABLE: stable_digest() identical across two builds, same inputs
# ---------------------------------------------------------------------------


def test_stable_digest_identical_across_builds() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    replication = _replication(frame, prereg, decision)
    eval_result = _clean_eval(frame)

    a = build_headline_scorecard(
        eval_result, replication=replication, decision=decision, secondaries=_secondaries()
    )
    b = build_headline_scorecard(
        eval_result, replication=replication, decision=decision, secondaries=_secondaries()
    )
    # Byte-identical published scorecard -> identical stable digest + render.
    assert a == b
    assert a.stable_digest() == b.stable_digest()
    assert a.content_identity() == b.content_identity()
    assert a.render() == b.render()
    # The digest is surfaced in the published render (the stable bit).
    assert a.stable_digest() in a.render()


def test_stable_digest_changes_when_inputs_change() -> None:
    """A genuine stability witness: a different secondary family -> different digest."""
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    eval_result = _clean_eval(frame)

    base = build_headline_scorecard(eval_result, decision=decision, secondaries=_secondaries())
    other = build_headline_scorecard(
        eval_result,
        decision=decision,
        secondaries=(SecondaryReading(name="x", point_estimate=0.9, raw_p=0.001),),
    )
    assert base.stable_digest() != other.stable_digest()


# ---------------------------------------------------------------------------
# FAIL-CLOSED: a non-valid FactoryEvalResult refuses to publish
# ---------------------------------------------------------------------------


def test_non_valid_eval_refuses_to_publish() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)

    scored = tuple(sorted(frame.scored_slice.task_ids))
    victim = scored[0]
    oracle = frame.fixture(victim).oracle
    # A reachable hidden test (§6) -> integrity not clean -> eval not valid.
    surfaces = {tid: AgentRuntimeSurface(task_id=tid) for tid in scored}
    surfaces[victim] = AgentRuntimeSurface(
        task_id=victim,
        reachable_refs=frozenset({oracle.hidden_test_id, oracle.expected_output}),
    )
    dirty = _clean_eval(frame, surfaces=surfaces)
    assert dirty.valid is False  # fail-closed invalidation upstream

    with pytest.raises(NotPublishableError):
        build_headline_scorecard(dirty, decision=decision)


def test_published_scorecard_is_always_publishable() -> None:
    """A HeadlineScorecard validated as non-publishable fails closed at construction."""
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    card = build_headline_scorecard(_clean_eval(frame), decision=decision)
    # The constructed scorecard IS publishable (fail-closed already fired upstream).
    assert card.publishable is True
    # And a validated construction with publishable=False fails closed (the
    # model_post_init invariant — a published scorecard IS publishable).
    fields = card.model_dump()
    fields["publishable"] = False
    with pytest.raises(NotPublishableError):
        HeadlineScorecard.model_validate(fields)


# ---------------------------------------------------------------------------
# Honesty banner present, non-empty, FIRST in render()
# ---------------------------------------------------------------------------


def test_honesty_banner_present_and_first() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    card = build_headline_scorecard(_clean_eval(frame), decision=decision)

    banner = card.honesty_banner()
    assert banner and banner.strip()
    rendered = card.render()
    assert rendered.startswith(banner)  # FIRST — a reader cannot miss it

    low = banner.lower()
    assert "dry-run" in low
    assert "synthetic" in low or "simulated" in low
    # §0/§4: a self-judge seam is never reported as independent validation.
    assert "self-judge" in low or "oracle-scored" in low
    # §6.5: the replication is estimation only, never a second gate.
    assert "estimation" in low
    assert "not a second gate" in low


# ---------------------------------------------------------------------------
# Primary works off the replication alone (no decision supplied) — still a 1.5 read
# ---------------------------------------------------------------------------


def test_primary_falls_back_to_replication_phase15_read() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    replication = _replication(frame, prereg, decision)

    card = build_headline_scorecard(_clean_eval(frame), replication=replication)
    p = card.primary
    # The arbiter read is the Phase-1.5 read the replication was checked against.
    assert p.decision is replication.phase15_decision
    assert p.effect == replication.phase15_effect
    assert p.alpha_source == "phase_1.5_arbiter"
    assert p.replication_present is True


def test_needs_at_least_one_phase15_read() -> None:
    with pytest.raises(ValueError, match=r"Phase-1\.5 arbiter read"):
        build_headline_scorecard(_clean_eval(_frame()))


# ---------------------------------------------------------------------------
# Determinism / provenance carried
# ---------------------------------------------------------------------------


def test_dry_run_provenance_carried() -> None:
    frame = _frame()
    prereg = _frozen_prereg(frame, ledger=GlobalRecords())
    decision = _phase15_decision(prereg)
    card = build_headline_scorecard(_clean_eval(frame), decision=decision)
    assert card.provenance is Provenance.DRY_RUN
    assert card.dry_run is True
