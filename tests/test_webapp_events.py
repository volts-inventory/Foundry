"""webapp.events — pure telemetry->game-event mapping (no server, no factory)."""
from __future__ import annotations

from webapp.events import events_for_payload, events_for_result, live_event_for_node


def test_live_event_for_node_seats_the_agent_at_its_desk() -> None:
    # START: a named agent node seats that agent in its room for the whole model call.
    ev = live_event_for_node("planner", "planning", "start")
    assert ev == {"type": "active", "room": "plan", "agent": "Planner",
                  "live": True, "phase": "start"}
    # FINISH: clears the agent and carries a human summary of what the step produced.
    fin = live_event_for_node("planner", "planning", "finish")
    assert fin["phase"] == "finish"
    assert fin["room"] == "plan" and fin["agent"] == "Planner"
    assert "Planner" in fin["summary"] and fin["summary"] != "Planner"
    # An unnamed node falls back to the subgraph scope -> room (room still lights).
    ev2 = live_event_for_node("some_infra_node", "engineering", "start")
    assert ev2 == {"type": "active", "room": "engineering", "agent": "", "live": True,
                   "phase": "start"}
    # A node with neither a known agent nor a room scope -> no desk to light.
    assert live_event_for_node("__start__", "", "start") is None


def test_infra_steps_are_reported_even_with_no_agent_and_no_scope() -> None:
    # The gate runners and fix-forward passes are TOP-LEVEL nodes, so the stream gives
    # them an EMPTY scope. They used to drop out entirely — no room, no log line — which
    # left the quest log blank through the slowest part of a build.
    gate = live_event_for_node("build_test_gate", "", "start")
    assert gate is not None
    assert gate["room"] == "gate" and gate["agent"] == ""
    assert "BUILD" in gate["step"]
    # A gate's OUTCOME is already reported by its own `gate` event, so the finish of the
    # runner carries no duplicate label (the event still fires, to clear the room).
    assert "step" not in live_event_for_node("build_test_gate", "", "finish")  # type: ignore[operator]

    # A fix-forward retry is the longest silence in a real build: labelled both ways.
    fix = live_event_for_node("build_test_fix", "", "start")
    assert fix is not None and fix["room"] == "engineering"
    assert "fix-forward" in fix["step"]
    done = live_event_for_node("build_test_fix", "", "finish")
    assert done is not None and done["step"] == "rebuild finished"

    # Named agents are unchanged — they still open an agent box, never a step line.
    named = live_event_for_node("sdet", "quality", "finish")
    assert named is not None and "step" not in named and "Reviewer" in named["summary"]


def test_subgraph_scopes_all_resolve_to_a_room() -> None:
    # The scope the graph stream actually hands us is a colon-joined namespace PATH
    # ("factory:implementation"), not a bare stage name — these are the real strings
    # observed from a run. Matching the whole string resolved nothing, so every
    # unnamed step inside a stage was dropped.
    for scope, room in (("factory:implementation", "engineering"),
                        ("factory:quality", "review"),
                        ("factory:assurance", "review"),   # old builds' replays
                        ("factory:architecture", "plan"),
                        ("factory:product", "plan"),
                        ("implementation", "engineering")):    # bare form still works
        ev = live_event_for_node("some_unnamed_node", scope, "start")
        assert ev is not None and ev["room"] == room, scope
    # A scope naming no stage at all still yields nothing to light.
    assert live_event_for_node("some_unnamed_node", "factory", "start") is None


def test_payload_maps_artifacts_journal_and_checkpoint() -> None:
    payload = {
        "boundary": "product",
        "artifacts": [{"kind": "prd", "uri": "cas://a", "produced_by": "product_manager"}],
        "gates": [{"name": "spec", "status": "GateStatus.PASS"}],
        "agent_journal": [{"node_id": "product_manager", "role": "pm",
                           "summary": "Authored the PRD", "concerns": ["thin criteria"]}],
    }
    seen: set[str] = set()
    evs = events_for_payload(payload, seen)
    types = [e["type"] for e in evs]
    assert types[0] == "stage" and types[-1] == "checkpoint"
    assert any(e["type"] == "artifact" and e["label"] == "PRD" for e in evs)
    assert any(e["type"] == "say" and "PRD" in e["text"] for e in evs)
    assert any(e["type"] == "gate" and e["name"] == "spec" and e["status"] == "pass" for e in evs)
    assert "cas://a" in seen  # idempotent: re-emit won't double-fire


def test_same_kind_artifacts_are_numbered_not_vague_dups() -> None:
    # A stage that emits many artifacts of ONE kind (e.g. 9 acceptance criteria)
    # must number the quest-log pickups (#1..#N) instead of N identical lines.
    payload = {
        "boundary": "product",
        "artifacts": [
            {"kind": "prd", "uri": "cas://prd", "produced_by": "product_manager"},
            {"kind": "acceptance_criterion", "uri": "cas://c1", "produced_by": "product_manager"},
            {"kind": "acceptance_criterion", "uri": "cas://c2", "produced_by": "product_manager"},
            {"kind": "acceptance_criterion", "uri": "cas://c3", "produced_by": "product_manager"},
        ],
    }
    labels = [e["label"] for e in events_for_payload(payload, set()) if e["type"] == "artifact"]
    # Lone kind stays unnumbered; the repeated kind is numbered in order.
    assert "PRD" in labels
    assert "Acceptance Criterion #1" in labels
    assert "Acceptance Criterion #2" in labels
    assert "Acceptance Criterion #3" in labels
    # No bare (unnumbered) duplicate of the repeated kind leaks through.
    assert "Acceptance Criterion" not in labels


def test_org_chart_emitted_once_from_payload() -> None:
    payload = {
        "boundary": "design",
        "artifacts": [],
        "gates": [],
        "agent_journal": [],
        "org": {
            "areas": [
                {"area": "frontend", "leads": ["ui_team"]},
                {"area": "backend", "leads": ["svc"]},
            ],
            "domains": [
                {"owner": "ui_team", "area": "frontend", "units": ["web/Nav.tsx"]},
                {"owner": "svc", "area": "backend", "units": ["services/a.py"]},
            ],
        },
    }
    seen: set[str] = set()
    evs = events_for_payload(payload, seen)
    org = [e for e in evs if e["type"] == "org"]
    assert len(org) == 1
    assert {a["area"] for a in org[0]["areas"]} == {"frontend", "backend"}
    assert any(d["owner"] == "svc" for d in org[0]["domains"])
    # re-emitting the same payload does NOT fire a second org event (idempotent)
    assert not any(e["type"] == "org" for e in events_for_payload(payload, seen))


def test_seen_uris_dedup() -> None:
    payload = {"boundary": "product",
               "artifacts": [{"kind": "prd", "uri": "cas://a", "produced_by": "pm"}],
               "gates": [], "agent_journal": []}
    seen = {"cas://a"}
    evs = events_for_payload(payload, seen)
    assert not any(e["type"] == "artifact" for e in evs)  # already seen


def test_result_release_opens_chest() -> None:
    evs = events_for_result({"passed": True, "phase": "done", "mode": "assistant",
                             "artifact_uri": "cas://diff",
                             "gate_statuses": {"test": "GateStatus.PASS"}})
    assert any(e["type"] == "release" and e["uri"] == "cas://diff" for e in evs)
    assert evs[-1] == {"type": "done", "passed": True, "phase": "done"}


def test_result_halt_on_fail() -> None:
    evs = events_for_result({"passed": False, "phase": "build_test_gate", "mode": "assistant",
                             "artifact_uri": None, "gate_statuses": {"test": "GateStatus.FAIL"}})
    assert any(e["type"] == "halt" for e in evs)
    assert not any(e["type"] == "release" for e in evs)


def test_checkpoint_carries_provenance_and_button_consequences() -> None:
    # The approval dialog used to get a title and one sentence. The payload always
    # carried WHERE the pause is and WHICH state it decides on — carry it through.
    payload = {
        "boundary": "design",
        "phase": "architecture",
        "route": "gated",
        "state_sha": "abc123def456789",
        "artifacts": [],
        "gates": [],
    }
    cp = next(e for e in events_for_payload(payload, set()) if e["type"] == "checkpoint")
    assert cp["phase"] == "architecture"
    assert cp["route"] == "gated"
    assert cp["state_sha"] == "abc123def456789"
    # Each button says what it DOES, per boundary — "approve" never claims to ship.
    assert "engineering" in cp["actions"]["approve"]
    assert "ship" not in cp["actions"]["approve"]
    assert "nothing ships" in cp["actions"]["reject"]


def test_missing_provenance_degrades_to_empty_not_none() -> None:
    # An older/leaner payload must not put "None" strings in front of a human.
    cp = next(e for e in events_for_payload({"boundary": "product"}, set())
              if e["type"] == "checkpoint")
    assert cp["phase"] == "" and cp["route"] == "" and cp["state_sha"] == ""
    assert cp["actions"]["approve"]      # copy is always present


def test_checkpoint_actions_differ_per_boundary() -> None:
    from webapp.events import checkpoint_actions

    assert checkpoint_actions("product")["approve"] != checkpoint_actions("design")["approve"]
    assert "gates" in checkpoint_actions("release")["approve"]
    # The autopilot proposal decides a whole NEXT release — all three differ there.
    prop = checkpoint_actions("autopilot_proposal")
    assert "next release" in prop["approve"]
    assert "edited" in prop["changes"]
    assert "autopilot" in prop["reject"]
    # An unknown boundary still yields true, generic copy (never a KeyError).
    unknown = checkpoint_actions("something_new")
    assert unknown["approve"] and unknown["changes"] and unknown["reject"]


def test_gate_events_carry_the_tree_they_ran_against() -> None:
    # A red gate at a checkpoint is only readable if you can tell a stale verdict
    # from a current one — `run_against` names the tree it was produced over.
    payload = {
        "boundary": "release",
        "artifacts": [],
        "gates": [{"name": "test", "status": "GateStatus.FAIL", "run_against": "sha-777"}],
    }
    gate = next(e for e in events_for_payload(payload, set()) if e["type"] == "gate")
    assert gate["status"] == "fail" and gate["run_against"] == "sha-777"
    # A payload without it degrades to "" (the dialog just omits the tooltip).
    bare = {"boundary": "release", "artifacts": [],
            "gates": [{"name": "test", "status": "pass"}]}
    bare_gate = next(e for e in events_for_payload(bare, set()) if e["type"] == "gate")
    assert bare_gate["run_against"] == ""


def test_reviewer_persona_does_not_collide_with_the_review_gate() -> None:
    # `review` is the REVIEW gate (infra, no sprite); `reviewer` is the Reviewer persona.
    gate = live_event_for_node("review", "", "start")
    assert gate is not None and gate["room"] == "gate" and gate["agent"] == ""
    who = live_event_for_node("reviewer", "", "start")
    assert who is not None and who["room"] == "review" and who["agent"] == "Reviewer"


def test_cast_is_planner_engineers_reviewer_and_old_ids_still_seat_someone() -> None:
    expect = {
        "planner": ("Planner", "plan"),
        "frontend_lead": ("Frontend Engineer", "frontend"),
        "backend_engineer": ("Backend Engineer", "backend"),
        "data_lead": ("Data Engineer", "data"),
        "reviewer": ("Reviewer", "review"),
        # replayed telemetry from old builds
        "stakeholder_proxy": ("Planner", "plan"),
        "principal_architect": ("Planner", "plan"),
        "eng_program_mgr": ("Planner", "plan"),
        "qa_engineer": ("Reviewer", "review"),
        "sdet": ("Reviewer", "review"),
        "reviewer_critic": ("Reviewer", "review"),
        "appsec_engineer": ("Reviewer", "review"),
        "audit_compliance": ("Reviewer", "review"),
        "supply_chain_reviewer": ("Reviewer", "review"),
    }
    for node, (agent, room) in expect.items():
        ev = live_event_for_node(node, "", "start")
        assert ev is not None and (ev["agent"], ev["room"]) == (agent, room), node
    out = events_for_payload(
        {"boundary": "design", "artifacts": [
            {"uri": "u1", "kind": "prd", "produced_by": "planner"},
            {"uri": "u2", "kind": "test_report", "produced_by": "reviewer"}]}, set())
    arts = {e["kind"]: (e["agent"], e["room"]) for e in out if e["type"] == "artifact"}
    assert arts == {"prd": ("Planner", "plan"), "test_report": ("Reviewer", "review")}


def test_ticket_artifact_surfaces_as_a_planner_pickup_and_a_ticket_event() -> None:
    import json

    from webapp.events import event_for_task, ticket_event

    detail = json.dumps({"id": "T-001", "title": "Add login", "specialty": "frontend",
                         "difficulty": "complex", "depends_on": ["T-000"]})
    payload = {"boundary": "design", "artifacts": [
        {"kind": "ticket", "uri": "cas://t1", "produced_by": "planner", "detail": detail},
        {"kind": "ticket", "uri": "cas://t2", "produced_by": "planner", "detail": "plain text"},
    ]}
    evs = events_for_payload(payload, set())
    arts = [e for e in evs if e["type"] == "artifact"]
    assert [(a["room"], a["agent"], a["kind"]) for a in arts] == [("plan", "Planner", "ticket")] * 2
    assert arts[0]["label"] == "Ticket #1" and arts[0]["detail"] == "T-001 — Add login"
    tickets = [e for e in evs if e["type"] == "ticket"]
    assert tickets == [{"type": "ticket", "id": "T-001", "title": "Add login",
                        "specialty": "frontend", "difficulty": "complex", "tier": "T1",
                        "depends_on": ["T-000"]}]          # the unparseable one stays a pickup only
    assert ticket_event("not json") is None

    # pool TaskEvent -> floor `task` event; malformed events are dropped
    ev = {"phase": "start", "ticket_id": "T-001", "title": "Add login",
          "specialty": "test", "tier": "T3"}
    assert event_for_task(ev) == {"type": "task", **ev}
    odd = event_for_task({**ev, "specialty": "weird"})
    assert odd is not None and odd["specialty"] == "backend"
    assert event_for_task({**ev, "phase": "bogus"}) is None
    assert event_for_task({**ev, "ticket_id": ""}) is None


def test_ticket_artifact_detail_feeds_the_tickets_tab() -> None:
    from foundry.schemas.checkpoint import _artifact_detail
    from foundry.schemas.implementation import Difficulty, Specialty, Ticket
    from foundry.state.seq import Seq
    from webapp.events import ticket_event

    long_title = "Add an item to the list " * 20
    ticket = Ticket(id="T-001", title=long_title, specialty=Specialty.DATA,
                    difficulty=Difficulty.COMPLEX, depends_on=("T-000",),
                    seq=Seq("r", 1, 1, "planner", 0))
    detail = _artifact_detail("ticket", ticket)
    assert detail is not None
    row = ticket_event(detail)
    assert row is not None
    assert row["id"] == "T-001" and row["specialty"] == "data"
    assert row["tier"] == "T1" and row["depends_on"] == ["T-000"]
    assert row["title"] == long_title.strip()
