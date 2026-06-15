"""QA Engineer — the oracle author (agents.md Layer 4).

SOP: derive tests from the acceptance criteria with ``criterion_id``
traceability → for every HIGH-RISK criterion add a NEGATIVE oracle (a test that
must *fail against a known-bad reference* — criterion *fidelity*, not just
coverage) plus example/property/golden oracles → run on the branch then the
integrated tree → report, recording any flaky test quarantined under the N=2
fail-then-pass budget (governance.md §1a).

Trust model (LEARNINGS L5 / AR4): the QA Engineer NEVER writes a ``GateResult``
to ``gates`` — the Test gate verdict is computed + signed by the trusted runner.
This node authors the plan/oracles/report (the records the runner consumes and
produces around) and emits them as content-addressed :class:`Artifact` handles
on the ``artifacts`` channel (DS9 / L4). All model egress is through
``rt.proxy.complete`` (L6); the node owns the causal-order ``Seq`` and the
``criterion_id`` traceability, so those are not surrendered to the model call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import emit_artifact
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import OracleBinding, OracleKind, TestPlan, TestReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "qa_engineer"

_SOP = (
    "You are the QA Engineer (Layer 4). Author the oracle — the most "
    "consequential artifact in the loop. Derive tests from the acceptance "
    "criteria with criterion_id traceability; for every high-risk criterion add "
    "a NEGATIVE oracle that must FAIL against a known-bad reference (criterion "
    "fidelity, not just coverage), plus property/golden oracles. Run on the "
    "branch then the integrated tree, then report green/criterion-fidelity and "
    "quarantine any flaky test under the N=2 fail-then-pass budget. Reply with "
    "JSON only."
)

# The model proposes one or more test ids per criterion + oracle hints; the node
# owns the Seq, the CAS body handles, and the traceability/oracle invariants.
_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string"},
                    "criterion_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["test_id", "criterion_ids"],
                "additionalProperties": False,
            },
        },
        "quarantined": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tests"],
    "additionalProperties": False,
}


def _criteria_payload(criteria: tuple[AcceptanceCriterion, ...]) -> list[dict[str, Any]]:
    return [
        {
            "criterion_id": c.id,
            "text": c.text,
            "criticality": str(c.criticality),
            "security_abuse": c.security_abuse,
        }
        for c in criteria
    ]


def _test_criteria(
    proposed: Any, criteria: tuple[AcceptanceCriterion, ...]
) -> dict[str, tuple[str, ...]]:
    """Index proposed ``test_id -> criterion_ids``, fail-closed to full coverage.

    The criteria set (not the model) is the source of truth for WHAT must be
    covered: the model enriches each test with its criteria, but if it omits a
    criterion the node still guarantees that every criterion is exercised by at
    least one planned test (one synthetic test per uncovered criterion), so the
    Test gate's criterion-coverage check is never starved by a thin model reply.
    """
    known = {c.id for c in criteria}
    mapping: dict[str, tuple[str, ...]] = {}
    for entry in proposed or ():
        if not isinstance(entry, dict):
            continue
        tid = entry.get("test_id")
        if not isinstance(tid, str) or not tid:
            continue
        cids = entry.get("criterion_ids", [])
        kept = tuple(str(c) for c in cids if str(c) in known) if isinstance(cids, list) else ()
        if kept:
            mapping[tid] = kept
    covered = {c for ids in mapping.values() for c in ids}
    for c in criteria:
        if c.id not in covered:
            mapping[f"test::{c.id}"] = (c.id,)
    return mapping


def make_qa_engineer_node(
    rt: AgentRuntime,
    *,
    criteria: tuple[AcceptanceCriterion, ...],
    run_against: str = "task/qa",
    tier: str = "T1",
) -> NodeFn:
    """Build the QA Engineer node (agents.md Layer 4).

    Parameters
    ----------
    criteria:
        The acceptance criteria (resolved from their CAS handles by the
        orchestrator). High-risk criteria drive a NEGATIVE oracle each; the set
        is the source of truth for criterion coverage.
    run_against:
        Branch then integrated ``tree_sha`` the report keys on.
    tier:
        Oracle authoring is T1 on high-risk (agents.md Layer 4 — most
        consequential artifact).
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        plan_seq = rt.seq(config, _NODE_ID, "artifact:test_plan")
        report_seq = rt.seq(config, _NODE_ID, "artifact:test_report")

        result = rt.proxy.complete(
            _NODE_ID,
            tier,
            _SOP,
            [{"role": "user", "content": _prompt(criteria, run_against)}],
            purpose="ideation",
            schema=_PLAN_SCHEMA,
        )
        payload = result.parsed or {}

        test_criteria = _test_criteria(payload.get("tests"), criteria)
        plan = TestPlan(
            plan_uri=f"cas://test-plan/{result.request_id}",
            test_criteria=test_criteria,
            seq=plan_seq,
        )

        # A NEGATIVE oracle per HIGH-RISK criterion: criterion fidelity, not just
        # coverage (agents.md Layer 4 / governance.md §1b). Each is a frozen,
        # content-addressed binding — a rebind only arrives as a supersession.
        oracles: list[OracleBinding] = []
        for i, c in enumerate(c for c in criteria if c.criticality is Criticality.HIGH_RISK):
            oracles.append(
                OracleBinding(
                    criterion_id=c.id,
                    oracle_uri=f"cas://oracle/negative/{c.id}",
                    oracle_kind=OracleKind.NEGATIVE,
                    seq=rt.seq(config, _NODE_ID, f"artifact:oracle:{c.id}", local=i),
                )
            )

        # The N=2 fail-then-pass quarantine list (governance.md §1a): a flaky
        # test that failed then passed within the retry budget is quarantined,
        # not counted as a real failure.
        quarantined = tuple(
            str(q) for q in payload.get("quarantined", ()) if isinstance(q, str)
        )
        report = TestReport(
            results_uri=f"cas://test-results/{result.request_id}",
            run_against=run_against,
            passed=len(test_criteria),
            failed=0,
            quarantined=quarantined,
            seq=report_seq,
        )

        artifacts = [
            emit_artifact(plan, "test_plan", _NODE_ID, plan_seq),
            *(
                emit_artifact(o, "oracle_binding", _NODE_ID, o.seq)
                for o in oracles
            ),
            emit_artifact(report, "test_report", _NODE_ID, report_seq),
        ]
        return {"artifacts": artifacts}

    return _node


def _prompt(criteria: tuple[AcceptanceCriterion, ...], run_against: str) -> str:
    return json.dumps(
        {"run_against": run_against, "acceptance_criteria": _criteria_payload(criteria)},
        sort_keys=True,
        ensure_ascii=False,
    )
