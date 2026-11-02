"""Two-zone gate runner + deterministic gate impls. (gov §1c, plan §3.1, §9)

These exercise the trust seam end to end: every verdict the runner emits must
verify under the zone-B key AND be admitted by ``merge_gates`` (an agent could
not have forged it); infra failure is a signed ERROR, never PASS; an integrated
FAIL latches cross-fork while ERROR never does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.gates import (
    BuildGate,
    DevGateWriteVerifier,
    SpecGate,
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


# ---- Spec gate -------------------------------------------------------------


# A criteria document that, on its OWN structural merits, would PASS — so any
# FAIL below is attributable solely to the materialized critique block.
_STRUCTURALLY_OK = (
    '"criteria":[{"id":"c1","text":"does X","oracle":"test_x"},'
    '{"id":"c2","text":"rejects bad input","oracle":"test_abuse","security_abuse":true}]'
)


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


def test_build_without_sandbox_is_error_not_unsandboxed(tmp_path, runner, verifier, monkeypatch):
    from foundry.gates import sandbox

    monkeypatch.delenv("FOUNDRY_GATE_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "unshare_available", lambda: False)
    r = runner.run(BuildGate(), "task/1", _seq("build", "task/1"), tmp_path)
    assert r.status is GateStatus.ERROR


@pytest.mark.parametrize(
    ("cmd", "install", "status"),
    [
        (("/bin/sh", "-c", "true"), "echo registry 503 >&2; exit 1", GateStatus.ERROR),
        (("/bin/sh", "-c", "no-such-toolchain-xyz build"), None, GateStatus.ERROR),  # 127
        (("/bin/sh", "-c", "true"), "no-such-installer-xyz ci", GateStatus.ERROR),  # 127
        (("/bin/sh", "-c", "exit 1"), "true", GateStatus.FAIL),
    ],
)
def test_build_infra_failures_are_unlatched_errors(
    tmp_path, runner, records, verifier, cmd, install, status, monkeypatch
):
    # These run in-process; the networked install step must be explicitly allowed there.
    monkeypatch.setenv("FOUNDRY_ALLOW_UNSANDBOXED_INSTALL", "1")
    digest = "c" * 64
    r = runner.run(BuildGate(cmd=cmd, install_cmd=install), digest, _seq("build", digest),
                   tmp_path)
    assert r.status is status, r.findings
    assert records.stable_fail_latched(digest, "build") is (status is GateStatus.FAIL)


def test_reuse_key_binds_the_gate_commands(runner):
    def key(g: GateImpl) -> str:
        return runner.reuse_key(g, "sha-x")

    sh = ("/bin/sh", "-c", "npm test")
    assert key(BuildGate()) == key(BuildGate())
    assert len({
        key(BuildGate()), key(BuildGate(cmd=("make",))),
        key(BuildGate(install_cmd="npm ci")), key(BuildGate(profile_name="stack:go")),
    }) == 4
    assert len({
        key(TestGate()), key(TestGate(cmd=sh, stack_harness=True)),
        key(TestGate(cmd=sh, stack_harness=True, bdd_cmd="npx cucumber-js")),
        key(TestGate(cmd=sh, stack_harness=True, bdd_cmd="npx cucumber-js --x")),
        key(TestGate(cmd=sh, stack_harness=True, profile_name="stack:typescript")),
    }) == 5


@pytest.mark.parametrize(
    ("stack", "rc", "bdd_rc", "status"),
    [
        (True, 0, None, GateStatus.PASS),
        (True, 5, None, GateStatus.FAIL),  # not pytest: 5 is not "no tests collected"
        (True, 2, None, GateStatus.FAIL),
        (True, 127, None, GateStatus.ERROR),
        (True, 126, None, GateStatus.ERROR),
        (True, 0, 127, GateStatus.ERROR),
        (True, 0, 1, GateStatus.FAIL),
        (False, 5, None, GateStatus.PASS),  # the pinned pytest harness keeps its codes
        (False, 2, None, GateStatus.ERROR),
    ],
)
def test_test_gate_exit_semantics(stack, rc, bdd_rc, status):
    from foundry.gates.runner import observe
    from foundry.gates.test import BDD_EXIT_KEY

    gate = TestGate(cmd=("x",), min_passed=0, stack_harness=stack)
    structured = {} if bdd_rc is None else {BDD_EXIT_KEY: bdd_rc}
    assert gate.decide(observe("test", exit_codes=(rc,), structured=structured)) is status


def test_generated_module_named_like_host_pkg_is_isolated(tmp_path, runner, records, verifier):
    """The real failure reproduced: a generated impl under ``foundry/`` (a namespace
    package, no ``__init__``) + a test importing it. In a shared interpreter the host
    ``foundry`` package shadows the workdir's -> pytest collection ERROR. The isolated
    pinned harness fences the host source off ``sys.path`` so the workdir's module wins
    and the suite runs -> PASS (generated code is isolated from ours)."""
    (tmp_path / "foundry").mkdir()
    (tmp_path / "foundry" / "text_processing.py").write_text(
        "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_rw.py").write_text(
        "from foundry.text_processing import reverse_words\n\n\n"
        "def test_rw():\n    assert reverse_words('a b c') == 'c b a'\n",
        encoding="utf-8",
    )
    r = runner.run(TestGate(), "task/iso", _seq("test", "task/iso"), tmp_path)
    assert r.status is GateStatus.PASS


def test_failing_test_gate_carries_the_failure_in_findings(tmp_path, runner, records, verifier):
    """A red run must carry its WHY: the failing test + assertion land in the gate's
    findings (so the fix loop can feed the SPECIFIC failure back to the engineer)."""
    _write(
        tmp_path,
        "test_rw.py",
        "def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n\n\n"
        "def test_double_spaces_preserved():\n"
        "    assert reverse_words('a  b') == 'b  a'\n",  # impl collapses -> fails
    )
    r = runner.run(TestGate(), "task/fb", _seq("test", "task/fb"), tmp_path)
    assert r.status is GateStatus.FAIL
    blob = "\n".join(r.findings)
    assert "test_double_spaces_preserved" in blob          # which test
    assert "assert" in blob                                 # the assertion detail
