"""Run-scoped fire counts (foundry.telemetry): hints, planner pinned constraints
and fix-loop stops counted per run, from worker threads too, and persisted."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from foundry import cli, telemetry
from foundry.agents.planner import _run_parallel
from foundry.agents.planner.constraints import _stack_constraints
from foundry.gates.test import shared_state_hints
from foundry.schemas.implementation import StackDecision


def test_count_is_a_noop_without_a_counter() -> None:
    telemetry.count("x")
    with telemetry.counting() as counts:
        telemetry.count("x")
    telemetry.count("x")
    assert counts.snapshot() == {"x": 1}


def test_counts_from_worker_threads_reach_the_runs_counter() -> None:
    def epic(name: str) -> str:
        for _ in range(100):
            telemetry.count(name)
        return name

    with telemetry.counting() as counts:
        _run_parallel(epic, ["a", "b", "a"])
        workers = [threading.Thread(target=telemetry.count, args=("t",)) for _ in range(3)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
    # A plain Thread starts in an empty context: only copied contexts are counted.
    assert counts.snapshot() == {"a": 200, "b": 100}


def test_a_hint_helper_counts_once_per_call_that_fires(tmp_path: Path) -> None:
    collision = "E   sqlite3.IntegrityError: UNIQUE constraint failed: users.email\n"
    with telemetry.counting() as counts:
        shared_state_hints(collision * 3, tmp_path)
        shared_state_hints(collision, tmp_path)
        shared_state_hints("E   AssertionError: assert 1 == 2\n", tmp_path)
    assert counts.snapshot() == {"test.shared_state": 2}


def test_each_pinned_stack_constraint_is_counted_by_id() -> None:
    with telemetry.counting() as counts:
        _stack_constraints(StackDecision(language="python"), {"x": "SQLAlchemy over SQLite"})
        _stack_constraints(StackDecision(language="python"), {"x": "Postgres"})
    assert counts.snapshot() == {
        "planner.PC-python-sqlalchemy-relationships": 1,
        "planner.PC-python-sqlalchemy-sqlite": 1,
        "planner.PC-python-sqlalchemy-typed": 1,
        "planner.PC-python-sqlite-memory": 1,
    }


def test_a_durable_run_writes_its_counts_into_the_state_dir(tmp_path: Path) -> None:
    result = cli.run(
        "build a tiny URL-shortener service", thread_id="telemetry-run", state_dir=str(tmp_path)
    )
    [path] = (tmp_path / "calls").glob(f"*/{telemetry.COUNTS_FILE}")
    assert json.loads(path.read_text()) == result.hint_counts
