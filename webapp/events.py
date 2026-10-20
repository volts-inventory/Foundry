"""Pure mapping: real factory telemetry -> game events for the workshop frontend.

These functions turn the REAL checkpoint inspection payload (artifacts, attested
gate verdicts, the advisory agent journal) and the final ``RunResult`` into a
stream of small JSON game events the canvas renders. No I/O, no sleeps, no model
calls — deterministic and unit-testable. The server (server.py) paces them.

The factory is the source of truth; this module is a costume. Nothing here can
influence a build — it only describes what already happened.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Workshop rooms, left -> right (the agent walks the floor as the build advances).
# Engineering is virtual here: its three lanes (frontend/backend/data) are rooms of
# their own on the floor. The cast is Planner / Engineers / Reviewer (+ the gated-mode
# security trio); the gate and vault are infrastructure.
ROOMS: tuple[str, ...] = (
    "plan",
    "engineering",
    "review",
    "security",
    "gate",
    "vault",
)

# Room ids from OLD builds' replayed telemetry -> the current room.
LEGACY_ROOM: dict[str, str] = {
    "product": "plan",
    "architecture": "plan",
    "qa": "review",
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
    "reviewer_critic": ("Reviewer", "review"),
    "appsec_engineer": ("AppSec Engineer", "security"),
    "audit_compliance": ("Audit & Compliance", "security"),
    "supply_chain_reviewer": ("Supply-chain Reviewer", "security"),
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
    "scope_tier": ("Planner", "plan"),
    "feature_diff": ("Junior Engineer", "engineering"),
    "test_plan": ("Reviewer", "review"),
    "oracle_binding": ("Reviewer", "review"),
    "test_report": ("Reviewer", "review"),
    "e2e_report": ("Reviewer", "review"),
    "threat_model": ("AppSec Engineer", "security"),
    "audit_finding": ("Audit & Compliance", "security"),
    "supply_chain_report": ("Supply-chain Reviewer", "security"),
    "runbook": ("Reviewer", "review"),
    "known_issues": ("Reviewer", "review"),
    "upgrade_guide": ("Reviewer", "review"),
    "perf_cost_report": ("Reviewer", "review"),
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
    "feature_diff": "Feature Diff (code!)",
    "test_plan": "Test Plan",
    "oracle_binding": "Oracle",
    "test_report": "Test Report",
    "e2e_report": "E2E Report",
    "threat_model": "Threat Model",
    "audit_finding": "Audit Finding",
    "supply_chain_report": "Supply-chain Report",
    "runbook": "Runbook",
    "known_issues": "Known Issues",
    "upgrade_guide": "Upgrade Guide",
    "perf_cost_report": "Perf/Cost Report",
}

_BOUNDARY_ROOM: dict[str, str] = {
    "product": "plan",
    "design": "plan",
    "release": "review",
}
# Subgraph SCOPE -> room, for a node inside a stage that isn't a named agent. Without
# the implementation/quality/assurance entries those steps resolved to no room at all
# and vanished from the floor AND the log.
_SCOPE_ROOM: dict[str, str] = {
    **_BOUNDARY_ROOM,
    "architecture": "plan",
    "planning": "plan",
    "implementation": "engineering",
    "quality": "review",
    "assurance": "security",
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
    # gate runners (top-level + inside the assurance/quality subgraphs)
    "spec_gate": ("gate", "running the SPEC gate", None),
    "build_gate": ("gate", "running the BUILD gate", None),
    "test_gate": ("gate", "running the TEST gate", None),
    "smoke_gate": ("gate", "running the SMOKE gate", None),
    "build_test_gate": ("gate", "running the BUILD / TEST gates", None),
    "assurance_gate": ("gate", "running the ASSURANCE gates", None),
    "review": ("gate", "running the REVIEW gate", None),
    "security_gate": ("gate", "running the SECURITY gate", None),
    "audit_gate": ("gate", "running the AUDIT gate", None),
    "supply_chain_gate": ("gate", "running the SUPPLY-CHAIN gate", None),
    # fix-forward retries: the longest silences in a real build — each is a full
    # agentic pass over the failing tree
    "spec_fix": ("plan", "fix-forward — reworking the spec after a gate failure",
                 "spec rework finished"),
    "build_test_fix": ("engineering", "fix-forward — rebuilding after a gate failure",
                       "rebuild finished"),
    "assurance_fix": ("engineering", "fix-forward — clearing the assurance findings",
                      "assurance fixes finished"),
    # the doc pass + the evidence packs behind each sign-off
    "docs": ("review", "writing the runbook & release docs", "docs written"),
    "design_sign_off": ("gate", "assembling the design evidence", None),
    "release_sign_off": ("gate", "assembling the release evidence", None),
}
# Conditional-architecture agents that have no sprite on the floor — they still get a
# step line so the log doesn't skip them. (agent name, room, running, done)
_INFRA_NODE.update({
    "domain_specialist": ("plan", "domain specialist reviewing the design",
                          "domain review finished"),
    "ux_design": ("plan", "UX pass over the interface", "UX pass finished"),
})
_BOUNDARY_TITLE: dict[str, str] = {
    "product": "Product & Requirements",
    "design": "Architecture & Design",
    "release": "Build, Test & Review",
}

# What each button at a checkpoint actually DOES, per boundary — the modal prints
# these under the buttons so the choice is not guessed from a verb. APPROVE names
# the work it unblocks (never "ships it": approve can only let the build REACH the
# gates); REQUEST CHANGES names the rework pass it costs; REJECT is terminal
# everywhere. An unknown boundary falls back to generic-but-true copy.
_BOUNDARY_NEXT: dict[str, str] = {
    "product": "architecture & design starts",
    "design": "engineering starts building",
    "release": "the signed release gates run",
}
_BOUNDARY_REWORK: dict[str, str] = {
    "product": "another spec pass",
    "design": "another design pass",
    "release": "a fix-forward rebuild",
}
# The autopilot PROPOSAL pause is not a stage boundary — it decides whether a whole
# NEXT release gets built, so all three buttons mean something different there.
_PROPOSAL_ACTIONS: dict[str, str] = {
    "approve": "builds the proposal as the next release",
    "changes": "builds YOUR edited request instead",
    "reject": "stops autopilot — the floor stays where it is",
}


def checkpoint_actions(boundary: str) -> dict[str, str]:
    """Per-button consequence copy for the checkpoint dialog (pure, display-only)."""
    if boundary == "autopilot_proposal":
        return dict(_PROPOSAL_ACTIONS)
    return {
        "approve": _BOUNDARY_NEXT.get(boundary, "the build continues"),
        "changes": _BOUNDARY_REWORK.get(boundary, "another pass") + " — costs a full rerun",
        "reject": "stops the build — nothing ships",
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
    "appsec_engineer": "built the threat model",
    "audit_compliance": "checked audit & compliance",
    "supply_chain_reviewer": "reviewed the supply chain",
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


def events_for_payload(
    payload: dict[str, Any],
    seen_uris: set[str],
    summaries: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Game events for ONE checkpoint pause (the REAL inspection payload).

    Emits: a stage banner, an ``artifact`` (desk lights up) + ``say`` (speech
    bubble from the agent journal) for each NEW artifact since the last pause, the
    advisory gate badges so far, and the ``checkpoint`` dialog itself. ``seen_uris``
    is mutated so a re-emitted handle never double-fires (idempotent, like the
    channel folds the data came from).

    ``summaries`` (optional, from the T3 caption pass) supplies a descriptive
    ``stage`` line for the checkpoint and per-``agent`` captions that override the raw
    journal text on the speech bubbles / log block headers."""
    agent_caps: dict[str, str] = dict((summaries or {}).get("agents") or {})
    boundary = str(payload.get("boundary", ""))
    room = _BOUNDARY_ROOM.get(boundary, "plan")
    out: list[dict[str, Any]] = [
        {"type": "stage", "room": room, "boundary": boundary,
         "label": _BOUNDARY_TITLE.get(boundary, boundary.title())},
    ]

    # Org chart (Wave E): the REAL hierarchy from the inspection payload — present
    # areas, their Tech-Lead domains, and each domain's units. Emitted ONCE (the
    # OwnershipMap is stable across boundaries); the canvas renders the org tree.
    org = payload.get("org")
    if isinstance(org, dict) and "__org__" not in seen_uris:
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

    # Journal: the agents' own words -> speech bubbles (advisory, observability).
    say_by_room: dict[str, list[dict[str, str]]] = {}
    for e in payload.get("agent_journal", []):
        agent, jroom = AGENT_BY_NODE.get(str(e.get("node_id", "")),
                                         (str(e.get("role", "agent")).title(), room))
        # Prefer the T3 caption (descriptive, concise) over the raw journal text.
        bubble = agent_caps.get(agent) or str(e.get("summary", "")).strip()
        concerns = [str(c) for c in (e.get("concerns") or []) if str(c).strip()]
        say_by_room.setdefault(jroom, []).append(
            {"agent": agent, "room": jroom, "text": bubble or "Working…",
             "concern": concerns[0] if concerns else ""}
        )

    # New artifacts -> the producing desk lights up + a quest-log pickup. A stage
    # often emits MANY artifacts of one kind (e.g. 9 acceptance criteria); numbering
    # same-kind pickups (#1..#9) keeps the quest log readable instead of a wall of
    # identical "Acceptance Criterion · Planner" lines. Lone kinds (one PRD)
    # are left unnumbered. Counts are over the NEW artifacts in THIS payload only.
    new_uris = [
        u for u in (str(a.get("uri", "")) for a in payload.get("artifacts", []))
        if u and u not in seen_uris
    ]
    kind_of = {str(a.get("uri", "")): str(a.get("kind", "")) for a in payload.get("artifacts", [])}
    kind_total: Counter[str] = Counter(kind_of[u] for u in new_uris)
    kind_seen: Counter[str] = Counter()
    for a in payload.get("artifacts", []):
        uri = str(a.get("uri", ""))
        if not uri or uri in seen_uris:
            continue
        seen_uris.add(uri)
        kind = str(a.get("kind", ""))
        agent, aroom = _origin(kind, str(a.get("produced_by", "")))
        label = ARTIFACT_LABEL.get(kind, kind.replace("_", " ").title())
        if kind_total[kind] > 1:
            kind_seen[kind] += 1
            label = f"{label} #{kind_seen[kind]}"
        out.append({"type": "agent", "room": aroom, "agent": agent, "action": "work"})
        out.append({
            "type": "artifact", "room": aroom, "agent": agent, "kind": kind,
            "label": label, "detail": str(a.get("detail", "")),  # advisory content one-liner
        })
        # Attach a speech bubble for this agent if the journal had one.
        for s in say_by_room.get(aroom, []):
            if s["agent"] == agent:
                out.append({"type": "say", **s})
                break

    # Advisory gate context so far (status only — the gates still decide).
    # `run_against` names the TREE the verdict was produced over, so a human reading
    # a red gate at the checkpoint can tell a stale verdict from a current one.
    for g in payload.get("gates", []):
        out.append({"type": "gate", "name": str(g.get("name", "")),
                    "status": _norm_status(g.get("status")), "attested": True,
                    "advisory": True, "run_against": str(g.get("run_against", ""))})

    stage_caption = str((summaries or {}).get("stage") or "").strip()
    out.append({
        "type": "checkpoint", "boundary": boundary, "room": room,
        "title": _BOUNDARY_TITLE.get(boundary, boundary.title()),
        "summary": stage_caption or _checkpoint_summary(payload),  # T3 caption, else counts
        # Provenance the payload already carries and the dialog used to drop: WHERE
        # in the pipeline this pause is, and WHICH state the decision is about.
        "phase": str(payload.get("phase") or ""),
        "route": str(payload.get("route") or ""),
        "state_sha": str(payload.get("state_sha") or ""),
        "actions": checkpoint_actions(boundary),
    })
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


def _checkpoint_summary(payload: dict[str, Any]) -> str:
    n_art = len(payload.get("artifacts", []))
    gates = payload.get("gates", [])
    n_pass = sum(1 for g in gates if _norm_status(g.get("status")) == "pass")
    n_fail = sum(1 for g in gates if _norm_status(g.get("status")) == "fail")
    parts = [f"{n_art} artifacts produced"]
    if gates:
        seg = f"{n_pass}/{len(gates)} gates green"
        if n_fail:
            seg += f" · {n_fail} failing"
        parts.append(seg)
    return " · ".join(parts)
