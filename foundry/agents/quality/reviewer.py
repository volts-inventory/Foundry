"""Reviewer — the single Layer-4 quality node (agents.md Layer 4).

ONE model call per quality pass: the review verdict through the trusted
``rt.proxy.judge`` path (see :func:`reviewer_critic.review` — proxy-extracted
status, proxy-stamped binding, server-issued per-request nonce; the agent never
mints a verdict, L5). The verdict lands on ``gates`` exactly as the old
``reviewer_critic`` node's did.

The Test / E2E bookkeeping artifacts (``test_plan``, one NEGATIVE
``oracle_binding`` per high-risk criterion, ``test_report``, ``e2e_report``) are
DERIVED deterministically from the acceptance criteria rather than from extra
model calls. Nothing gates on them — the Test / E2E verdicts come from the
attested runner — and the model-authored parts they used to carry (test /
scenario ids, quarantine lists of tests that were never executed) added no
information the criteria set does not already fix. The criteria remain the
source of truth for coverage (Q4): one test and one scenario per criterion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import ArtifactResolver, emit_artifact, read_criteria
from foundry.agents.quality.reviewer_critic import review
from foundry.schemas.product import Criticality
from foundry.schemas.quality import E2EReport, OracleBinding, OracleKind, TestPlan, TestReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "reviewer"

# The determinism guarantees recorded on the E2E report (governance.md §1a).
_DETERMINISM_NOTE = (
    "pinned seeds (RNG seeded per scenario); frozen clock (time mocked, "
    "no wall-clock reads); network mocked (no live egress); N=2 fail-then-pass "
    "quarantine per governance.md §1a"
)


def make_reviewer_node(rt: AgentRuntime, *, resolve: ArtifactResolver | None) -> NodeFn:
    """Build the quality ``reviewer`` node: one judge call + derived test records.

    ``resolve`` reconstitutes ``acceptance_criterion`` bodies from their handles;
    when handles are present and it is ``None`` the node FAILS CLOSED
    (``MissingCriteriaResolverError``) before any model call.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        criteria = read_criteria(state, resolve)
        result, note = review(
            rt, state, config, node_id=_NODE_ID, role="Reviewer", criteria=criteria
        )
        verdict = result.gate_result
        request_id = result.completion.request_id

        plan_seq = rt.seq(config, _NODE_ID, "artifact:test_plan")
        plan = TestPlan(
            plan_uri=f"cas://test-plan/{request_id}",
            test_criteria={f"test::{c.id}": (c.id,) for c in criteria},
            seq=plan_seq,
        )
        oracles = [
            OracleBinding(
                criterion_id=c.id,
                oracle_uri=f"cas://oracle/negative/{c.id}",
                oracle_kind=OracleKind.NEGATIVE,
                seq=rt.seq(config, _NODE_ID, f"artifact:oracle:{c.id}", local=i),
            )
            for i, c in enumerate(c for c in criteria if c.criticality is Criticality.HIGH_RISK)
        ]
        # passed/failed are AUTHORING placeholders (as before the merge): this node
        # executes nothing; the attested Test / E2E gates decide from real runs.
        report_seq = rt.seq(config, _NODE_ID, "artifact:test_report")
        report = TestReport(
            results_uri=f"cas://test-results/{request_id}",
            run_against=verdict.run_against,
            passed=len(plan.test_criteria),
            failed=0,
            seq=report_seq,
        )
        e2e_seq = rt.seq(config, _NODE_ID, "artifact:e2e_report")
        scenarios: dict[str, tuple[str, ...]] = {f"e2e::{c.id}": (c.id,) for c in criteria}
        e2e = E2EReport(
            results_uri=f"cas://e2e-results/{request_id}",
            run_against=verdict.run_against,
            scenarios_passed=len(scenarios),
            scenarios_failed=0,
            pinned_seeds_note=_DETERMINISM_NOTE,
            scenario_criteria=scenarios,
            seq=e2e_seq,
        )

        artifacts = [
            emit_artifact(plan, "test_plan", _NODE_ID, plan_seq, store=rt.cas),
            *(emit_artifact(o, "oracle_binding", _NODE_ID, o.seq, store=rt.cas) for o in oracles),
            emit_artifact(report, "test_report", _NODE_ID, report_seq, store=rt.cas),
            emit_artifact(e2e, "e2e_report", _NODE_ID, e2e_seq, store=rt.cas),
        ]
        return {"gates": [verdict], "artifacts": artifacts, "agent_journal": [note]}

    return _node
