"""Agent-specific VALIDITY evals (evaluation.md §4) — T-M6-03.

Drives the REAL role agents (a real :class:`ModelProviderProxy` over the existing
test doubles — LEARNINGS D1/D4, no live API key) against fixtures carrying a
KNOWN planted defect/omission and asserts each per-role validity check:

* is NON-VACUOUS — a role agent that CATCHES its planted defect scores valid; one
  that MISSES it scores invalid (the eval distinguishes them);
* aggregates into an :class:`AgentValidityReport` (the ``factory_eval`` slice);
* is DISTINCT from the outcome eval (``validity ⊥ outcome``): a pipeline can
  resolve while a contribution is invalid, and vice versa — validity is read off
  the agent's OWN contribution, never a pipeline-resolution bit;
* is DETERMINISTIC over the structured fixtures.

HONESTY: synthetic dry-run — the planted defects are hand-authored, so the report
asserts NOTHING about real agent quality (``provenance == DRY_RUN``); it only
proves the per-role validity CHECK is real and non-vacuous.
"""

from __future__ import annotations

import pytest

from foundry.evaluation.agent_validity import (
    AgentValidityReport,
    RoleValidity,
    ValidityRole,
    agent_validity_report,
    criteria_critic_validity,
    qa_engineer_validity,
    reviewer_critic_validity,
)
from foundry.evaluation.harness import ArmResult, OutcomeStatus, TaskOutcome, itt_pass_rate
from foundry.evaluation.hypothesis import Arm, Provenance

# ---- 1. each per-role check is NON-VACUOUS: catcher valid, misser invalid ----

_ROLE_CHECKS = [
    (ValidityRole.CRITERIA_CRITIC, criteria_critic_validity),
    (ValidityRole.REVIEWER_CRITIC, reviewer_critic_validity),
    (ValidityRole.QA_ENGINEER, qa_engineer_validity),
]


@pytest.mark.parametrize(("role", "check"), _ROLE_CHECKS)
def test_role_check_catches_planted_defect(role: ValidityRole, check) -> None:  # type: ignore[no-untyped-def]
    """A role agent that CATCHES its planted defect is scored valid."""
    rv = check(catches=True)
    assert isinstance(rv, RoleValidity)
    assert rv.role is role
    assert rv.valid is True
    assert rv.planted_defect  # the fixture states what was planted
    assert rv.provenance is Provenance.DRY_RUN


@pytest.mark.parametrize(("role", "check"), _ROLE_CHECKS)
def test_role_check_misses_planted_defect(role: ValidityRole, check) -> None:  # type: ignore[no-untyped-def]
    """A role agent that MISSES its planted defect is scored INVALID.

    Non-vacuity: the same check that scores the catcher valid scores the misser
    invalid — so a passing validity score is real signal, not a tautology.
    """
    rv = check(catches=False)
    assert rv.role is role
    assert rv.valid is False


def test_validity_distinguishes_catcher_from_misser() -> None:
    """The eval is non-vacuous for EVERY role: catch.valid != miss.valid."""
    for _role, check in _ROLE_CHECKS:
        assert check(catches=True).valid != check(catches=False).valid


# ---- 2. per-role detail: WHAT a valid contribution looks like ----------------


def test_criteria_critic_flags_security_omission_blocking() -> None:
    """A valid critic flags the omitted security/abuse criterion as Spec-blocking."""
    valid = criteria_critic_validity(catches=True)
    invalid = criteria_critic_validity(catches=False)
    assert "blocking=True" in valid.observed
    assert "blocking=False" in invalid.observed


def test_reviewer_request_changes_is_the_valid_contribution() -> None:
    """A valid reviewer returns a request-changes (FAIL) verdict on the defect;
    an approve MISSES it — validity is the reviewer's OWN verdict, not a merge."""
    assert "verdict=fail" in reviewer_critic_validity(catches=True).observed
    assert "verdict=pass" in reviewer_critic_validity(catches=False).observed


def test_qa_binds_criterion_traceable_negative_oracle() -> None:
    """A valid QA authors a criterion-traceable NEGATIVE oracle for the high-risk
    criterion (fidelity, not coverage); the miss case authors none."""
    valid = qa_engineer_validity(catches=True)
    invalid = qa_engineer_validity(catches=False)
    assert "=True" in valid.observed and "oracle_binding" in valid.observed
    assert "=False" in invalid.observed


# ---- 3. the report aggregates per-role validity ------------------------------


def test_report_aggregates_all_roles_valid() -> None:
    report = agent_validity_report()
    assert isinstance(report, AgentValidityReport)
    assert {r.role for r in report.roles} == set(ValidityRole)
    assert report.all_valid is True
    assert report.validity_score == 1.0
    assert report.provenance is Provenance.DRY_RUN


def test_report_flags_a_single_invalid_role() -> None:
    """One invalid contribution drops the score + ``all_valid`` even when the rest
    are valid — the report does not hide a single role's invalidity."""
    report = agent_validity_report(catches={ValidityRole.QA_ENGINEER: False})
    assert report.all_valid is False
    assert report.validity_score == pytest.approx(2 / 3)
    assert report.role_validity(ValidityRole.QA_ENGINEER).valid is False
    assert report.role_validity(ValidityRole.CRITERIA_CRITIC).valid is True
    assert report.role_validity(ValidityRole.REVIEWER_CRITIC).valid is True


def test_report_all_invalid_scores_zero() -> None:
    report = agent_validity_report(
        catches={role: False for role in ValidityRole}
    )
    assert report.validity_score == 0.0
    assert report.all_valid is False


def test_role_validity_lookup_raises_for_unevaluated_role() -> None:
    report = AgentValidityReport(
        roles=(criteria_critic_validity(catches=True),)
    )
    with pytest.raises(KeyError):
        report.role_validity(ValidityRole.REVIEWER_CRITIC)


# ---- 4. validity ⊥ outcome: a RESOLVED pipeline with an INVALID contribution --


def _resolved_arm_result() -> ArmResult:
    """A FOUNDRY :class:`ArmResult` whose every task RESOLVED (outcome = pass).

    Hand-built to stand in for a pipeline run that the outcome eval scores fully
    resolved — INDEPENDENTLY of any agent-contribution validity.
    """
    outcomes = tuple(
        TaskOutcome(
            task_id=f"T-{i}",
            repeat=0,
            arm=Arm.FOUNDRY,
            status=OutcomeStatus.RESOLVED,
            resolved=True,
            cost_microusd=1_000,
            expected_output="ok",
            produced_output="ok",
        )
        for i in range(3)
    )
    return ArmResult(
        arm=Arm.FOUNDRY,
        slice_identity="slice-id",
        slice_name="scored",
        k_repeats=1,
        seed=0,
        budget_usd_microusd=5_000_000,
        attempted=3,
        outcomes=outcomes,
        simulated=False,
    )


def test_validity_is_orthogonal_to_outcome() -> None:
    """``validity ⊥ outcome``: a pipeline can be fully RESOLVED (outcome eval pass)
    while a role agent's CONTRIBUTION is invalid (it missed its planted defect).

    The outcome eval reports ``itt_pass_rate == 1.0`` (every task resolved), yet
    the agent-validity report — measured on the agent's OWN contribution, NOT the
    resolution bit — flags the reviewer invalid. The two axes are independent:
    a green outcome does NOT certify a valid contribution, so a separate per-role
    validity eval is necessary (evaluation.md §4)."""
    outcome = _resolved_arm_result()
    assert itt_pass_rate(outcome) == 1.0  # pipeline RESOLVED on every task

    # ... yet the reviewer's contribution on its planted-defect fixture is invalid.
    validity = agent_validity_report(catches={ValidityRole.REVIEWER_CRITIC: False})
    assert validity.role_validity(ValidityRole.REVIEWER_CRITIC).valid is False
    assert validity.all_valid is False

    # Distinct verdicts from distinct measurements: a resolved outcome and an
    # invalid contribution coexist — the validity eval is NOT a re-derivation of
    # the outcome.
    assert itt_pass_rate(outcome) == 1.0 and not validity.all_valid


def test_valid_contribution_does_not_imply_resolution() -> None:
    """The mirror: every contribution can be valid while the outcome eval is a
    SEPARATE measurement (here an all-unresolved arm) — a valid agent does not, on
    its own, resolve the pipeline."""
    unresolved = ArmResult(
        arm=Arm.FOUNDRY,
        slice_identity="slice-id",
        slice_name="scored",
        k_repeats=1,
        seed=0,
        budget_usd_microusd=5_000_000,
        attempted=1,
        outcomes=(
            TaskOutcome(
                task_id="T-0",
                repeat=0,
                arm=Arm.FOUNDRY,
                status=OutcomeStatus.UNRESOLVED,
                resolved=False,
                cost_microusd=1_000,
                expected_output="ok",
                produced_output=None,
            ),
        ),
        simulated=False,
    )
    assert itt_pass_rate(unresolved) == 0.0  # pipeline did NOT resolve
    assert agent_validity_report().all_valid is True  # ... yet contributions valid


# ---- 5. determinism over the structured fixtures -----------------------------


def test_report_is_deterministic() -> None:
    """Same inputs ⇒ identical (content-addressable) report — no RNG/IO leak."""
    a = agent_validity_report()
    b = agent_validity_report()
    assert a == b
    assert a.content_identity() == b.content_identity()


def test_report_identity_flips_on_a_changed_role_verdict() -> None:
    """A different ``catches`` map ⇒ a different report identity (the verdict is
    part of the content identity, so a changed contribution is never coalesced)."""
    base = agent_validity_report()
    flipped = agent_validity_report(catches={ValidityRole.QA_ENGINEER: False})
    assert base.content_identity() != flipped.content_identity()


def test_scored_provenance_does_not_coalesce_with_dry_run() -> None:
    """provenance ∈ identity (EV4): a SCORED report differs from its DRY_RUN twin."""
    dry = agent_validity_report()
    scored = agent_validity_report(provenance=Provenance.SCORED)
    assert dry.content_identity() != scored.content_identity()
