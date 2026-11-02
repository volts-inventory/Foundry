"""CLI parity flags: --state-dir, --thread-id, --max-fix-attempts.

Each closes a programmatic/env-only setting that previously had no CLI flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.cli import main, run

# ---- --thread-id ------------------------------------------------------------


def test_thread_id_param_is_reflected() -> None:
    assert run("build a thing", thread_id="my-run").thread_id == "my-run"


# ---- --state-dir / run(state_dir=) ------------------------------------------


def test_state_dir_flag_via_main(tmp_path: Path) -> None:
    assert main(["--state-dir", str(tmp_path), "build a thing"]) == 0
    assert (tmp_path / "global_records.db").is_file()


def test_state_dir_param_overrides_env(tmp_path, monkeypatch) -> None:
    # An explicit state_dir= wins over FOUNDRY_STATE_DIR.
    monkeypatch.setenv("FOUNDRY_STATE_DIR", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    run("build a thing", state_dir=str(explicit))
    assert (explicit / "checkpoints.db").is_file()      # checkpointer
    assert (explicit / "global_records.db").is_file()   # trust ledgers
    assert (explicit / "cas").is_dir()                  # content-addressed store
    assert not (tmp_path / "env").exists()


# ---- --profile ---------------------------------------------------------------


def test_profile_flag_pins_a_known_stack() -> None:
    # a valid --profile runs clean (mock backend, stub gates)
    assert main(["--profile", "python-app", "build a thing"]) == 0


def test_profile_flag_rejects_unknown_stack() -> None:
    # fail-closed: an unknown stack is a usage error, never a silent fallback
    assert main(["--profile", "cobol", "build a thing"]) == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--max-fix-attempts", "abc"],
        ["--max-fix-attempts", "0"],
        ["--max-slots", "abc"],
        ["--max-slots", "0"],
        ["--cost-cap-microusd", "nope"],
    ],
)
def test_int_flags_reject_bad_values(argv: list[str]) -> None:
    assert main([*argv, "build a thing"]) == 2


def test_valid_int_flags_run() -> None:
    assert main(["--max-fix-attempts", "5", "--max-slots", "2", "build a thing"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--checkpoints", "build a thing"],
        ["--packs", "regulated", "build a thing"],
        ["--packs=regulated", "build a thing"],
        ["evolve", "./released", "fix login"],
    ],
)
def test_removed_flags_and_subcommands_are_rejected(argv: list[str]) -> None:
    # Supervision/packs/evolve are gone: each must fail loudly, never join the
    # request text or start a build.
    assert main(argv) == 2


# ---- the de-duplicated value-flag helpers (H5) ------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["build a thing", "--out"],
        ["--max-slots"],
        ["--backend"],
        ["build a thing", "--adapter"],
        ["--spec"],
    ],
)
def test_value_flag_missing_value_is_usage_error(argv: list[str]) -> None:
    # A value flag at the end with no following token is a clean exit-2 usage error.
    assert main(argv) == 2
