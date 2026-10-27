"""CLI ``--mode`` wiring — assistant is the only mode; anything else is refused."""

from __future__ import annotations

import pytest

from foundry.cli import MODES, run


def test_non_assistant_mode_is_rejected_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        run("build a thing", mode="gated", thread_id="cli-modes-bad")


# ---- main(): flag parsing ----------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--mode", "assistant"]])
def test_main_default_is_assistant_and_passes(
    flags: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main_with([*flags, "build a tiny URL-shortener service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode:      assistant" in out
    assert "advisory" in out.lower()


def test_main_unknown_mode_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main_with(["--mode", "turbo", "build a thing"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown --mode" in err


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
