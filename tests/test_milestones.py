"""Phase 5 (ROADMAP): the milestone loop — long-horizon autonomous growth.

Plans (or takes) an ordered milestone list and runs the FULL factory per
milestone, chaining each RELEASED tree as the next milestone's seeded base.
Fail-closed: the loop never builds milestone k+1 on an unreleased tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.lifecycle.milestones import (
    MilestoneReport,
    MilestoneResult,
    plan_milestones,
    run_milestones,
)


class FakeRun:
    """Records run_fn calls; scripted pass/fail per milestone index."""

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: str, **kwargs: Any) -> Any:
        self.calls.append({"request": request, **kwargs})
        passed = self.outcomes[len(self.calls) - 1]

        class R:  # the two fields the loop reads
            pass

        r = R()
        r.passed = passed
        return r


def test_chains_released_trees(tmp_path: Path) -> None:
    fake = FakeRun([True, True, True])
    report = run_milestones(
        "a stardew-style moon colony game",
        out_dir=str(tmp_path),
        milestones=["engine loop", "world + tilemap", "farming"],
        run_fn=fake,
        backend="mock",
    )
    assert report.passed and len(report.results) == 3
    # Milestone 1: greenfield; milestone k: seeded with the PRIOR released tree.
    assert fake.calls[0]["base_dir"] is None
    assert fake.calls[1]["base_dir"] == str(tmp_path / "m01")
    assert fake.calls[2]["base_dir"] == str(tmp_path / "m02")
    # Derived thread ids => each milestone independently resumable.
    assert fake.calls[1]["thread_id"] == "milestones-m02"
    # The spec carries the WHOLE plan + the current focus.
    assert "MILESTONE PLAN:" in fake.calls[2]["spec"]
    assert "ONLY milestone 3" in fake.calls[2]["spec"]
    assert report.released_dir == str(tmp_path / "m03")


def test_stops_at_first_gate_refusal(tmp_path: Path) -> None:
    fake = FakeRun([True, False, True])
    report = run_milestones(
        "target",
        out_dir=str(tmp_path),
        milestones=["a", "b", "c"],
        run_fn=fake,
    )
    assert not report.passed
    assert len(fake.calls) == 2  # milestone 3 NEVER ran on the unreleased tree
    assert report.released_dir == str(tmp_path / "m01")  # furthest released increment
    assert [r.passed for r in report.results] == [True, False]


def test_plan_milestones_schema_call() -> None:
    class FakeProxy:
        def complete(self, node_id: str, tier: str, sop: str, messages: Any, *,
                     purpose: str, schema: dict[str, Any]) -> Any:
            assert schema["properties"]["milestones"]["maxItems"] == 12

            class R:
                parsed = {"milestones": ["engine loop", "farming", "economy"]}
                text = json.dumps(parsed)

            return R()

    assert plan_milestones("big game", FakeProxy()) == ["engine loop", "farming", "economy"]


def test_plan_failure_is_fail_closed() -> None:
    class BadProxy:
        def complete(self, *a: Any, **k: Any) -> Any:
            class R:
                parsed = None
                text = "not json"

            return R()

    with pytest.raises(ValueError, match="no usable plan"):
        plan_milestones("big game", BadProxy())


def test_end_to_end_two_milestones_on_mock_backend(tmp_path: Path) -> None:
    # The real factory (mock backend, stub adapter) twice: milestone 2 seeds from
    # milestone 1's released repo. Slow-ish but the true exit criterion.
    report = run_milestones(
        "build a thing",
        out_dir=str(tmp_path / "out"),
        milestones=["walking skeleton", "second increment"],
        backend="mock",
        state_dir=None,
    )
    assert report.passed, report.summary()
    assert (tmp_path / "out" / "m01").is_dir()
    assert (tmp_path / "out" / "m02").is_dir()


def test_cli_milestones_subcommand(tmp_path: Path) -> None:
    from foundry.cli import main

    rc = main(
        [
            "milestones",
            "build a thing",
            "--out",
            str(tmp_path / "out"),
            "--milestones",
            "skeleton; increment",
        ]
    )
    assert rc == 0


def test_cli_milestones_usage_error() -> None:
    from foundry.cli import main

    assert main(["milestones", "--out"]) == 2
    assert main(["milestones", "no out dir given"]) == 2
