"""CLI ``--mode`` / ``--packs`` wiring — each deployment-trust mode is selectable
end to end, and an engaged domain pack's mode VETO is enforced fail-closed (task [9]).

(EXECUTION-PLAN §4 [9]; lifecycle.md §4 deployment-trust modes; T-M5-08.)

Drives :func:`foundry.cli.run` / :func:`foundry.cli.main` with NO live API key
(MockTransport) and NO ``claude -p`` binary (StubAdapter), asserting:

* the DEFAULT (no flags) is byte-identical to today — Assistant, a bare ``open_pr``
  against the FeatureDiff, no assurance / sign-off / pack machinery;
* ``mode="gated"`` wires the Phase-2 spine — the release PR carries the evidence
  pack (``cas://sha256/`` payload) after the assurance gates PASS;
* ``mode="autonomous"`` wires the dev TRUST ROOT — a hands-off ``deploy`` action
  off the admitting decision (``cas://admission/`` payload), admission is the gate;
* a regulated PACK VETOES Autonomous (and forces a human approver-of-record): the
  CLI refuses the vetoed mode fail-closed, and a regulated Gated run is BLOCKED at
  the release boundary (no human approver ⇒ no PR);
* ``main()`` parses the flags + surfaces the per-mode trust posture; unknown
  mode / unknown pack / a veto are clean exit-2 usage errors.
"""

from __future__ import annotations

import pytest

from foundry.cli import MODES, run
from foundry.graph.supervisor import MODE_ASSISTANT, MODE_AUTONOMOUS, MODE_GATED
from foundry.schemas.gates import GateStatus

# ---- default (no flags) is byte-identical to the Phase-1 Assistant run -------


def test_default_run_is_assistant_phase1_bare_pr() -> None:
    """No ``mode`` / ``packs`` → Assistant: a bare open_pr against the FeatureDiff,
    no assurance gates, no sign-offs, no packs (unchanged behavior)."""
    result = run("build a tiny URL-shortener service", thread_id="cli-modes-default")

    assert result.mode == MODE_ASSISTANT
    assert result.packs == ()
    assert result.passed is True
    assert result.phase == "done"
    # Spec/Build/Test PASS; the assurance family is NOT present (Assistant spine).
    for name in ("spec", "build", "test"):
        assert result.gate_statuses.get(name) is GateStatus.PASS
    for name in ("security", "audit", "supply_chain"):
        assert name not in result.gate_statuses
    # The PR opens against the FeatureDiff handle (NOT an evidence pack / decision).
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://feature_diff/")
    # The advisory-posture note is in the summary.
    assert "advisory" in result.summary().lower()


def test_default_matches_explicit_assistant() -> None:
    """``mode="assistant"`` is exactly the default — same artifact + key."""
    a = run("build a tiny URL-shortener service", thread_id="cli-modes-same")
    b = run(
        "build a tiny URL-shortener service",
        mode=MODE_ASSISTANT,
        thread_id="cli-modes-same",
    )
    assert a.artifact_uri == b.artifact_uri
    assert a.open_pr_idempotency_key == b.open_pr_idempotency_key
    assert a.mode == b.mode == MODE_ASSISTANT


# ---- gated mode wires the Phase-2 spine (evidence-pack release) --------------


def test_gated_mode_emits_evidence_pack_release() -> None:
    """``mode="gated"`` runs the assurance gates + records a Release sign-off, and
    the open_pr carries the WORM evidence-pack handle (cas://sha256/...)."""
    result = run("build a tiny URL-shortener service", mode=MODE_GATED, thread_id="cli-modes-gated")

    assert result.mode == MODE_GATED
    assert result.passed is True
    assert result.phase == "done"
    # The assurance family ran + PASSed (the Gated spine, not Assistant).
    for name in ("spec", "build", "test", "security", "audit"):
        assert result.gate_statuses.get(name) is GateStatus.PASS, name
    # The release travels WITH its evidence: the payload is the pack handle.
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://sha256/")
    assert "human sign-off" in result.summary()


# ---- autonomous mode wires the trust root (hands-off deploy) -----------------


def test_autonomous_mode_builds_admits_and_deploys() -> None:
    """``mode="autonomous"`` constructs the dev trust-root chain (builder/admission/
    deployer/verifier) and emits a hands-off deploy off the admitting decision."""
    result = run(
        "build a tiny URL-shortener service",
        mode=MODE_AUTONOMOUS,
        thread_id="cli-modes-auto",
    )

    assert result.mode == MODE_AUTONOMOUS
    assert result.passed is True
    assert result.phase == "done"
    for name in ("spec", "build", "test", "security", "audit"):
        assert result.gate_statuses.get(name) is GateStatus.PASS, name
    # Admission is the sole release gate: the deploy fires off the admitting
    # decision handle (cas://admission/), NOT an open_pr/feature_diff/pack.
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://admission/")
    assert "full trust root" in result.summary()


def test_unknown_mode_is_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        run("build a thing", mode="turbo", thread_id="cli-modes-bad")


# ---- packs: a regulated pack VETOES autonomous (fail-closed) -----------------


def test_regulated_pack_vetoes_autonomous_fail_closed() -> None:
    """A regulated request + the regulated pack makes hands-off Autonomous
    UNREACHABLE; the CLI refuses to silently run the vetoed mode."""
    with pytest.raises(ValueError, match="VETOED"):
        run(
            "build a fintech ledger",
            mode=MODE_AUTONOMOUS,
            packs=["regulated"],
            thread_id="cli-modes-veto",
        )


def test_regulated_pack_gated_run_blocked_at_release() -> None:
    """A regulated Gated run is allowed to RUN (Gated is reachable) but the release
    is BLOCKED at the emit boundary — no recorded human approver-of-record ⇒ no PR.
    The gates still PASS (the change compiles + is assured); only the release halts."""
    result = run(
        "build a fintech ledger",
        mode=MODE_GATED,
        packs=["regulated"],
        thread_id="cli-modes-reg-gated",
    )
    assert result.mode == MODE_GATED
    assert result.packs == ("regulated",)
    # Integrity gates PASS...
    for name in ("spec", "build", "test", "security", "audit"):
        assert result.gate_statuses.get(name) is GateStatus.PASS, name
    # ...but the pack TIGHTENS the release: no human approver ⇒ no PR (fail-closed).
    assert result.phase == "halted"
    assert result.artifact_uri is None
    assert result.open_pr_idempotency_key is None


def test_ml_pack_gated_run_completes() -> None:
    """An ML pack does NOT veto Gated nor force a human approver, so a clean Gated
    run with the ml_data pack completes + emits the evidence-pack release."""
    result = run(
        "train a classifier model",
        mode=MODE_GATED,
        packs=["ml_data"],
        thread_id="cli-modes-ml",
    )
    assert result.mode == MODE_GATED
    assert result.packs == ("ml_data",)
    assert result.passed is True
    assert result.phase == "done"
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://sha256/")


def test_unknown_pack_is_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown pack"):
        run("build a thing", mode=MODE_GATED, packs=["nonesuch"], thread_id="cli-modes-badpack")


# ---- main(): flag parsing + per-mode trust posture in the summary -----------


def test_main_default_is_assistant_and_passes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["build a tiny URL-shortener service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode:      assistant" in out
    assert "advisory" in out.lower()


def test_main_mode_gated(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "gated", "build a tiny URL-shortener service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode:      gated" in out
    assert "cas://sha256/" in out


def test_main_mode_autonomous(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "autonomous", "build a tiny URL-shortener service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode:      autonomous" in out
    assert "cas://admission/" in out


def test_main_unknown_mode_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "turbo", "build a thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown --mode" in err


def test_main_pack_veto_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(
        ["--mode", "autonomous", "--packs", "regulated", "build a fintech ledger"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "VETOED" in err
    assert "--mode gated" in err


def test_main_unknown_pack_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "gated", "--packs", "nope", "build a thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown pack" in err


def test_main_help_lists_modes_and_packs(capsys: pytest.CaptureFixture[str]) -> None:
    """No request → usage error (exit 2) that names every mode + the packs."""
    rc = main_with([])
    err = capsys.readouterr().err
    assert rc == 2
    for mode in MODES:
        assert mode in err
    assert "--packs" in err
    assert "regulated" in err


def main_with(argv: list[str]) -> int:
    """Invoke ``foundry.cli.main`` with an explicit argv (never reads sys.argv)."""
    from foundry.cli import main

    return main(argv)
