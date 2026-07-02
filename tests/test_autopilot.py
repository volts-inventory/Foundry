"""Autopilot: Director proposes, the panel votes, the tally is deterministic.

Covers the tally math (majority veto, score floor, stable ties), ballot
robustness (malformed ballots dropped, never guessed), the loop's chaining and
its two fail-closed stops (gate refusal, panel decline), and the CLI route.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.lifecycle.autopilot import (
    Candidate,
    PanelVote,
    autopilot,
    propose_candidates,
    run_panel,
    tally,
)

C = [
    Candidate(title="fishing minigame", rationale="depth", scope="rod, lake, 3 fish"),
    Candidate(title="save slots", rationale="retention", scope="3 slots + autosave"),
    Candidate(title="rewrite renderer", rationale="perf", scope="new pipeline"),
]


def vote(name: str, scores: tuple[int, ...], veto: int = -1, amendment: str = "") -> PanelVote:
    return PanelVote(panelist=name, scores=scores, veto_index=veto, amendment=amendment)


# ---- tally --------------------------------------------------------------------


def test_highest_mean_wins_and_amendments_ride() -> None:
    d = tally(C, [
        vote("eng", (6, 8, 3)),
        vote("ux", (7, 9, 2), amendment="autosave must be on by default"),
        vote("qa", (6, 8, 1)),
    ])
    assert d.winner is C[1] and d.winner_index == 1
    assert d.amendments == ("autosave must be on by default",)


def test_majority_veto_kills_the_top_scorer() -> None:
    d = tally(C, [
        vote("eng", (5, 9, 1), veto=1),
        vote("ux", (6, 9, 1), veto=1),
        vote("qa", (6, 9, 1)),
    ])
    assert d.winner is C[0]  # candidate 1 had the top mean but 2/3 vetoed it


def test_nothing_above_floor_declines() -> None:
    d = tally(C, [vote("eng", (4, 4, 2)), vote("ux", (3, 4, 1))])
    assert d.declined


def test_no_ballots_declines() -> None:
    assert tally(C, []).declined


def test_tie_breaks_to_director_order() -> None:
    d = tally(C, [vote("eng", (8, 8, 2))])
    assert d.winner is C[0]  # stable: earlier candidate wins the tie


# ---- director / panel calls -------------------------------------------------------


class ScriptedProxy:
    """Returns scripted parsed payloads per node_id prefix."""

    def __init__(self, director: Any, ballots: dict[str, Any]) -> None:
        self.director = director
        self.ballots = ballots

    def complete(self, node_id: str, tier: str, sop: str, messages: Any, *,
                 purpose: str, schema: dict[str, Any]) -> Any:
        assert purpose == "ideation"  # advisory only, never the verdict path

        class R:
            parsed: Any = None
            text = ""

        r = R()
        if node_id == "autopilot_director":
            r.parsed = self.director
        else:
            r.parsed = self.ballots.get(node_id.removeprefix("autopilot_panel_"))
        return r


def test_director_and_panel_round_trip() -> None:
    proxy = ScriptedProxy(
        director={"candidates": [
            {"title": "a", "rationale": "r", "scope": "s"},
            {"title": "b", "rationale": "r", "scope": "s"},
        ]},
        ballots={
            "staff_engineer": {"scores": [3, 9]},
            "usability_designer": {"scores": [4, 8], "amendment": "add tooltips"},
            "product_designer": {"scores": [5, 7]},
            "qa_lead": {"scores": "not-a-list"},  # malformed ballot: DROPPED
        },
    )
    cands = propose_candidates(proxy, "target", "evidence")
    votes = run_panel(proxy, "target", "evidence", cands)
    assert [v.panelist for v in votes] == [
        "staff_engineer", "usability_designer", "product_designer"
    ]
    d = tally(cands, votes)
    assert d.winner.title == "b"
    assert d.amendments == ("add tooltips",)


def test_director_with_no_candidates_fails_closed() -> None:
    proxy = ScriptedProxy(director={"candidates": []}, ballots={})
    with pytest.raises(ValueError, match="no usable candidates"):
        propose_candidates(proxy, "t", "e")


# ---- the loop ----------------------------------------------------------------------


class FakeRun:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes, self.calls = outcomes, []

    def __call__(self, request: str, **kw: Any) -> Any:
        self.calls.append({"request": request, **kw})

        class R: ...

        r = R()
        r.passed = self.outcomes[len(self.calls) - 1]
        return r


def happy_proxy() -> ScriptedProxy:
    return ScriptedProxy(
        director={"candidates": [
            {"title": "next-a", "rationale": "r", "scope": "do a"},
            {"title": "next-b", "rationale": "r", "scope": "do b"},
        ]},
        ballots={
            "staff_engineer": {"scores": [8, 4]},
            "usability_designer": {"scores": [9, 3]},
            "product_designer": {"scores": [7, 5]},
            "qa_lead": {"scores": [8, 4]},
        },
    )


def test_loop_chains_released_trees_with_panel_chosen_requests(tmp_path: Path) -> None:
    fake = FakeRun([True, True, True])
    report = autopilot(
        "moon colony game", out_dir=str(tmp_path), iterations=3,
        proxy=happy_proxy(), run_fn=fake,
    )
    assert len(report.iterations) == 3 and all(r.passed for r in report.iterations)
    assert fake.calls[0]["request"] == "moon colony game"
    assert "NEXT ITERATION: next-a" in fake.calls[1]["request"]  # the panel's winner
    assert fake.calls[1]["base_dir"] == str(tmp_path / "i01")  # chained released tree
    assert fake.calls[2]["base_dir"] == str(tmp_path / "i02")
    assert "budget" in report.stopped


def test_loop_stops_on_gate_refusal(tmp_path: Path) -> None:
    fake = FakeRun([True, False])
    report = autopilot(
        "t", out_dir=str(tmp_path), iterations=5, proxy=happy_proxy(), run_fn=fake,
    )
    assert len(fake.calls) == 2 and "gates refused" in report.stopped
    assert report.released_dir == str(tmp_path / "i01")


def test_loop_stops_on_panel_decline(tmp_path: Path) -> None:
    proxy = ScriptedProxy(
        director={"candidates": [
            {"title": "a", "rationale": "r", "scope": "s"},
            {"title": "b", "rationale": "r", "scope": "s"},
        ]},
        ballots={"staff_engineer": {"scores": [2, 3]}},  # nothing clears the floor
    )
    fake = FakeRun([True, True])
    report = autopilot("t", out_dir=str(tmp_path), iterations=4, proxy=proxy, run_fn=fake)
    assert len(fake.calls) == 1  # never built what the panel declined
    assert "panel declined" in report.stopped


def test_cli_autopilot_usage_error() -> None:
    from foundry.cli import main

    assert main(["autopilot", "--out"]) == 2
    assert main(["autopilot", "no out given"]) == 2
