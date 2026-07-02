"""Workshop autopilot: a green build with budget spawns the panel-chosen next
release on the SAME floor; a decline (or ideation failure) stops fail-closed
with a recorded note. The AI drives in every mode — the child inherits the
parent's mode/supervision, so autonomous stays hands-off and gated keeps its
human checkpoints.
"""

from __future__ import annotations

import time
from typing import Any

import webapp.server as server
from foundry.lifecycle.autopilot import Candidate, PanelDecision, PanelVote
from webapp.server import Project, _maybe_autopilot_next


def finished_project(**overrides: Any) -> Project:
    with server._LOCK:
        server._FLOOR[0] += 1
        floor = server._FLOOR[0]
    p = Project(
        id=f"test-ap-{floor}", request="moon colony game", mode="assistant",
        supervised=False, floor=floor, backend="mock", adapter="stub",
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    p.passed = True
    p.status = "done"
    with server._LOCK:
        server._PROJECTS[p.id] = p
    server._STORE.upsert({"id": p.id, "floor": p.floor, "request": p.request,
                          "backend": p.backend, "adapter": p.adapter, "gates": {}})
    return p


def wait_for(predicate: Any, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_green_build_with_budget_spawns_panel_chosen_child(monkeypatch: Any) -> None:
    decision = PanelDecision(
        winner=Candidate(title="save slots", rationale="retention", scope="3 slots"),
        winner_index=0,
        mean_scores=(8.0,),
        amendments=("autosave on by default",),
        votes=(PanelVote(panelist="usability_designer", scores=(8,)),),
    )
    import foundry.lifecycle.autopilot as ap

    monkeypatch.setattr(ap, "propose_candidates", lambda *a, **k: [decision.winner])
    monkeypatch.setattr(ap, "run_panel", lambda *a, **k: list(decision.votes))
    monkeypatch.setattr(ap, "tally", lambda *a, **k: decision)

    p = finished_project(autopilot_budget=2, mode="autonomous")
    before = set(server._PROJECTS)
    _maybe_autopilot_next(p)

    assert wait_for(lambda: set(server._PROJECTS) - before), "no child spawned"
    child = server._PROJECTS[(set(server._PROJECTS) - before).pop()]
    assert child.parent_id == p.id and child.floor == p.floor  # same floor, next release
    assert child.autopilot_budget == 1  # budget decrements toward a bounded stop
    assert child.mode == "autonomous" and child.supervised is False  # hands-off inherited
    assert "save slots" in child.request and "autosave on by default" in child.request
    assert wait_for(lambda: any(e.get("type") == "narrate" and "Autopilot chose" in
                                str(e.get("text", "")) for e in child.log))
    # the spawned build actually RUNS (mock backend) to done — the AI kept driving
    assert wait_for(lambda: child.status == "done", timeout=120)


def test_panel_decline_stops_with_a_recorded_note(monkeypatch: Any) -> None:
    import foundry.lifecycle.autopilot as ap

    monkeypatch.setattr(ap, "propose_candidates",
                        lambda *a, **k: [Candidate(title="a", rationale="", scope="")])
    monkeypatch.setattr(ap, "run_panel", lambda *a, **k: [])
    monkeypatch.setattr(ap, "tally", lambda *a, **k: PanelDecision(winner=None))

    p = finished_project(autopilot_budget=3)
    before = set(server._PROJECTS)
    _maybe_autopilot_next(p)
    assert wait_for(lambda: "declined" in p.autopilot_note)
    assert set(server._PROJECTS) == before  # nothing was built


def test_no_budget_or_failed_build_is_a_noop() -> None:
    p = finished_project(autopilot_budget=0)
    before = set(server._PROJECTS)
    _maybe_autopilot_next(p)
    time.sleep(0.3)
    assert set(server._PROJECTS) == before
    p2 = finished_project(autopilot_budget=2)
    p2.passed = False
    _maybe_autopilot_next(p2)
    time.sleep(0.3)
    assert set(server._PROJECTS) == before | {p2.id}
