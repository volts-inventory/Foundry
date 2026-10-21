"""CLI ``--mode`` wiring — assistant is the only mode; anything else is refused."""

from __future__ import annotations

import pytest

from foundry.cli import MODES, run
from foundry.graph.supervisor import MODE_ASSISTANT
from foundry.schemas.gates import GateStatus

# ---- default (no flags) is byte-identical to the Phase-1 Assistant run -------


def test_default_run_is_assistant_phase1_bare_pr() -> None:
    """No ``mode`` → Assistant: a bare open_pr against the FeatureDiff."""
    result = run("build a tiny URL-shortener service", thread_id="cli-modes-default")

    assert result.mode == MODE_ASSISTANT
    assert result.passed is True
    assert result.phase == "done"
    for name in ("spec", "build", "test"):
        assert result.gate_statuses.get(name) is GateStatus.PASS
    # The PR opens against the FeatureDiff handle.
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


@pytest.mark.parametrize("mode", ["gated", "autonomous", "turbo"])
def test_non_assistant_mode_is_rejected_fail_closed(mode: str) -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        run("build a thing", mode=mode, thread_id="cli-modes-bad")


# ---- main(): flag parsing ----------------------------------------------------


def test_main_default_is_assistant_and_passes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["build a tiny URL-shortener service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode:      assistant" in out
    assert "advisory" in out.lower()


def test_main_explicit_assistant_mode_passes(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "assistant", "build a tiny URL-shortener service"])
    assert rc == 0
    assert "mode:      assistant" in capsys.readouterr().out


def test_main_unknown_mode_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "turbo", "build a thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown --mode" in err


def test_main_removed_flags_are_not_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "gated", "build a thing"])
    assert rc == 2
    assert "unknown --mode" in capsys.readouterr().err


def test_main_help_lists_modes(capsys: pytest.CaptureFixture[str]) -> None:
    """No request → usage error (exit 2) that names the mode."""
    rc = main_with([])
    err = capsys.readouterr().err
    assert rc == 2
    for mode in MODES:
        assert mode in err
    assert "--packs" not in err


def main_with(argv: list[str]) -> int:
    """Invoke ``foundry.cli.main`` with an explicit argv (never reads sys.argv)."""
    from foundry.cli import main

    return main(argv)
