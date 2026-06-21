"""Assurance subgraph + the bounded evaluator-optimizer rework loop (T-M2-08).

Drives :func:`build_assurance_subgraph` over a REAL :class:`ModelProviderProxy`
(``MockTransport`` + a dev ``ZoneBSigner`` — no live API key, LEARNINGS D1/D4), a
REAL :class:`TwoZoneGateRunner` (dev signer + ``GlobalRecords``) with a
``DevGateWriteVerifier`` installed, and in-memory ``resolve`` / ``resolve_blob``
seams. Compiles the subgraph STANDALONE (no checkpointer — the supervisor owns
that) and exercises:

* a CLEAN change → security agents (advisory triage) → assurance gates attested
  PASS → reviewer approve → ``advance``;
* a gate FAIL / reviewer request-changes drives a BOUNDED rework loop that
  TERMINATES within ``max_fix_attempts`` (asserts it HALTS, not hangs);
* a KNOWN-VULNERABLE change is CAUGHT through the subgraph (security gate attested
  FAIL → no advance);
* re-verification scoping: a rework iteration re-runs ONLY the scoped gates (not
  the full set) — asserted via the reverification plan (``rework_scoped_gates``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.assurance_wiring import assurance_gate_set
from foundry.graph.gate_wiring import CRITERION_KIND, FEATURE_DIFF_KIND
from foundry.graph.subgraphs.assurance import (
    build_assurance_subgraph,
    rework_scoped_gates,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.governance import RiskTier, TierDecision
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

KEY = b"zone-b-dev-key-assurance-subgraph0"
KEY_ID = "zoneB-dev-assurance"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
RUN_ID = "run-assurance-subgraph"

# A hardcoded secret the SecurityGate dev rule (hardcoded-password) catches.
_VULN_MODULE = 'API_KEY = "supersecrethardcodedtoken123"\n'
_GOOD_MODULE = "VALUE = 42\n"
_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'


# ---- per-agent scripted JSON payloads (schema-constrained IDEATION; DT1) ------

_APPSEC_PAYLOAD = json.dumps(
    {
        "threat_model_body": "STRIDE model",
        "oracle_bindings": {"S1": "test_auth"},
        "residual_item_ids": [],
        "severity": "low",
        "findings": [],
    }
)
_AUDIT_PAYLOAD = json.dumps(
    {"sbom_body": "sbom", "severity": "low", "findings": []}
)
_SUPPLY_CHAIN_PAYLOAD = json.dumps(
    {"report_body": "supply-chain review", "policy_violations": [], "trust_root_touched": False}
)


def _seq(node: str, key: str, epoch: int = 1) -> Seq:
    return Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _tier(level: RiskTier) -> TierDecision:
    return TierDecision(
        tier=level,
        rationale=f"test {level}",
        run_against="design-ref",
        seq=_seq("triage", f"tier:{level}"),
    )


def _transport(*, default_verdict: str = "approve") -> MockTransport:
    # The 3 security agents ride free-text completions (schema-constrained
    # IDEATION; AC3/DT1), keyed by their SOP marker so the order is robust and
    # COUNT-INDEPENDENT (the rework loop never re-runs them, but keying is safe).
    # The reviewer rides the verdict channel (default_verdict).
    return MockTransport.scripted(
        {
            "You are the Security / AppSec Engineer": _APPSEC_PAYLOAD,
            "You are the Audit / Compliance agent": _AUDIT_PAYLOAD,
            "You are the Supply-Chain Reviewer": _SUPPLY_CHAIN_PAYLOAD,
        },
        default_verdict=default_verdict,  # the reviewer/critic verdict channel
    )


def _runtime(transport: MockTransport | None = None) -> AgentRuntime:
    proxy = ModelProviderProxy(transport or _transport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def _runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


# ---- seeding: criterion + feature_diff handles + the two resolvers -----------


def _criterion(cid: str, *, security: bool = False) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given X When Y Then Z ({cid})",
        oracle_hint=f"test_{cid}",
        security_abuse=security,
        seq=_seq("product_manager", f"criterion:{cid}"),
    )


def _addition_diff(files: dict[str, str]) -> str:
    parts: list[str] = []
    for path in sorted(files):
        lines = files[path].splitlines(keepends=True)
        parts.append(f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n")
        parts.extend(f"+{ln}" for ln in lines)
    return "".join(parts)


def _feature_diff(files: dict[str, str], *, task_id: str = "T1") -> tuple[FeatureDiff, str, bytes]:
    blob = _addition_diff(files).encode("utf-8")
    diff_uri = f"cas://sha256/{hashlib.sha256(blob).hexdigest()}"
    diff = FeatureDiff(
        diff_uri=diff_uri,
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="base-sha",
        seq=_seq("junior_engineer", f"diff:{task_id}"),
    )
    return diff, diff_uri, blob


def _seed(
    criteria: tuple[AcceptanceCriterion, ...],
    diffs: tuple[tuple[FeatureDiff, str, bytes], ...],
    *,
    subject_uri: str,
) -> tuple[FactoryState, Any, Any]:
    artifacts: list[Artifact] = []
    by_handle: dict[str, Any] = {}
    blobs: dict[str, bytes] = {}

    for c in criteria:
        handle = emit_artifact(c, CRITERION_KIND, "product_manager", c.seq)
        artifacts.append(handle)
        by_handle[handle.uri] = c

    for diff, diff_uri, blob in diffs:
        handle = Artifact(
            kind=FEATURE_DIFF_KIND,
            uri=subject_uri,
            produced_by="junior_engineer",
            task_id=diff.task_id,
            seq=diff.seq,
        )
        artifacts.append(handle)
        by_handle[handle.uri] = diff
        blobs[diff_uri] = blob

    def _resolve(handle: Artifact) -> Any:
        return by_handle[handle.uri]

    def _resolve_blob(uri: str) -> bytes:
        return blobs[uri]

    state: FactoryState = {"request": "ship the feature", "artifacts": artifacts}
    return state, _resolve, _resolve_blob


_SUBJECT_URI = "cas://feature_diff/T1"


def _clean_files() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


def _config(step: int = 1) -> RunnableConfig:
    # verdict_nonce / subject_artifact_uri / state_sha are RUN INPUTS the runner
    # threads onto the config (Q1), exactly like thread_id (AR2). Raise the
    # recursion limit so the bounded fix loop has room to terminate on its own.
    return {
        "configurable": {
            "thread_id": RUN_ID,
            "verdict_nonce": "N-server-issued",
            "subject_artifact_uri": _SUBJECT_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": step},
        "recursion_limit": 64,
    }


def _install(records: GlobalRecords) -> Any:
    return set_gate_write_verifier(DevGateWriteVerifier(KEY, KEY_ID, records))


def _artifacts(state: FactoryState) -> list[Artifact]:
    return list(state.get("artifacts") or ())


# ---- the builder returns an UNCOMPILED StateGraph ----------------------------


def test_build_assurance_subgraph_returns_uncompiled_state_graph() -> None:
    records = GlobalRecords()
    g = build_assurance_subgraph(
        _runtime(),
        runner=_runner(records),
        resolve=lambda h: h,
        resolve_blob=lambda u: b"",
    )
    assert isinstance(g, StateGraph)
    assert not hasattr(g, "invoke")  # compilation is the caller's job


# ---- a CLEAN change: agents -> gates PASS -> reviewer approve -> advance ------


def test_clean_change_runs_agents_gates_pass_reviewer_approve_advance() -> None:
    records = GlobalRecords()
    token = _install(records)
    try:
        diff = _feature_diff(_clean_files())
        criteria = (_criterion("c1"), _criterion("c2", security=True))
        state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)

        app = build_assurance_subgraph(
            _runtime(_transport(default_verdict="approve")),
            runner=_runner(records),
            resolve=resolve,
            resolve_blob=resolve_blob,
            tier=_tier(RiskTier.HIGH),
        ).compile()
        final: FactoryState = app.invoke(state, _config())
    finally:
        reset_gate_write_verifier(token)

    # The security agents authored advisory triage (never a GateResult; L5).
    triage_names = {t.name for t in (final.get("gate_triage") or ())}
    assert {"security", "audit", "supply_chain"} <= triage_names

    by_name = {g.name: g for g in final["gates"]}
    # Every tier-relevant assurance gate is an ATTESTED PASS...
    for name in ("security", "audit", "supply_chain", "e2e"):
        assert by_name[name].status is GateStatus.PASS, name
    # ...and the reviewer's proxy-stamped approve is admitted (PASS verdict).
    assert by_name["review"].status is GateStatus.PASS
    assert by_name["review"].judgment_binding is not None  # the PROXY signed it

    # No stable-FAIL latch, no fix attempts spent: a clean advance.
    assert not records.stable_fail_latched(by_name["security"].run_against, "security")
    budget = final.get("budget")
    assert budget is None or budget.fix_attempts_since_all_green == 0


# ---- a KNOWN-VULNERABLE change is CAUGHT through the subgraph -----------------


def test_known_vulnerable_change_caught_no_advance() -> None:
    records = GlobalRecords()
    token = _install(records)
    try:
        files = {"app.py": _VULN_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}
        diff = _feature_diff(files)
        criteria = (_criterion("c1", security=True),)
        state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)

        app = build_assurance_subgraph(
            _runtime(_transport(default_verdict="approve")),  # even with an approve...
            runner=_runner(records),
            resolve=resolve,
            resolve_blob=resolve_blob,
            tier=_tier(RiskTier.HIGH),
            max_fix_attempts=2,
        ).compile()
        final: FactoryState = app.invoke(state, _config())
    finally:
        reset_gate_write_verifier(token)

    by_name = {g.name: g for g in final["gates"]}
    security = by_name["security"]
    # The Security gate caught the hardcoded secret — an ATTESTED FAIL.
    assert security.status is GateStatus.FAIL
    assert any("hardcoded-password" in f for f in security.findings)
    # Runner-attested -> it reached the trust-filtered ``gates`` channel during
    # the run (no agent could have forged it; only attested verdicts are admitted
    # by the REAL merge_gates the graph folds through, L5).
    assert security.judgment_binding is None and security.runner_attestation is not None
    # The vulnerable change is BLOCKED: a FAIL on the integrated digest latches
    # stable-FAIL (terminal) -> the run halted, it did NOT advance.
    assert records.stable_fail_latched(security.run_against, "security")


# ---- a request-changes / FAIL drives a BOUNDED rework loop that TERMINATES ----


def test_request_changes_drives_bounded_rework_loop_that_terminates() -> None:
    records = GlobalRecords()
    token = _install(records)
    try:
        diff = _feature_diff(_clean_files())  # gates PASS; the REVIEWER rejects
        criteria = (_criterion("c1"),)
        state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)

        # The reviewer always requests-changes -> every iteration is non-advancing
        # -> the loop must terminate within max_fix_attempts (not hang).
        app = build_assurance_subgraph(
            _runtime(_transport(default_verdict="request-changes")),
            runner=_runner(records),
            resolve=resolve,
            resolve_blob=resolve_blob,
            tier=_tier(RiskTier.LOW),  # floor only -> a small, fast scoped set
            max_fix_attempts=2,
        ).compile()
        # If the bound did not converge this would raise GraphRecursionError
        # (recursion_limit) — reaching a terminal state proves it HALTS, not hangs.
        final: FactoryState = app.invoke(state, _config())
    finally:
        reset_gate_write_verifier(token)

    # The convergence bound was spent: the loop ran max_fix_attempts iterations
    # then halted (it did NOT advance, and it did NOT loop forever).
    budget = final.get("budget")
    assert budget is not None
    assert budget.fix_attempts_since_all_green == 2  # == max_fix_attempts -> halt
    # The terminal phase is the fix node's mark (the last superstep before halt
    # routed to END) — proof the rework path was taken, bounded.
    assert final.get("phase") in {"assurance_fix", "assurance_gate"}


# ---- ERROR (no lockfile) also drives a bounded loop that terminates -----------


def test_gate_error_drives_bounded_rework_loop_that_terminates() -> None:
    records = GlobalRecords()
    token = _install(records)
    try:
        # No uv.lock -> the Audit gate ERRORs (inventory not rederivable, fail-
        # closed). ERROR never latches (DS7) -> bounded fix -> halt.
        files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}
        diff = _feature_diff(files)
        criteria = (_criterion("c1"),)
        state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)

        app = build_assurance_subgraph(
            _runtime(_transport(default_verdict="approve")),
            runner=_runner(records),
            resolve=resolve,
            resolve_blob=resolve_blob,
            tier=_tier(RiskTier.LOW),
            max_fix_attempts=3,
        ).compile()
        final: FactoryState = app.invoke(state, _config())
    finally:
        reset_gate_write_verifier(token)

    by_name = {g.name: g for g in final["gates"]}
    assert by_name["audit"].status is GateStatus.ERROR
    # ERROR never latches; the loop converged on the bound (no infinite cycle).
    assert not records.stable_fail_latched(by_name["audit"].run_against, "audit")
    budget = final.get("budget")
    assert budget is not None
    assert budget.fix_attempts_since_all_green == 3  # bound spent -> halt


# ---- re-verification scoping: a rework re-runs ONLY the scoped gates ----------


def test_rework_reverification_scopes_the_rerun_set() -> None:
    """A rework iteration re-runs only the re-verification-scoped gates (gov §1f),
    NOT the full assurance family — asserted via ``rework_scoped_gates`` (the plan)."""
    records = GlobalRecords()
    runner = _runner(records)
    tier = _tier(RiskTier.HIGH)

    full = assurance_gate_set(tier)
    assert set(full) == {"security", "audit", "supply_chain", "e2e"}

    # Build genuine attested prior verdicts via the runner over a tiny workdir so
    # the reverification plan has real records to reason about.
    import tempfile
    from pathlib import Path

    from foundry.graph.assurance_wiring import _assurance_gates, materialize_assurance_workdir

    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}
    diff = _feature_diff(files)
    criteria = (_criterion("c1"),)
    state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)

    token = _install(records)
    try:
        wd = Path(tempfile.mkdtemp())
        run_against = materialize_assurance_workdir(
            state, wd, resolve=resolve, resolve_blob=resolve_blob
        )
        prior: list[GateResult] = []
        for node_id, gate in _assurance_gates(tier, allow_build_changes=False):
            seq = _seq(node_id, f"verdict:{gate.name}")
            prior.append(runner.run(gate, run_against, seq, wd))
        prior_state: FactoryState = {**state, "gates": merge_gates(prior, None)}

        # All gates PASSed on this clean tree -> the scope is disjoint + reuse-key
        # matches -> the rework re-runs a STRICT SUBSET (here: none re-run), never
        # the full set. The key property: the scoped set != the full family.
        scoped = rework_scoped_gates(prior_state, runner=runner, tier=tier)
        assert set(scoped) <= set(full)
        assert set(scoped) != set(full)  # change-proportional, not a full re-run

        # If a gate's prior verdict is NOT a PASS, the rework re-runs exactly it
        # (fail-closed: the fix targets the unsatisfied gate's inputs).
        downgraded = [
            (g.model_copy(update={"status": GateStatus.FAIL}) if g.name == "security" else g)
            for g in prior
        ]
        ds: FactoryState = {**state, "gates": downgraded}
        scoped2 = rework_scoped_gates(ds, runner=runner, tier=tier)
        assert "security" in scoped2  # the unsatisfied gate re-runs
        assert set(scoped2) <= set(full)
    finally:
        reset_gate_write_verifier(token)


# ---- the bounded-loop mechanism: the bump is read post-fold (no infinite loop) -


def test_bounded_loop_bump_is_monotonic_and_halts() -> None:
    """The convergence bound is bumped once per non-advancing iteration and read
    post-fold, so it can only ever increase toward the cap (mirrors the
    supervisor's mechanism; SUP4)."""
    records = GlobalRecords()
    token = _install(records)
    try:
        diff = _feature_diff(_clean_files())
        criteria = (_criterion("c1"),)
        state, resolve, resolve_blob = _seed(criteria, (diff,), subject_uri=_SUBJECT_URI)
        state = {**state, "budget": BudgetLedger()}

        app = build_assurance_subgraph(
            _runtime(_transport(default_verdict="request-changes")),
            runner=_runner(records),
            resolve=resolve,
            resolve_blob=resolve_blob,
            tier=_tier(RiskTier.LOW),
            max_fix_attempts=1,  # one fix iteration, then halt
        ).compile()
        final: FactoryState = app.invoke(state, _config())
    finally:
        reset_gate_write_verifier(token)

    budget = final.get("budget")
    assert budget is not None
    assert budget.fix_attempts_since_all_green == 1  # halted at the bound
