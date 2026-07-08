"""CLI parity flags: --state-dir, --thread-id, --max-fix-attempts, evolve --strict.

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
    assert (tmp_path / "checkpoints.db").is_file()      # checkpointer (+ outbox/audit)
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


# ---- --checkpoints heuristic (H5: must not greedily eat the request) ---------


def test_checkpoints_bare_enables_all_and_does_not_eat_request() -> None:
    # `--checkpoints` directly before the request must NOT consume it as a value.
    assert main(["--checkpoints", "build a thing"]) == 0


def test_checkpoints_all_and_boundary_list_parse() -> None:
    assert main(["--checkpoints", "all", "build a thing"]) == 0
    assert main(["--checkpoints", "product,design", "build a thing"]) == 0


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


# ---- evolve --strict --------------------------------------------------------


def test_evolve_strict_refused_returns_1(tmp_path: Path) -> None:
    prior = tmp_path / "v1"
    run("build a URL shortener", output_dir=str(prior))
    assert (prior / "test_app.py").is_file()
    overlay = tmp_path / "changes"
    overlay.mkdir()
    # Break the prior oracle -> failing re-gate -> --strict makes it exit non-zero.
    _broken = "def test_ok():\n    assert 1 + 1 == 3\n"
    (overlay / "test_app.py").write_text(_broken, encoding="utf-8")
    rc = main(["evolve", str(prior), "break it", "--apply", str(overlay), "--strict"])
    assert rc == 1
