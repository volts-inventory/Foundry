"""webapp.events — pure telemetry->game-event mapping (no server, no factory)."""
from __future__ import annotations

from webapp.events import events_for_payload, events_for_result


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
