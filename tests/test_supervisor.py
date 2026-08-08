"""Supervisor — phase routing across the four layer subgraphs (T-M1-13).

Drives :func:`build_supervisor_graph` END-TO-END over a REAL
:class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API key,
LEARNINGS D1) + a dev :class:`ZoneBSigner`, a REAL :class:`TwoZoneGateRunner`
(dev signer + :class:`GlobalRecords`) and the REAL ``merge_gates`` admission
path (a :class:`DevGateWriteVerifier` installed on the reducer contextvar), with
a :class:`StubAdapter` as ``rt.adapter`` (no ``claude -p`` binary, D3).

The two materialization seams (GW1a/D-R1-02) are in-memory doubles:

* ``resolve`` (typed) — a KIND-dispatching reconstitutor. Bodies are produced
  mid-run by the architecture / implementation agents (``emit_artifact`` only
  mints a content-addressed HANDLE; the body is never persisted in a test, and
  the handle uri encodes the body's epoch-dependent ``content_identity`` so it
  can't be pre-keyed). The double therefore synthesizes a faithful typed body
  from the handle's ``kind`` + ``seq`` — sufficient for the Senior's
  decomposition (SystemDesign / OwnershipMap) and the gates' inputs
  (AcceptanceCriterion / FeatureDiff).
* ``resolve_blob`` — returns the raw unified-diff body (mirrors
  ``sandbox.get_blob``); the test controls whether that diff includes a passing
  test (Build+Test PASS) or no test (a vacuous Test FAIL).

Asserts:

* the FULL phase order executes (triage -> product -> architecture -> spec gate
  -> implementation -> quality -> build+test gate);
* advancement happens ONLY on an attested gate PASS at each boundary;
* a forced gate FAIL routes back to the responsible layer and the run TERMINATES
  within ``max_fix_attempts`` (no infinite loop);
* triage sets ``route`` + seeds a per-route ``budget``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.supervisor import (
    DEFAULT_ROUTE_CAPS_MICROUSD,
    build_supervisor_graph,
    classify_request,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier

KEY = b"dev-zone-b-supervisor-key-0123456789"
KEY_ID = "dev-key-supervisor"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"

# A constant diff_uri the synthetic FeatureDiff points at; resolve_blob returns
# the diff body keyed on it (we ignore the actual StubAdapter-minted uri — the
# gate trust seam itself is covered by tests/test_gate_wiring.py; here we test
# the SUPERVISOR's sequencing/routing, with the body under the test's control).
_DIFF_BODY_URI = "cas://test/diff-body"


# ---- scripted per-node payloads (free-text completions, SOP order) ----------

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user"],
    "unknowns": [],
    "success_criteria": ["a user can add a todo"],
    "must_haves": ["add todo"],
    "nice_to_haves": [],
}

# No required_capabilities -> architecture skips the conditional Layer-2 nodes
# (one architect call), keeping the model-call script simple. One criterion is
# security_abuse=True so the Spec gate's mandatory security coverage is met.
PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add and list todos.",
    "required_capabilities": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears.",
            "criticality": "normal",
            "oracle_hint": "POST then GET returns the item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401; negative oracle",
            "security_abuse": True,
        },
    ],
}

CRITIQUE_PAYLOAD: dict[str, Any] = {"missing": [], "security_abuse_omissions": []}

SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "none",
    "done_definition": "all criteria green",
}

DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "design_body": "api -> store",
    "components": ["api"],
    "adrs": [],
    "interfaces": [],
    "ownership": {"app.py": "owner-a"},  # one unit -> one task -> one diff
    "pinned_constraints": [],
}

DECOMPOSITION_PAYLOAD: dict[str, Any] = {
    "tasks": [{"unit": "app.py", "title": "build app", "criterion_ids": ["AC-001"]}]
}

QA_PAYLOAD: dict[str, Any] = {
    "tests": [{"test_id": "t-1", "criterion_ids": ["AC-001", "AC-002"]}],
    "quarantined": [],
}

SDET_PAYLOAD: dict[str, Any] = {
    "scenarios": [{"scenario_id": "e2e-1", "criterion_ids": ["AC-001"]}],
    "quarantined": [],
}

# Keyed by a substring of each agent's system SOP (first-match-wins, order- AND
# count-INDEPENDENT) so the fix-loop can re-run the implementation + quality
# phases as many times as the convergence bound allows without exhausting a FIFO
# queue (a `completions=` queue would empty on the 2nd loop -> empty default_text
# -> a parse failure). The reviewer's verdict rides the verdict channel
# (default_verdict="approve", repeatable).
_SCRIPTS: dict[str, str] = {
    # Every node that can make a model call needs a script: an unscripted node
    # serves "" and dies as ProxyUnavailable after the retries.
    "You are the Scope Judge": json.dumps(
        {"tier": "standard", "rationale": "a full-scope build", "signals": []}
    ),
    "Stakeholder / Client Proxy": json.dumps(INTENT_PAYLOAD),
    "Product Manager": json.dumps(PRD_PAYLOAD),
    "Criteria-Completeness Critic": json.dumps(CRITIQUE_PAYLOAD),
    "Engineering / Program Manager": json.dumps(SPRINT_PAYLOAD),
    "Principal Architect": json.dumps(DESIGN_PAYLOAD),
    "Senior Engineer": json.dumps(DECOMPOSITION_PAYLOAD),
    "QA Engineer": json.dumps(QA_PAYLOAD),
    "SDET": json.dumps(SDET_PAYLOAD),
}


# ---- in-memory materialization doubles --------------------------------------

# A unified-diff ADDITION blob (the shape the adapters emit + gate_wiring parses).
_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
)
# A compiling module + a test whose COLLECTION fails (imports a missing module):
# compileall PASSes (syntactically valid), but pytest exits 2 -> a signed Test
# ERROR (infra, not a verdict, DS7). An ERROR routes to bounded retry (`fix`) and
# NEVER latches -> it exercises the route-back + convergence-bound path (a hard
# FAIL on an integrated digest would instead latch + halt at once, gov §5).
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


def _synthetic_resolve(handle: Artifact) -> FrozenModel:
    """KIND-dispatching typed resolver (an in-memory infra double).

    The architecture / product agents mint content-addressed HANDLES whose uri
    encodes an epoch-dependent ``content_identity`` (so it can't be pre-keyed in
    a test); the body is never persisted. Synthesize a faithful typed body from
    the handle's ``kind`` + ``seq`` — enough for the Senior's decomposition and
    the gates' inputs. Every synthesized AcceptanceCriterion is
    ``security_abuse=True`` so the Spec gate's mandatory security coverage holds.
    """
    if handle.kind == SystemDesign.__name__ or handle.kind == "system_design":
        return SystemDesign(
            title="Todo service",
            design_uri="cas://design/body",
            components=("api",),
            interface_ids=(),
            seq=handle.seq,
        )
    if handle.kind == "ownership_map":
        return OwnershipMap(owners={"app.py": "owner-a"}, seq=handle.seq)
    if handle.kind == "acceptance_criterion":
        cid = f"AC-{handle.seq.local:03d}-{handle.seq.emission_epoch}"
        return AcceptanceCriterion(
            id=cid,
            text=f"Given/When/Then for {cid}",
            criticality=Criticality.HIGH_RISK,
            oracle_hint=f"oracle for {cid}",
            security_abuse=True,  # guarantees Spec-gate security coverage
            seq=handle.seq,
        )
    if handle.kind == "criteria_findings":
        # The Criteria-Completeness Critic finding the Spec gate now consumes.
        # No security/abuse omission here -> does not block the clean run.
        return CriteriaCritique(
            prd_uri="cas://design/prd",
            security_abuse_omissions=(),
            judgment_provenance=JudgmentProvenance.SELF,
            seq=handle.seq,
        )
    if handle.kind == "feature_diff":
        return FeatureDiff(
            diff_uri=_DIFF_BODY_URI,
            task_id=handle.task_id or "T1",
            branch=f"task/{handle.task_id or 'T1'}",
            base_tree_sha="base-sha",
            seq=handle.seq,
        )
    raise AssertionError(f"unexpected artifact kind for resolve: {handle.kind!r}")


def _resolve_blob_factory(diff_body: str) -> Any:
    def _resolve_blob(uri: str) -> bytes:
        return diff_body.encode("utf-8")

    return _resolve_blob


# ---- harness ----------------------------------------------------------------


def _runtime(transport: MockTransport | None = None) -> AgentRuntime:
    tx = transport or MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    # verdict_nonce / subject_artifact_uri / state_sha are RUN INPUTS the runtime
    # threads onto the run config (Q1 / LEARNINGS), exactly like thread_id.
    return {
        "configurable": {
            "thread_id": run_id,
            "verdict_nonce": f"N-{run_id}",
            "subject_artifact_uri": _DIFF_BODY_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
        "recursion_limit": 200,
    }


def _build_app(
    records: GlobalRecords, source_dir: str, *, diff_body: str, max_fix_attempts: int = 3
) -> Any:
    return build_supervisor_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        max_fix_attempts=max_fix_attempts,
    ).compile(checkpointer=MemorySaver())


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        prev = out.get(g.name)
        # (the channel may carry multiple; here one per name is fine)
        out[g.name] = g.status if prev is None else prev
    return out


# ---- triage classification --------------------------------------------------


def test_classify_request_defaults_to_greenfield() -> None:
    assert classify_request("build a brand new todo app") == "greenfield"
    assert classify_request("fix the broken login bug") == "bugfix"
    assert classify_request("spike: investigate a caching approach") == "spike"
    assert classify_request("add pagination to the list endpoint") == "evolution"


def test_builder_returns_uncompiled_state_graph(tmp_path: Any) -> None:
    records = GlobalRecords()
    g = build_supervisor_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(_PASSING_DIFF),
        source_dir=str(tmp_path),
    )
    assert isinstance(g, StateGraph)
    assert not hasattr(g, "invoke")  # compilation is factory_graph's job (T-M1-14)


# ---- end-to-end: full phase order + PASS-gated advancement ------------------


def test_end_to_end_greenfield_passes_all_boundaries(tmp_path: Any) -> None:
    """A one-line greenfield request runs the FULL phase order and advances at
    each boundary ONLY on an attested gate PASS (spec PASS, build+test PASS)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_app(records, str(src), diff_body=_PASSING_DIFF)
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-pass"))
    finally:
        reset_gate_write_verifier(token)

    # Triage seeded route + budget.
    assert final["route"] == "greenfield"
    assert final["budget"].cap_microusd == DEFAULT_ROUTE_CAPS_MICROUSD["greenfield"]

    # Every layer ran (its artifacts landed) -> the full phase order executed.
    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert {"prd", "acceptance_criterion"} <= kinds  # product
    assert "system_design" in kinds  # architecture
    assert "feature_diff" in kinds  # implementation
    assert {"test_plan", "e2e_report"} <= kinds  # quality

    # The gate verdicts are runner-ATTESTED and all PASS -> the run advanced
    # through both boundaries to END (advancement only on attested PASS).
    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS
    # The reviewer's judgment verdict also rode `gates` (admitted by merge_gates).
    assert statuses.get("review") is GateStatus.PASS
    # No fix iterations were needed (the convergence counter stayed at 0).
    assert final["budget"].fix_attempts_since_all_green == 0


# ---- end-to-end: a forced gate FAIL routes back + terminates bounded --------


def test_forced_test_error_routes_to_fix_and_terminates_bounded(tmp_path: Any) -> None:
    """A Test ERROR at the build+test boundary routes back to the implementation
    layer for re-verification; the convergence counter bumps each loop, and the
    run HALTS within ``max_fix_attempts`` — no infinite loop.

    An ERROR (not a hard FAIL) is the case that exercises the route-back path:
    it never latches (infra, DS7), so it must be the bounded ``fix`` counter that
    converges the loop. (A hard FAIL on an integrated digest latches + halts at
    once, gov §5 — terminal, not retried.)"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    max_fix = 2
    try:
        app = _build_app(records, str(src), diff_body=_ERROR_DIFF, max_fix_attempts=max_fix)
        # If routing did not bound the loop this would recurse until LangGraph's
        # recursion_limit; it instead terminates because the fix counter is
        # bumped each loop and route_after_gates returns `halt` once it is spent.
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-error"))
    finally:
        reset_gate_write_verifier(token)

    # Spec advanced (PASS); the build compiles; the Test gate is a signed ERROR
    # (collection import failure) — never an advance.
    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS
    assert statuses.get("build") is GateStatus.PASS  # the module still compiles
    assert statuses.get("test") is GateStatus.ERROR

    # The run terminated bounded: the convergence counter reached the bound and
    # routing halted (escalate) rather than spinning forever. An ERROR never
    # latches (infra, not a verdict).
    assert final["budget"].fix_attempts_since_all_green >= max_fix
    test_verdict = next(g for g in final["gates"] if g.name == "test")
    assert not records.stable_fail_latched(test_verdict.run_against, "test")
