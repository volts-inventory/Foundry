"""``factory_eval`` — the unified self-eval aggregator (docs/evaluation.md).

This is **Foundry's eval OF ITSELF** — the "factory eval" — explicitly NOT a
delivered product's own shipped eval suite (docs/evaluation.md preamble: a
product, especially ML, needs its *own* eval/monitoring artifact with its own
acceptance criteria, produced by the pipeline and NEVER conflated with this
factory self-eval). This module is the single ORCHESTRATOR that ties the Wave-A
eval layers into one frozen, decision-ready :class:`FactoryEvalResult` the
headline scorecard (§7) and the §9 optimization consume.

It COMPOSES (never re-implements) the existing layers, run over the synthetic
task frame (:mod:`foundry.evaluation.task_fixtures`) via the existing
``harness.run_arm`` over the test doubles (MockTransport / StubAdapter — no live
API key, no ``claude -p``, no network):

* **OUTCOME** — the §3 oracle-scored suites (:func:`suites.run_suite`): per-arm
  resolution + ITT denominator (all initiated tasks; non-completion = unresolved).
* **AGENT-VALIDITY** — the §4 per-role planted-defect-caught report
  (:func:`agent_validity.agent_validity_report`).
* **TRAJECTORY** — the §5 deterministic process scores
  (:func:`trajectory.score_trajectory`).
* **COST** — the §8 per-arm + A/B-scale cost model
  (:class:`cost_model.CostModel`).
* **EVAL-INTEGRITY** — the §6 fail-closed check
  (:func:`integrity.check_eval_integrity`): a reachable hidden test / contaminated
  task / conditioned-or-dropped ITT denominator INVALIDATES the eval.
* **SAFETIES** — the §10 fail-closed check (:func:`safeties.check_arm_safeties`):
  any reward-hacking / eval-lie / nondeterminism finding INVALIDATES the eval.

FAIL-CLOSED (load-bearing, docs/evaluation.md §6/§10). The result carries an
overall ``valid`` bit:

    valid = (integrity report is clean) AND (every arm's safety report is clean)

A result that is **not** ``valid`` MUST NOT be treated as publishable: the
scorecard / confirmatory A/B consume ONLY a valid result. The invalidation is
the whole point — a reachable hidden test or a reward-hacking finding means the
numbers cannot be reported as evidence, full stop, even though every other layer
produced a value. ``valid`` is computed from the layer verdicts, never asserted
independently, so a tripped integrity/safety check can never be silently dropped.

ALPHA LEDGER (READ-ONLY, §6.5). Phase 1.5 is the SOLE confirmatory arbiter and
owns the pre-registered alpha; this Phase-6 aggregator is estimation/replication
only and **never re-spends alpha**. When a :class:`GlobalRecords`-style ledger is
supplied, ``run_factory_eval`` calls ONLY ``alpha_looks_spent`` (a read) and
records the observed look count for audit — it NEVER calls ``record_alpha_look``
/ ``try_first_alpha_look``.

HONESTY NOTE (read before reuse): every layer here is a **methodology dry-run on
synthetic fixtures** — synthetic frame, mock model, dev HMAC roots. A
:class:`FactoryEvalResult` defaults ``provenance == Provenance.DRY_RUN`` and
:attr:`FactoryEvalResult.honesty_banner` states it plainly; the result asserts
NOTHING about real Foundry-vs-baseline performance. It only proves the assembly
is real, deterministic, and fail-closed.

Deterministic: the same ``(frame, arms, seed, ...)`` yields an IDENTICAL
:class:`FactoryEvalResult` (every composed layer is itself deterministic and the
arms are iterated in a fixed, sorted order — never dict-iteration-dependent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import Field

from foundry.evaluation.agent_validity import AgentValidityReport, agent_validity_report
from foundry.evaluation.cost_model import CostBreakdown, CostModel, RunSignals
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.integrity import (
    AgentRuntimeSurface,
    IntegrityReport,
    TaskInputManifest,
    check_eval_integrity,
)
from foundry.evaluation.safeties import EvalSafetyReport, check_arm_safeties
from foundry.evaluation.suites import OutcomeSuite, SuiteResult, make_suite, run_suite
from foundry.evaluation.trajectory import ProcessScore, TrajectorySignals, score_trajectory
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from foundry.evaluation.harness import ArmResult, FoundryRunner
    from foundry.evaluation.task_fixtures import OracleDescriptor, TaskFrame

__all__ = [
    "ArmEval",
    "FactoryEvalResult",
    "LedgerView",
    "run_factory_eval",
]

# The honesty banner stamped on every dry-run result (docs/evaluation.md preamble
# + the Phase-1.5/6 honesty discipline): synthetic fixtures + mock model + dev
# HMAC roots = a methodology exercise, NOT real funded evidence.
_DRY_RUN_BANNER = (
    "DRY-RUN: factory self-eval on SYNTHETIC fixtures via mock model + dev HMAC "
    "roots — a methodology exercise, NOT real funded Foundry-vs-baseline evidence. "
    "This is the FACTORY self-eval, NOT a delivered product's own shipped eval."
)


class LedgerView(Protocol):
    """The READ-ONLY slice of the alpha look-ledger this aggregator may touch.

    Phase 1.5 is the sole confirmatory arbiter (§6.5); this Phase-6 aggregator is
    estimation/replication only and NEVER re-spends alpha. So it depends on ONLY
    the read method — ``alpha_looks_spent`` — and structurally CANNOT call
    ``record_alpha_look`` / ``try_first_alpha_look`` (they are not on this
    Protocol). ``foundry.state.global_records.GlobalRecords`` satisfies it.
    """

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int: ...


class ArmEval(FrozenModel):
    """The per-arm composition of every eval layer for ONE arm (over one suite).

    Holds the layer outputs for a single :class:`~foundry.evaluation.hypothesis.Arm`:
    the §3 outcome :class:`SuiteResult`, the §5 trajectory
    :class:`ProcessScore`, the §8 per-arm :class:`CostBreakdown`, and the §10
    fail-closed :class:`EvalSafetyReport`. ``safety_clean`` mirrors the safety
    report's clean bit so the overall ``valid`` roll-up is auditable per arm.

    HONESTY: ``provenance`` defaults to DRY_RUN; the simulated baseline arms make
    no claim about a real single agent.
    """

    arm: Arm
    outcome: SuiteResult
    trajectory: ProcessScore
    cost: CostBreakdown
    safety: EvalSafetyReport
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def safety_clean(self) -> bool:
        """True iff this arm's §10 safety report found no violation (fail-closed)."""
        return self.safety.clean

    @property
    def resolution_rate(self) -> float:
        """The arm's ITT outcome resolution rate (§2/§3 — convenience view)."""
        return self.outcome.resolution_rate


class FactoryEvalResult(FrozenModel):
    """The unified, frozen factory self-eval result (docs/evaluation.md).

    Aggregates every eval layer into one decision-ready record:

    * ``arms`` — per-arm :class:`ArmEval` (outcome + trajectory + cost + safety),
      in sorted-arm order (deterministic);
    * ``agent_validity`` — the §4 per-role validity report (one, factory-wide);
    * ``integrity`` — the §6 fail-closed integrity report (one, eval-wide);
    * ``ab_scale_cost`` — the §8 A/B-scale roll-up across the arms;
    * ``alpha_looks_spent`` — the OBSERVED ledger look-count (read-only audit; the
      aggregator never re-spends alpha — §6.5);
    * ``valid`` — the load-bearing fail-closed bit.

    THE FAIL-CLOSED CONTRACT (§6/§10): ``valid`` is

        integrity.clean AND every arm's safety.clean

    computed from the layer verdicts (:func:`_compute_valid`), NEVER asserted
    separately — so a reachable hidden test / contamination (integrity) or a
    reward-hack / eval-lie / nondeterminism finding (safeties) INVALIDATES the
    WHOLE result. A non-``valid`` result is NOT publishable: the scorecard /
    confirmatory A/B consume only a valid one.

    HONESTY: ``provenance`` defaults to DRY_RUN and :attr:`honesty_banner` states
    the dry-run-on-synthetic-fixtures provenance; the result asserts NOTHING about
    real performance.
    """

    arms: tuple[ArmEval, ...]
    agent_validity: AgentValidityReport
    integrity: IntegrityReport
    ab_scale_cost: CostBreakdown
    # Read-only audit of the alpha look-ledger (None when no ledger was bound).
    # The aggregator NEVER re-spends alpha (§6.5); this is the OBSERVED count only.
    alpha_looks_spent: int | None = Field(default=None, ge=0)
    valid: bool
    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    @property
    def honesty_banner(self) -> str:
        """The DRY-RUN honesty banner (synthetic fixtures / mock model / dev roots)."""
        return _DRY_RUN_BANNER

    @property
    def integrity_clean(self) -> bool:
        """True iff the §6 integrity report is clean (a fail-closed input to ``valid``)."""
        return self.integrity.clean

    @property
    def safeties_clean(self) -> bool:
        """True iff EVERY arm's §10 safety report is clean (fail-closed input to ``valid``)."""
        return all(a.safety_clean for a in self.arms)

    @property
    def publishable(self) -> bool:
        """Alias for ``valid`` — a non-valid result is NOT publishable (§6/§10).

        The scorecard / confirmatory A/B gate on THIS: an invalidated eval can
        never be reported as evidence.
        """
        return self.valid

    def arm_eval(self, arm: Arm) -> ArmEval:
        """The :class:`ArmEval` for ``arm`` (raises ``KeyError`` if not evaluated)."""
        for a in self.arms:
            if a.arm is arm:
                return a
        raise KeyError(arm)


def _compute_valid(integrity: IntegrityReport, arms: Sequence[ArmEval]) -> bool:
    """The fail-closed ``valid`` bit: integrity clean AND every arm's safety clean.

    Computed from the layer verdicts so a tripped integrity/safety check can never
    be silently dropped (§6/§10). An empty arm set is NOT vacuously valid — a
    factory eval with no arm produced no outcome to publish, so it fails closed.
    """
    if not arms:
        return False
    return integrity.clean and all(a.safety_clean for a in arms)


def _default_suites(frame: TaskFrame) -> tuple[OutcomeSuite, ...]:
    """Build the default §3 SWE-EVO-style outcome suite over the frame's scored slice.

    The §2 A/B arena is the SWE-EVO-style long-horizon suite; the aggregator
    scores the arms over the frame's SCORED slice (disjoint from the pilot — §2)
    by default. Membership is the sorted scored task-ids so the suite identity is
    deterministic.
    """
    from foundry.evaluation.suites import SuiteKind  # local: keep module import-cheap

    scored = frame.scored_slice
    suite = make_suite(
        name="swe_evo_scored",
        kind=SuiteKind.SWE_EVO_STYLE,
        task_ids=tuple(sorted(scored.task_ids)),
    )
    return (suite,)


def _default_trajectory_signals(arm: Arm, suite: OutcomeSuite) -> TrajectorySignals:
    """A deterministic per-arm trajectory-signal stand-in (SYNTHETIC dry-run).

    The §5 trajectory eval scores a path's process from structured signals; in a
    dry run there is no real OTel trace, so we manufacture a per-arm signal record
    that is deterministic in the arm. The FOUNDRY arm is given a clean,
    first-pass, converged path (the structure-helps hypothesis); the SIMULATED
    arms a worse path. This asserts NOTHING about real trajectories — it only
    drives the deterministic process score through the aggregator.
    """
    feature_id = f"{arm.value}:{suite.identity}"
    if arm is Arm.FOUNDRY:
        return TrajectorySignals(
            feature_id=feature_id,
            steps=4,
            ideal_steps=4,
            rework_loops=0,
            gate_fail_then_fix=0,
            policy_violations=0,
            converged=True,
        )
    return TrajectorySignals(
        feature_id=feature_id,
        steps=8,
        ideal_steps=4,
        rework_loops=1,
        gate_fail_then_fix=1,
        policy_violations=0,
        converged=True,
    )


def _default_run_signals(arm: Arm) -> RunSignals:
    """A deterministic per-arm §8 cost-signal stand-in (SYNTHETIC dry-run).

    Foundry pays coordination + monitor overhead the baseline pours into the task
    (§8 roll-up), so its per-task cost is HIGHER. Integer micro-USD / units / ms
    throughout (no floats for money). SYNTHETIC: the accounting is real, the
    numbers are manufactured.
    """
    if arm is Arm.FOUNDRY:
        return RunSignals(
            model_token_microusd=1_200_000,
            compute_units=20,
            compute_unit_microusd=10_000,
            wall_clock_ms=900_000,
            wall_clock_unit_microusd=1,
            human_review_ms=120_000,
            human_review_unit_microusd=2,
            infra_microusd=50_000,
        )
    return RunSignals(
        model_token_microusd=800_000,
        compute_units=10,
        compute_unit_microusd=10_000,
        wall_clock_ms=600_000,
        wall_clock_unit_microusd=1,
        human_review_ms=20_000,
        human_review_unit_microusd=2,
        infra_microusd=20_000,
    )


def run_factory_eval(
    frame: TaskFrame,
    *,
    arms: Sequence[Arm] = (Arm.FOUNDRY, Arm.BASELINE_SINGLE_AGENT, Arm.SINGLE_AGENT_PLUS_GATES),
    suites: Sequence[OutcomeSuite] | None = None,
    seed: int = 0,
    k_repeats: int = 1,
    budget_usd_microusd: int = 5_000_000,
    foundry_runner: FoundryRunner | None = None,
    surfaces: Mapping[str, AgentRuntimeSurface] | None = None,
    manifests: Mapping[str, TaskInputManifest] | None = None,
    trajectory_signals: Mapping[Arm, TrajectorySignals] | None = None,
    run_signals: Mapping[Arm, RunSignals] | None = None,
    agent_validity: AgentValidityReport | None = None,
    safety_inputs: Mapping[Arm, Sequence[ArmResult]] | None = None,
    discriminators: Mapping[Arm, object] | None = None,
    self_reports: Mapping[Arm, dict[tuple[str, int], bool]] | None = None,
    ledger: LedgerView | None = None,
    decision_id: str = "phase6_factory_eval",
    provenance: Provenance = Provenance.DRY_RUN,
) -> FactoryEvalResult:
    """Orchestrate every eval layer over ``frame``'s arms/suites → one result.

    Runs the §3 outcome suites (via the existing ``harness.run_arm`` over the test
    doubles), the §4 agent-validity report, the §5 trajectory scores, the §8 cost
    model, the §6 fail-closed integrity check, and the §10 fail-closed safeties,
    and folds them into a frozen :class:`FactoryEvalResult` whose ``valid`` bit is
    ``integrity.clean AND every arm's safety.clean`` (:func:`_compute_valid`).

    FAIL-CLOSED (§6/§10): a reachable hidden test / contaminated task (integrity)
    or a reward-hack / eval-lie / nondeterminism finding (safeties) sets
    ``valid is False`` — the whole eval is INVALIDATED and is not publishable. The
    layers still produce their values (so the failure is auditable), but the
    scorecard / confirmatory A/B consume only a ``valid`` result.

    Deterministic: identical inputs ⇒ identical result (arms iterate in sorted
    order; every composed layer is itself deterministic).

    Parameters
    ----------
    frame:
        The synthetic :class:`TaskFrame`; outcome suites default to its SCORED
        slice (disjoint from the pilot — §2).
    arms:
        The arms to evaluate (default: the three §2 A/B arms). Iterated in a fixed
        sorted order, so the result is independent of the caller's argument order.
    suites:
        The §3 outcome suites; defaults to a SWE-EVO-style suite over the scored
        slice (:func:`_default_suites`). Each arm is scored over EACH suite; the
        first suite is the arm's headline outcome on the result.
    seed / k_repeats / budget_usd_microusd / foundry_runner:
        Forwarded verbatim to ``harness.run_arm`` (via ``suites.run_suite``).
    surfaces / manifests:
        The §6 integrity inputs — the agent's recorded runtime-reachable surface
        and input manifest per task. A reachable hidden test or contaminated task
        INVALIDATES the eval (fail-closed). Defaults are isolated/uncontaminated
        (a clean dry run); a caller injects a dirty one to exercise invalidation.
    trajectory_signals / run_signals:
        Per-arm §5 / §8 signal overrides; deterministic dry-run stand-ins
        otherwise (:func:`_default_trajectory_signals` / :func:`_default_run_signals`).
    agent_validity:
        An injected §4 report; otherwise :func:`agent_validity_report` runs the
        per-role planted-defect checks (a fully-valid factory by default).
    safety_inputs / discriminators / self_reports:
        Per-arm §10 safety inputs. ``safety_inputs[arm]`` are same-seed re-runs for
        the nondeterminism check (defaults to the single scored run — vacuously
        deterministic); ``discriminators[arm]`` an oracle-grounded
        ``DiscriminatorReport`` for the reward-hacking check; ``self_reports[arm]``
        the UNTRUSTED self-report channel for the eval-lie check. A finding on ANY
        of these INVALIDATES the eval (fail-closed).
    ledger:
        An optional READ-ONLY alpha look-ledger (a :class:`LedgerView`). When
        supplied, the OBSERVED ``alpha_looks_spent`` is recorded for audit; the
        aggregator NEVER re-spends alpha (§6.5 — Phase 1.5 is the sole arbiter).
    decision_id:
        The ledger decision id to read the look-count under (read-only).
    provenance:
        Carried through to every minted record (default DRY_RUN — honesty).
    """
    suite_tuple = tuple(suites) if suites is not None else _default_suites(frame)
    if not suite_tuple:
        raise ValueError("run_factory_eval requires at least one outcome suite")
    if not arms:
        raise ValueError("run_factory_eval requires at least one arm")

    fixtures = {f.task_id: f for f in frame.fixtures}
    cost_model = CostModel(provenance=provenance)
    sorted_arms = tuple(sorted(set(arms), key=lambda a: a.value))  # deterministic order

    # ---- per-arm composition (outcome + trajectory + cost + safety) ----------
    arm_evals: list[ArmEval] = []
    headline_arm_results: list[ArmResult] = []
    per_arm_run_signals: dict[Arm, RunSignals] = {}
    for arm in sorted_arms:
        # OUTCOME (§3): score the arm over every suite; the first suite is the
        # arm's headline outcome (used for integrity/safeties), the rest are run
        # for completeness so a multi-suite eval is honest end-to-end.
        suite_results = tuple(
            run_suite(
                arm,
                suite,
                fixtures,
                k_repeats=k_repeats,
                seed=seed,
                budget_usd_microusd=budget_usd_microusd,
                foundry_runner=foundry_runner,
            )
            for suite in suite_tuple
        )
        headline = suite_results[0]
        headline_arm_results.append(headline.arm_result)

        # TRAJECTORY (§5): deterministic process score over the per-arm signals.
        signals = (
            trajectory_signals.get(arm) if trajectory_signals is not None else None
        ) or _default_trajectory_signals(arm, suite_tuple[0])
        traj = score_trajectory(signals)

        # COST (§8): per-arm breakdown (the A/B-scale roll-up follows below).
        rs = (run_signals.get(arm) if run_signals is not None else None) or _default_run_signals(
            arm
        )
        per_arm_run_signals[arm] = rs
        cost = cost_model.feature_cost(rs)

        # SAFETIES (§10, fail-closed): nondeterminism over same-seed re-runs +
        # optional eval-lie / reward-hacking signals. Any finding invalidates.
        runs = list(safety_inputs[arm]) if safety_inputs and arm in safety_inputs else [
            headline.arm_result
        ]
        sr = self_reports.get(arm) if self_reports is not None else None
        disc = discriminators.get(arm) if discriminators is not None else None
        safety = check_arm_safeties(
            runs,
            self_reports=dict(sr) if sr is not None else None,
            discriminator=disc,  # type: ignore[arg-type]  # DiscriminatorReport | None
        )

        arm_evals.append(
            ArmEval(
                arm=arm,
                outcome=headline,
                trajectory=traj,
                cost=cost,
                safety=safety,
                provenance=provenance,
            )
        )

    # ---- AGENT-VALIDITY (§4): one factory-wide per-role report ---------------
    validity = (
        agent_validity
        if agent_validity is not None
        else agent_validity_report(provenance=provenance)
    )

    # ---- EVAL-INTEGRITY (§6, fail-closed): hidden-test reachability +
    # contamination + ITT-unconditional over every arm's headline result. -------
    oracles: dict[str, OracleDescriptor] = {
        tid: fixtures[tid].oracle
        for suite in suite_tuple
        for tid in suite.task_ids
    }
    integrity = check_eval_integrity(
        oracles=oracles,
        surfaces=dict(surfaces) if surfaces is not None else _isolated_surfaces(oracles),
        manifests=dict(manifests) if manifests is not None else {},
        arm_results=tuple(headline_arm_results),
    )

    # ---- COST (§8): the A/B-scale roll-up across the arms --------------------
    n = max(1, len(oracles))
    ab_scale = cost_model.ab_scale_cost(per_arm_run_signals, n=n, k=k_repeats)

    # ---- ALPHA LEDGER (READ-ONLY, §6.5): observe, never re-spend -------------
    looks = (
        ledger.alpha_looks_spent(frame.eval_set_identity, decision_id)
        if ledger is not None
        else None
    )

    # ---- FAIL-CLOSED ROLL-UP: valid = integrity clean AND every safety clean --
    valid = _compute_valid(integrity, arm_evals)

    return FactoryEvalResult(
        arms=tuple(arm_evals),
        agent_validity=validity,
        integrity=integrity,
        ab_scale_cost=ab_scale,
        alpha_looks_spent=looks,
        valid=valid,
        provenance=provenance,
        dry_run=provenance is Provenance.DRY_RUN,
    )


def _isolated_surfaces(
    oracles: Mapping[str, OracleDescriptor],
) -> dict[str, AgentRuntimeSurface]:
    """The default §6 surfaces: an EMPTY reachable surface per task (provably isolated).

    The integrity check fails closed on a MISSING surface (unreachability unproven)
    but passes on an EMPTY one (the hidden test was demonstrably out of the agent's
    reach — no shared FS, no returned logs). So the clean-run default records an
    isolated surface for every scored task; a caller injects a surface that
    intersects a held-out token to exercise the reachable-hidden-test invalidation.
    """
    return {tid: AgentRuntimeSurface(task_id=tid) for tid in oracles}
