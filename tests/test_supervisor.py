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
  from the handle's ``kind`` + ``seq`` — sufficient for the gates' inputs
  (AcceptanceCriterion / FeatureDiff).
* ``resolve_blob`` — returns the raw unified-diff body (mirrors
  ``sandbox.get_blob``); the test controls whether that diff includes a passing
  test (Build+Test PASS) or no test (a vacuous Test FAIL).

Asserts triage classification and that a Spec FAIL re-enters the planner with
the gate's findings. The end-to-end pass / bounded-fix runs live in
tests/test_factory_graph.py (the same spine, compiled with a checkpointer).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.supervisor import (
    build_supervisor_graph,
    classify_request,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateStatus, JudgmentProvenance
from foundry.schemas.implementation import FeatureDiff, Specialty, Ticket
from foundry.schemas.product import (
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
)
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier
from tests.planner_payload import PLANNER_SOP_KEY, plan_payload

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

# No required_capabilities -> the planner skips the conditional Layer-2 nodes
# (one planner call), keeping the model-call script simple. One criterion is
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
    PLANNER_SOP_KEY: json.dumps(
        plan_payload(
            intent=INTENT_PAYLOAD,
            prd=PRD_PAYLOAD,
            critique=CRITIQUE_PAYLOAD,
            sprint=SPRINT_PAYLOAD,
            design=DESIGN_PAYLOAD,
            decomposition=DECOMPOSITION_PAYLOAD,
        )
    ),
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
# The passing tree plus specific pytest-bdd steps for the scenarios the planner payload
# emits (legacy text criteria render as one ``Then <text>`` step each). Catch-all
# steps would not do: the Test gate rejects scenarios bound only by them.
_BDD_PASSING_DIFF = _PASSING_DIFF + (
    "--- /dev/null\n+++ b/test_steps.py\n@@ -0,0 +1,15 @@\n"
    "+from pytest_bdd import scenarios, then\n"
    "+\n"
    "+import app\n"
    "+\n"
    "+scenarios('features')\n"
    "+\n"
    "+\n"
    "+@then('Given a user, When they add a todo, Then it appears.')\n"
    "+def _todo_appears():\n"
    "+    assert app.VALUE == 42\n"
    "+\n"
    "+\n"
    "+@then('Given an unauthenticated request, When it hits /todos, Then 401.')\n"
    "+def _unauthenticated_rejected():\n"
    "+    assert app.VALUE != 401\n"
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
    if handle.kind == "ticket":
        tid = f"T-{handle.seq.local:03d}"
        # No `files`: unrestricted write scope, so the canned diff lands whole.
        return Ticket(id=tid, title=f"Ticket {tid}", specialty=Specialty.BACKEND,
                      seq=handle.seq)
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


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


# ---- triage classification --------------------------------------------------


def test_classify_request_defaults_to_greenfield() -> None:
    assert classify_request("build a brand new todo app") == "greenfield"
    assert classify_request("fix the broken login bug") == "bugfix"
    assert classify_request("spike: investigate a caching approach") == "spike"
    assert classify_request("add pagination to the list endpoint") == "evolution"


# ---- spec_fix re-enters the planner with the Spec gate's findings -----------


def _respec_app(tmp_path: Any, records: GlobalRecords, **kwargs: Any) -> tuple[Any, MockTransport]:
    """A supervisor whose first plan FAILs the Spec gate (no security/abuse
    criterion) and whose REVISION PASS plan passes it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")

    no_abuse = {**PRD_PAYLOAD, "acceptance_criteria": [PRD_PAYLOAD["acceptance_criteria"][0]]}

    def _plan(prd: dict[str, Any]) -> str:
        return json.dumps(
            plan_payload(
                intent=INTENT_PAYLOAD,
                prd=prd,
                critique=CRITIQUE_PAYLOAD,
                sprint=SPRINT_PAYLOAD,
                design=DESIGN_PAYLOAD,
                decomposition=DECOMPOSITION_PAYLOAD,
            )
        )

    # First match wins: the revision prompt gets the fixed plan, the first pass
    # a plan with no security/abuse criterion (a Spec FAIL).
    scripts = {"REVISION PASS": _plan(PRD_PAYLOAD), **_SCRIPTS, PLANNER_SOP_KEY: _plan(no_abuse)}
    tx = MockTransport.scripted(scripts, default_verdict="approve")
    cas = ArtifactStore()
    rt = AgentRuntime(
        proxy=ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID)),
        adapter=StubAdapter(),
        cas=cas,
    )

    def _resolve(handle: Artifact) -> FrozenModel:
        # Real criteria from the CAS, so the gate sees what the planner wrote.
        if handle.kind in ("acceptance_criterion", "criteria_findings"):
            body: FrozenModel = cas.resolve(handle)
            return body
        return _synthetic_resolve(handle)

    app = build_supervisor_graph(
        rt,
        runner=_runner(records),
        resolve=_resolve,
        resolve_blob=_resolve_blob_factory(_BDD_PASSING_DIFF),
        source_dir=str(src),
        **kwargs,
    ).compile(checkpointer=MemorySaver())
    return app, tx


def test_spec_fail_replans_with_the_gate_findings_then_advances(tmp_path: Any) -> None:
    """A Spec FAIL routes spec_fix -> planner; the re-entry prompt carries the
    signed gate's findings, and the revised plan passes the gate."""
    records = GlobalRecords()
    app, tx = _respec_app(tmp_path, records)
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-respec"))
    finally:
        reset_gate_write_verifier(token)

    planner_prompts = [json.dumps(r) for r in tx.requests if PLANNER_SOP_KEY in json.dumps(r)]
    assert len(planner_prompts) == 2
    assert "REVISION PASS" not in planner_prompts[0]
    assert "REVISION PASS" in planner_prompts[1]
    assert "no criterion flags security_abuse=true" in planner_prompts[1]

    spec = [g for g in final["gates"] if g.name == "spec"]
    assert {g.status for g in spec} == {GateStatus.FAIL, GateStatus.PASS}
    assert max(spec, key=lambda g: g.seq).status is GateStatus.PASS
    assert _gates_by_name(final).get("test") is GateStatus.PASS
