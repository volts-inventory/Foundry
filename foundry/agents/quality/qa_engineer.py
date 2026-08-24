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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import emit_artifact
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.schemas.quality import OracleBinding, OracleKind, TestPlan, TestReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "qa_engineer"

# The product layer emits each AcceptanceCriterion as a content-addressed
# Artifact HANDLE of this kind (it has a ``.uri`` so it survives ``merge_by_uri``
# on the shared ``artifacts`` channel — raw typed bodies, which carry no ``.uri``,
# cannot ride that channel through a fan-in fold).
ACCEPTANCE_CRITERION_KIND = "acceptance_criterion"

# INFRA dependency: reconstitutes a typed body from its Artifact handle (CAS in
# prod, an in-memory map in tests) — exactly the Senior Engineer's seam.
ArtifactResolver = Callable[[Artifact], FrozenModel]


class MissingCriteriaResolverError(RuntimeError):
    """``acceptance_criterion`` handles are on state but no ``resolve`` is wired.

    Fail-closed (C-R1-01): the product layer emits each criterion ONLY as a
    content-addressed handle, so without a ``resolve`` to reconstitute the bodies
    the QA / SDET nodes would read ZERO criteria, author an empty ``TestPlan``
    with no oracles, and silently sail through the Test gate (a VACUOUS pass).
    Raising here surfaces the wiring gap instead of masking it — ``resolve`` is
    REQUIRED whenever criteria handles are present (the production case)."""


def read_criteria(
    state: FactoryState, resolve: ArtifactResolver | None
) -> tuple[AcceptanceCriterion, ...]:
    """The acceptance criteria the QA / SDET nodes plan against — read from STATE.

    The CRITERIA SET (not the model) is the source of truth for coverage (Q4) and
    is a DATA input, so it is read from ``state`` inside the closure, NOT a
    build-time kwarg (AR1-AR4). The product layer emits each criterion as a
    content-addressed ``acceptance_criterion`` Artifact HANDLE (it has a ``.uri``
    so it survives ``merge_by_uri`` on the shared ``artifacts`` channel; raw typed
    bodies, which carry no ``.uri``, cannot ride that channel through a fan-in
    fold — QS2). Each handle is reconstituted via the injected ``resolve`` — the
    same seam the Senior Engineer reads SystemDesign / OwnershipMap through.

    ``resolve`` is INFRA (a dependency), so it may be a factory arg; the DATA
    (WHICH criteria) still comes from state. Records are deduped + ordered by
    ``id`` (fold-order-independent, byte-identical across a resume re-fold).

    Fail-closed (C-R1-01): if ``acceptance_criterion`` handles are present but no
    ``resolve`` is wired, raise :class:`MissingCriteriaResolverError` rather than
    silently returning ``()`` — an empty plan with no oracles is a VACUOUS Test
    gate pass. With no handles at all (a layer that genuinely declared no criteria)
    the result is ``()`` and the empty plan surfaces the gap at the Test gate.
    """
    handles = [
        h
        for h in (state.get("artifacts") or ())
        if getattr(h, "kind", None) == ACCEPTANCE_CRITERION_KIND
    ]
    if handles and resolve is None:
        raise MissingCriteriaResolverError(
            f"{len(handles)} {ACCEPTANCE_CRITERION_KIND!r} handle(s) on "
            "state['artifacts'] but no resolve= was wired; without it the QA/SDET "
            "nodes would read zero criteria and emit an empty TestPlan — a vacuous "
            "Test-gate pass. Thread the CAS resolver into build_quality_subgraph."
        )
    by_id: dict[str, AcceptanceCriterion] = {}
    if resolve is not None:
        for handle in handles:
            body = resolve(handle)
            if isinstance(body, AcceptanceCriterion):
                by_id[body.id] = body
    return tuple(sorted(by_id.values(), key=lambda c: c.id))

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
    resolve: ArtifactResolver | None = None,
    run_against: str = "task/qa",
    tier: str = "T2",
) -> NodeFn:
    """Build the QA Engineer node (agents.md Layer 4).

    The acceptance criteria are a **DATA input** read from ``state`` INSIDE the
    closure (AR1-AR4: only infra deps/config are factory args) via
    :func:`read_criteria` — NOT frozen in as a build-time kwarg. High-risk
    criteria drive a NEGATIVE oracle each; the set is the source of truth for
    coverage.

    Parameters
    ----------
    resolve:
        INFRA dependency: reconstitutes an ``AcceptanceCriterion`` body from its
        content-addressed Artifact handle (CAS in prod, an in-memory map in
        tests) — the same seam the Senior Engineer reads its design/ownership
        through. The DATA (WHICH criteria) is read from ``state['artifacts']``.
        Criteria ride the channel as ``acceptance_criterion`` handles only, so a
        resolver is required to read them: if handles are present but ``resolve``
        is ``None``, :func:`read_criteria` FAILS CLOSED
        (``MissingCriteriaResolverError``) rather than silently planning against an
        empty set. ``build_quality_subgraph`` requires ``resolve`` for this reason.
    run_against:
        Branch then integrated ``tree_sha`` the report keys on.
    tier:
        Oracle authoring is T1 on high-risk (agents.md Layer 4 — most
        consequential artifact).
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        criteria = read_criteria(state, resolve)
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
        # CR3: passed/failed are AUTHORING-ONLY placeholders. This node authors
        # the plan/oracles; it does NOT execute tests. Until the gate-wiring wave
        # (T-M1-12) feeds REAL execution results from the runner, `passed` is
        # optimistically the covered-criteria count and `failed` is 0 — the Test
        # gate, not this report, decides the verdict from real run output (L5).
        report = TestReport(
            results_uri=f"cas://test-results/{result.request_id}",
            run_against=run_against,
            passed=len(test_criteria),  # placeholder until T-M1-12 wires execution
            failed=0,  # placeholder until T-M1-12 wires execution
            quarantined=quarantined,
            seq=report_seq,
        )

        artifacts = [
            emit_artifact(plan, "test_plan", _NODE_ID, plan_seq, store=rt.cas),
            *(
                emit_artifact(o, "oracle_binding", _NODE_ID, o.seq, store=rt.cas)
                for o in oracles
            ),
            emit_artifact(report, "test_report", _NODE_ID, report_seq, store=rt.cas),
        ]
        return {"artifacts": artifacts}

    return _node


def _prompt(criteria: tuple[AcceptanceCriterion, ...], run_against: str) -> str:
    return json.dumps(
        {"run_against": run_against, "acceptance_criteria": _criteria_payload(criteria)},
        sort_keys=True,
        ensure_ascii=False,
    )
