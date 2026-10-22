"""CLI parity flags: --state-dir, --thread-id, --max-fix-attempts.

Each closes a programmatic/env-only setting that previously had no CLI flag.
"""

from __future__ import annotations

from pathlib import Path

from foundry.cli import main, run

# ---- --thread-id ------------------------------------------------------------


def test_thread_id_param_is_reflected() -> None:
    assert run("build a thing", thread_id="my-run").thread_id == "my-run"


# ---- --state-dir / run(state_dir=) ------------------------------------------


def test_state_dir_param_writes_durable_files(tmp_path: Path) -> None:
    run("build a thing", state_dir=str(tmp_path))
    assert (tmp_path / "checkpoints.db").is_file()      # checkpointer
    assert (tmp_path / "global_records.db").is_file()   # trust ledgers
    assert (tmp_path / "cas").is_dir()                  # content-addressed store


def test_state_dir_flag_via_main(tmp_path: Path) -> None:
    assert main(["--state-dir", str(tmp_path), "build a thing"]) == 0
    assert (tmp_path / "global_records.db").is_file()


def test_state_dir_param_overrides_env(tmp_path, monkeypatch) -> None:
    # An explicit state_dir= wins over FOUNDRY_STATE_DIR.
    monkeypatch.setenv("FOUNDRY_STATE_DIR", str(tmp_path / "env"))
    run("build a thing", state_dir=str(tmp_path / "explicit"))
    assert (tmp_path / "explicit" / "global_records.db").is_file()
    assert not (tmp_path / "env").exists()


# ---- --max-fix-attempts -----------------------------------------------------


def test_profile_flag_pins_a_known_stack() -> None:
    # a valid --profile runs clean (mock backend, stub gates)
    assert main(["--profile", "python-app", "build a thing"]) == 0


def test_profile_flag_rejects_unknown_stack() -> None:
    # fail-closed: an unknown stack is a usage error, never a silent fallback
    assert main(["--profile", "cobol", "build a thing"]) == 2


def test_max_fix_attempts_rejects_non_integer() -> None:
    assert main(["--max-fix-attempts", "abc", "build a thing"]) == 2


def test_max_fix_attempts_rejects_below_one() -> None:
    assert main(["--max-fix-attempts", "0", "build a thing"]) == 2


def test_max_fix_attempts_valid_runs() -> None:
    assert main(["--max-fix-attempts", "5", "build a thing"]) == 0


def test_removed_supervision_flag_is_rejected() -> None:
    # Supervision is gone: the old flag must fail loudly, not join the request text.
    assert main(["--checkpoints", "build a thing"]) == 2


# ---- the de-duplicated value-flag helpers (H5) ------------------------------


def test_value_flag_missing_value_is_usage_error() -> None:
    # A value flag at the end with no following token is a clean exit-2 usage error.
    assert main(["build a thing", "--out"]) == 2
    assert main(["--max-slots"]) == 2


def test_max_slots_and_cost_cap_validate() -> None:
    assert main(["--max-slots", "abc", "build a thing"]) == 2
    assert main(["--max-slots", "0", "build a thing"]) == 2
    assert main(["--cost-cap-microusd", "nope", "build a thing"]) == 2
    assert main(["--max-slots", "2", "build a thing"]) == 0


def test_removed_flags_are_rejected_not_folded_into_the_request() -> None:
    assert main(["--packs", "regulated", "build a thing"]) == 2


def test_removed_flag_in_equals_form_is_rejected() -> None:
    assert main(["--packs=regulated", "build a thing"]) == 2


def test_removed_evolve_subcommand_is_rejected_not_built() -> None:
    assert main(["evolve", "./released", "fix login"]) == 2
