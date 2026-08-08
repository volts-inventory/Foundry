"""Factory graph — compile + run the full Phase-1 spine end-to-end (T-M1-14).

Drives :func:`build_factory_graph` over the SAME end-to-end harness the
supervisor test uses (LEARNINGS SUP7): a REAL :class:`ModelProviderProxy` backed
by :class:`MockTransport` (no live API key, D1) + a dev :class:`ZoneBSigner`, a
REAL :class:`TwoZoneGateRunner` (dev signer + :class:`GlobalRecords`) admitted
through the REAL ``merge_gates`` path (a :class:`DevGateWriteVerifier` on the
reducer contextvar), a :class:`StubAdapter` as ``rt.adapter`` (no ``claude -p``),
in-memory KIND-dispatching ``resolve`` / ``resolve_blob`` doubles, and a durable
checkpointer (:class:`FoundryCheckpointer`).

Asserts:

* the graph COMPILES and runs a one-line greenfield request end-to-end,
  terminating with the SPEC core path complete — a reviewed FeatureDiff +
  EXACTLY ONE ``open_pr`` ExternalAction on ``actions``, behind attested
  Spec/Build/Test PASS;
* kill-then-resume idempotency — re-invoking the SAME ``thread_id`` does NOT
  produce a duplicate PR action (the keyed ``union_actions`` + the same
  ``content_digest`` key coalesce it);
* a halted/failed run emits NO ``open_pr`` action (the PASS guard works).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.docs import DocumentationBundle
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import build_factory_graph, terminal_gates_pass
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

KEY = b"dev-zone-b-factory-graph-key-0123456789"
KEY_ID = "dev-key-factory-graph"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"

# A constant diff_uri the synthetic FeatureDiff points at; resolve_blob returns
# the diff body keyed on it.
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

# A unified-diff ADDITION blob with a PASSING test (Build + Test PASS).
_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
)
# A compiling module whose test COLLECTION fails (imports a missing module):
# Build PASS, Test ERROR -> routes to bounded fix, never advances -> no PR.
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


def _synthetic_resolve(handle: Artifact) -> FrozenModel:
    """KIND-dispatching typed resolver (in-memory infra double; SUP7)."""
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


def _runtime() -> AgentRuntime:
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


def _config(run_id: str, step: int = 1) -> RunnableConfig:
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
    records: GlobalRecords,
    source_dir: str,
    *,
    diff_body: str,
    checkpointer: Any,
    max_fix_attempts: int = 3,
) -> Any:
    return build_factory_graph(
        _runtime(),
        runner=_runner(records),
        resolve=_synthetic_resolve,
        resolve_blob=_resolve_blob_factory(diff_body),
        source_dir=source_dir,
        checkpointer=checkpointer,
        max_fix_attempts=max_fix_attempts,
    )


def _open_pr_actions(state: FactoryState) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "open_pr"]


def _gates_by_name(state: FactoryState) -> dict[str, GateStatus]:
    out: dict[str, GateStatus] = {}
    for g in state.get("gates") or ():
        out.setdefault(g.name, g.status)
    return out


# ---- end-to-end: full spine -> exactly one PR, behind attested PASS ---------


def test_factory_graph_passes_end_to_end_and_opens_one_pr(tmp_path: Any) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_app(records, str(src), diff_body=_PASSING_DIFF, checkpointer=ckpt.saver)
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-pass"))
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # The full phase order ran: every layer's artifacts landed.
    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert {"prd", "acceptance_criterion", "system_design", "feature_diff"} <= kinds
    assert {"test_plan", "e2e_report"} <= kinds

    # Terminal gates are runner-ATTESTED and all PASS.
    statuses = _gates_by_name(final)
    assert statuses.get("spec") is GateStatus.PASS
    assert statuses.get("build") is GateStatus.PASS
    assert statuses.get("test") is GateStatus.PASS
    assert terminal_gates_pass(final) is True

    # EXACTLY ONE open_pr ExternalAction, opened against the integrated FeatureDiff.
    prs = _open_pr_actions(final)
    assert len(prs) == 1
    pr = prs[0]
    feature_diff_uris = {
        a.uri for a in final["artifacts"] if a.kind == "feature_diff"
    }
    assert pr.payload_uri in feature_diff_uris
    assert pr.kind == "open_pr"
    assert final["phase"] == "done"


# ---- kill-then-resume idempotency: no duplicate PR --------------------------


def test_kill_then_resume_does_not_duplicate_pr(tmp_path: Any) -> None:
    """Re-invoking the SAME thread_id (resume / re-fold) re-emits the SAME
    deterministic idempotency key, so ``union_actions`` coalesces it — exactly
    one open_pr action persists across both runs."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_app(records, str(src), diff_body=_PASSING_DIFF, checkpointer=ckpt.saver)
        cfg = _config("run-resume")
        first: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
        assert len(_open_pr_actions(first)) == 1
        first_key = _open_pr_actions(first)[0].idempotency_key

        # Re-invoke the SAME thread_id: LangGraph resumes from the checkpoint and
        # re-folds the committed writes; the emit node re-derives the SAME key.
        second: FactoryState = app.invoke({"request": "build a todo app"}, cfg)
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    prs = _open_pr_actions(second)
    assert len(prs) == 1  # coalesced — NOT a duplicate
    assert prs[0].idempotency_key == first_key


# ---- a halted / failed run opens NO PR --------------------------------------


def test_halted_run_emits_no_pr(tmp_path: Any) -> None:
    """A Test ERROR routes to bounded fix and never advances to an attested PASS;
    the emit guard therefore opens NO PR (governance §1/§5)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = _build_app(
            records, str(src), diff_body=_ERROR_DIFF, checkpointer=ckpt.saver, max_fix_attempts=2
        )
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-halt"))
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    statuses = _gates_by_name(final)
    assert statuses.get("test") is GateStatus.ERROR
    assert terminal_gates_pass(final) is False
    assert _open_pr_actions(final) == []  # no PR for a failed run


# ---- task [3]: the terminal documentation node runs IN-GRAPH ----------------

# The Technical Writer's SOP markers (foundry/docs/writers) + a minimal valid
# payload each, so the in-graph docs node authors real doc bodies (vs degrading
# to empty docs). Layered ON TOP of the spine scripts so the upstream phases are
# unchanged.
_DOC_SCRIPTS: dict[str, str] = {
    **_SCRIPTS,
    "OPERATIONAL RUNBOOK": json.dumps({"overview": "ops", "deploy_steps": ["a"]}),
    "KNOWN-ISSUES": json.dumps({"issues": [], "limitations": ["dev"]}),
    "UPGRADE / MIGRATION GUIDE": json.dumps({"from_version": "0", "to_version": "1"}),
    "PERFORMANCE & COST": json.dumps({"summary": "ok", "observations": ["x"]}),
}

_DOC_KINDS = {"runbook", "known_issues", "upgrade_guide", "perf_cost_report"}


def _runtime_with_docs() -> AgentRuntime:
    """Spine + Technical-Writer scripts, backed by a real content-addressed store
    (so the in-graph docs node resolves the upstream bodies + persists the
    authored doc bodies under their handles)."""
    tx = MockTransport.scripted(dict(_DOC_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=ArtifactStore())


def test_doc_node_runs_in_graph_and_emits_doc_artifacts(tmp_path: Any) -> None:
    """On a PASSING build the terminal docs node runs IN-GRAPH: the authored doc
    artifacts appear on state['artifacts'] (checkpointed) and the writer's model
    spend is metered through the SAME proxy (visible on the proxy manifest)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    rt = _runtime_with_docs()
    sink: list[DocumentationBundle] = []
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))
    try:
        app = build_factory_graph(
            rt,
            runner=_runner(records),
            resolve=_synthetic_resolve,
            resolve_blob=_resolve_blob_factory(_PASSING_DIFF),
            source_dir=str(src),
            checkpointer=ckpt.saver,
            doc_sink=sink,
        )
        # The writer's model calls are budget-metered through the proxy: capture
        # the manifest length before vs after — the docs node adds >= 1 call.
        calls_before = len(rt.proxy.manifest)
        final: FactoryState = app.invoke({"request": "build a todo app"}, _config("run-docs"))
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    assert terminal_gates_pass(final) is True
    # The authored doc artifacts entered the checkpointed artifacts channel.
    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert kinds >= _DOC_KINDS
    # The writer's spend was metered IN-GRAPH (more model calls after invoke).
    assert len(rt.proxy.manifest) > calls_before
    # The assembled bundle reached the in-process sink for on-disk writing.
    assert len(sink) == 1
    assert {a.kind for a in sink[0].doc_artifacts} == _DOC_KINDS
    # The PR is still emitted exactly once — docs never alter the release bit.
    assert len(_open_pr_actions(final)) == 1


def test_doc_node_failure_does_not_fail_the_build(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Documentation is BEST-EFFORT: if in-graph assembly raises, the docs node
    swallows it (warning only) and the build still PASSES + opens its PR."""
    import foundry.docs.assemble as asm

    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    records = GlobalRecords()
    rt = _runtime_with_docs()
    ckpt = FoundryCheckpointer()
    token = set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))

    # Force the in-graph assembly to blow up — the node (which wraps it fail-safe)
    # must still not fail the build (a docs failure can never block the PR).
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(asm, "assemble_documentation", _boom)
    try:
        app = build_factory_graph(
            rt,
            runner=_runner(records),
            resolve=_synthetic_resolve,
            resolve_blob=_resolve_blob_factory(_PASSING_DIFF),
            source_dir=str(src),
            checkpointer=ckpt.saver,
        )
        final: FactoryState = app.invoke(
            {"request": "build a todo app"}, _config("run-docs-boom")
        )
    finally:
        reset_gate_write_verifier(token)
        ckpt.close()

    # The build still PASSED and opened its PR despite the docs failure.
    assert terminal_gates_pass(final) is True
    assert len(_open_pr_actions(final)) == 1
    # No doc artifacts landed (assembly never returned a bundle).
    kinds = {a.kind for a in final.get("artifacts") or ()}
    assert not (_DOC_KINDS & kinds)
