"""Conformance tests for the Phase-6 estimation/replication A/B (T-M6-06).

Contract (docs/evaluation.md §6.5; the Phase-6 A/B is estimation/replication ONLY
— Phase 1.5 is the SOLE confirmatory arbiter that SPENT alpha):

* REPRODUCTION: a replication on a frame where FOUNDRY beats baseline REPRODUCES a
  Phase-1.5 PROCEED (``reproduces_phase15`` True); a null replication does NOT
  reproduce (False) but the Phase-1.5 decision/ledger is UNCHANGED.
* ALPHA NOT RE-SPENT (the load-bearing invariant): ``alpha_looks_spent`` is
  IDENTICAL before and after ``run_replication_ab`` (delta 0); the result is
  ``is_estimation_only``. A spy ledger proves the write methods are never called.
* CITE-FROZEN-PREREG GATE: a tampered manifest raises before estimation.
* DETERMINISM: same (frame, prereg, seed) -> identical ReplicationResult.

METHODOLOGY DRY-RUN on synthetic fixtures — asserts NOTHING about real perf.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.ab_runner import (
    ABResult,
    ArmReport,
    BootstrapComparison,
    SecondaryComparison,
)
from foundry.evaluation.decision_table import DecisionRecord, decide
from foundry.evaluation.hypothesis import Arm, CentralHypothesis, DecisionClass, Provenance
from foundry.evaluation.pilot import PilotResult
from foundry.evaluation.power import FundedPowerRung, compute_power_rung
from foundry.evaluation.preregistration import (
    PreRegistration,
    TamperedPreRegistrationError,
    freeze_preregistration,
)
from foundry.evaluation.replication_ab import (
    AlphaReSpentError,
    ReplicationResult,
    reproduces_phase15,
    run_replication_ab,
)
from foundry.evaluation.task_fixtures import generate_synthetic_frame
from foundry.state.global_records import GlobalRecords

_MDE = 0.1
_ALPHA = 0.05
_TARGET_POWER = 0.8
_REPEAT_COST = 1_000
_BIG_BUDGET = 10_000_000_000
_DECISION_ID = "dec-phase15"


# ---------------------------------------------------------------------------
# Builders: a frozen Phase-1.5 prereg + hand-built ABResults / decisions
# ---------------------------------------------------------------------------


def _funded_rung(*, k_repeats: int = 1) -> FundedPowerRung:
    rung = compute_power_rung(
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
    return rung.model_copy(update={"k_repeats": k_repeats, "n_tasks": rung.n_tasks})


def _frozen_prereg(frame, *, ledger: GlobalRecords | None = None) -> PreRegistration:
    """Freeze the Phase-1.5 manifest BOUND to the frame's scored slice + draw alpha."""
    rung = _funded_rung()
    hyp = CentralHypothesis(
        mde=_MDE, funded_power_rung=rung, provenance=Provenance.SCORED, dry_run=False
    )
    return freeze_preregistration(
        hyp,
        rung,
        frame.scored_slice.identity,  # BIND to the scored slice
        decision_id=_DECISION_ID,
        ledger=ledger or GlobalRecords(),
    )


def _arm_report(arm: Arm, *, rate: float, cost: int, simulated: bool) -> ArmReport:
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


def _ab_result(
    *,
    significant: bool,
    meets_mde: bool,
    point: float,
    identity: str,
    scored_identity: str,
    foundry_cost: int = 900,
    baseline_cost: int = 1_000,
) -> ABResult:
    """A hand-built ABResult with the exact primary bits we need (a known read)."""
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
        significant_at_alpha=significant,
        meets_mde=meets_mde,
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
    return ABResult(
        registration_identity=identity,
        scored_eval_set_identity=scored_identity,
        seed=0,
        k_repeats=1,
        n_tasks=8,
        arm_reports=(
            _arm_report(Arm.FOUNDRY, rate=0.6, cost=foundry_cost, simulated=False),
            _arm_report(Arm.BASELINE_SINGLE_AGENT, rate=0.3, cost=baseline_cost, simulated=True),
            _arm_report(Arm.SINGLE_AGENT_PLUS_GATES, rate=0.4, cost=900, simulated=True),
        ),
        primary=primary,
        secondaries=(secondary,),
        provenance=Provenance.SCORED,
        dry_run=False,
    )


def _phase15_decision(prereg: PreRegistration, *, win: bool) -> DecisionRecord:
    """A REAL Phase-1.5 DecisionRecord via ``decide`` (PROCEED on a win, else KILL)."""
    ab = _ab_result(
        significant=win,
        meets_mde=win,
        point=0.18 if win else 0.0,
        identity=prereg.registration_identity(),
        scored_identity=prereg.scored_eval_set_identity,
    )
    return decide(ab, prereg)


def _winning_core(frame, prereg, seed: int) -> ABResult:
    """An estimation core returning a FOUNDRY-beats-baseline read (a positive)."""
    return _ab_result(
        significant=True,
        meets_mde=True,
        point=0.17,
        identity=prereg.registration_identity(),
        scored_identity=frame.scored_slice.identity,
    )


def _null_core(frame, prereg, seed: int) -> ABResult:
    """An estimation core returning a NULL read (no FOUNDRY-vs-baseline signal)."""
    return _ab_result(
        significant=False,
        meets_mde=False,
        point=0.0,
        identity=prereg.registration_identity(),
        scored_identity=frame.scored_slice.identity,
    )


# ---------------------------------------------------------------------------
# REPRODUCTION
# ---------------------------------------------------------------------------


def test_winning_replication_reproduces_a_phase15_proceed() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=1)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)
    assert decision.decision is DecisionClass.PROCEED

    result = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_winning_core)

    assert result.reproduces is True
    assert result.reproduces_phase15(decision) is True
    assert reproduces_phase15(result, decision) is True  # module-level alias agrees
    assert result.significant_at_alpha and result.meets_mde and result.point_estimate > 0.0
    assert result.is_estimation_only is True


def test_null_replication_does_not_reproduce_proceed_but_phase15_unchanged() -> None:
    # A null replication does NOT reproduce a Phase-1.5 PROCEED — but the Phase-1.5
    # decision/ledger is UNTOUCHED (a disconfirmation cannot un-spend the sunk cost).
    ledger = GlobalRecords()
    frame = generate_synthetic_frame(n_tasks=8, seed=2)
    prereg = _frozen_prereg(frame, ledger=ledger)
    decision = _phase15_decision(prereg, win=True)
    assert decision.decision is DecisionClass.PROCEED

    looks_before = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    result = run_replication_ab(
        frame, prereg, decision, seed=0, ledger=ledger, estimation_core=_null_core
    )
    looks_after = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)

    # A Phase-6 DISCONFIRMATION of the PROCEED is recorded...
    assert result.reproduces is False
    assert result.reproduces_phase15(decision) is False
    # ...but the Phase-1.5 decision/ledger is UNCHANGED (§6.5): not a gate.
    assert decision.decision is DecisionClass.PROCEED  # decision object untouched
    assert looks_before == looks_after  # the sunk alpha is neither un-spent nor re-spent
    assert result.alpha_spent_delta == 0


def test_null_replication_reproduces_a_phase15_kill() -> None:
    # A Phase-1.5 HARD_KILL (a primary null) is REPRODUCED by a null replication.
    frame = generate_synthetic_frame(n_tasks=8, seed=3)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=False)
    assert decision.decision is DecisionClass.HARD_KILL

    result = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_null_core)
    assert result.reproduces is True  # null reproduces a kill (same null direction)


def test_winning_replication_does_not_reproduce_a_phase15_kill() -> None:
    # A replication that FLIPS a Phase-1.5 null into a positive does NOT reproduce
    # the KILL (a materially-inconsistent read — an investigation trigger, §6.5).
    frame = generate_synthetic_frame(n_tasks=8, seed=4)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=False)
    assert decision.decision is DecisionClass.HARD_KILL

    result = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_winning_core)
    assert result.reproduces is False


# ---------------------------------------------------------------------------
# ALPHA NOT RE-SPENT (the load-bearing invariant) — before == after
# ---------------------------------------------------------------------------


def test_alpha_not_re_spent_before_equals_after() -> None:
    # Construct a ledger with the Phase-1.5 look ALREADY drawn (freeze records it),
    # then prove the replication does NOT add a second look.
    ledger = GlobalRecords()
    frame = generate_synthetic_frame(n_tasks=8, seed=5)
    prereg = _frozen_prereg(frame, ledger=ledger)  # this DREW the Phase-1.5 alpha look

    looks_before = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    assert looks_before == 1  # Phase 1.5 spent exactly one look

    decision = _phase15_decision(prereg, win=True)
    result = run_replication_ab(
        frame, prereg, decision, seed=0, ledger=ledger, estimation_core=_winning_core
    )

    looks_after = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    assert looks_after == looks_before == 1  # NO second look drawn
    assert result.alpha_looks_before == 1
    assert result.alpha_looks_after == 1
    assert result.alpha_spent_delta == 0
    assert result.is_estimation_only is True


class _SpyLedger:
    """A LedgerView spy: serves reads, RAISES if a write method is ever reached."""

    def __init__(self, looks: int) -> None:
        self._looks = looks
        self.read_calls = 0

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int:
        self.read_calls += 1
        return self._looks

    def record_alpha_look(self, *args: object, **kwargs: object) -> int:  # pragma: no cover
        raise AssertionError("the replication MUST NOT call record_alpha_look (§6.5)")

    def try_first_alpha_look(self, *args: object, **kwargs: object) -> bool:  # pragma: no cover
        raise AssertionError("the replication MUST NOT call try_first_alpha_look (§6.5)")


def test_replication_never_calls_a_ledger_write_method() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=6)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)
    spy = _SpyLedger(looks=1)

    result = run_replication_ab(
        frame, prereg, decision, seed=0, ledger=spy, estimation_core=_winning_core
    )
    # Only the READ method was used (before + after); no write path was reachable.
    assert spy.read_calls == 2
    assert result.alpha_looks_before == 1 and result.alpha_looks_after == 1


def test_no_ledger_records_none_look_counts_never_a_fabricated_zero() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=7)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)

    result = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_winning_core)
    assert result.alpha_looks_before is None  # never a fabricated 0
    assert result.alpha_looks_after is None
    assert result.alpha_spent_delta == 0


def test_re_spent_alpha_fails_closed() -> None:
    # If the ledger look-count somehow CHANGES across the run, the invariant
    # fails closed (AlphaReSpentError). A mutating spy simulates a re-spend.
    class _MutatingLedger:
        def __init__(self) -> None:
            self._n = 1

        def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int:
            n = self._n
            self._n += 1  # the second read returns a HIGHER count -> a re-spend
            return n

    frame = generate_synthetic_frame(n_tasks=8, seed=8)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)
    with pytest.raises(AlphaReSpentError):
        run_replication_ab(
            frame, prereg, decision, seed=0, ledger=_MutatingLedger(),
            estimation_core=_winning_core,
        )


# ---------------------------------------------------------------------------
# CITE-FROZEN-PREREG GATE + DETERMINISM
# ---------------------------------------------------------------------------


def test_replication_rejects_a_tampered_prereg() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=9)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)
    tampered = prereg.model_copy(update={"mde": 0.2})  # mirror divergence
    with pytest.raises(TamperedPreRegistrationError):
        run_replication_ab(frame, tampered, decision, seed=0, estimation_core=_winning_core)


def test_replication_is_deterministic() -> None:
    frame = generate_synthetic_frame(n_tasks=8, seed=10)
    prereg = _frozen_prereg(frame)
    decision = _phase15_decision(prereg, win=True)
    a = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_winning_core)
    b = run_replication_ab(frame, prereg, decision, seed=0, estimation_core=_winning_core)
    assert a == b  # the whole frozen result coincides


def test_replication_default_core_runs_the_real_ab_machinery() -> None:
    # With NO injected core, run_replication_ab drives the real run_ab over the
    # scored slice (keyless doubles). On the synthetic frame FOUNDRY resolves none,
    # so the estimate is a null -> it reproduces a Phase-1.5 KILL. This exercises
    # the default path end-to-end (no alpha drawn).
    ledger = GlobalRecords()
    frame = generate_synthetic_frame(n_tasks=8, seed=11)
    prereg = _frozen_prereg(frame, ledger=ledger)
    decision = _phase15_decision(prereg, win=False)
    assert decision.decision is DecisionClass.HARD_KILL

    looks_before = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)
    result = run_replication_ab(frame, prereg, decision, seed=0, ledger=ledger)
    looks_after = ledger.alpha_looks_spent(prereg.scored_eval_set_identity, _DECISION_ID)

    assert isinstance(result, ReplicationResult)
    assert result.point_estimate == 0.0  # the real keyless FOUNDRY arm resolves none
    assert result.reproduces is True  # a null reproduces the KILL
    assert looks_before == looks_after  # the default path re-spends NO alpha
