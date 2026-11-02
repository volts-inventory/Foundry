"""Implementation-layer helpers shared by the ticket path and the fixer.

Pinned-constraint injection order (live view, stable), the typed ``ready_tasks``
read, the process-stable idempotency key (L7), and the failed-gate feedback the
fix loop hands the engineer.
"""

from __future__ import annotations

from typing import Any

from foundry.agents.implementation import idempotency_key, pinned_texts, ready_tasks
from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.schemas.implementation import Task
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq


def a_seq(node: str = "test", epoch: int = 1) -> Seq:
    return Seq("run", epoch, 1, node, 0)


def a_constraint(cid: str, text: str) -> PinnedConstraint:
    return PinnedConstraint(
        id=cid,
        text=text,
        scope=ConstraintScope.GLOBAL,
        source="ADR-1",
        seq=a_seq("planner"),
    )


def a_task(tid: str = "t-001") -> Task:
    return Task(
        id=tid,
        spec_uri=f"cas://task/{tid}",
        criterion_ids=("AC-1",),
        branch=f"task/{tid}",
        seq=a_seq("senior_engineer"),
    )


def tasks_state(*tasks: Task) -> list[dict[str, Any]]:
    return [t.model_dump(mode="json") for t in tasks]


def two_constraints() -> list[PinnedConstraint]:
    # ids chosen so sorted-by-id order is c1 then c2 (deterministic injection).
    return [
        a_constraint("c1", "MUST store money as integer cents, never floats."),
        a_constraint("c2", "MUST log no PII."),
    ]


def test_pinned_texts_uses_live_view_and_stable_order() -> None:
    base = two_constraints()
    superseding = PinnedConstraint(
        id="c3",
        text="MUST log no secrets and no PII.",
        scope=ConstraintScope.GLOBAL,
        source="ADR-2",
        supersedes="c2",
        seq=a_seq(),
    )
    texts = pinned_texts([*base, superseding])
    assert "MUST log no PII." not in texts  # c2 retired
    assert "MUST log no secrets and no PII." in texts
    assert pinned_texts([superseding, *reversed(base)]) == texts


def test_ready_tasks_reads_state_in_merge_order() -> None:
    t_a = Task(id="t-a", spec_uri="cas://a", criterion_ids=(), branch="task/t-a", seq=a_seq("s", 1))
    t_b = Task(id="t-b", spec_uri="cas://b", criterion_ids=(), branch="task/t-b", seq=a_seq("s", 3))
    # Reversed input order; ready_tasks must order by Seq.
    state: FactoryState = {"tasks": tasks_state(t_b, t_a)}
    ids = [t.id for t in ready_tasks(state)]
    assert ids == ["t-a", "t-b"]


def test_idempotency_key_is_process_stable() -> None:
    task = a_task()
    constraints = two_constraints()
    k1 = idempotency_key(task, constraints, "base-sha-xyz")
    k2 = idempotency_key(task, constraints, "base-sha-xyz")
    assert k1 == k2  # content_digest, not hash() (L7)
    assert idempotency_key(task, constraints, "other-base") != k1
    assert idempotency_key(task, [], "base-sha-xyz") != k1


def test_prior_failures_feeds_failed_gate_findings_back_to_the_engineer() -> None:
    """The fix loop's feedback seam: a FAILED gate's findings become the packet's
    prior_failures; a clean / PASSed state contributes nothing (first-pass blind)."""
    from foundry.agents.implementation.junior_engineer import _prior_failures
    from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
    from foundry.state.seq import Seq

    failing = GateResult(
        name="test",
        status=GateStatus.FAIL,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=("FAILED test_rw::test_double_spaces", "E   assert 'a b' == 'a  b'"),
        run_against="a" * 64,
        seq=Seq("r", 1, 1, "test", 0),
    )
    fed = _prior_failures({"gates": [failing]})
    assert "E   assert 'a b' == 'a  b'" in fed
    assert any("test gate FAILED" in f for f in fed)
    assert _prior_failures({}) == ()                                   # first pass: blind
    passing = failing.model_copy(update={"status": GateStatus.PASS})
    assert _prior_failures({"gates": [passing]}) == ()                 # green: no feedback


def test_prior_failures_reports_only_the_latest_verdict_per_gate() -> None:
    """The gates channel keeps one record per (gate, tree). Superseded trees' failures
    must not pile up in every later fix prompt; a gate that has since passed adds none."""
    from foundry.agents.implementation.junior_engineer import _prior_failures
    from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
    from foundry.state.seq import Seq

    def gate(name: str, epoch: int, status: GateStatus, finding: str) -> GateResult:
        return GateResult(
            name=name, status=status, decision_deterministic=True,
            execution_deterministic=True,
            verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
            findings=(finding,), run_against=str(epoch) * 64,
            seq=Seq("r", epoch, 1, name, 0),
        )

    fed = _prior_failures({"gates": [
        gate("test", 1, GateStatus.FAIL, "OLD test failure"),
        gate("test", 2, GateStatus.FAIL, "NEW test failure"),
        gate("build", 1, GateStatus.FAIL, "OLD build failure"),
        gate("build", 2, GateStatus.PASS, "build ok"),
    ]})
    assert "NEW test failure" in fed
    assert "OLD test failure" not in fed and "OLD build failure" not in fed

    # Build failed on the newest tree, so tests never re-ran there: the older tree's
    # test FAIL is stale and must not be fed back as a current failure.
    fed = _prior_failures({"gates": [
        gate("test", 1, GateStatus.FAIL, "STALE test failure"),
        gate("build", 2, GateStatus.FAIL, "CURRENT build failure"),
    ]})
    assert "CURRENT build failure" in fed and "STALE test failure" not in fed
