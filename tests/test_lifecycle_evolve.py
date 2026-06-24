"""The evolution route — ``evolve <existing repo> "<change>"`` end-to-end (T-M6-13).

Exercises ``foundry/lifecycle/evolve.py`` (and the ``foundry evolve`` CLI
affordance) against the REAL trust seams: an EXISTING released codebase + a
change request RE-ENTERS the factory through the maintenance re-gate path
(:mod:`foundry.lifecycle.maintenance`), never a side door.

The load-bearing properties (lifecycle.md §1/§3):

* a benign change RE-VERIFIES change-proportionally + RE-PASSES + RE-ADMITS ->
  RELEASED, and the result's ``prior_uri`` chains to the prior version;
* FAIL-CLOSED: a change that introduces a hardcoded secret FAILs the Security
  re-gate -> NOT released (no PR/deploy) — evolution never bypasses the gates;
* a change that BREAKS a prior oracle (the carried-forward regression suite) ->
  Test FAIL -> NOT released (``regression_passed`` False);
* a protected criterion/oracle/gate edit ESCALATEs (deterministic HITL) and
  BLOCKS release unless explicitly approved;
* the greenfield ``run()`` path is unchanged (additive);
* determinism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.cli import RunResult, main, run
from foundry.lifecycle.evolve import EvolveRefused, EvolveResult, evolve
from foundry.schemas.gates import GateStatus

# A prior released tree: a passing module + its oracle + a clean lockfile + the
# acceptance criteria the Spec gate reads. This is the shape a `--out` dir carries.
_GOOD_MODULE = "VALUE = 42\n"
_PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
_CLEAN_LOCK = 'name = "pkg"\nlicense = "MIT"\n'
_SECRET_MODULE = 'API_KEY = "AKIAIOSFODNN7EXAMPLEsecrettoken1234567890"\n'
_FAILING_TEST = "def test_ok():\n    assert 1 + 1 == 3\n"

_CRITERIA = [
    {"id": "c1", "text": "do a thing", "oracle": "test_ok", "security_abuse": False},
    {"id": "c2", "text": "no abuse", "oracle": "test_ok", "security_abuse": True},
]


def _prior_repo(tmp_path: Path) -> Path:
    """Write a prior released codebase to disk (the input to ``evolve``)."""
    root = tmp_path / "released"
    root.mkdir()
    (root / "app.py").write_text(_GOOD_MODULE, encoding="utf-8")
    (root / "test_app.py").write_text(_PASSING_TEST, encoding="utf-8")
    (root / "uv.lock").write_text(_CLEAN_LOCK, encoding="utf-8")
    (root / "criteria.json").write_text(json.dumps(_CRITERIA), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# A benign change re-verifies change-proportionally, re-passes -> RELEASED
# ---------------------------------------------------------------------------


def test_benign_change_reverifies_and_releases_chaining_to_prior(tmp_path):
    prior = _prior_repo(tmp_path)
    result = evolve(
        prior,
        "tweak the constant",
        changes={"app.py": "VALUE = 43\n"},
        prior_subject_uri="cas://artifact/v1",
    )
    assert isinstance(result, EvolveResult)
    assert result.released is True
    assert result.admission_admitted is True
    assert result.admission_reason == "ok"
    assert not result.flags

    # CHANGE-PROPORTIONAL: the touched code re-runs build/test/security; the
    # criteria-only Spec and the lockfile-only Audit REUSE the prior verdict.
    assert {"build", "test", "security"} <= set(result.rerun_gates)
    assert {"spec", "audit"} <= set(result.reuse_gates)

    # Every re-verified gate is PASS, and the carried-forward regression passes.
    for status in result.gate_statuses.values():
        assert status is GateStatus.PASS
    assert result.regression_passed is True

    # PROVENANCE: the maintained version chains to the prior released version.
    assert result.prior_uri == "cas://artifact/v1"
    assert result.prior_subject_uri == "cas://artifact/v1"
    assert "RELEASED" in result.summary()


# ---------------------------------------------------------------------------
# FAIL-CLOSED: a failing re-gate is NOT released (no PR/deploy)
# ---------------------------------------------------------------------------


def test_failing_regate_is_not_released_no_pr(tmp_path):
    """A change introducing a hardcoded secret FAILs the Security re-gate ->
    NOT released. Evolution never bypasses the gates (fail-closed)."""
    prior = _prior_repo(tmp_path)
    result = evolve(
        prior,
        "add an api key (oops, hardcoded)",
        changes={"app.py": _SECRET_MODULE},
    )
    assert result.released is False
    assert result.gate_statuses["security"] is GateStatus.FAIL
    assert any(f.startswith("required_gate_not_pass:security") for f in result.flags)
    # The maintained subject also fails re-admission (stable-FAIL latched).
    assert result.admission_admitted is False
    assert result.admission_reason != "ok"
    assert "NOT RELEASED" in result.summary()


def test_strict_evolve_raises_when_not_released(tmp_path):
    prior = _prior_repo(tmp_path)
    with pytest.raises(EvolveRefused):
        evolve(prior, "hardcode a secret", changes={"app.py": _SECRET_MODULE}, strict=True)


# ---------------------------------------------------------------------------
# Regression: a change that breaks a prior oracle is NOT released
# ---------------------------------------------------------------------------


def test_change_breaking_prior_oracle_blocks_release(tmp_path):
    """The prior oracle is carried forward as a regression suite: a change that
    makes the prior test FAIL -> Test re-gate FAIL -> NOT released."""
    prior = _prior_repo(tmp_path)
    result = evolve(
        prior,
        "regress the behaviour",
        changes={"test_app.py": _FAILING_TEST},
    )
    assert result.released is False
    assert result.regression_passed is False
    assert result.gate_statuses["test"] is GateStatus.FAIL


# ---------------------------------------------------------------------------
# Guardian HITL: a protected criterion/oracle edit ESCALATEs + blocks release
# ---------------------------------------------------------------------------


def test_editing_existing_criteria_escalates_and_blocks_without_approval(tmp_path):
    prior = _prior_repo(tmp_path)
    edited = [
        {"id": "c1", "text": "do a thing", "oracle": "test_ok", "security_abuse": False},
    ]
    result = evolve(
        prior,
        "relax the criteria",
        changes={"criteria.json": json.dumps(edited)},
    )
    # The criteria edit is a deterministic CRITICAL-DIFF -> HITL escalation.
    assert result.guardian_escalated is True
    assert "criteria.json" in result.guardian_paths
    # Un-approved -> BLOCKED even if the re-gate would otherwise be green.
    assert result.released is False
    assert any(f.startswith("guardian_escalation_unapproved") for f in result.flags)


def test_guardian_approval_lets_an_approved_criteria_edit_proceed(tmp_path):
    prior = _prior_repo(tmp_path)
    # Add a NEW criterion (a superset — the existing oracle still holds, so the
    # re-gate stays green); the edit still ESCALATEs (it touches criteria.json).
    edited = [
        *_CRITERIA,
        {"id": "c3", "text": "extra", "oracle": "test_ok", "security_abuse": False},
    ]
    result = evolve(
        prior,
        "extend the criteria",
        changes={"criteria.json": json.dumps(edited)},
        guardian_approval=True,
    )
    assert result.guardian_escalated is True
    # With the recorded HITL approval AND a green re-gate, the change is released.
    assert result.released is True
    assert not any(f.startswith("guardian_escalation_unapproved") for f in result.flags)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_evolve_is_deterministic(tmp_path):
    prior = _prior_repo(tmp_path)

    def once() -> tuple[bool, str, tuple[str, ...]]:
        r = evolve(prior, "tweak", changes={"app.py": "VALUE = 9\n"})
        return r.released, r.new_subject_digest, tuple(sorted(r.rerun_gates))

    assert once() == once()


# ---------------------------------------------------------------------------
# The CLI affordance: `foundry evolve <prior> "<change>" --apply OVERLAY`
# ---------------------------------------------------------------------------


def test_cli_evolve_releases_on_clean_change(tmp_path):
    prior = _prior_repo(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "app.py").write_text("VALUE = 44\n", encoding="utf-8")
    code = main(["evolve", str(prior), "bump the constant", "--apply", str(overlay)])
    assert code == 0  # RELEASED


def test_cli_evolve_fails_closed_on_bad_change(tmp_path):
    prior = _prior_repo(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "app.py").write_text(_SECRET_MODULE, encoding="utf-8")
    code = main(["evolve", str(prior), "hardcode a secret", "--apply", str(overlay)])
    assert code == 1  # NOT released (fail-closed) -> non-zero exit


def test_cli_evolve_usage_errors(tmp_path):
    prior = _prior_repo(tmp_path)
    # Missing --apply.
    assert main(["evolve", str(prior), "do a thing"]) == 2
    # Missing prior + change.
    assert main(["evolve"]) == 2
    # Nonexistent prior dir.
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert main(["evolve", str(tmp_path / "nope"), "x", "--apply", str(overlay)]) == 2


def test_cli_evolve_guardian_edit_blocks_without_approval(tmp_path):
    prior = _prior_repo(tmp_path)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "criteria.json").write_text(json.dumps(_CRITERIA[:1]), encoding="utf-8")
    # Un-approved protected edit -> blocked -> exit 1.
    assert main(["evolve", str(prior), "relax", "--apply", str(overlay)]) == 1


# ---------------------------------------------------------------------------
# Greenfield run() is unchanged (additive)
# ---------------------------------------------------------------------------


def test_greenfield_run_unchanged():
    result = run("build a tiny service", thread_id="evolve-greenfield-smoke")
    assert isinstance(result, RunResult)
    assert result.passed is True
    assert result.gate_statuses.get("spec") is GateStatus.PASS
    assert result.gate_statuses.get("build") is GateStatus.PASS
    assert result.gate_statuses.get("test") is GateStatus.PASS


def test_replacing_prior_root_oracle_with_vacuous_test_escalates(tmp_path):
    """Guardian oracle-glob coverage (critic MAJOR fix): the factory emits its
    oracle as a ROOT-LEVEL ``test_app.py``. Silently replacing it with a
    vacuous-but-passing test is a protected-oracle edit -> deterministic
    CRITICAL-DIFF HITL -> blocked without approval (otherwise the gamed-green
    suite would re-pass the dev-grade Test gate and slip through)."""
    prior = _prior_repo(tmp_path)
    result = evolve(
        prior,
        "weaken the oracle",
        changes={"test_app.py": "def test_ok():\n    pass\n"},
    )
    assert result.guardian_escalated is True
    assert result.released is False
    assert any("guardian" in f for f in result.flags)
