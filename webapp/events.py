"""Pure mapping: real factory telemetry -> game events for the workshop frontend.

These functions turn the REAL per-node telemetry (each finished node's artifacts,
org chart and agent journal) and the final ``RunResult`` into a stream of small
JSON game events the canvas renders. No I/O, no sleeps, no model calls —
deterministic and unit-testable.

The factory is the source of truth; this module is a costume. Nothing here can
influence a build — it only describes what already happened.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

# Workshop rooms, left -> right (the agent walks the floor as the build advances).
# Engineering is virtual here: its three lanes (frontend/backend/data) are rooms of
# their own on the floor. The cast is Planner / Engineers / Reviewer; the gate and
# vault are infrastructure.
ROOMS: tuple[str, ...] = (
    "plan",
    "engineering",
    "review",
    "gate",
    "vault",
)

# Room ids from OLD builds' replayed telemetry -> the current room.
LEGACY_ROOM: dict[str, str] = {
    "product": "plan",
    "architecture": "plan",
    "qa": "review",
    "security": "review",
}

# Which room + sprite a journal node_id belongs to. Labels are the floor's sprite keys.
# The planner / reviewer are single nodes; the old per-role ids stay mapped so replayed
# telemetry still seats someone (product + architecture -> Planner, qa -> Reviewer, each
# lane's lead -> that lane's Engineer).
AGENT_BY_NODE: dict[str, tuple[str, str]] = {
    "planner": ("Planner", "plan"),
    "stakeholder_proxy": ("Planner", "plan"),
    "product_manager": ("Planner", "plan"),
    "criteria_completeness_critic": ("Planner", "plan"),
    "eng_program_mgr": ("Planner", "plan"),
    "principal_architect": ("Planner", "plan"),
    # Legacy single-node ids kept so an OLD build's replayed telemetry still seats someone.
    "senior_engineer": ("Senior Engineer", "engineering"),
    "junior_engineer": ("Junior Engineer", "engineering"),
    "frontend_lead": ("Frontend Engineer", "frontend"),
    "frontend_engineer": ("Frontend Engineer", "frontend"),
    "backend_lead": ("Backend Engineer", "backend"),
    "backend_engineer": ("Backend Engineer", "backend"),
    "data_lead": ("Data Engineer", "data"),
    "data_engineer": ("Data Engineer", "data"),
    "fixer": ("Engineer", "engineering"),
    "reviewer": ("Reviewer", "review"),
    "qa_engineer": ("Reviewer", "review"),
    "sdet": ("Reviewer", "review"),
    # The removed assurance crew: old ids seat the Reviewer on replay.
    "reviewer_critic": ("Reviewer", "review"),
    "appsec_engineer": ("Reviewer", "review"),
    "audit_compliance": ("Reviewer", "review"),
    "supply_chain_reviewer": ("Reviewer", "review"),
}

# Which agent/room PRODUCED each artifact kind (so a new artifact lights up a desk).
ARTIFACT_ORIGIN: dict[str, tuple[str, str]] = {
    "intent_brief": ("Planner", "plan"),
    "prd": ("Planner", "plan"),
    "acceptance_criterion": ("Planner", "plan"),
    "criteria_findings": ("Planner", "plan"),
    "sprint_plan": ("Planner", "plan"),
    "system_design": ("Planner", "plan"),
    "adr": ("Planner", "plan"),
    "interface_contract": ("Planner", "plan"),
    "ownership_map": ("Planner", "plan"),
    "ticket": ("Planner", "plan"),
    "epic": ("Planner", "plan"),
    "scope_tier": ("Planner", "plan"),
    "feature_diff": ("Junior Engineer", "engineering"),
    "test_plan": ("Reviewer", "review"),
    "oracle_binding": ("Reviewer", "review"),
    "test_report": ("Reviewer", "review"),
    "e2e_report": ("Reviewer", "review"),
    # Assurance artifacts from old builds' replayed telemetry.
    "threat_model": ("Reviewer", "review"),
    "audit_finding": ("Reviewer", "review"),
    "supply_chain_report": ("Reviewer", "review"),
}

# A friendly label per artifact kind (shown as a little pickup icon + quest line).
ARTIFACT_LABEL: dict[str, str] = {
    "intent_brief": "Intent Brief",
    "prd": "PRD",
    "acceptance_criterion": "Acceptance Criterion",
    "criteria_findings": "Criteria Critique",
    "sprint_plan": "Sprint Plan",
    "system_design": "System Design",
    "adr": "ADR",
    "interface_contract": "Interface Contract",
    "ownership_map": "Ownership Map",
    "ticket": "Ticket",
    "epic": "Epic",
    "feature_diff": "Feature Diff (code!)",
    "test_plan": "Test Plan",
    "oracle_binding": "Oracle",
    "test_report": "Test Report",
    "e2e_report": "E2E Report",
    "threat_model": "Threat Model",
    "audit_finding": "Audit Finding",
    "supply_chain_report": "Supply-chain Report",
}

# Subgraph SCOPE -> room, for a node inside a stage that isn't a named agent. Without
# the implementation/quality entries those steps resolved to no room at all
# and vanished from the floor AND the log.
_SCOPE_ROOM: dict[str, str] = {
    "architecture": "plan",
    "planning": "plan",
    "implementation": "engineering",
    "quality": "review",
    "assurance": "review",  # old builds' replayed telemetry
}


def _room_for_scope(scope: str) -> str:
    """Room for a graph-stream scope, or "" if it names none.

    ``scope`` is the graph's namespace PATH, colon-joined outermost-first — the real
    values are ``"factory"``, ``"factory:implementation"``, ``"factory:quality"``… so it
    has to be matched per segment (innermost stage first), never as a whole string.
    """
    for seg in reversed(scope.split(":")):
        room = _SCOPE_ROOM.get(seg) or LEGACY_ROOM.get(seg) or (seg if seg in ROOMS else "")
        if room:
            return room
    return ""
# INFRA steps — real graph nodes with no desk to sit at. They used to be invisible:
# most are registered on the TOP-LEVEL graph, where the stream namespace is empty, so
# `scope` was "" and the event was dropped entirely. That silence covered the slowest
# work in a build (the fix-forward passes are full agentic retries) and left the quest
# log blank while the floor visibly re-staffed around it.
#
# node -> (room, label while running, label when done). A ``None`` done-label means
# ANOTHER event already reports the outcome — the gate runners emit their own
# pass/fail verdict, so announcing the finish twice would just be noise.
_INFRA_NODE: dict[str, tuple[str, str, str | None]] = {
    "triage": ("plan", "sorting the request into a build plan", "build plan ready"),
    # gate runners
    "spec_gate": ("gate", "running the SPEC gate", None),
    "build_gate": ("gate", "running the BUILD gate", None),
    "test_gate": ("gate", "running the TEST gate", None),
    "smoke_gate": ("gate", "running the SMOKE gate", None),
    "build_test_gate": ("gate", "running the BUILD / TEST gates", None),
    "review": ("gate", "running the REVIEW gate", None),
    # fix-forward retries: the longest silences in a real build — each is a full
    # agentic pass over the failing tree
    "spec_fix": ("plan", "fix-forward — reworking the spec after a gate failure",
                 "spec rework finished"),
    "build_test_fix": ("engineering", "fix-forward — rebuilding after a gate failure",
                       "rebuild finished"),
}
# Conditional-architecture agents that have no sprite on the floor — they still get a
# step line so the log doesn't skip them. (agent name, room, running, done)
_INFRA_NODE.update({
    "domain_specialist": ("plan", "domain specialist reviewing the design",
                          "domain review finished"),
    "ux_design": ("plan", "UX pass over the interface", "UX pass finished"),
})
# A supervisor phase node STARTING opens a new stage on the floor: (room, banner).
STAGE_FOR_NODE: dict[str, tuple[str, str]] = {
    "planner": ("plan", "Product & Design"),
    "implementation": ("engineering", "Engineering"),
    "quality": ("review", "Test & Review"),
}


def _origin(kind: str, produced_by: str) -> tuple[str, str]:
    """(agent label, room) that produced an artifact of ``kind``."""
    if kind in ARTIFACT_ORIGIN:
        return ARTIFACT_ORIGIN[kind]
    if produced_by in AGENT_BY_NODE:
        return AGENT_BY_NODE[produced_by]
    return (produced_by.replace("_", " ").title() or "Agent", "engineering")


# A short, human "what this node just did" line for the quest log (task completion).
# Keyed by node id; falls back to the agent name when a node isn't listed here.
_NODE_SUMMARY: dict[str, str] = {
    "planner": "wrote the PRD, acceptance criteria, design & sprint plan",
    "stakeholder_proxy": "clarified the intent brief",
    "product_manager": "wrote the PRD & acceptance criteria",
    "criteria_completeness_critic": "critiqued the acceptance criteria",
    "eng_program_mgr": "drafted the sprint plan",
    "principal_architect": "produced the system design & interface contracts",
    "senior_engineer": "decomposed the design into ownership-ordered tasks",
    "junior_engineer": "built the feature diffs (code) across the task set",
    "frontend_lead": "lined up the frontend work",
    "frontend_engineer": "built the frontend feature diffs (code)",
    "backend_lead": "lined up the backend work",
    "backend_engineer": "built the backend feature diffs (code)",
    "data_lead": "lined up the data-layer work",
    "data_engineer": "built the data-layer feature diffs (code)",
    "fixer": "repaired the failing gates",
    "reviewer": "reviewed the change and ran the test plan",
    "qa_engineer": "ran the test plan",
    "sdet": "ran the end-to-end checks",
    "reviewer_critic": "reviewed the change",
}


def live_event_for_node(
    node: str, scope: str, phase: str = "finish", result: Any = None
) -> dict[str, Any] | None:
    """A LIVE "this agent's node is executing / just finished" event for a real graph
    node, streamed per-node (start AND finish), not the retroactive checkpoint burst.

    ``phase="start"`` seats the agent at their desk for the WHOLE model call; ``"finish"``
    clears them and carries a ``summary`` of what the step produced (for the quest log).

    Resolution order: the node id -> (agent, room) for a named lead; else the
    :data:`_INFRA_NODE` table, which gives desk-less steps (gates, fix-forward passes,
    the doc pass) a room and a ``step`` label so they show up in the log too; else the
    subgraph ``scope`` -> room, so an unmapped step at least lights the right room.
    Returns ``None`` only for pure plumbing with neither a room nor a label."""
    who = AGENT_BY_NODE.get(node)
    infra = None if who is not None else _INFRA_NODE.get(node)
    if who is not None:
        agent, room = who
    elif infra is not None:
        room, agent = infra[0], ""
    else:
        room = _room_for_scope(scope)
        if not room:
            return None
        agent = ""
    ev: dict[str, Any] = {
        "type": "active", "room": room, "agent": agent, "live": True, "phase": phase,
    }
    if infra is not None:
        # A desk-less step: `step` is what the quest log prints (no agent box). The
        # done-label is None where another event already reports the outcome.
        label = infra[2] if phase == "finish" else infra[1]
        if label:
            ev["step"] = label
    elif phase == "finish":
        verb = _NODE_SUMMARY.get(node)
        if not verb and agent:
            verb = "finished its step"
        if verb:
            ev["summary"] = f"{agent} {verb}" if agent else verb
    return ev


TASK_PHASES = ("start", "done", "failed")
_SPECIALTIES = ("frontend", "backend", "data", "test")
_TIER_FOR_DIFFICULTY = {"trivial": "T3", "standard": "T2", "complex": "T1"}


def event_for_task(ev: Mapping[str, Any]) -> dict[str, Any] | None:
    """A work-pool TaskEvent -> the floor's ``task`` event (a worker sprite appears on
    ``start`` and leaves on ``done``/``failed``). None for a malformed event."""
    phase = str(ev.get("phase", ""))
    ticket_id = str(ev.get("ticket_id", ""))
    if phase not in TASK_PHASES or not ticket_id:
        return None
    specialty = str(ev.get("specialty", ""))
    return {
        "type": "task", "phase": phase, "ticket_id": ticket_id,
        "title": str(ev.get("title", "")),
        "specialty": specialty if specialty in _SPECIALTIES else "backend",
        "tier": str(ev.get("tier", "")),
    }


def ticket_event(
    detail: str, ticket_tiers: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """The Tickets-tab row for a ``ticket`` artifact. Its ``detail`` one-liner is a JSON
    object (id, title, specialty, difficulty, depends_on); anything else yields None and
    the artifact still shows as an ordinary quest-log pickup."""
    try:
        d = json.loads(detail)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or not str(d.get("id", "")).strip():
        return None
    difficulty = str(d.get("difficulty") or "standard")
    specialty = str(d.get("specialty") or "backend")
    return {
        "type": "ticket", "id": str(d["id"]), "title": str(d.get("title", "")),
        "specialty": specialty if specialty in _SPECIALTIES else "backend",
        "difficulty": difficulty,
        "tier": (ticket_tiers or {}).get(difficulty)
        or _TIER_FOR_DIFFICULTY.get(difficulty, "T2"),
        "depends_on": [str(x) for x in (d.get("depends_on") or [])],
        "retired": bool(d.get("retired")),
    }


def journal_dicts(entries: Any) -> list[dict[str, Any]]:
    """A node's ``agent_journal`` writes (JournalEntry records) as plain dicts."""
    out: list[dict[str, Any]] = []
    for e in entries or ():
        if isinstance(e, Mapping):
            out.append(dict(e))
            continue
        out.append({
            "node_id": str(getattr(e, "node_id", "")),
            "role": str(getattr(e, "role", "")),
            "summary": str(getattr(e, "summary", "")),
            "assumptions": list(getattr(e, "assumptions", ()) or ()),
            "concerns": list(getattr(e, "concerns", ()) or ()),
            "confidence": getattr(e, "confidence", None),
        })
    return out


def events_for_node_output(
    result: Mapping[str, Any],
    seen_uris: set[str],
    ticket_tiers: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Game events for ONE finished node's output (its channel writes plus the CLI's
    ``_art_details`` / ``_org`` telemetry annotations).

    Emits the org chart (once), and for each NEW artifact a desk pulse + quest-log
    pickup (+ a Tickets-tab row for a ticket, + the agent's journal note as a speech
    bubble). ``seen_uris`` is mutated so a handle re-reported by an enclosing node
    never double-fires."""
    out: list[dict[str, Any]] = []
    org = result.get("_org")
    if isinstance(org, Mapping) and "__org__" not in seen_uris:
        seen_uris.add("__org__")
        out.append({
            "type": "org",
            "areas": [
                {"area": str(a.get("area", "")), "leads": [str(x) for x in a.get("leads", [])]}
                for a in org.get("areas", [])
            ],
            "domains": [
                {"owner": str(d.get("owner", "")), "area": str(d.get("area", "")),
                 "units": [str(u) for u in d.get("units", [])]}
                for d in org.get("domains", [])
            ],
        })

    say_by_agent: dict[str, dict[str, str]] = {}
    for e in journal_dicts(result.get("agent_journal")):
        agent, jroom = AGENT_BY_NODE.get(str(e.get("node_id", "")),
                                         (str(e.get("role", "agent")).title(), "plan"))
        concerns = [str(c) for c in (e.get("concerns") or []) if str(c).strip()]
        say_by_agent.setdefault(agent, {
            "agent": agent, "room": jroom,
            "text": str(e.get("summary", "")).strip() or "Working…",
            "concern": concerns[0] if concerns else "",
        })

    # A node often emits MANY artifacts of one kind (e.g. 9 acceptance criteria);
    # numbering same-kind pickups (#1..#9) keeps the quest log readable.
    arts = [a for a in (result.get("_art_details") or []) if isinstance(a, Mapping)]
    new = [a for a in arts if str(a.get("uri", "")) and str(a["uri"]) not in seen_uris]
    kind_total: Counter[str] = Counter(str(a.get("kind", "")) for a in new)
    kind_seen: Counter[str] = Counter()
    said: set[str] = set()
    for a in new:
        uri = str(a["uri"])
        if uri in seen_uris:
            continue
        seen_uris.add(uri)
        kind = str(a.get("kind", ""))
        agent, aroom = _origin(kind, str(a.get("produced_by", "")))
        label = ARTIFACT_LABEL.get(kind, kind.replace("_", " ").title())
        if kind_total[kind] > 1:
            kind_seen[kind] += 1
            label = f"{label} #{kind_seen[kind]}"
        out.append({"type": "agent", "room": aroom, "agent": agent, "action": "work"})
        detail = str(a.get("detail", ""))   # advisory content one-liner
        tk = ticket_event(detail, ticket_tiers) if kind == "ticket" else None
        if tk is not None:
            detail = f"{tk['id']} — {tk['title']}"
        out.append({
            "type": "artifact", "room": aroom, "agent": agent, "kind": kind,
            "label": label, "detail": detail,
        })
        if tk is not None:
            out.append(tk)
        say = say_by_agent.get(agent)
        if say is not None and agent not in said:
            said.add(agent)
            out.append({"type": "say", **say})

    # Gate verdicts as they land, so the HUD shows a mid-run FAIL -> fix -> PASS.
    # Advisory: the closing events carry the verdicts that decide the release.
    for g in result.get("gates") or ():
        def field(key: str, g: Any = g) -> Any:
            return g.get(key) if isinstance(g, Mapping) else getattr(g, key, None)

        name = str(field("name") or "")
        key = f"gate:{name}:{field('seq')}"
        if not name or key in seen_uris:
            continue
        seen_uris.add(key)
        out.append({"type": "gate", "name": name, "status": _norm_status(field("status")),
                    "attested": True, "advisory": True,
                    "run_against": str(field("run_against") or "")})
    return out


def events_for_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Closing game events from the final RunResult: the signed gate verdicts, then
    the chest opening (release) or the red door (halt)."""
    out: list[dict[str, Any]] = []
    for name, status in sorted((result.get("gate_statuses") or {}).items()):
        out.append({"type": "gate", "name": name, "status": _norm_status(status),
                    "attested": True, "advisory": False})
    if result.get("passed"):
        out.append({"type": "release", "uri": result.get("artifact_uri") or "",
                    "mode": result.get("mode", "assistant")})
    else:
        out.append({"type": "halt", "phase": result.get("phase", "unknown"),
                    "reason": "a gate did not PASS — the build is held (fail-closed)"})
    out.append({"type": "done", "passed": bool(result.get("passed")),
                "phase": result.get("phase", "unknown")})
    return out


def _norm_status(status: Any) -> str:
    s = str(status).lower()
    if "pass" in s:
        return "pass"
    if "fail" in s:
        return "fail"
    return "error" if "error" in s else s
