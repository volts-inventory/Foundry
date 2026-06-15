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

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import cas_uri, emit_artifact
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.gate_wiring import (
    BUILD_TEST_BOUNDARY,
    CRITERION_KIND,
    FEATURE_DIFF_KIND,
    SPEC_BOUNDARY,
    FeatureDiffBodyError,
    flake_quarantined,
    materialize_workdir,
    route_after_gates,
    run_build_test_gates,
    run_spec_gate,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
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


def _feature_diff(files: dict[str, str], *, task_id: str = "T1") -> tuple[FeatureDiff, str]:
    """A FeatureDiff whose diff_uri body is the path->content mapping ``files``."""
    diff_uri = cas_uri({"task_id": task_id, "files": files})
    diff = FeatureDiff(
        diff_uri=diff_uri,
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="base-sha",
        seq=_seq("junior_engineer", f"diff:{task_id}"),
    )
    return diff, diff_uri


def _seed(
    criteria: tuple[AcceptanceCriterion, ...],
    diffs: tuple[tuple[FeatureDiff, str, dict[str, str]], ...] = (),
) -> tuple[FactoryState, Any]:
    """Seed state with criterion + feature_diff HANDLES and an in-memory resolver.

    The resolver reconstitutes: each criterion handle -> its AcceptanceCriterion;
    each feature_diff handle -> its FeatureDiff; each diff_uri -> its path->content
    body (the materialization contract gate_wiring's _diff_files relies on).
    """
    artifacts: list[Artifact] = []
    by_uri: dict[str, Any] = {}

    for c in criteria:
        handle = emit_artifact(c, CRITERION_KIND, "product_manager", c.seq)
        artifacts.append(handle)
        by_uri[handle.uri] = c

    for diff, diff_uri, files in diffs:
        handle = Artifact(
            kind=FEATURE_DIFF_KIND,
            uri=f"cas://feature_diff/{diff.task_id}",
            produced_by="junior_engineer",
            task_id=diff.task_id,
            seq=diff.seq,
        )
        artifacts.append(handle)
        by_uri[handle.uri] = diff
        by_uri[diff_uri] = files

    def _resolve(handle: Artifact) -> Any:
        return by_uri[handle.uri]

    state: FactoryState = {"artifacts": artifacts}
    return state, _resolve


_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"


# ---- 1. signed PASS path advances; verdicts admitted by real merge_gates ----


def test_signed_pass_path_advances(tmp_path, rt, runner, records, verifier):
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}
    diff, uri = _feature_diff(files)
    state, resolve = _seed(criteria, ((diff, uri, files),))

    spec_out = run_spec_gate(
        state, _config(), rt=rt, runner=runner, resolve=resolve, workdir=tmp_path / "spec"
    )
    bt_out = run_build_test_gates(
        state, _config(2), rt=rt, runner=runner, resolve=resolve, workdir=tmp_path / "bt"
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
    diff, uri = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve = _seed(criteria, ((diff, uri, {"app.py": _GOOD_MODULE}),))

    from foundry.gates.build import BuildGate

    run_against = materialize_workdir(state, tmp_path / "wd", resolve=resolve)
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


# ---- 4. integrated FAIL latches; a later PASS does not auto-clear ------------


def test_integrated_fail_latches_and_does_not_autoclear(tmp_path, rt, runner, records, verifier):
    # Criteria omit a security_abuse criterion -> Spec FAIL on the integrated digest.
    bad_state, bad_resolve = _seed((_criterion("c1"),))  # no security criterion
    spec_out = run_spec_gate(
        bad_state, _config(), rt=rt, runner=runner, resolve=bad_resolve, workdir=tmp_path / "bad"
    )
    fail = spec_out["gates"][0]
    assert fail.status is GateStatus.FAIL
    # run_against is an integrated tree_sha (hex digest) -> stable-FAIL latched.
    assert records.stable_fail_latched(fail.run_against, "spec")

    state_with = {**bad_state, "gates": spec_out["gates"]}
    assert (
        route_after_gates(state_with, _config(), boundary=SPEC_BOUNDARY, records=records) == "halt"
    )

    # A later genuine PASS on the SAME digest does not auto-clear the latch.
    assert records.stable_fail_latched(fail.run_against, "spec")
    # Even folding a hypothetical PASS, routing still halts (latch is terminal).
    good_pass = fail.model_copy(update={"status": GateStatus.PASS})
    # (Not attested for PASS; routing reads the latch regardless of channel content.)
    still = route_after_gates(
        {**bad_state, "gates": [good_pass]}, _config(), boundary=SPEC_BOUNDARY, records=records
    )
    assert still == "halt"


# ---- 5. adequacy floor: a vacuous (0-test) suite is FAIL, not PASS -----------


def test_adequacy_floor_zero_tests_is_fail(tmp_path, rt, runner, records, verifier):
    # A compiling module but NO test sources -> pytest collects 0 -> adequacy FAIL.
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve = _seed(criteria, ((diff, uri, {"app.py": _GOOD_MODULE}),))

    bt_out = run_build_test_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
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
    diff, uri = _feature_diff({"app.py": _GOOD_MODULE})
    state, resolve = _seed(criteria, ((diff, uri, {"app.py": _GOOD_MODULE}),))

    # A resolver that returns a non-mapping body for the diff_uri -> fail-closed.
    def _broken(handle: Artifact) -> Any:
        body = resolve(handle)
        if handle.uri == uri:
            return "not a mapping"
        return body

    with pytest.raises(FeatureDiffBodyError):
        materialize_workdir(state, tmp_path / "wd", resolve=_broken)
