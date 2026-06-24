"""Eval safeties — the checks that protect the SCORECARD from being gamed.

A scorecard is only worth publishing if the eval that produced it cannot be
quietly gamed. This module is the **fail-closed** defense over the structured
eval signals the harness (:mod:`foundry.evaluation.harness`) and the gamed-suite
discriminator (:mod:`foundry.gates.discriminator`) already emit. It detects the
three standard ways an agent eval silently lies (docs/evaluation.md §10) and
**invalidates** any affected result so it can never reach the scorecard as clean:

* **reward-hacking** — a result that improves the METRIC without genuinely doing
  the work: a suite that is "green" but games the metric (surviving held-out
  mutants / a kill score below the bar / violated behavioral invariants — the
  discriminator's oracle-grounded signal). The metric (a green pytest run, a
  resolved task) moved without the underlying work being done. A LIE can't fool
  the ORACLE, so we ground the check on the discriminator, never on the suite's
  own green bit.
* **eval-lie** — an agent SELF-REPORT of success the ORACLE/ground-truth
  contradicts. The eval trusts the oracle, NEVER the self-report: this module
  asserts the self-report channel can never set the score. A self-reported
  "resolved" the oracle scored unresolved (``produced_output != expected_output``)
  is flagged; the oracle wins.
* **nondeterminism** — a result that CHANGES across re-runs with the SAME
  seed/inputs (flaky). A non-reproducible eval can't be trusted to score — the
  ``k``-run averaging (§7) assumes per-run reproducibility *given a fixed seed*,
  so a seed-stable re-run that disagrees is untrustworthy.

FAIL-CLOSED (load-bearing): any safety violation INVALIDATES the affected result
— :attr:`EvalSafetyReport.clean` is the AND of every check, and a violated report
must never be published to the scorecard as clean. The checks are
**pure/deterministic** over the structured signals — no RNG, no IO, no model
calls — so the safety verdict is itself reproducible (a flaky safety check could
not be trusted to catch a flaky eval).

HONESTY NOTE: these checks operate over the DRY-RUN synthetic eval signals like
everything else in Phase-1.5; they assert NOTHING about real Foundry-vs-baseline
performance — they only protect the eval's INTEGRITY.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from foundry.evaluation.harness import ArmResult, TaskOutcome
    from foundry.gates.discriminator import DiscriminatorReport

__all__ = [
    "EvalSafetyReport",
    "SafetyFinding",
    "SafetyKind",
    "check_arm_safeties",
    "detect_eval_lies",
    "detect_nondeterminism",
    "detect_reward_hacking",
]


class SafetyKind(StrEnum):
    """The three eval-safety violation classes (docs/evaluation.md §10)."""

    REWARD_HACKING = "reward_hacking"  # metric moved without the work being done
    EVAL_LIE = "eval_lie"  # self-report contradicted by the oracle
    NONDETERMINISM = "nondeterminism"  # differs across same-seed re-runs


class SafetyFinding(FrozenModel):
    """One eval-safety violation over a structured eval signal.

    A finding is, by construction, a fail-closed INVALIDATION: its mere presence
    on an :class:`EvalSafetyReport` flips ``clean`` to False. ``subject`` names the
    affected unit (a ``task_id`` for a per-task lie/nondeterminism, or a suite
    identity for reward-hacking) so the invalidation can be traced.
    """

    kind: SafetyKind
    subject: str  # task_id / suite identity the violation attaches to
    detail: str  # human-readable root cause (for audit)
    version: int = 1


class EvalSafetyReport(FrozenModel):
    """The fail-closed safety verdict over a set of eval signals.

    ``clean`` is the AND of every check — it is True IFF there are NO findings, so
    ANY violation invalidates the result and it can never be published to the
    scorecard as clean (docs/evaluation.md §10). Pure/deterministic: the same
    signals always yield the same frozen report.
    """

    subject: str  # the result this report scopes (arm/slice/suite identity)
    findings: tuple[SafetyFinding, ...] = ()
    version: int = 1

    @property
    def clean(self) -> bool:
        """True IFF no safety violation was found (fail-closed: any finding ⇒ dirty)."""
        return not self.findings

    @property
    def invalidated(self) -> bool:
        """The fail-closed complement of :attr:`clean` (a violation invalidates)."""
        return bool(self.findings)

    def findings_of(self, kind: SafetyKind) -> tuple[SafetyFinding, ...]:
        """All findings of one :class:`SafetyKind` (for targeted assertions)."""
        return tuple(f for f in self.findings if f.kind is kind)


# --------------------------------------------------------------------------- #
# reward-hacking: the metric improved without the work being done.
# --------------------------------------------------------------------------- #
def detect_reward_hacking(
    report: DiscriminatorReport, *, subject: str
) -> tuple[SafetyFinding, ...]:
    """Flag a metric-gaming suite from the ORACLE-GROUNDED discriminator signal.

    Reward-hacking is improving the METRIC without doing the work: a pytest suite
    that is "green" (the metric the scorecard reads) but games it — the
    discriminator's held-out mutants SURVIVE (the suite tested nothing real) or a
    behavioral invariant the spec implies is VIOLATED. We ground the check on the
    discriminator's oracle signal — NOT the suite's own green bit — because a LIE
    can't fool the oracle: a gamed suite passes its own tests by construction, so
    only an independent oracle (held-out mutants + invariants the agent could not
    see coming) can separate genuine work from metric-gaming.

    A genuine result (``report.passed``) yields NO finding. A failing
    discriminator yields one :class:`SafetyKind.REWARD_HACKING` finding per
    distinct evidence (surviving mutants, a sub-bar kill score, violated
    invariants), so the invalidation is fully traced. Pure/deterministic.
    """
    findings: list[SafetyFinding] = []
    if report.passed:
        return ()

    if report.survived:
        findings.append(
            SafetyFinding(
                kind=SafetyKind.REWARD_HACKING,
                subject=subject,
                detail=(
                    f"held-out mutants SURVIVED ({len(report.survived)} of "
                    f"{report.mutants_total}: {', '.join(report.survived)}) — the suite "
                    "is green but tested nothing real (metric gamed)"
                ),
            )
        )
    elif report.kill_score < report.required_kill_ratio:
        # A sub-bar kill score with no named survivor (e.g. zero mutants run but a
        # positive bar) is still metric-gaming evidence — fail closed.
        findings.append(
            SafetyFinding(
                kind=SafetyKind.REWARD_HACKING,
                subject=subject,
                detail=(
                    f"kill score {report.kill_score:.3f} below required "
                    f"{report.required_kill_ratio:.3f} — weak/gamed suite"
                ),
            )
        )

    if report.invariants_violated:
        findings.append(
            SafetyFinding(
                kind=SafetyKind.REWARD_HACKING,
                subject=subject,
                detail=(
                    f"behavioral invariants VIOLATED ({len(report.invariants_violated)}: "
                    f"{', '.join(report.invariants_violated)}) — the produced code is "
                    "wrong though the suite is green (metric gamed)"
                ),
            )
        )

    if not findings:
        # The discriminator failed for a reason not itemized above — fail closed
        # rather than let a non-passing oracle signal slip through as clean.
        findings.append(
            SafetyFinding(
                kind=SafetyKind.REWARD_HACKING,
                subject=subject,
                detail="discriminator did not pass (metric not oracle-grounded) — invalidated",
            )
        )
    return tuple(findings)


# --------------------------------------------------------------------------- #
# eval-lie: a self-report of success the oracle contradicts.
# --------------------------------------------------------------------------- #
def _oracle_resolved(outcome: TaskOutcome) -> bool:
    """The ORACLE verdict for one outcome: produced output EQUALS the target.

    This is the ground truth (the deterministic oracle equality of §4) — the eval
    trusts THIS, never a self-report. A ``None`` produced output (a crash before
    producing anything) can never equal the target, so it is unresolved.
    """
    return (
        outcome.produced_output is not None
        and outcome.produced_output == outcome.expected_output
    )


def detect_eval_lies(
    outcomes: Iterable[TaskOutcome], *, self_reports: dict[tuple[str, int], bool]
) -> tuple[SafetyFinding, ...]:
    """Flag any agent SELF-REPORT of success the ORACLE contradicts.

    The eval trusts the ORACLE, NEVER the self-report — this is the assertion that
    the self-report channel can never set the score. ``self_reports`` maps
    ``(task_id, repeat) -> claimed_resolved`` (the agent's OWN claim, an UNTRUSTED
    channel). For each outcome we re-derive the oracle verdict from the
    ground-truth (:func:`_oracle_resolved`) and flag a lie iff the agent CLAIMED
    success the oracle did NOT grant.

    A self-report ABSENT for an outcome is treated as no claim (no lie). A claim of
    FAILURE the oracle actually resolved is NOT a lie (the agent under-claimed —
    the oracle still wins and sets the score; no integrity violation). The check is
    deliberately asymmetric: it guards the SCORE-SETTING direction (a false success
    claim) because that is the channel that could inflate the metric.

    Pure/deterministic: no RNG/IO. One :class:`SafetyKind.EVAL_LIE` finding per
    contradicted outcome, keyed on the ``task_id``.
    """
    findings: list[SafetyFinding] = []
    for outcome in outcomes:
        key = (outcome.task_id, outcome.repeat)
        if key not in self_reports:
            continue
        claimed = self_reports[key]
        truth = _oracle_resolved(outcome)
        if claimed and not truth:
            findings.append(
                SafetyFinding(
                    kind=SafetyKind.EVAL_LIE,
                    subject=outcome.task_id,
                    detail=(
                        f"self-report claimed resolved on (task={outcome.task_id!r}, "
                        f"repeat={outcome.repeat}) but the ORACLE scored it unresolved "
                        f"(produced={outcome.produced_output!r} != expected="
                        f"{outcome.expected_output!r}) — oracle wins, self-report cannot "
                        "set the score"
                    ),
                )
            )
    return tuple(findings)


# --------------------------------------------------------------------------- #
# nondeterminism: differs across same-seed re-runs (flaky).
# --------------------------------------------------------------------------- #
def detect_nondeterminism(runs: Sequence[ArmResult]) -> tuple[SafetyFinding, ...]:
    """Flag any result that CHANGES across same-seed/same-input re-runs (flaky).

    ``runs`` are re-runs of the SAME arm over the SAME slice with the SAME seed
    (and same per-attempt budget). A trustworthy eval must be reproducible GIVEN a
    fixed seed (the §7 ``k``-run averaging assumes per-run reproducibility); a
    re-run that disagrees on ANY per-(task, repeat) outcome — its oracle
    ``resolved`` bit, its status, or its produced output — is flaky and CANNOT be
    trusted to score.

    A single run (or zero) is vacuously deterministic (no finding). The first run
    is the reference; each later run is compared per ``(task_id, repeat)`` against
    it, and a divergence yields one :class:`SafetyKind.NONDETERMINISM` finding for
    that unit. A re-run whose SET of attempts differs (a missing/extra unit) is
    itself a nondeterminism violation (the denominator moved).

    Pure/deterministic: comparing recorded outcomes is itself reproducible. The
    runs MUST share ``(arm, slice_identity, seed, budget_usd_microusd)`` (a guard
    raises otherwise — comparing apples to oranges would manufacture a false flake).
    """
    findings: list[SafetyFinding] = []
    if len(runs) < 2:
        return ()

    reference = runs[0]
    _assert_comparable(runs)
    ref_map = _outcome_map(reference)
    ref_keys = set(ref_map)

    for run in runs[1:]:
        run_map = _outcome_map(run)
        run_keys = set(run_map)

        # A moved attempt SET (missing/extra unit) is itself nondeterminism —
        # the ITT denominator changed across a same-seed re-run.
        for key in sorted(ref_keys - run_keys):
            findings.append(_missing_unit_finding(key, present_in_reference=True))
        for key in sorted(run_keys - ref_keys):
            findings.append(_missing_unit_finding(key, present_in_reference=False))

        for key in sorted(ref_keys & run_keys):
            diff = _outcome_divergence(ref_map[key], run_map[key])
            if diff is not None:
                task_id, _repeat = key
                findings.append(
                    SafetyFinding(
                        kind=SafetyKind.NONDETERMINISM,
                        subject=task_id,
                        detail=diff,
                    )
                )
    return tuple(findings)


def _assert_comparable(runs: Sequence[ArmResult]) -> None:
    """Guard: re-runs must share (arm, slice, seed, budget) to be comparable."""
    first = runs[0]
    key = (first.arm, first.slice_identity, first.seed, first.budget_usd_microusd)
    for run in runs[1:]:
        other = (run.arm, run.slice_identity, run.seed, run.budget_usd_microusd)
        if other != key:
            msg = (
                "nondeterminism check requires same-seed/same-input re-runs; got "
                f"{key!r} vs {other!r} — comparing different runs would manufacture "
                "a false flake"
            )
            raise ValueError(msg)


def _outcome_map(run: ArmResult) -> dict[tuple[str, int], TaskOutcome]:
    """Index a run's outcomes by ``(task_id, repeat)`` (the per-attempt unit)."""
    return {(o.task_id, o.repeat): o for o in run.outcomes}


def _outcome_divergence(ref: TaskOutcome, other: TaskOutcome) -> str | None:
    """The per-attempt divergence string, or None if the two agree.

    Compares the score-bearing fields: the oracle ``resolved`` bit, the terminal
    ``status``, and the ``produced_output`` — any disagreement means the same-seed
    re-run is flaky and the result can't be trusted to score.
    """
    if ref.resolved != other.resolved:
        return (
            f"same-seed re-run flipped resolved on (task={ref.task_id!r}, "
            f"repeat={ref.repeat}): {ref.resolved} -> {other.resolved} (flaky — untrustworthy)"
        )
    if ref.status != other.status:
        return (
            f"same-seed re-run changed status on (task={ref.task_id!r}, "
            f"repeat={ref.repeat}): {ref.status.value} -> {other.status.value} (flaky)"
        )
    if ref.produced_output != other.produced_output:
        return (
            f"same-seed re-run changed produced output on (task={ref.task_id!r}, "
            f"repeat={ref.repeat}): {ref.produced_output!r} -> {other.produced_output!r} (flaky)"
        )
    return None


def _missing_unit_finding(
    key: tuple[str, int], *, present_in_reference: bool
) -> SafetyFinding:
    """A finding for an attempt unit present in one re-run but not another."""
    task_id, repeat = key
    where = "the reference run only" if present_in_reference else "a later re-run only"
    return SafetyFinding(
        kind=SafetyKind.NONDETERMINISM,
        subject=task_id,
        detail=(
            f"attempt (task={task_id!r}, repeat={repeat}) present in {where} — the "
            "same-seed attempt set is non-reproducible (denominator moved, untrustworthy)"
        ),
    )


# --------------------------------------------------------------------------- #
# the combined, fail-closed safety verdict over an arm result.
# --------------------------------------------------------------------------- #
def check_arm_safeties(
    runs: Sequence[ArmResult],
    *,
    self_reports: dict[tuple[str, int], bool] | None = None,
    discriminator: DiscriminatorReport | None = None,
) -> EvalSafetyReport:
    """Run all three eval safeties over an arm's re-runs, FAIL-CLOSED.

    ``runs`` are one-or-more re-runs of the SAME arm/slice/seed (the first is the
    canonical result; >= 2 enables the nondeterminism check). The verdict is the
    AND of:

    * **nondeterminism** over the re-run set (:func:`detect_nondeterminism`);
    * **eval-lie** over the canonical run's outcomes vs the optional UNTRUSTED
      ``self_reports`` (:func:`detect_eval_lies`);
    * **reward-hacking** over the optional oracle-grounded ``discriminator`` report
      (:func:`detect_reward_hacking`).

    ANY finding INVALIDATES the result (``report.clean is False``) so it can never
    be published to the scorecard as clean (docs/evaluation.md §10). Omitted inputs
    (no ``self_reports`` / no ``discriminator``) simply contribute no findings —
    they do NOT fabricate a pass; the present signals still fail closed.

    Pure/deterministic: same inputs ⇒ identical frozen :class:`EvalSafetyReport`.
    """
    if not runs:
        msg = "check_arm_safeties requires at least one ArmResult to scope the report"
        raise ValueError(msg)

    canonical = runs[0]
    subject = f"{canonical.arm.value}:{canonical.slice_identity}"
    findings: list[SafetyFinding] = []

    findings.extend(detect_nondeterminism(runs))

    if self_reports:
        findings.extend(detect_eval_lies(canonical.outcomes, self_reports=self_reports))

    if discriminator is not None:
        findings.extend(detect_reward_hacking(discriminator, subject=subject))

    return EvalSafetyReport(subject=subject, findings=tuple(findings))
