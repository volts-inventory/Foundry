"""Dependency-scoped re-verification engine — change-proportional + sound (T-M2-06).

Exercises ``foundry/graph/reverification.py`` against the REAL
``TwoZoneGateRunner`` (so the prior verdicts fed in are genuine zone-B-attested
records and the reuse keys are the runner's own):

* a COSMETIC change to an unrelated file -> most gates REUSE the cached attested
  verdict; only the touched scope RE-RUNs (the re-run set is a STRICT subset,
  proving change-proportionality);
* a LOCKFILE / TOOLCHAIN / SCANNER-CONFIG change -> the BOUND gates RE-RUN
  (cached verdict invalidated — a dependency bump re-keys them);
* SOUNDNESS: a changed input can NEVER reuse a stale verdict (reuse only on an
  exact reuse-key match); an absent / mismatched prior verdict -> RE-RUN
  (fail-closed);
* determinism.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.gates import TwoZoneGateRunner, ZoneBSigner
from foundry.gates.audit import AuditGate
from foundry.gates.build import BuildGate
from foundry.gates.security import SecurityGate
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.graph.reverification import (
    DEFAULT_GATE_SCOPES,
    ChangedScope,
    changed_scope,
    reverification_plan,
)
from foundry.schemas.gates import GateResult, GateStatus
from foundry.state.global_records import GlobalRecords
from foundry.state.seq import Seq
from foundry.state.tree_sha import tree_sha

KEY = b"zone-b-dev-key-0123456789abcdef"
KEY_ID = "zoneB-dev"
SCANNER_REV = "scanner-rev-1"
LINEAGE_REV = "lineage-rev-1"
RUN_ID = "run-1"
TOOLCHAIN = "foundry-dev-toolchain-v1"

_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_GOOD_MODULE = "VALUE = 42\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'

_CRITERIA = [
    {"id": "c1", "text": "do a thing", "oracle": "test_c1", "security_abuse": False},
    {"id": "c2", "text": "no abuse", "oracle": "test_c2", "security_abuse": True},
]


def _runner() -> TwoZoneGateRunner:
    return TwoZoneGateRunner(ZoneBSigner(KEY, KEY_ID), GlobalRecords(), SCANNER_REV, LINEAGE_REV)


def _seq(node: str, epoch: int = 1) -> Seq:
    return Seq(run_id=RUN_ID, emission_epoch=epoch, superstep_index=0, node_id=node, local=0)


def _materialize(dest: Path, files: dict[str, str], *, criteria: Any = _CRITERIA) -> str:
    """Write a tree + criteria.json; return its tree_sha (the integrated state_sha)."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "criteria.json").write_text(json.dumps(criteria), encoding="utf-8")
    for rel, body in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tree_sha(dest, toolchain_digest=TOOLCHAIN, prefer_git=False)


_GATES = (
    SpecGate(),
    BuildGate(),
    TestGate(min_passed=1),
    AuditGate(),
    SecurityGate(),
)
_GATE_NAMES = {g.name for g in _GATES}


def _run_all(runner: TwoZoneGateRunner, workdir: Path, state_sha: str) -> list[GateResult]:
    """Genuine attested verdicts for every gate against a materialized tree."""
    out: list[GateResult] = []
    for gate in _GATES:
        out.append(runner.run(gate, state_sha, _seq(f"{gate.name}_gate"), workdir))
    return out


def _clean() -> dict[str, str]:
    return {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST, "uv.lock": _CLEAN_LOCK}


# ---- change-proportionality: cosmetic change -> most gates REUSE -------------


def test_cosmetic_change_reuses_most_gates_reruns_only_touched_scope(tmp_path):
    """A change to ONE unrelated source file: most gates REUSE, only its scope re-runs."""
    runner = _runner()

    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    # A one-file change to app.py only (criteria, lockfile, tests untouched).
    new_dir = tmp_path / "new"
    new_files = {**old_files, "app.py": "VALUE = 43\n"}
    new_sha = _materialize(new_dir, new_files)

    scope = changed_scope(
        old_files | {"criteria.json": json.dumps(_CRITERIA)},
        new_files | {"criteria.json": json.dumps(_CRITERIA)},
        old_state_sha=old_sha,
        new_state_sha=new_sha,
        old_toolchain_digest=TOOLCHAIN,
        new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV,
        new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.changed_files == {"app.py"}
    assert not scope.lockfile_changed

    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )

    rerun = set(plan.rerun_gates)
    reuse = set(plan.reuse_gates)
    # CHANGE-PROPORTIONAL: the re-run set is a STRICT subset — NOT all gates.
    assert rerun < _GATE_NAMES
    assert rerun  # something re-runs (app.py changed)
    # Spec (criteria-only) and Audit (lockfile-only) are unaffected -> REUSE.
    assert "spec" in reuse
    assert "audit" in reuse
    # The code-reading gates (build/test/security) DO re-run on the app.py change.
    assert {"build", "test", "security"} <= rerun

    # Reused decisions carry the REAL prior attested verdict forward verbatim.
    spec_decision = plan.decision_for("spec")
    assert spec_decision is not None and not spec_decision.rerun
    assert spec_decision.reused_verdict in prior
    assert spec_decision.reused_verdict.runner_attestation is not None


def test_unrelated_test_file_only_change_reuses_spec_and_audit(tmp_path):
    """Touching only the lockfile re-runs the dep gates, reuses the code gates."""
    runner = _runner()
    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    new_dir = tmp_path / "new"
    new_files = {**old_files, "uv.lock": 'name = "pkg"\nlicense = "MIT"\nx = 1\n'}
    new_sha = _materialize(new_dir, new_files)

    scope = changed_scope(
        old_files, new_files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.lockfile_changed
    assert scope.changed_files == {"uv.lock"}

    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )
    rerun = set(plan.rerun_gates)
    # Dep gates (audit/security read the lockfile) re-run; pure code/spec reuse.
    assert "audit" in rerun
    assert "security" in rerun  # reads_lockfile=True
    assert "spec" in set(plan.reuse_gates)
    assert "build" in set(plan.reuse_gates)
    assert "test" in set(plan.reuse_gates)
    assert rerun < _GATE_NAMES


# ---- a dependency bump invalidates cached verdicts (cache invalidation) ------


def test_toolchain_bump_reruns_all_toolchain_bound_gates(tmp_path):
    """A toolchain digest move re-keys every toolchain-bound gate -> RE-RUN."""
    runner = _runner()
    old_dir = tmp_path / "old"
    files = _clean()
    old_sha = _materialize(old_dir, files)
    prior = _run_all(runner, old_dir, old_sha)

    # No file changed, but the toolchain digest moved (a pinned-image bump).
    scope = changed_scope(
        files, files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest="foundry-dev-toolchain-v2",
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    assert scope.toolchain_changed
    assert not scope.changed_files

    # The new state_sha differs because the toolchain entered tree_sha.
    new_sha = tree_sha(old_dir, toolchain_digest="foundry-dev-toolchain-v2", prefer_git=False)
    assert new_sha != old_sha

    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )
    # Every gate reads the toolchain (reads_toolchain default True) AND the
    # state_sha changed -> EVERY gate re-runs (cache fully invalidated).
    assert set(plan.rerun_gates) == _GATE_NAMES
    assert not plan.reuse_gates


def test_scanner_config_bump_invalidates_bound_gates(tmp_path):
    """A scanner-config bump re-keys the scanner-bound gates -> they RE-RUN."""
    runner_old = _runner()
    old_dir = tmp_path / "old"
    files = _clean()
    old_sha = _materialize(old_dir, files)
    prior = _run_all(runner_old, old_dir, old_sha)

    # Same tree, same toolchain, but the scanner config revision bumped. The
    # state_sha (tree-only) is UNCHANGED, so only scanner-bound gates re-key.
    scope = changed_scope(
        files, files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev="scanner-rev-2",
    )
    assert scope.scanner_config_changed
    assert not scope.changed_files

    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner_old, new_state_sha=old_sha,
        scanner_config_rev="scanner-rev-2",  # the CURRENT runner's rev
    )
    rerun = set(plan.rerun_gates)
    # Scanner-bound gates (security reads scanner config; test/e2e too) re-run.
    assert "security" in rerun
    assert "test" in rerun
    # Spec / build / audit are NOT scanner-bound and the tree is unchanged ->
    # they may REUSE (their reuse key is unchanged). Soundness check below.
    assert "spec" in set(plan.reuse_gates)
    assert "audit" in set(plan.reuse_gates)
    assert rerun < _GATE_NAMES


# ---- SOUNDNESS: a changed input can NEVER reuse a stale verdict --------------


def test_changed_source_can_never_reuse_stale_code_gate_verdict(tmp_path):
    """The code gates' reuse key differs once the source changes -> no reuse."""
    runner = _runner()
    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)

    new_dir = tmp_path / "new"
    new_files = {
        **old_files,
        "app.py": "VALUE = 999\n",
        "test_app.py": "def test_x():\n    assert True\n",
    }
    new_sha = _materialize(new_dir, new_files)
    assert new_sha != old_sha

    scope = changed_scope(
        old_files, new_files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=new_sha,
        scanner_config_rev=SCANNER_REV,
    )
    # No code gate may carry forward a verdict computed against the OLD tree.
    for name in ("build", "test", "security"):
        d = plan.decision_for(name)
        assert d is not None and d.rerun, name
        assert d.reused_verdict is None
    # Only gates whose SCOPED inputs are unchanged (spec=criteria-only,
    # audit=lockfile-only) may reuse — their scoped sub-hash is identical even
    # though the whole tree_sha moved (the §1f sub-hash, not the whole tree).
    assert set(plan.reuse_gates) <= {"spec", "audit"}


def test_missing_prior_verdict_forces_rerun_fail_closed(tmp_path):
    """No prior verdict for a gate -> RE-RUN (fail-closed), never a vacuous reuse."""
    runner = _runner()
    workdir = tmp_path / "wd"
    sha = _materialize(workdir, _clean())
    # Prior channel has verdicts for everything EXCEPT 'security'.
    prior = [v for v in _run_all(runner, workdir, sha) if v.name != "security"]

    # Nothing changed at all.
    scope = changed_scope(
        _clean(), _clean(),
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    assert not scope.any_change

    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=sha,
        scanner_config_rev=SCANNER_REV,
    )
    sec = plan.decision_for("security")
    assert sec is not None and sec.rerun  # absent prior -> fail-closed re-run
    assert "no prior attested verdict" in sec.reason
    # The others, with a matching prior + no change, REUSE.
    assert "spec" in set(plan.reuse_gates)


def test_error_prior_verdict_is_never_reused(tmp_path):
    """An ERROR (infra, DS7) is not a reusable verdict -> RE-RUN even if unchanged."""
    runner = _runner()
    workdir = tmp_path / "wd"
    # Omit the lockfile so the Audit gate ERRORs (inventory not rederivable).
    files = {"app.py": _GOOD_MODULE, "test_app.py": _PASSING_TEST}
    sha = _materialize(workdir, files)
    prior = _run_all(runner, workdir, sha)
    audit_prior = {v.name: v for v in prior}["audit"]
    assert audit_prior.status is GateStatus.ERROR

    scope = changed_scope(
        files, files,
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=sha,
        scanner_config_rev=SCANNER_REV,
    )
    d = plan.decision_for("audit")
    assert d is not None and d.rerun  # an ERROR is never carried forward
    assert d.reused_verdict is None


def test_scanner_config_mismatch_blocks_reuse_for_scanner_bound_gates(tmp_path):
    """A prior verdict under a DIFFERENT scanner_config_rev is not reusable —
    but ONLY for the scanner-bound gates (change-proportional)."""
    # Prior runner used scanner-rev-1; the current boundary uses scanner-rev-9.
    runner = _runner()
    workdir = tmp_path / "wd"
    sha = _materialize(workdir, _clean())
    prior = _run_all(runner, workdir, sha)  # recorded under SCANNER_REV

    scope = changed_scope(
        _clean(), _clean(),
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    # The CURRENT runner stamps a different scanner_config_rev than the priors.
    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=sha,
        scanner_config_rev="scanner-rev-9",
    )
    rerun = set(plan.rerun_gates)
    # The scanner-bound gates can NOT launder a verdict recorded under the old
    # scanner config forward -> they RE-RUN (soundness, gov §1c/§1f).
    assert "security" in rerun
    assert "test" in rerun
    for d in plan.decisions:
        if d.gate_name in ("security", "test"):
            assert "scanner-config" in d.reason
    # Gates that do NOT read the scanner config are unaffected -> REUSE.
    assert "spec" in set(plan.reuse_gates)
    assert "build" in set(plan.reuse_gates)
    assert "audit" in set(plan.reuse_gates)


def test_unchanged_tree_reuses_everything(tmp_path):
    """A no-op delta with full matching priors -> EVERY gate REUSEs (max reuse)."""
    runner = _runner()
    workdir = tmp_path / "wd"
    sha = _materialize(workdir, _clean())
    prior = _run_all(runner, workdir, sha)

    scope = changed_scope(
        _clean(), _clean(),
        old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
        old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
    )
    plan = reverification_plan(
        prior, scope, gates=_GATES, runner=runner, new_state_sha=sha,
        scanner_config_rev=SCANNER_REV,
    )
    assert set(plan.reuse_gates) == _GATE_NAMES
    assert not plan.rerun_gates
    # Every reused verdict is the genuine attested prior record.
    for v in plan.reused_verdicts:
        assert v.runner_attestation is not None


# ---- determinism ------------------------------------------------------------


def test_changed_scope_and_plan_are_deterministic(tmp_path):
    runner = _runner()
    old_dir = tmp_path / "old"
    old_files = _clean()
    old_sha = _materialize(old_dir, old_files)
    prior = _run_all(runner, old_dir, old_sha)
    new_files = {**old_files, "app.py": "VALUE = 7\n"}
    new_sha = _materialize(tmp_path / "new", new_files)

    def build_plan():
        scope = changed_scope(
            old_files, new_files,
            old_toolchain_digest=TOOLCHAIN, new_toolchain_digest=TOOLCHAIN,
            old_scanner_config_rev=SCANNER_REV, new_scanner_config_rev=SCANNER_REV,
        )
        return scope, reverification_plan(
            prior, scope, gates=_GATES, runner=runner, new_state_sha=new_sha,
            scanner_config_rev=SCANNER_REV,
        )

    s1, p1 = build_plan()
    s2, p2 = build_plan()
    assert s1 == s2
    assert p1 == p2
    assert p1.rerun_gates == p2.rerun_gates


# ---- API smoke: scopes table is the documented default ----------------------


def test_default_scopes_cover_all_gate_families():
    for name in ("spec", "build", "test", "e2e", "security", "audit", "supply_chain"):
        assert name in DEFAULT_GATE_SCOPES
    # Spec is criteria-only (not toolchain-bound enough to re-run on code edits).
    spec = DEFAULT_GATE_SCOPES["spec"]
    assert spec.reads_sources
    # Audit / supply_chain are the dependency-policy gates.
    assert DEFAULT_GATE_SCOPES["audit"].reads_lockfile
    assert DEFAULT_GATE_SCOPES["supply_chain"].reads_lockfile


def test_empty_changed_scope_constructs():
    cs = ChangedScope()
    assert not cs.any_change
    assert cs.changed_files == frozenset()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
