"""Eval-integrity mechanisms — fail-closed (evaluation.md §6).

An outcome suite's resolution rate only MEANS generalization if the eval was
clean: the hidden tests must have been genuinely held out, the ITT rate must be
reported unconditionally, and no task/oracle must have leaked into the producing
agent's inputs. This module is the **integrity check** that decides whether an
eval may be reported as CLEAN — and it is **fail-closed**: any violation
INVALIDATES the eval. A reachable hidden test or a contaminated task means the
number cannot be reported as evidence, full stop.

The three §6 mechanisms, each fail-closed:

1. **Hidden tests are provably runtime-UNREACHABLE** (§6 / AI8): a held-out test
   must live in a scoring harness the *producing* agent's sandbox cannot read or
   reach (no shared FS, no returned logs), applied only AFTER the run terminates.
   :func:`assert_held_out_unreachable` checks each hidden test against the agent's
   recorded RUNTIME-REACHABLE surface (the paths/uris/log streams the agent could
   read). A hidden test that intersects that surface is a violation — the eval
   measures overfitting, not generalization, so it is INVALID.

2. **ITT pass-rate reported UNCONDITIONALLY** (§6 / AI-R9-F7): conditioning the
   hidden-test pass rate on "self-passing runs" is collider bias that differs by
   arm. :func:`assert_itt_unconditional` asserts the reported denominator is ALL
   initiated attempts (never the self-passing subset) — a conditioned denominator
   is a violation.

3. **Contamination probes** (§6 / AI8): canary / oracle-leak checks. If a task's
   oracle target, hidden-test id, or canary token appears in the agent's recorded
   inputs, the task is contaminated and the eval is INVALID.

Fail-closed contract: :func:`check_eval_integrity` returns an
:class:`IntegrityReport` whose ``clean`` bit is True ONLY when zero violations are
found. A caller MUST refuse to report a non-``clean`` eval as evidence —
:func:`require_clean` raises :class:`EvalIntegrityError` to enforce it at the
boundary.

HONESTY NOTE: synthetic dry-run fixtures; an integrity report asserts the
INTEGRITY of the eval mechanism, not that the underlying numbers are real
evidence (``provenance`` defaults to DRY_RUN).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.hypothesis import Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.harness import ArmResult
    from foundry.evaluation.suites import SuiteResult
    from foundry.evaluation.task_fixtures import OracleDescriptor

__all__ = [
    "AgentRuntimeSurface",
    "EvalIntegrityError",
    "IntegrityReport",
    "IntegrityViolation",
    "TaskInputManifest",
    "ViolationKind",
    "assert_held_out_unreachable",
    "assert_itt_unconditional",
    "check_eval_integrity",
    "contamination_probe",
    "require_clean",
]


class ViolationKind(StrEnum):
    """The §6 integrity-violation classes. ANY one fail-closes the eval."""

    REACHABLE_HIDDEN_TEST = "reachable_hidden_test"  # held-out test was runtime-reachable (§6/AI8)
    CONDITIONED_ITT = "conditioned_itt"  # ITT denominator was conditioned (collider bias — §6)
    CONTAMINATED_TASK = "contaminated_task"  # oracle/canary leaked into agent inputs (§6/AI8)
    MISSING_OUTCOME = "missing_outcome"  # an initiated attempt has no retained outcome (§6 ITT)


class AgentRuntimeSurface(FrozenModel):
    """What the PRODUCING agent could read/reach at runtime, for one task/run (§6).

    The integrity check asserts the held-out set is DISJOINT from this surface: a
    hidden test whose id/uri/path appears here was reachable, so "passes hidden
    tests" measures overfitting, not generalization. Populated from the policy
    plane's record of the sandbox's actual read surface (no shared FS, no returned
    logs is the INVARIANT this proves).

    SYNTHETIC: a dry-run record of the reachable surface.
    """

    task_id: str
    # Every identifier the agent's sandbox could read/reach at runtime — file
    # paths, CAS uris, returned log stream ids, test ids visible in the worktree:
    reachable_refs: frozenset[str] = Field(default_factory=frozenset)
    synthetic: bool = True
    version: int = 1


class TaskInputManifest(FrozenModel):
    """The exact inputs handed to the producing agent for one task (§6 contamination).

    The contamination probe asserts NONE of the task's oracle target / hidden-test
    id / canary token appears in these inputs. A leak means the agent could have
    pattern-matched the answer rather than generalized — the task is contaminated
    and the eval is INVALID.

    SYNTHETIC: a dry-run record of the agent's inputs.
    """

    task_id: str
    # Verbatim input strings handed to the agent (spec/prompt/context/retrieval):
    input_texts: tuple[str, ...] = Field(default_factory=tuple)
    # Canary tokens planted to detect verbatim oracle leakage (§6 canary probe):
    canary_tokens: frozenset[str] = Field(default_factory=frozenset)
    synthetic: bool = True
    version: int = 1


class IntegrityViolation(FrozenModel):
    """ONE fail-closed integrity violation (evaluation.md §6).

    Carries the :class:`ViolationKind`, the offending ``task_id`` (or ``None`` for
    an eval-wide violation like a conditioned ITT denominator), and a terse
    auditable ``detail``. The mere PRESENCE of one violation flips an
    :class:`IntegrityReport` to not-``clean`` (fail-closed).
    """

    kind: ViolationKind
    task_id: str | None
    detail: str
    version: int = 1


class IntegrityReport(FrozenModel):
    """The fail-closed verdict on an eval's integrity (evaluation.md §6).

    ``clean`` is True IFF ``violations`` is empty — an eval may be reported as
    evidence ONLY when clean. ``report_id`` is content-addressed (L7) over the
    violations so an identical integrity state yields an identical id. The report
    is the boundary object: a caller gates reporting on ``clean`` (or calls
    :func:`require_clean`, which raises on a dirty report).

    HONESTY: ``provenance`` defaults to DRY_RUN — the report attests the eval's
    INTEGRITY MECHANISM, not that the underlying numbers are funded evidence.
    """

    violations: tuple[IntegrityViolation, ...]
    n_tasks_checked: int = Field(ge=0)
    report_id: str  # content_digest over the violations — L7
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    @property
    def clean(self) -> bool:
        """The eval is reportable IFF there are ZERO integrity violations (fail-closed)."""
        return len(self.violations) == 0

    def violations_of(self, kind: ViolationKind) -> tuple[IntegrityViolation, ...]:
        """All violations of a given kind (audit helper)."""
        return tuple(v for v in self.violations if v.kind is kind)


class EvalIntegrityError(RuntimeError):
    """Raised by :func:`require_clean` when an eval is reported despite a violation.

    Fail-closed enforcement at the reporting boundary: a non-``clean`` eval can
    NEVER be silently reported as evidence.
    """

    def __init__(self, report: IntegrityReport) -> None:
        kinds = sorted({v.kind.value for v in report.violations})
        super().__init__(
            f"eval integrity violated ({len(report.violations)} violation(s): "
            f"{', '.join(kinds)}) — the eval is INVALID and cannot be reported as clean"
        )
        self.report = report


# --------------------------------------------------------------------------- #
# Mechanism 1 — hidden tests provably runtime-UNREACHABLE (§6 / AI8)
# --------------------------------------------------------------------------- #
def assert_held_out_unreachable(
    oracles: dict[str, OracleDescriptor],
    surfaces: dict[str, AgentRuntimeSurface],
) -> list[IntegrityViolation]:
    """Check each task's hidden test is DISJOINT from the agent's reachable surface.

    For every ``(task_id, oracle)`` the hidden-test id (and its expected-output
    token) MUST NOT appear in that task's :class:`AgentRuntimeSurface.reachable_refs`
    — otherwise the held-out test was runtime-reachable and the eval measures
    overfitting, not generalization (§6 / AI8). Each intersection is a
    :class:`ViolationKind.REACHABLE_HIDDEN_TEST` violation.

    Fail-closed default: a task with NO recorded surface is treated as a violation
    (we cannot PROVE unreachability from a missing record — absence of evidence is
    not evidence of isolation). So the policy plane MUST record the surface for
    every scored task.
    """
    violations: list[IntegrityViolation] = []
    for task_id, oracle in oracles.items():
        surface = surfaces.get(task_id)
        if surface is None:
            violations.append(
                IntegrityViolation(
                    kind=ViolationKind.REACHABLE_HIDDEN_TEST,
                    task_id=task_id,
                    detail=(
                        "no recorded runtime surface — unreachability is UNPROVEN "
                        "(fail-closed: cannot certify the hidden test was held out)"
                    ),
                )
            )
            continue
        # The held-out tokens the agent must not have been able to reach:
        held_out = {oracle.hidden_test_id, oracle.expected_output}
        reachable_hits = held_out & surface.reachable_refs
        if reachable_hits:
            violations.append(
                IntegrityViolation(
                    kind=ViolationKind.REACHABLE_HIDDEN_TEST,
                    task_id=task_id,
                    detail=(
                        f"held-out token(s) {sorted(reachable_hits)} were RUNTIME-REACHABLE "
                        f"to the producing agent — the hidden test is not held out"
                    ),
                )
            )
    return violations


# --------------------------------------------------------------------------- #
# Mechanism 2 — ITT pass-rate reported UNCONDITIONALLY (§6 / AI-R9-F7)
# --------------------------------------------------------------------------- #
def assert_itt_unconditional(arm_result: ArmResult) -> list[IntegrityViolation]:
    """Assert the reported denominator is ALL initiated attempts, never a subset.

    Conditioning the hidden-test pass rate on "self-passing runs" is collider bias
    that differs by arm (§6 / AI-R9-F7). The ITT denominator MUST be every
    initiated attempt (``arm_result.attempted``), and every initiated attempt MUST
    have a retained outcome — a dropped crash/over-budget attempt would silently
    condition the denominator. A mismatch is a fail-closed violation.

    Returns a :class:`ViolationKind.MISSING_OUTCOME` violation if outcomes were
    dropped, and a :class:`ViolationKind.CONDITIONED_ITT` violation if the
    denominator excludes any retained non-resolved outcome (i.e. someone filtered
    to self-passing). On a well-formed ITT result this returns ``[]``.
    """
    violations: list[IntegrityViolation] = []

    # (a) Every initiated attempt must have a retained outcome — no dropped sample.
    if arm_result.attempted != len(arm_result.outcomes):
        violations.append(
            IntegrityViolation(
                kind=ViolationKind.MISSING_OUTCOME,
                task_id=None,
                detail=(
                    f"denominator {arm_result.attempted} != {len(arm_result.outcomes)} "
                    f"retained outcomes — an initiated attempt was dropped (ITT broken)"
                ),
            )
        )

    # (b) The denominator must NOT have been conditioned to the resolved subset.
    # If attempted equals the resolved count while unresolved outcomes exist, the
    # rate was conditioned on self-passing (collider bias). This catches the case
    # where a caller passes a denominator equal to the numerator.
    unresolved = [o for o in arm_result.outcomes if not o.resolved]
    if unresolved and arm_result.attempted == arm_result.resolved_count:
        violations.append(
            IntegrityViolation(
                kind=ViolationKind.CONDITIONED_ITT,
                task_id=None,
                detail=(
                    "ITT denominator equals the resolved count while unresolved attempts "
                    "exist — the rate was conditioned on self-passing (collider bias)"
                ),
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# Mechanism 3 — contamination probes (§6 / AI8)
# --------------------------------------------------------------------------- #
def contamination_probe(
    oracles: dict[str, OracleDescriptor],
    manifests: dict[str, TaskInputManifest],
) -> list[IntegrityViolation]:
    """Detect oracle / canary leakage into the producing agent's inputs (§6 / AI8).

    For every task, the oracle's **expected-output token** (the ANSWER) plus every
    planted canary token MUST NOT appear (as a substring) in any of the agent's
    input texts. A hit means the agent could pattern-match the answer rather than
    generalize — the task is CONTAMINATED and the eval is INVALID.

    NB: the hidden-test *id* is deliberately NOT probed — a task spec legitimately
    NAMES the hidden test it will be scored by (a reference, not the answer); the
    synthetic frame does exactly this. Leaking the test's NAME is not leaking its
    EXPECTED OUTPUT. Only the answer token + canaries are contamination signals.

    A task with an oracle but NO input manifest is NOT flagged here (the
    contamination probe can only test recorded inputs); reachability isolation is
    Mechanism 1's job. Returns one
    :class:`ViolationKind.CONTAMINATED_TASK` violation per contaminated task.
    """
    violations: list[IntegrityViolation] = []
    for task_id, oracle in oracles.items():
        manifest = manifests.get(task_id)
        if manifest is None:
            continue
        probes = {oracle.expected_output} | set(manifest.canary_tokens)
        leaked: set[str] = set()
        for probe in probes:
            if not probe:
                continue
            for text in manifest.input_texts:
                if probe in text:
                    leaked.add(probe)
                    break
        if leaked:
            violations.append(
                IntegrityViolation(
                    kind=ViolationKind.CONTAMINATED_TASK,
                    task_id=task_id,
                    detail=(
                        f"oracle/canary token(s) {sorted(leaked)} leaked into the agent's "
                        f"inputs — the task is contaminated"
                    ),
                )
            )
    return violations


# --------------------------------------------------------------------------- #
# Aggregate — fail-closed eval-integrity verdict
# --------------------------------------------------------------------------- #
def check_eval_integrity(
    *,
    oracles: dict[str, OracleDescriptor],
    surfaces: dict[str, AgentRuntimeSurface],
    manifests: dict[str, TaskInputManifest],
    arm_results: tuple[ArmResult, ...] = (),
) -> IntegrityReport:
    """Run all three §6 integrity mechanisms and return the fail-closed verdict.

    Aggregates:

    1. :func:`assert_held_out_unreachable` (hidden tests runtime-unreachable),
    2. :func:`assert_itt_unconditional` over every ``arm_result`` (ITT
       unconditional / no dropped sample),
    3. :func:`contamination_probe` (oracle/canary leak detection).

    The report's ``clean`` bit is True IFF there are ZERO violations across all
    three — fail-closed. A genuinely held-out + uncontaminated + unconditional-ITT
    eval is ``clean``; a single reachable hidden test, contaminated task, or
    conditioned/dropped ITT sample INVALIDATES it.
    """
    violations: list[IntegrityViolation] = []
    violations.extend(assert_held_out_unreachable(oracles, surfaces))
    violations.extend(contamination_probe(oracles, manifests))
    for arm_result in arm_results:
        violations.extend(assert_itt_unconditional(arm_result))

    report_id = content_digest(
        {
            "violations": [v.model_dump(mode="json") for v in violations],
            "n_tasks": len(oracles),
        }
    )
    return IntegrityReport(
        violations=tuple(violations),
        n_tasks_checked=len(oracles),
        report_id=report_id,
    )


def check_suite_integrity(
    suite_result: SuiteResult,
    *,
    oracles: dict[str, OracleDescriptor],
    surfaces: dict[str, AgentRuntimeSurface],
    manifests: dict[str, TaskInputManifest],
) -> IntegrityReport:
    """Convenience: run :func:`check_eval_integrity` for ONE :class:`SuiteResult`.

    Threads the suite's wrapped :class:`ArmResult` into the ITT-unconditional
    check so a suite cannot be reported clean unless its hidden tests were
    unreachable, its tasks uncontaminated, AND its ITT denominator was honest.
    """
    return check_eval_integrity(
        oracles=oracles,
        surfaces=surfaces,
        manifests=manifests,
        arm_results=(suite_result.arm_result,),
    )


def require_clean(report: IntegrityReport) -> IntegrityReport:
    """Fail-closed boundary: raise :class:`EvalIntegrityError` if not ``clean``.

    Call this wherever an eval result is about to be REPORTED as evidence — a
    non-clean eval can never pass this gate, so an integrity violation can never be
    silently reported. Returns the report unchanged when clean (so it composes).
    """
    if not report.clean:
        raise EvalIntegrityError(report)
    return report
