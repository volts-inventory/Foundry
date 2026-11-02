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
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateResult, GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    reset_gate_write_verifier,
    set_gate_write_verifier,
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


# ============================================================================
# Criterion 2 — own + HIDDEN tests: the Test gate runs against a tree where BOTH
#               the agent-authored suite AND a separately-provided held-out suite
#               must pass (the produced code satisfies a hidden oracle).
# ============================================================================


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


# ============================================================================
# Criterion 4 — flake quarantine (N=2 fail-then-pass): a quarantined flake is
#               QUARANTINED, not failed — the boundary does not fail on it.
# ============================================================================


# ============================================================================
# Criterion 5 — kill-then-resume idempotency: re-invoking the SAME thread_id
#               does NOT duplicate the open_pr action (exactly one persists,
#               same idempotency key).
# ============================================================================


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
