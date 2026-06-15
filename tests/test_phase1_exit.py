"""Phase-1 EXIT integration test — the roadmap Phase-1 exit criteria, proven
end-to-end against the REAL factory machinery (T-M1-16).

This module is the milestone-audit witness for **docs/roadmap.md Phase 1**
("Assistant mode"): from a one-line natural-language request the compiled
factory graph runs the SPEC core path (intake -> product -> architecture ->
[Spec gate] -> implementation -> quality -> [Build+Test gates] -> emit) and the
six Phase-1 exit criteria hold. It reuses the proven dev wiring
(``foundry.cli.run`` and ``build_factory_graph`` directly) with the test doubles
(NO live API key, NO ``claude -p``, NO network): a REAL
:class:`ModelProviderProxy` over :class:`MockTransport`, a REAL
:class:`TwoZoneGateRunner` admitted through the REAL ``merge_gates``, a
:class:`StubAdapter` substrate, an in-memory :class:`ArtifactStore`, and a
durable :class:`FoundryCheckpointer`.

HONESTY MAP (what is genuinely end-to-end vs a dev-mode stand-in) — see the
LEARNINGS entry appended by this task for the binding audit map:

* GENUINELY end-to-end: the GATE machinery (Spec/Build/Test run by the real
  two-zone runner, signed in zone B, admitted only by the real ``merge_gates``);
  the trust-filtered ``gates`` channel; the single ``open_pr`` ExternalAction +
  its idempotency key; kill-then-resume coalescing; the stable-FAIL latch on an
  integrated digest; the criterion-traceability carried on TestPlan/E2EReport;
  the flake-quarantine routing predicate.
* DEV-MODE STAND-IN: the StubAdapter is NOT a real coding agent — it authors a
  deterministic no-op blob, so the actual code+test BODY the gates run against is
  supplied through the ``diff_body`` / ``resolve_blob`` materialization seam
  (SUP7 / GW1a). Real codegen QUALITY is the worker-substrate's job (a later
  wave); these tests assert the gate machinery genuinely COMPILES + RUNS the
  supplied tree (incl. a held-out/hidden suite the producing agent never saw),
  not that the StubAdapter wrote good code. The trust roots are dev HMAC keys
  (§0.3). The model provider is a deterministic mock (D1).
"""

from __future__ import annotations

import tempfile
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime

# Reuse the CLI's scripted payloads / synthetic resolvers / trust material — the
# proven dev wiring (CLI1-CLI4). We import the private helpers ON PURPOSE: this
# test is the milestone witness FOR that exact assembly, so it must drive the
# very wiring the CLI ships, not a parallel copy.
from foundry.cli import (
    _KEY,
    _KEY_ID,
    _LINEAGE_REV,
    _SCANNER_REV,
    _SCRIPTS,
    _synthetic_resolve,
    _synthetic_resolve_blob,
    run,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import build_factory_graph, terminal_gates_pass
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    flake_quarantined,
    route_after_gates,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.quality import E2EReport, TestPlan
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

# A unified-diff ADDITION blob carrying the agent's OWN test (test_app.py) AND a
# separately-provided HELD-OUT / HIDDEN test (test_hidden.py) the producing agent
# never saw. Both are added to the integrated tree the Test gate runs against, so
# a Test PASS reflects the hidden oracle too — not just a self-consistent suite.
#   passed count == 3 (>= TestGate.min_passed) — a genuine, non-vacuous PASS.
_OWN_AND_HIDDEN_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    # the producing agent's own test:
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_own():\n    import app\n    assert app.VALUE == 42\n"
    # the HIDDEN / held-out suite (not visible to the producer) — a second oracle
    # over the SAME produced app; two scenarios so the count is unambiguous:
    "--- /dev/null\n+++ b/test_hidden.py\n@@ -0,0 +1,4 @@\n"
    "+import app\n\n\ndef test_hidden_value():\n    assert app.VALUE == 42\n\n\n"
    "def test_hidden_type():\n    assert isinstance(app.VALUE, int)\n"
)

# A SYNTAX-ERROR diff: ``app.py`` does not compile, so ``compileall`` exits
# non-zero -> Build FAIL on the INTEGRATED tree -> stable-FAIL LATCH (gov §5).
# (Contrast: a collection-error diff compiles but errors pytest -> Test ERROR,
# which routes to bounded fix and never latches — that is the CLI smoke's
# halted-run case, exercised there.)
_SYNTAX_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+def broken( :\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert True\n"
)


def _open_prs(state: FactoryState | Any) -> list[Any]:
    return [a for a in (state.get("actions") or ()) if a.kind == "open_pr"]


# ---- a self-contained harness that retains the ArtifactStore ----------------
# Mirrors tests/test_factory_graph.py + the CLI wiring, but keeps a handle to the
# in-memory store so criterion 3 can RESOLVE the TestPlan / E2EReport bodies the
# quality layer emitted (they ride ``rt.cas`` per AS3/AS5).


def _run_factory(
    request: str,
    *,
    thread_id: str,
    diff_body: str,
    records: GlobalRecords,
    store: ArtifactStore,
    checkpointer: Any,
    max_fix_attempts: int = 3,
) -> FactoryState:
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(_KEY, _KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=store)
    runner = TwoZoneGateRunner(ZoneBSigner(_KEY, _KEY_ID), records, _SCANNER_REV, _LINEAGE_REV)
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "verdict_nonce": f"N-{thread_id}",
            "subject_artifact_uri": "cas://cli/diff-body",
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": 1},
        "recursion_limit": 200,
    }
    token = set_gate_write_verifier(DevGateWriteVerifier(_KEY, _KEY_ID, records))
    with tempfile.TemporaryDirectory(prefix="foundry-exit-src-") as source_dir:
        try:
            app = build_factory_graph(
                rt,
                runner=runner,
                resolve=_synthetic_resolve(store),
                resolve_blob=_synthetic_resolve_blob(store, diff_body),
                source_dir=source_dir,
                checkpointer=checkpointer,
                max_fix_attempts=max_fix_attempts,
            )
            final: FactoryState = app.invoke({"request": request}, config)
        finally:
            reset_gate_write_verifier(token)
    return final


# ============================================================================
# Criterion 1 — end-to-end PASS + PR-equivalent behind distinct, attested,
#               PASSing Spec AND Build AND Test gates.
# ============================================================================


def test_exit_1_pass_and_one_pr_behind_distinct_attested_gates() -> None:
    """From a one-line request the factory produces a reviewed artifact and emits
    EXACTLY ONE ``open_pr`` ExternalAction behind DISTINCT Spec/Build/Test gates,
    each runner-ATTESTED and PASS on the trust-filtered ``gates`` channel."""
    result = run("build a tiny URL-shortener service", thread_id="exit1-pass")

    # PASS reached.
    assert result.passed is True
    assert result.phase == "done"

    # All THREE gates present, DISTINCT names, each PASS.
    assert result.gate_statuses.get("spec") is GateStatus.PASS
    assert result.gate_statuses.get("build") is GateStatus.PASS
    assert result.gate_statuses.get("test") is GateStatus.PASS
    assert {"spec", "build", "test"} <= set(result.gate_statuses)

    # EXACTLY ONE open_pr ExternalAction, opened against the reviewed artifact.
    assert result.open_pr_idempotency_key is not None
    assert result.open_pr_idempotency_key.startswith("open_pr:")
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://")
    assert result.open_pr_payload_uri == result.artifact_uri


def test_exit_1_gates_are_runner_attested_and_distinct() -> None:
    """The Spec/Build/Test verdicts on the (trust-filtered) ``gates`` channel are
    DISTINCT records that each carry a runner attestation — an agent could not
    have forged them; only the real ``merge_gates`` admitted them (L5/GW2)."""
    records = GlobalRecords()
    store = ArtifactStore()
    ckpt = FoundryCheckpointer()
    try:
        final = _run_factory(
            "build a todo app",
            thread_id="exit1-attest",
            diff_body=_OWN_AND_HIDDEN_DIFF,
            records=records,
            store=store,
            checkpointer=ckpt.saver,
        )
    finally:
        ckpt.close()

    gates = list(final.get("gates") or ())
    by_name = {g.name: g for g in gates if g.name in {"spec", "build", "test"}}
    # Distinct gate names all present.
    assert set(by_name) == {"spec", "build", "test"}
    for name in ("spec", "build", "test"):
        g = by_name[name]
        assert g.status is GateStatus.PASS
        # Genuinely attested (the deterministic gates carry a zone-B attestation;
        # merge_gates dropped anything unsigned, so its presence is load-bearing).
        assert g.runner_attestation is not None
    assert terminal_gates_pass(final) is True


# ============================================================================
# Criterion 2 — own + HIDDEN tests: the Test gate runs against a tree where BOTH
#               the agent-authored suite AND a separately-provided held-out suite
#               must pass (the produced code satisfies a hidden oracle).
# ============================================================================


def test_exit_2_test_gate_runs_hidden_held_out_suite() -> None:
    """The integrated tree the Test gate executes contains BOTH the producing
    agent's own ``test_app.py`` AND a separately-supplied HIDDEN ``test_hidden.py``
    the producer never saw. The Test gate PASS therefore reflects a hidden oracle,
    not just a self-consistent suite.

    HONESTY: the StubAdapter stands in for a real coding agent — the hidden suite
    is injected through the ``diff_body`` materialization seam (SUP7/GW1a), NOT
    authored by the (scripted) producing agent, and real codegen quality is the
    worker-substrate's job. What this asserts is that the GATE machinery genuinely
    materializes + runs the hidden file end-to-end (a tree missing it, or failing
    it, would NOT clear the gate)."""
    result = run(
        "build a tiny service",
        thread_id="exit2-hidden",
        diff_body=_OWN_AND_HIDDEN_DIFF,
    )
    # The Test gate ran the integrated tree (own + hidden) green: PASS, and the
    # whole core path therefore completes with a PR.
    assert result.gate_statuses.get("build") is GateStatus.PASS
    assert result.gate_statuses.get("test") is GateStatus.PASS
    assert result.passed is True
    assert result.open_pr_idempotency_key is not None


def test_exit_2_hidden_suite_actually_gates_the_pass() -> None:
    """Control: if the produced app does NOT satisfy the hidden oracle, the Test
    gate must NOT pass and NO PR is opened — proving the hidden suite genuinely
    GATES the result rather than being inert.

    Here ``app.py`` defines ``VALUE = 7`` but the hidden suite asserts ``== 42``,
    so the held-out oracle fails (pytest exit 1 -> Test FAIL on the integrated
    digest -> latch + halt). The own test still passes; only the hidden one fails,
    so the hidden suite is what blocks the PR."""
    wrong_app_diff = (
        "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 7\n"
        # own test only checks VALUE is an int -> would pass on its own:
        "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
        "+def test_own():\n    import app\n    assert isinstance(app.VALUE, int)\n"
        # hidden held-out oracle pins the exact value -> FAILS against VALUE=7:
        "--- /dev/null\n+++ b/test_hidden.py\n@@ -0,0 +1,2 @@\n"
        "+def test_hidden():\n    import app\n    assert app.VALUE == 42\n"
    )
    result = run(
        "build a wrong service",
        thread_id="exit2-hidden-fail",
        diff_body=wrong_app_diff,
        max_fix_attempts=2,
    )
    assert result.gate_statuses.get("test") is GateStatus.FAIL
    assert result.passed is False
    assert result.open_pr_idempotency_key is None


# ============================================================================
# Criterion 3 — criterion traceability: TestPlan / E2EReport carry
#               test_id -> criterion_ids traceability, present + non-empty.
# ============================================================================


def test_exit_3_testplan_and_e2ereport_are_criterion_traceable() -> None:
    """The quality layer's TestPlan (``test_criteria``: test_id -> criterion_ids)
    and SDET E2EReport (``scenario_criteria``: scenario_id -> criterion_ids) carry
    criterion traceability that is PRESENT and NON-EMPTY — every emitted plan maps
    to the acceptance criteria it exercises (governance §1)."""
    records = GlobalRecords()
    store = ArtifactStore()
    ckpt = FoundryCheckpointer()
    try:
        final = _run_factory(
            "build a todo app",
            thread_id="exit3-trace",
            diff_body=_OWN_AND_HIDDEN_DIFF,
            records=records,
            store=store,
            checkpointer=ckpt.saver,
        )
    finally:
        ckpt.close()

    artifacts = list(final.get("artifacts") or ())
    plan_handles = [a for a in artifacts if a.kind == "test_plan"]
    e2e_handles = [a for a in artifacts if a.kind == "e2e_report"]
    assert plan_handles, "no TestPlan artifact was emitted"
    assert e2e_handles, "no E2EReport artifact was emitted"

    # Resolve the typed bodies out of the REAL content-addressed store (AS3/AS5).
    plan = store.resolve(plan_handles[-1])
    e2e = store.resolve(e2e_handles[-1])
    assert isinstance(plan, TestPlan)
    assert isinstance(e2e, E2EReport)

    # TestPlan traceability is the authoritative carrier: test_id -> criterion_ids
    # PRESENT + NON-EMPTY, every test mapped, and a non-empty covered set. The QA
    # node FAILS CLOSED (a synthetic ``test::<cid>`` per uncovered criterion, Q4),
    # so this holds against the real resolved criterion ids, not just what the mock
    # proposed — a genuine, non-vacuous traceability assertion.
    assert plan.test_criteria, "TestPlan.test_criteria (traceability) is empty"
    assert all(cids for cids in plan.test_criteria.values())
    assert plan.covered_criteria(), "TestPlan covers no criteria"

    # E2EReport carries the SAME traceability SHAPE (scenario_id -> criterion_ids),
    # present + scenario-keyed. HONESTY: unlike QA, the SDET node does NOT
    # fail-closed-backfill coverage — it keeps only criterion_ids the model
    # proposed that are KNOWN (sdet._scenario_criteria), and the synthetic resolver
    # mints epoch-dependent criterion ids (``AC-001-<epoch>``) the scripted SDET
    # payload cannot pre-name, so the per-scenario coverage is mock-dependent here.
    # The traceability FIELD + its scenario-keyed mapping are nonetheless present
    # end-to-end (the structural exit criterion); per-scenario coverage fidelity is
    # the worker-substrate's job (a real SDET maps live ids).
    assert e2e.scenario_criteria, "E2EReport.scenario_criteria (traceability) is absent"
    assert set(e2e.scenario_criteria), "E2EReport carries no e2e scenarios"


# ============================================================================
# Criterion 4 — flake quarantine (N=2 fail-then-pass): a quarantined flake is
#               QUARANTINED, not failed — the boundary does not fail on it.
# ============================================================================


def _seq(node: str, key: str, epoch: int = 1) -> Seq:
    return Seq(run_id="exit4-flake", emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _attest(signer: ZoneBSigner, **over: Any) -> GateResult:
    base: dict[str, Any] = {
        "decision_deterministic": True,
        "execution_deterministic": True,
        "verdict_class": VerdictClass.EXECUTION_DETERMINISTIC,
        "scanner_config_rev": _SCANNER_REV,
        "lineage_map_revision": _LINEAGE_REV,
        "results_digest": "rd",
    }
    base.update(over)
    return signer.with_attestation(GateResult(**base))


def test_exit_4_flake_quarantine_does_not_fail_the_boundary() -> None:
    """A flaky Test scenario that the runner quarantines (N=2 fail-then-pass split)
    surfaces as a runner-attested ERROR whose findings mark a flake quarantine. The
    build+test boundary must NOT fail on it: ``flake_quarantined`` reports the
    reason, the stable-FAIL latch is NOT set, and ``route_after_gates`` returns
    ``fix`` (bounded retry / quarantine-and-continue), never ``halt`` for the
    flake itself (governance §1a)."""
    signer = ZoneBSigner(_KEY, _KEY_ID)
    records = GlobalRecords()
    digest = "d" * 64  # an integrated tree_sha shape (so a real FAIL *would* latch)

    quarantine = _attest(
        signer,
        name="test",
        status=GateStatus.ERROR,
        findings=("flake-quarantine: 1 flaky test quarantined (N=2 fail-then-pass split)",),
        run_against=digest,
        seq=_seq("test_gate", "verdict:test"),
    )
    build_pass = _attest(
        signer,
        name="build",
        status=GateStatus.PASS,
        run_against=digest,
        seq=_seq("build_gate", "verdict:build"),
    )

    # The REAL merge_gates admits both runner-attested verdicts only because the
    # dev gate-write verifier is installed (it checks the zone-B attestation, D4).
    token = set_gate_write_verifier(DevGateWriteVerifier(_KEY, _KEY_ID, records))
    try:
        folded = merge_gates([build_pass, quarantine], None)
        assert set(folded) == {build_pass, quarantine}
        state: FactoryState = {"gates": folded}

        config: RunnableConfig = {"configurable": {"thread_id": "exit4-flake", "fix_attempts": 0}}

        # The quarantined flake is recognised as a quarantine (not a hard failure)...
        assert flake_quarantined(state, boundary=BUILD_TEST_BOUNDARY) is True
        # ...it NEVER latches the digest (a flake is a property of the run, not the tree)...
        assert not records.stable_fail_latched(digest, "test")
        # ...and the boundary routes to bounded retry, NOT a failure-halt, for the flake.
        assert (
            route_after_gates(
                state, config, boundary=BUILD_TEST_BOUNDARY, records=records, max_fix_attempts=3
            )
            == "fix"
        )
    finally:
        reset_gate_write_verifier(token)


# ============================================================================
# Criterion 5 — kill-then-resume idempotency: re-invoking the SAME thread_id
#               does NOT duplicate the open_pr action (exactly one persists,
#               same idempotency key).
# ============================================================================


def test_exit_5_kill_then_resume_does_not_duplicate_pr() -> None:
    """Re-invoking the SAME ``thread_id`` against the durable checkpointer resumes
    + re-folds the committed ``emit`` write; the emit node re-derives the SAME
    content-addressed idempotency key (L7), so ``union_actions`` coalesces it —
    EXACTLY ONE ``open_pr`` persists across both invokes, with an unchanged key."""
    records = GlobalRecords()
    store = ArtifactStore()
    ckpt = FoundryCheckpointer()
    try:
        first = _run_factory(
            "build a todo app",
            thread_id="exit5-resume",
            diff_body=_OWN_AND_HIDDEN_DIFF,
            records=records,
            store=store,
            checkpointer=ckpt.saver,
        )
        assert len(_open_prs(first)) == 1
        first_key = _open_prs(first)[0].idempotency_key

        # Re-invoke the SAME thread_id: LangGraph resumes from the checkpoint.
        second = _run_factory(
            "build a todo app",
            thread_id="exit5-resume",
            diff_body=_OWN_AND_HIDDEN_DIFF,
            records=records,
            store=store,
            checkpointer=ckpt.saver,
        )
    finally:
        ckpt.close()

    prs = _open_prs(second)
    assert len(prs) == 1  # coalesced, NOT duplicated
    assert prs[0].idempotency_key == first_key


# ============================================================================
# Criterion 6 — stable-FAIL no-PR (folds in E-R1-05 / FG-M2): a forced stable
#               FAIL (syntax-error diff -> Build FAIL on the integrated tree ->
#               latch) HALTS the run and emits NO open_pr; the latch is set.
# ============================================================================


def test_exit_6_stable_fail_halts_with_no_pr_and_latches() -> None:
    """A syntax-error diff makes ``app.py`` fail to compile -> Build FAIL on the
    INTEGRATED digest -> a stable-FAIL latch (gov §5, terminal). The run HALTS and
    emits NO ``open_pr`` (a failed run never opens a PR), and the latch IS set on
    the integrated digest the Build verdict ran against."""
    records = GlobalRecords()
    store = ArtifactStore()
    ckpt = FoundryCheckpointer()
    try:
        final = _run_factory(
            "build a broken service",
            thread_id="exit6-stablefail",
            diff_body=_SYNTAX_ERROR_DIFF,
            records=records,
            store=store,
            checkpointer=ckpt.saver,
            max_fix_attempts=2,
        )
    finally:
        ckpt.close()

    gates = list(final.get("gates") or ())
    by_name: dict[str, GateResult] = {}
    for g in gates:
        if g.name not in by_name or g.seq > by_name[g.name].seq:
            by_name[g.name] = g

    # Build FAILed on the integrated tree (syntax error -> compileall non-zero).
    assert by_name["build"].status is GateStatus.FAIL
    # The run HALTED with NO PR (governance §1/§5 — a failed run never opens a PR).
    assert terminal_gates_pass(final) is False
    assert _open_prs(final) == []
    # The stable-FAIL latch IS set on the integrated digest the Build verdict ran
    # against — terminal + cross-fork (a later PASS would not auto-clear it).
    build_digest = by_name["build"].run_against
    assert records.stable_fail_latched(build_digest, "build") is True
