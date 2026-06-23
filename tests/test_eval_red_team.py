"""Red-team harness — adversarial probes against the REAL defenses (T-M6-11).

The harness runs a SUITE of adversarial probes and asserts each is DEFENDED (the
attack FAILS). Each probe genuinely invokes the REAL shipped defense — the real
Guardian with a malicious analyzer, the real discriminator on a gamed suite, the
real two-zone runner on a sealed channel, the real admission controller, the real
eval safeties. These tests prove:

* ``run_red_team()`` reports ALL probes DEFENDED on the real system;
* NON-VACUITY: the SAME suite wired to a DELIBERATELY-WEAKENED/bypassed defense
  reports UNDEFENDED (the harness detects a real hole, not always-green);
* the cost/latency report flags an overspend / latency regression vs the budget,
  and passes within budget;
* fail-closed: an inconclusive/errored probe counts as NOT defended;
* determinism.
"""

from __future__ import annotations

from foundry.evaluation.cost_model import CostModel, RunSignals
from foundry.evaluation.hypothesis import Provenance
from foundry.evaluation.red_team import (
    CostLatencyReport,
    ProbeResult,
    RedTeamHarness,
    RedTeamProbe,
    RedTeamReport,
    cost_latency_report,
    default_probes,
)

# The seven attack classes the default suite covers.
_EXPECTED_ATTACKS = {
    "prompt-injection",
    "gate-evasion",
    "forged-results",
    "forged-verdict",
    "fork-divergence",
    "reward-hacking",
    "eval-lie",
}


# --------------------------------------------------------------------------- #
# run_red_team(): all probes DEFENDED on the real system.
# --------------------------------------------------------------------------- #
def test_real_system_all_probes_defended() -> None:
    """Every default probe HOLDS on the real, unmodified defenses."""
    report = RedTeamHarness().run_red_team()
    assert report.all_defended
    assert report.critical_findings == ()
    assert len(report.probes) == 7
    for p in report.probes:
        assert p.defended, f"{p.name}: {p.detail}"
        assert p.conclusive
        assert not p.critical_finding


def test_default_suite_covers_every_attack_class() -> None:
    """The default probe set covers all seven required attack classes."""
    attacks = {p.attack for p in default_probes()}
    assert attacks == _EXPECTED_ATTACKS


def test_report_probe_lookup() -> None:
    report = RedTeamHarness().run_red_team()
    probe = report.probe("gate_evasion_gamed_suite_caught")
    assert probe.attack == "gate-evasion"
    assert probe.defended


# --------------------------------------------------------------------------- #
# Each probe genuinely invokes its REAL defense (spot-checked individually).
# --------------------------------------------------------------------------- #
def test_prompt_injection_probe_runs_real_guardian() -> None:
    """The malicious analyzer cannot move the deterministic trip (gov §3)."""
    report = RedTeamHarness().run_red_team()
    p = report.probe("prompt_injection_cannot_clear_trip")
    assert p.defended
    # the real Guardian kept the deterministic action despite override prose.
    assert "advisory" in p.detail.lower() or "inert" in p.detail.lower()


def test_gate_evasion_probe_runs_real_discriminator() -> None:
    """The real discriminator FAILs a gamed-green suite (surviving mutant)."""
    p = RedTeamHarness().run_red_team().probe("gate_evasion_gamed_suite_caught")
    assert p.defended
    assert "passed=False" in p.detail


def test_forged_results_probe_runs_real_runner() -> None:
    """The real sealed-channel runner rejects a swapped FAIL->PASS observation."""
    p = RedTeamHarness().run_red_team().probe("forged_results_swap_is_error")
    assert p.defended
    assert "error" in p.detail.lower()


def test_forged_verdict_probe_runs_real_admission() -> None:
    """The real admission controller DENYs an unsigned (forged) required gate."""
    p = RedTeamHarness().run_red_team().probe("forged_verdict_denied")
    assert p.defended
    assert "signature_invalid" in p.detail


def test_fork_divergence_probe_runs_real_admission() -> None:
    """A laundering fork re-presenting a DISQUALIFIED digest is still DENIED."""
    p = RedTeamHarness().run_red_team().probe("fork_divergence_disqualified_denied")
    assert p.defended
    assert "disqualified" in p.detail.lower()


def test_reward_hacking_and_eval_lie_probes_run_real_safeties() -> None:
    report = RedTeamHarness().run_red_team()
    assert report.probe("reward_hacking_caught").defended
    assert report.probe("eval_lie_caught").defended


# --------------------------------------------------------------------------- #
# NON-VACUITY: a deliberately-weakened defense reports UNDEFENDED.
# --------------------------------------------------------------------------- #
def test_weakened_defenses_report_undefended() -> None:
    """Wired to BYPASSED defenses, EVERY probe reports UNDEFENDED.

    This proves the harness's DEFENDED verdict tracks the real defense — it is not
    always-green. The SAME suite, run with weaken=True, detects the planted hole.
    """
    report = RedTeamHarness().run_red_team(weaken=True)
    assert not report.all_defended
    assert len(report.critical_findings) == 7
    for p in report.probes:
        assert not p.defended, f"{p.name} should be UNDEFENDED when weakened"
        assert p.conclusive  # the weaken path reaches a conclusive (UNDEFENDED) verdict
        assert "WEAKENED" in p.detail or "UNDEFENDED" in p.detail


def test_each_probe_flips_individually_under_weaken() -> None:
    """Probe-by-probe: each probe is DEFENDED on real, UNDEFENDED when weakened."""
    real = {p.name: p.defended for p in RedTeamHarness().run_red_team().probes}
    weak = {p.name: p.defended for p in RedTeamHarness().run_red_team(weaken=True).probes}
    for name in real:
        assert real[name] is True, f"{name} not defended on real system"
        assert weak[name] is False, f"{name} not flipped under weaken"


# --------------------------------------------------------------------------- #
# Fail-closed: an inconclusive/errored probe counts as NOT defended.
# --------------------------------------------------------------------------- #
def _raising_probe(_weaken: bool) -> ProbeResult:
    raise RuntimeError("boom — the probe could not reach a verdict")


def test_errored_probe_is_not_defended_fail_closed() -> None:
    """A probe that RAISES is recorded INCONCLUSIVE + NOT defended (never a pass)."""
    harness = RedTeamHarness(
        probes=[RedTeamProbe(name="explodes", attack="synthetic", run=_raising_probe)]
    )
    report = harness.run_red_team()
    p = report.probe("explodes")
    assert not p.defended
    assert not p.conclusive
    assert "INCONCLUSIVE" in p.detail
    assert not report.all_defended
    assert report.critical_findings == (p,)


def _inconclusive_probe(_weaken: bool) -> ProbeResult:
    """A probe that returns a NON-conclusive verdict (cannot decide)."""
    return ProbeResult(
        name="murky",
        attack="synthetic",
        defended=False,
        conclusive=False,
        detail="could not establish a verdict",
    )


def test_inconclusive_probe_blocks_all_defended() -> None:
    harness = RedTeamHarness(
        probes=[RedTeamProbe(name="murky", attack="synthetic", run=_inconclusive_probe)]
    )
    report = harness.run_red_team()
    assert not report.all_defended
    assert report.probe("murky").critical_finding


def test_empty_suite_is_not_vacuously_clean() -> None:
    """A red-team that ran NO probe proved nothing — all_defended is False."""
    report = RedTeamHarness(probes=[]).run_red_team()
    assert report.probes == ()
    assert not report.all_defended


# --------------------------------------------------------------------------- #
# Cost / latency hardening vs the CostModel budget.
# --------------------------------------------------------------------------- #
def _signals() -> RunSignals:
    return RunSignals(
        model_token_microusd=1_000_000,
        compute_units=10,
        compute_unit_microusd=10_000,
        wall_clock_ms=500_000,
        wall_clock_unit_microusd=1,
    )


def test_cost_latency_within_budget_passes() -> None:
    """A run inside both the USD and wall-clock budget flags no regression."""
    report = cost_latency_report(
        _signals(), budget_microusd=5_000_000, budget_wall_clock_ms=600_000
    )
    assert report.within_budget
    assert not report.overspend
    assert not report.latency_regression
    # the report REUSES the cost model's breakdown (total = priced terms).
    assert report.total_microusd == report.breakdown.total_microusd


def test_cost_latency_flags_overspend() -> None:
    """A run whose USD total EXCEEDS the budget flags an overspend (§8)."""
    signals = _signals()
    total = CostModel().feature_cost(signals).total_microusd
    report = cost_latency_report(
        signals, budget_microusd=total - 1, budget_wall_clock_ms=10**12
    )
    assert report.overspend
    assert not report.within_budget


def test_cost_latency_flags_latency_regression() -> None:
    """A run whose wall-clock EXCEEDS the budget flags a latency regression (§8)."""
    report = cost_latency_report(
        _signals(), budget_microusd=10**15, budget_wall_clock_ms=499_999
    )
    assert report.latency_regression
    assert not report.within_budget


def test_cost_latency_within_budget_at_exact_bound() -> None:
    """The budget IS the bound: total == budget and wall == budget is within."""
    signals = _signals()
    total = CostModel().feature_cost(signals).total_microusd
    report = cost_latency_report(
        signals, budget_microusd=total, budget_wall_clock_ms=500_000
    )
    assert report.within_budget


def test_cost_latency_report_is_a_frozen_record() -> None:
    report = cost_latency_report(
        _signals(), budget_microusd=10**12, budget_wall_clock_ms=10**12
    )
    assert isinstance(report, CostLatencyReport)
    assert report.provenance is Provenance.DRY_RUN
    assert report.dry_run is True


# --------------------------------------------------------------------------- #
# Determinism.
# --------------------------------------------------------------------------- #
def test_run_red_team_is_deterministic() -> None:
    """The same harness yields an identical report across runs (no RNG/clock)."""
    r1 = RedTeamHarness().run_red_team()
    r2 = RedTeamHarness().run_red_team()
    assert isinstance(r1, RedTeamReport)
    assert r1 == r2


def test_cost_latency_report_is_deterministic() -> None:
    a = cost_latency_report(_signals(), budget_microusd=10**12, budget_wall_clock_ms=10**12)
    b = cost_latency_report(_signals(), budget_microusd=10**12, budget_wall_clock_ms=10**12)
    assert a == b


def test_provenance_carried_through() -> None:
    report = RedTeamHarness(provenance=Provenance.DRY_RUN).run_red_team()
    assert report.provenance is Provenance.DRY_RUN
    assert report.dry_run is True
