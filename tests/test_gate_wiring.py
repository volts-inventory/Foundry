"""Graph-side gate wiring — the trust seam at phase boundaries (T-M1-12).

Exercises ``foundry/graph/gate_wiring.py`` end to end against the REAL
``TwoZoneGateRunner`` + REAL ``merge_gates``: materialize an integrated workdir
from seeded artifacts, run Spec / Build / Test through the runner, and assert

* a signed PASS path advances and the attested verdicts are ADMITTED by the
  real ``merge_gates`` (an agent could not have forged them);
* an UNSIGNED GateResult is dropped (only runner-attested verdicts count);
* a zone-A exception is a signed ERROR that routes to retry and never latches;
* an integrated FAIL (criteria omission → Spec FAIL) latches stable-FAIL, and a
  later PASS does not auto-clear it;
* the adequacy floor: a vacuous (0-test) suite is FAIL, not PASS;
* a flake-quarantine (N=2 split) Test ERROR does not fail the boundary.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.execution.adapter_contract import TaskPacket
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    CRITERIA_CRITIQUE_KIND,
    CRITERION_KIND,
    FEATURE_DIFF_KIND,
    SPEC_BOUNDARY,
    FeatureDiffBodyError,
    PathContainmentError,
    _parse_addition_diff,
    flake_quarantined,
    materialize_workdir,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.schemas.gates import (
    GateResult,
    GateStatus,
    JudgmentProvenance,
    VerdictClass,
)
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, CriteriaCritique
from foundry.state.factory_state import FactoryState
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import (
    merge_gates,
    reset_gate_write_verifier,
    set_gate_write_verifier,
)
from foundry.state.seq import Seq

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
RUN_ID = "run-1"


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


@pytest.fixture
def verifier(records: GlobalRecords):
    """Install the matching verifier on the reducer contextvar; reset on teardown."""
    v = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(v)
    try:
        yield v
    finally:
        reset_gate_write_verifier(token)


@pytest.fixture
def rt() -> AgentRuntime:
    # gate_wiring never calls the proxy; rt is only used for rt.seq epochs.
    proxy = ModelProviderProxy(MockTransport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def _config(step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": RUN_ID}, "metadata": {"langgraph_step": step}}


def _seq(node: str, key: str, epoch: int = 1) -> Seq:
    return Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


# ---- seeding artifacts + the in-memory resolver -----------------------------


def _criterion(cid: str, *, security: bool = False) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given X When Y Then Z ({cid})",
        oracle_hint=f"test_{cid}",
        security_abuse=security,
        seq=_seq("product_manager", f"criterion:{cid}"),
    )


def _addition_diff(files: dict[str, str]) -> str:
    """A real unified-diff ADDITION blob — the body the adapters emit via put_blob.

    Mirrors the producer contract: ``--- /dev/null`` / ``+++ b/<path>`` / ``@@``
    with every added line ``+``-prefixed. This is the SAME shape gate_wiring's
    Phase-1 applier consumes off ``sandbox.get_blob`` — NOT a {uri: dict} that no
    emitter produces.
    """
    parts: list[str] = []
    for path in sorted(files):
        body = files[path]
        lines = body.splitlines(keepends=True)
        parts.append(f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n")
        parts.extend(f"+{ln}" for ln in lines)
    return "".join(parts)


def _feature_diff(files: dict[str, str], *, task_id: str = "T1") -> tuple[FeatureDiff, str, bytes]:
    """A FeatureDiff + its diff_uri + the unified-diff blob body stored behind it."""
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


def _critique(*, omissions: tuple[str, ...] = ()) -> CriteriaCritique:
    """A Criteria-Completeness Critic finding — ``security_abuse_omissions`` is the
    blocking input the Spec gate re-derives a FAIL from (it carries NO verdict)."""
    return CriteriaCritique(
        prd_uri="cas://sha256/prd",
        security_abuse_omissions=omissions,
        judgment_provenance=JudgmentProvenance.INDEPENDENT,
        seq=_seq("criteria_completeness_critic", "artifact:criteria_findings"),
    )


def _seed(
    criteria: tuple[AcceptanceCriterion, ...],
    diffs: tuple[tuple[FeatureDiff, str, bytes], ...] = (),
    *,
    critique: CriteriaCritique | None = None,
) -> tuple[FactoryState, Any, Any]:
    """Seed state with criterion + feature_diff HANDLES, a typed resolver, and a BlobResolver.

    Two DISTINCT seams (D-R1-01/02): ``resolve`` reconstitutes a TYPED artifact
    from its handle (criterion -> AcceptanceCriterion; feature_diff handle ->
    FeatureDiff); ``resolve_blob`` fetches the raw unified-diff BODY keyed on its
    ``cas://`` uri — exactly what ``sandbox.get_blob`` does in production.

    An optional ``critique`` seeds the Criteria-Completeness Critic's finding on
    the SAME ``criteria_findings`` handle the Spec gate's materializer reads.
    """
    artifacts: list[Artifact] = []
    by_handle: dict[str, Any] = {}
    blobs: dict[str, bytes] = {}

    for c in criteria:
        handle = emit_artifact(c, CRITERION_KIND, "product_manager", c.seq)
        artifacts.append(handle)
        by_handle[handle.uri] = c

    if critique is not None:
        handle = emit_artifact(
            critique, CRITERIA_CRITIQUE_KIND, "criteria_completeness_critic", critique.seq
        )
        artifacts.append(handle)
        by_handle[handle.uri] = critique

    for diff, diff_uri, blob in diffs:
        handle = Artifact(
            kind=FEATURE_DIFF_KIND,
            uri=f"cas://feature_diff/{diff.task_id}",
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

    state: FactoryState = {"artifacts": artifacts}
    return state, _resolve, _resolve_blob


_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"


# ---- 1. signed PASS path advances; verdicts admitted by real merge_gates ----


def test_signed_pass_path_advances(tmp_path, rt, runner, records, verifier):
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    spec_out = run_spec_gate(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "spec",
    )
    bt_out = run_build_test_gates(
        state,
        _config(2),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "bt",
    )

    spec = spec_out["gates"][0]
    build, test = bt_out["gates"]
    assert spec.name == "spec" and spec.status is GateStatus.PASS
    assert build.name == "build" and build.status is GateStatus.PASS
    assert test.name == "test" and test.status is GateStatus.PASS

    # Every verdict is runner-attested -> the REAL merge_gates admits all three.
    folded = merge_gates(spec_out["gates"] + bt_out["gates"], None)
    assert set(folded) == {spec, build, test}

    # Fold onto state and route: all relevant gates PASS, nothing latched.
    state_with = {**state, "gates": folded}
    assert (
        route_after_gates(state_with, _config(), boundary=SPEC_BOUNDARY, records=records)
        == "advance"
    )
    assert (
        route_after_gates(state_with, _config(), boundary=BUILD_TEST_BOUNDARY, records=records)
        == "advance"
    )


# ---- 2. an UNSIGNED GateResult is dropped by merge_gates --------------------


def test_unsigned_gateresult_dropped(verifier):
    """Only runner-attested verdicts count — an unsigned GateResult is dropped."""
    unsigned = GateResult(
        name="build",
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        scanner_config_rev=SCANNER_REV,
        lineage_map_revision=LINEAGE_REV,
        results_digest="rd",
        run_against="a" * 64,
        seq=_seq("build_gate", "verdict:build"),
    )
    assert merge_gates([unsigned], None) == []


# ---- 3. zone-A exception is a signed ERROR; routes to fix; no latch ----------


def test_zone_a_error_retries_without_latch(tmp_path, rt, runner, records, verifier):
    # Build gate with a missing tool -> GroundingUnavailable in zone A -> ERROR.
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, blob = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    from foundry.gates.build import BuildGate

    run_against = materialize_workdir(
        state, tmp_path / "wd", resolve=resolve, resolve_blob=resolve_blob
    )
    gate = BuildGate(cmd=("this-binary-does-not-exist-xyz",))
    result = runner.run(gate, run_against, _seq("build_gate", "verdict:build"), tmp_path / "wd")

    assert result.status is GateStatus.ERROR
    assert merge_gates([result], None) == [result]  # signed ERROR is still admitted
    # ERROR never latches (infra, not a verdict, DS7).
    assert not records.stable_fail_latched(run_against, "build")

    state_with = {**state, "gates": [result]}
    # Within the convergence bound -> fix/retry, not halt.
    assert (
        route_after_gates(
            state_with, _config(), boundary=BUILD_TEST_BOUNDARY, records=records, max_fix_attempts=3
        )
        == "fix"
    )


# ---- 4. integrated FAIL: digest disqualified forever, but fix-forward bounded -


def test_integrated_fail_latches_but_fixes_forward_then_halts_when_spent(
    tmp_path, rt, runner, records, verifier
):
    # Criteria omit a security_abuse criterion -> Spec FAIL on the integrated digest.
    bad_state, bad_resolve, bad_blob = _seed((_criterion("c1"),))  # no security criterion
    spec_out = run_spec_gate(
        bad_state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=bad_resolve,
        resolve_blob=bad_blob,
        workdir=tmp_path / "bad",
    )
    fail = spec_out["gates"][0]
    assert fail.status is GateStatus.FAIL
    # The failing digest is permanently disqualified (anti-gaming, gov §5).
    assert records.stable_fail_latched(fail.run_against, "spec")

    state_with = {**bad_state, "gates": spec_out["gates"]}
    # WITHIN the bound: route to a FRESH attempt (fix forward) — not a dead stop.
    assert (
        route_after_gates(state_with, _config(), boundary=SPEC_BOUNDARY, records=records) == "fix"
    )

    # The latch is NEVER cleared: even a (flaky/gamed) PASS on the SAME digest does
    # not advance — it routes to another fresh attempt, never shipping that tree.
    good_pass = fail.model_copy(update={"status": GateStatus.PASS})
    assert records.stable_fail_latched(fail.run_against, "spec")
    assert (
        route_after_gates(
            {**bad_state, "gates": [good_pass]}, _config(), boundary=SPEC_BOUNDARY, records=records
        )
        == "fix"
    )

    # Once the convergence bound is SPENT, escalate (halt) — don't loop forever.
    spent = {"configurable": {"thread_id": RUN_ID, "fix_attempts": 3}}
    assert (
        route_after_gates(state_with, spent, boundary=SPEC_BOUNDARY, records=records) == "halt"
    )


# ---- 4b. critic-flagged security/abuse omission FAILS the Spec gate ----------


def test_critic_security_omission_fails_spec_no_pr(tmp_path, rt, runner, records, verifier):
    """ADVERSARIAL (plan [4]): the criteria are STRUCTURALLY complete — every
    criterion well-formed, a security_abuse criterion present — so the structural
    rules alone would PASS. But the Criteria-Completeness Critic flagged an
    applicable security/abuse omission (``security_abuse_omissions``). That finding
    is materialized into the workdir as a gate input, and the Spec gate re-derives
    a FAIL from it -> the boundary halts -> no PR (gov §1b, LEARNINGS L5).
    """
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    critique = _critique(omissions=("no rate-limit abuse criterion",))
    state, resolve, resolve_blob = _seed(criteria, critique=critique)

    spec_out = run_spec_gate(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "blocked",
    )
    fail = spec_out["gates"][0]
    assert fail.name == "spec" and fail.status is GateStatus.FAIL
    # Attested FAIL on an integrated digest -> latched (digest disqualified) + routes
    # to the bounded fix loop (fix forward into a new tree), HALT once the bound spends.
    assert merge_gates(spec_out["gates"], None) == spec_out["gates"]
    assert records.stable_fail_latched(fail.run_against, "spec")
    state_with = {**state, "gates": spec_out["gates"]}
    assert (
        route_after_gates(state_with, _config(), boundary=SPEC_BOUNDARY, records=records) == "fix"
    )
    spent = {"configurable": {"thread_id": RUN_ID, "fix_attempts": 3}}
    assert (
        route_after_gates(state_with, spent, boundary=SPEC_BOUNDARY, records=records) == "halt"
    )


def test_critic_no_omission_passes_spec(tmp_path, rt, runner, records, verifier):
    """Inverse: the SAME structurally-complete criteria with an EMPTY
    ``security_abuse_omissions`` critique PASS — the finding does not block."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    critique = _critique(omissions=())  # critic found nothing to block on
    state, resolve, resolve_blob = _seed(criteria, critique=critique)

    spec_out = run_spec_gate(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "ok",
    )
    spec = spec_out["gates"][0]
    assert spec.name == "spec" and spec.status is GateStatus.PASS
    assert merge_gates(spec_out["gates"], None) == spec_out["gates"]
    assert not records.stable_fail_latched(spec.run_against, "spec")
    state_with = {**state, "gates": spec_out["gates"]}
    assert (
        route_after_gates(state_with, _config(), boundary=SPEC_BOUNDARY, records=records)
        == "advance"
    )


def test_clean_empty_and_blank_critique_materialize_identical_tree(tmp_path):
    """The conditional-wrapper rationale (gate_wiring writes the critique block
    ONLY when blocking, so a flagged run's reshaped tree never perturbs the clean
    path's tree_sha / deploy idempotency key): no-critique, empty-omissions, and
    whitespace-only-omissions all materialize a BYTE-IDENTICAL criteria.json ->
    identical tree_sha. Only a REAL (non-blank) omission reshapes the tree. This
    pins the byte-identity the wrapper-gating depends on AND the materializer/gate
    blank-omission alignment (spec_gate_blocking ignores whitespace-only entries)."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}

    def _materialized_sha(critique, sub):
        diff, uri, blob = _feature_diff(files)
        state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),), critique=critique)
        return materialize_workdir(
            state, tmp_path / sub, resolve=resolve, resolve_blob=resolve_blob
        )

    sha_absent = _materialized_sha(None, "absent")
    sha_empty = _materialized_sha(_critique(omissions=()), "empty")
    sha_blank = _materialized_sha(_critique(omissions=("   ", "\t")), "blank")
    assert sha_absent == sha_empty == sha_blank  # clean path is resume-stable

    # A genuine omission DOES reshape the tree (the FAIL-input path).
    sha_real = _materialized_sha(_critique(omissions=("no rate-limit abuse criterion",)), "real")
    assert sha_real != sha_absent


# ---- 5. adequacy floor: a vacuous (0-test) suite is FAIL, not PASS -----------


def test_adequacy_floor_zero_tests_is_fail(tmp_path, rt, runner, records, verifier):
    # A compiling module but NO test sources -> pytest collects 0 -> adequacy FAIL.
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, blob = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    bt_out = run_build_test_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "bt",
        min_passed=1,
    )
    build, test = bt_out["gates"]
    assert build.status is GateStatus.PASS  # it compiles
    assert test.status is GateStatus.FAIL  # vacuous suite cannot clear the floor
    assert merge_gates(bt_out["gates"], None)  # both attested + admitted


# ---- 6. flake quarantine (N=2 split) does not fail the boundary -------------


def test_flake_quarantine_does_not_fail_boundary(tmp_path, runner, records, verifier):
    # A signed Test ERROR whose findings mark a flake quarantine.
    from foundry.gates.runner import observe

    obs = observe("test", structured={"quarantine": True})
    # Sign a test ERROR via the runner's signer over a real result shape.
    signer = ZoneBSigner(KEY, KEY_ID)
    quarantine = signer.with_attestation(
        GateResult(
            name="test",
            status=GateStatus.ERROR,
            decision_deterministic=True,
            execution_deterministic=True,
            verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
            scanner_config_rev=SCANNER_REV,
            lineage_map_revision=LINEAGE_REV,
            results_digest=obs.results_digest,
            findings=("flake-quarantine: 2 quarantined flaky tests (N=2 split)",),
            run_against="c" * 64,
            seq=_seq("test_gate", "verdict:test"),
        )
    )
    build_pass = signer.with_attestation(
        GateResult(
            name="build",
            status=GateStatus.PASS,
            decision_deterministic=True,
            execution_deterministic=True,
            verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
            scanner_config_rev=SCANNER_REV,
            lineage_map_revision=LINEAGE_REV,
            results_digest="bd",
            run_against="c" * 64,
            seq=_seq("build_gate", "verdict:build"),
        )
    )
    folded = merge_gates([build_pass, quarantine], None)
    assert set(folded) == {build_pass, quarantine}

    state: FactoryState = {"gates": folded}
    # A flake-quarantine ERROR must NOT fail (halt-as-failure) the boundary: it
    # quarantines -> bounded retry (fix), never latches.
    assert flake_quarantined(state, boundary=BUILD_TEST_BOUNDARY)
    assert not records.stable_fail_latched("c" * 64, "test")
    assert (
        route_after_gates(
            state, _config(), boundary=BUILD_TEST_BOUNDARY, records=records, max_fix_attempts=3
        )
        == "fix"
    )
    # Once the convergence bound is spent, the boundary halts (escalate).
    assert (
        route_after_gates(
            {**state, "gates": folded},
            {"configurable": {"thread_id": RUN_ID, "fix_attempts": 3}, "metadata": {}},
            boundary=BUILD_TEST_BOUNDARY,
            records=records,
            max_fix_attempts=3,
        )
        == "halt"
    )


# ---- materialization fail-closed: unresolvable diff body --------------------


def test_unresolvable_diff_body_fails_closed(tmp_path, verifier):
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, blob = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve, _ = _seed(criteria, ((diff, uri, blob),))

    # A BlobResolver that returns neither bytes nor str for the diff_uri.
    def _broken_blob(diff_uri: str) -> Any:
        return {"not": "a diff blob"}

    with pytest.raises(FeatureDiffBodyError):
        materialize_workdir(state, tmp_path / "wd", resolve=resolve, resolve_blob=_broken_blob)


def test_unsupported_hunk_shape_fails_closed(tmp_path, verifier):
    """A modification/deletion hunk (not a pure addition) is fail-closed."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, _ = _feature_diff({"app.py": _GOOD_MODULE})
    # Replace the blob with a MODIFICATION diff the Phase-1 applier rejects.
    mod = b"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-OLD = 1\n+NEW = 2\n"
    state, resolve, _ = _seed(criteria, ((diff, uri, mod),))

    with pytest.raises(FeatureDiffBodyError):
        materialize_workdir(
            state, tmp_path / "wd", resolve=resolve, resolve_blob=lambda u: mod
        )


def test_real_git_style_addition_diff_is_parsed() -> None:
    """A REAL git unified diff (what `claude -p` / the OpenAI SDK emit) round-trips:
    the `diff --git`/`index`/`new file mode` preamble is skipped, every `+` line is
    content, and `\\ No newline at end of file` is ignored."""
    body = (
        "diff --git a/pkg/reverse_words.py b/pkg/reverse_words.py\n"
        "new file mode 100644\n"
        "index 0000000..a1b2c3d\n"
        "--- /dev/null\n"
        "+++ b/pkg/reverse_words.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def reverse_words(s):\n"
        "+    return ' '.join(reversed(s.split()))\n"
        "\\ No newline at end of file\n"
    )
    files = _parse_addition_diff("cas://sha256/x", body)
    assert files == {
        "pkg/reverse_words.py": "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n"
    }


def test_real_git_style_multi_file_diff_splits_on_diff_git() -> None:
    """A multi-file git diff must split on the next `diff --git`, not bleed one
    file's preamble into the prior file's content."""
    body = (
        "diff --git a/app.py b/app.py\n--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n"
        "+X = 1\n"
        "diff --git a/test_app.py b/test_app.py\n--- /dev/null\n+++ b/test_app.py\n"
        "@@ -0,0 +1,1 @@\n+def test_x(): assert X == 1\n"
    )
    files = _parse_addition_diff("cas://sha256/x", body)
    assert files == {"app.py": "X = 1\n", "test_app.py": "def test_x(): assert X == 1\n"}


def test_path_traversal_rejected_before_write(tmp_path, verifier):
    """An agent-influenced diff path escaping the integrated tree is fail-closed."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, _ = _feature_diff({"app.py": _GOOD_MODULE})
    # A diff body naming a traversal path -> rejected BEFORE any write.
    evil = b"--- /dev/null\n+++ b/../escape.py\n@@ -0,0 +1,1 @@\n+PWNED = 1\n"
    state, resolve, _ = _seed(criteria, ((diff, uri, evil),))

    with pytest.raises(PathContainmentError):
        materialize_workdir(
            state, tmp_path / "wd", resolve=resolve, resolve_blob=lambda u: evil
        )
    # Nothing escaped the integrated tree.
    assert not (tmp_path / "escape.py").exists()


# ---- the GENUINE producer -> consumer contract: real StubAdapter diff --------


def test_stub_adapter_diff_round_trips_through_get_blob(tmp_path, rt, runner, records, verifier):
    """Drive a REAL StubAdapter.run, then resolve its diff_uri via sandbox.get_blob.

    This exercises the genuine producer->consumer contract end to end: the
    StubAdapter emits the FeatureDiff body as a UNIFIED-DIFF blob via
    ``sandbox.put_blob``; gate_wiring's materializer fetches it back through a
    BlobResolver backed by ``sandbox.get_blob`` and applies it — NOT a
    hand-fabricated {uri: dict} no emitter produces.
    """
    source = tmp_path / "src"
    source.mkdir()
    store = tmp_path / "store"  # durable so get_blob survives (sibling of source)

    sandbox = Sandbox(source, snapshot_store=store)
    try:
        adapter = StubAdapter(run_id=RUN_ID, node_id="junior_engineer", output_path="app.py")
        packet = TaskPacket(
            task={"id": "T1", "title": "T1"},
            worktree_path=str(sandbox.worktree),
            turn_budget=1,
            cost_budget_microusd=1,
        )
        result = adapter.run(packet, sandbox)

        # The genuine diff_uri + a BlobResolver backed by the REAL get_blob.
        diff = FeatureDiff(
            diff_uri=result.diff_uri,
            task_id="T1",
            branch="task/T1",
            base_tree_sha="base-sha",
            seq=_seq("junior_engineer", "diff:T1"),
        )
        handle = Artifact(
            kind=FEATURE_DIFF_KIND,
            uri="cas://feature_diff/T1",
            produced_by="junior_engineer",
            task_id="T1",
            seq=diff.seq,
        )

        c1, c2 = _criterion("c1"), _criterion("c2", security=True)
        crit_handles = [
            emit_artifact(c1, CRITERION_KIND, "product_manager", c1.seq),
            emit_artifact(c2, CRITERION_KIND, "product_manager", c2.seq),
        ]
        by_handle: dict[str, Any] = {handle.uri: diff}
        for h, c in zip(crit_handles, (c1, c2), strict=True):
            by_handle[h.uri] = c

        def _resolve_all(h: Artifact) -> Any:
            return by_handle[h.uri]

        state: FactoryState = {"artifacts": [*crit_handles, handle]}
        materialize_workdir(
            state,
            tmp_path / "wd",
            resolve=_resolve_all,
            resolve_blob=sandbox.get_blob,  # the REAL CAS read
        )
    finally:
        sandbox.destroy()

    # The StubAdapter's deterministic file body was materialized into the tree.
    materialized = (tmp_path / "wd" / "app.py").read_text(encoding="utf-8")
    assert materialized.startswith("stub change for task T1")
