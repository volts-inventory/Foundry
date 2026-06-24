"""``factory_eval`` aggregator tests — the unified, fail-closed self-eval result.

Exercises :mod:`foundry.evaluation.factory_eval`: the single orchestrator that
ties the Wave-A eval layers (outcome suites §3, agent-validity §4, trajectory §5,
integrity §6, cost §8, safeties §10) into one frozen
:class:`FactoryEvalResult`. The load-bearing properties (the prompt + backlog):

* a CLEAN run aggregates every layer into a ``valid`` result (outcome + validity
  + trajectory + cost present; integrity clean; safeties clean);
* a reachable-hidden-test / contaminated input → integrity not clean →
  ``valid is False`` (the eval is INVALIDATED, fail-closed);
* a reward-hacking / eval-lie / nondeterminism finding → safeties not clean →
  ``valid is False``;
* DETERMINISM — same seed ⇒ identical result;
* NON-VACUITY — the invalidating cases FLIP ``valid`` relative to the clean run;
* the alpha ledger is bound READ-ONLY (only ``alpha_looks_spent`` is called).

These run over the synthetic frame via the existing test doubles — no live key.
A precomputed agent-validity report is injected in most tests to keep them fast
and deterministic; one test exercises the default (real per-role) path.
"""

from __future__ import annotations

from foundry.evaluation.agent_validity import (
    AgentValidityReport,
    RoleValidity,
    ValidityRole,
    agent_validity_report,
)
from foundry.evaluation.factory_eval import FactoryEvalResult, run_factory_eval
from foundry.evaluation.harness import OutcomeStatus, TaskOutcome
from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.evaluation.integrity import AgentRuntimeSurface, TaskInputManifest, ViolationKind
from foundry.evaluation.safeties import SafetyKind
from foundry.evaluation.task_fixtures import TaskFrame, generate_synthetic_frame
from foundry.gates.discriminator import DiscriminatorReport

# --------------------------------------------------------------------------- #
# builders / fixtures
# --------------------------------------------------------------------------- #


def _frame() -> TaskFrame:
    # Small frame keeps the suite (and the FOUNDRY-arm pipeline runs) cheap.
    return generate_synthetic_frame(n_tasks=4, seed=0, pilot_fraction=0.5)


def _valid_validity() -> AgentValidityReport:
    """A precomputed fully-valid §4 report (injected to avoid the slow agent runs)."""
    return AgentValidityReport(
        roles=tuple(
            RoleValidity(role=r, valid=True, planted_defect="planted", observed="caught")
            for r in ValidityRole
        )
    )


def _scored_task_ids(frame: TaskFrame) -> tuple[str, ...]:
    return tuple(sorted(frame.scored_slice.task_ids))


def _clean(frame: TaskFrame, **overrides: object) -> FactoryEvalResult:
    """A clean run with the validity report injected (fast, deterministic)."""
    kwargs: dict[str, object] = {"agent_validity": _valid_validity()}
    kwargs.update(overrides)
    return run_factory_eval(frame, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# clean run: every layer present, valid
# --------------------------------------------------------------------------- #


def test_clean_run_aggregates_all_layers_and_is_valid() -> None:
    frame = _frame()
    result = _clean(frame)

    assert isinstance(result, FactoryEvalResult)
    # All three default arms, in sorted-arm order (deterministic).
    assert tuple(a.arm for a in result.arms) == tuple(
        sorted(
            (Arm.FOUNDRY, Arm.BASELINE_SINGLE_AGENT, Arm.SINGLE_AGENT_PLUS_GATES),
            key=lambda a: a.value,
        )
    )
    # Every layer is present per arm.
    for arm_eval in result.arms:
        assert arm_eval.outcome.attempted > 0  # OUTCOME (§3)
        assert 0.0 <= arm_eval.trajectory.score <= 1.0  # TRAJECTORY (§5)
        assert arm_eval.cost.total_microusd > 0  # COST (§8)
        assert arm_eval.safety.clean  # SAFETIES (§10)
    # Factory-wide layers.
    assert result.agent_validity.all_valid  # AGENT-VALIDITY (§4)
    assert result.integrity.clean  # EVAL-INTEGRITY (§6)
    assert result.ab_scale_cost.total_microusd > 0  # A/B-scale roll-up (§8)

    # The fail-closed roll-up: clean integrity + clean safeties => valid.
    assert result.integrity_clean is True
    assert result.safeties_clean is True
    assert result.valid is True
    assert result.publishable is True
    # Honesty: dry-run banner + provenance carried through.
    assert result.provenance is Provenance.DRY_RUN
    assert result.dry_run is True
    assert "DRY-RUN" in result.honesty_banner
    assert "product" in result.honesty_banner.lower()  # distinct from product eval


def test_foundry_arm_lookup_and_resolution_rate() -> None:
    frame = _frame()
    result = _clean(frame)
    foundry = result.arm_eval(Arm.FOUNDRY)
    assert foundry.arm is Arm.FOUNDRY
    assert 0.0 <= foundry.resolution_rate <= 1.0


# --------------------------------------------------------------------------- #
# integrity fail-closed: reachable hidden test / contamination => not valid
# --------------------------------------------------------------------------- #


def test_reachable_hidden_test_invalidates_eval() -> None:
    frame = _frame()
    scored = _scored_task_ids(frame)
    victim = scored[0]
    oracle = frame.fixture(victim).oracle
    # The agent could reach the held-out token at runtime — overfitting, not
    # generalization. §6 mechanism 1: this INVALIDATES the eval (fail-closed).
    surfaces = {
        tid: AgentRuntimeSurface(task_id=tid) for tid in scored  # isolated default
    }
    surfaces[victim] = AgentRuntimeSurface(
        task_id=victim,
        reachable_refs=frozenset({oracle.hidden_test_id, oracle.expected_output}),
    )
    result = _clean(frame, surfaces=surfaces)

    assert result.integrity.clean is False
    assert result.integrity.violations_of(ViolationKind.REACHABLE_HIDDEN_TEST)
    assert result.integrity_clean is False
    assert result.valid is False  # fail-closed invalidation
    assert result.publishable is False


def test_contaminated_input_invalidates_eval() -> None:
    frame = _frame()
    scored = _scored_task_ids(frame)
    victim = scored[0]
    oracle = frame.fixture(victim).oracle
    # The oracle answer leaked into the agent's inputs — the agent could
    # pattern-match rather than generalize. §6 mechanism 3: INVALIDATES.
    manifests = {
        victim: TaskInputManifest(
            task_id=victim,
            input_texts=(f"context blob {oracle.expected_output} trailing",),
        )
    }
    result = _clean(frame, manifests=manifests)

    assert result.integrity.violations_of(ViolationKind.CONTAMINATED_TASK)
    assert result.valid is False


# --------------------------------------------------------------------------- #
# safeties fail-closed: reward-hack / eval-lie / nondeterminism => not valid
# --------------------------------------------------------------------------- #


def test_reward_hacking_finding_invalidates_eval() -> None:
    frame = _frame()
    # An oracle-grounded discriminator that FAILED (held-out mutants survived) —
    # the suite is green but tested nothing real. §10: INVALIDATES.
    bad_disc = DiscriminatorReport(
        passed=False,
        kill_score=0.0,
        required_kill_ratio=1.0,
        mutants_total=3,
        mutants_killed=0,
        survived=("m1", "m2", "m3"),
    )
    result = _clean(frame, discriminators={Arm.FOUNDRY: bad_disc})

    foundry = result.arm_eval(Arm.FOUNDRY)
    assert foundry.safety.findings_of(SafetyKind.REWARD_HACKING)
    assert result.safeties_clean is False
    assert result.valid is False


def test_eval_lie_finding_invalidates_eval() -> None:
    frame = _frame()
    # Drive the FOUNDRY runner to a KNOWN non-resolution (produced != expected),
    # then plant a self-report claiming success the oracle contradicts. §10 eval-
    # lie: oracle wins, the lie INVALIDATES.
    def _missing_runner(fixture: object, repeat: int) -> tuple[bool, str | None]:
        return False, None  # never completes => oracle scores unresolved

    scored = _scored_task_ids(frame)
    # Claim success on every (task, repeat=0) — the oracle scored them unresolved.
    lies = {(tid, 0): True for tid in scored}
    result = _clean(
        frame,
        foundry_runner=_missing_runner,
        self_reports={Arm.FOUNDRY: lies},
    )

    foundry = result.arm_eval(Arm.FOUNDRY)
    assert foundry.safety.findings_of(SafetyKind.EVAL_LIE)
    assert result.valid is False


def test_nondeterminism_finding_invalidates_eval() -> None:
    frame = _frame()
    scored = _scored_task_ids(frame)
    victim = scored[0]
    # Two same-seed "re-runs" that DISAGREE on an outcome — flaky, untrustworthy.
    # §10 nondeterminism: INVALIDATES. We build the divergent re-run pair directly
    # and inject it as the FOUNDRY safety input.
    from foundry.evaluation.harness import ArmResult

    def _outcome(resolved: bool) -> TaskOutcome:
        return TaskOutcome(
            task_id=victim,
            repeat=0,
            arm=Arm.FOUNDRY,
            status=OutcomeStatus.RESOLVED if resolved else OutcomeStatus.UNRESOLVED,
            resolved=resolved,
            cost_microusd=1_000,
            expected_output="ok",
            produced_output="ok" if resolved else None,
        )

    def _run(resolved: bool) -> ArmResult:
        return ArmResult(
            arm=Arm.FOUNDRY,
            slice_identity="slice-flaky",
            slice_name="flaky",
            k_repeats=1,
            seed=0,
            budget_usd_microusd=5_000_000,
            attempted=1,
            outcomes=(_outcome(resolved),),
            simulated=False,
        )

    result = _clean(frame, safety_inputs={Arm.FOUNDRY: [_run(True), _run(False)]})

    foundry = result.arm_eval(Arm.FOUNDRY)
    assert foundry.safety.findings_of(SafetyKind.NONDETERMINISM)
    assert result.valid is False


# --------------------------------------------------------------------------- #
# determinism + non-vacuity
# --------------------------------------------------------------------------- #


def test_determinism_same_seed_identical_result() -> None:
    frame = _frame()
    a = _clean(frame, seed=7)
    b = _clean(frame, seed=7)
    # Identical content (the frozen-model content digest is the determinism witness).
    assert a.content_identity() == b.content_identity()
    assert a.model_dump() == b.model_dump()


def test_non_vacuity_invalidating_cases_flip_valid() -> None:
    """The clean run is valid; each invalidating case flips it (non-vacuous)."""
    frame = _frame()
    clean = _clean(frame)
    assert clean.valid is True

    scored = _scored_task_ids(frame)
    victim = scored[0]
    oracle = frame.fixture(victim).oracle

    # integrity flip
    dirty_surfaces = {tid: AgentRuntimeSurface(task_id=tid) for tid in scored}
    dirty_surfaces[victim] = AgentRuntimeSurface(
        task_id=victim, reachable_refs=frozenset({oracle.hidden_test_id})
    )
    integrity_flipped = _clean(frame, surfaces=dirty_surfaces)
    assert integrity_flipped.valid is False

    # safety flip
    bad_disc = DiscriminatorReport(
        passed=False,
        kill_score=0.0,
        required_kill_ratio=1.0,
        mutants_total=1,
        mutants_killed=0,
        survived=("m1",),
    )
    safety_flipped = _clean(frame, discriminators={Arm.FOUNDRY: bad_disc})
    assert safety_flipped.valid is False

    # The flips are genuine: same frame, only the invalidating input changed.
    assert {clean.valid, integrity_flipped.valid, safety_flipped.valid} == {True, False}


# --------------------------------------------------------------------------- #
# alpha ledger is bound READ-ONLY (§6.5)
# --------------------------------------------------------------------------- #


class _SpyLedger:
    """A spy LedgerView: records reads, and ASSERTS write methods are never called."""

    def __init__(self, looks: int) -> None:
        self._looks = looks
        self.read_calls: list[tuple[str, str]] = []

    def alpha_looks_spent(self, eval_set_identity: str, decision_id: str) -> int:
        self.read_calls.append((eval_set_identity, decision_id))
        return self._looks

    # The write methods exist so a misuse would be loud — but the aggregator
    # depends on the read-only LedgerView Protocol and must never reach these.
    def record_alpha_look(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("factory_eval must NEVER re-spend alpha (§6.5)")

    def try_first_alpha_look(self, *args: object, **kwargs: object) -> bool:
        raise AssertionError("factory_eval must NEVER re-spend alpha (§6.5)")


def test_alpha_ledger_bound_read_only() -> None:
    frame = _frame()
    ledger = _SpyLedger(looks=2)
    result = _clean(frame, ledger=ledger, decision_id="d-1")

    # The aggregator READ the look-count once, bound to the frame's eval-set id.
    assert ledger.read_calls == [(frame.eval_set_identity, "d-1")]
    assert result.alpha_looks_spent == 2
    # No ledger means no observed look-count (None), never a fabricated 0.
    no_ledger = _clean(frame)
    assert no_ledger.alpha_looks_spent is None


# --------------------------------------------------------------------------- #
# default agent-validity path (the real per-role checks, no injection)
# --------------------------------------------------------------------------- #


def test_default_agent_validity_path_runs_real_checks() -> None:
    """Without an injected report, the §4 per-role planted-defect checks run."""
    frame = _frame()
    result = run_factory_eval(frame)  # no agent_validity override
    # The default factory catches every planted defect => all roles valid.
    assert result.agent_validity.all_valid
    assert result.valid is True
    # Matches the standalone report (composition reuses the module verbatim).
    standalone = agent_validity_report()
    assert result.agent_validity.validity_score == standalone.validity_score
