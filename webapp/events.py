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
ROOMS: tuple[str, ...] = (
    "product",
    "architecture",
    "engineering",
    "qa",
    "security",
    "gate",
    "vault",
)

# Which room + sprite a journal node_id belongs to.
AGENT_BY_NODE: dict[str, tuple[str, str]] = {
    "stakeholder_proxy": ("Stakeholder Proxy", "product"),
    "product_manager": ("Product Manager", "product"),
    "criteria_completeness_critic": ("Criteria Critic", "product"),
    "eng_program_mgr": ("Program Manager", "product"),
    "principal_architect": ("Principal Architect", "architecture"),
    "senior_engineer": ("Senior Engineer", "engineering"),
    "junior_engineer": ("Junior Engineer", "engineering"),
    "qa_engineer": ("QA Engineer", "qa"),
    "sdet": ("SDET", "qa"),
    "reviewer_critic": ("Reviewer", "qa"),
    "appsec_engineer": ("AppSec Engineer", "security"),
    "audit_compliance": ("Audit & Compliance", "security"),
    "supply_chain_reviewer": ("Supply-chain Reviewer", "security"),
}

# Which agent/room PRODUCED each artifact kind (so a new artifact lights up a desk).
ARTIFACT_ORIGIN: dict[str, tuple[str, str]] = {
    "intent_brief": ("Stakeholder Proxy", "product"),
    "prd": ("Product Manager", "product"),
    "acceptance_criterion": ("Product Manager", "product"),
    "criteria_findings": ("Criteria Critic", "product"),
    "sprint_plan": ("Program Manager", "product"),
    "system_design": ("Principal Architect", "architecture"),
    "adr": ("Principal Architect", "architecture"),
    "interface_contract": ("Principal Architect", "architecture"),
    "ownership_map": ("Principal Architect", "architecture"),
    "feature_diff": ("Junior Engineer", "engineering"),
    "test_plan": ("QA Engineer", "qa"),
    "oracle_binding": ("QA Engineer", "qa"),
    "test_report": ("QA Engineer", "qa"),
    "e2e_report": ("SDET", "qa"),
    "threat_model": ("AppSec Engineer", "security"),
    "audit_finding": ("Audit & Compliance", "security"),
    "supply_chain_report": ("Supply-chain Reviewer", "security"),
    "runbook": ("Technical Writer", "qa"),
    "known_issues": ("Technical Writer", "qa"),
    "upgrade_guide": ("Technical Writer", "qa"),
    "perf_cost_report": ("Technical Writer", "qa"),
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
    "product": "product",
    "design": "architecture",
    "release": "qa",
}
_BOUNDARY_TITLE: dict[str, str] = {
    "product": "Product & Requirements",
    "design": "Architecture & Design",
    "release": "Build, Test & Review",
}


def _origin(kind: str, produced_by: str) -> tuple[str, str]:
    """(agent label, room) that produced an artifact of ``kind``."""
    if kind in ARTIFACT_ORIGIN:
        return ARTIFACT_ORIGIN[kind]
    if produced_by in AGENT_BY_NODE:
        return AGENT_BY_NODE[produced_by]
    return (produced_by.replace("_", " ").title() or "Agent", "engineering")


def live_event_for_node(node: str, scope: str) -> dict[str, Any] | None:
    """A LIVE "this agent is at its desk working RIGHT NOW" event for a graph node
    that just executed in the real run (streamed per-node, not the checkpoint burst).

    Maps the node id -> (agent, room) so the canvas seats that agent at its desk and
    keeps them working until the stage's checkpoint. Falls back to the subgraph
    ``scope`` -> room when the node isn't a named agent (so a stage still lights the
    right room). Returns ``None`` for infra/non-agent nodes with no desk to light."""
    who = AGENT_BY_NODE.get(node)
    if who is not None:
        agent, room = who
        return {"type": "active", "room": room, "agent": agent, "live": True}
    room = _BOUNDARY_ROOM.get(scope) or (scope if scope in ROOMS else "")
    if room:
        return {"type": "active", "room": room, "agent": "", "live": True}
    return None


def events_for_payload(
    payload: dict[str, Any], seen_uris: set[str]
) -> list[dict[str, Any]]:
    """Game events for ONE checkpoint pause (the REAL inspection payload).

    Emits: a stage banner, an ``artifact`` (desk lights up) + ``say`` (speech
    bubble from the agent journal) for each NEW artifact since the last pause, the
    advisory gate badges so far, and the ``checkpoint`` dialog itself. ``seen_uris``
    is mutated so a re-emitted handle never double-fires (idempotent, like the
    channel folds the data came from)."""
    boundary = str(payload.get("boundary", ""))
    room = _BOUNDARY_ROOM.get(boundary, "product")
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
        bubble = str(e.get("summary", "")).strip()
        concerns = [str(c) for c in (e.get("concerns") or []) if str(c).strip()]
        say_by_room.setdefault(jroom, []).append(
            {"agent": agent, "room": jroom, "text": bubble or "Working…",
             "concern": concerns[0] if concerns else ""}
        )

    # New artifacts -> the producing desk lights up + a quest-log pickup. A stage
    # often emits MANY artifacts of one kind (e.g. 9 acceptance criteria); numbering
    # same-kind pickups (#1..#9) keeps the quest log readable instead of a wall of
    # identical "Acceptance Criterion · Product Manager" lines. Lone kinds (one PRD)
    # are left unnumbered. Counts are over the NEW artifacts in THIS payload only.
    new_uris = [
        u for u in (str(a.get("uri", "")) for a in payload.get("artifacts", []))
        if u and u not in seen_uris
    ]
    kind_of = {str(a.get("uri", "")): str(a.get("kind", "")) for a in payload.get("artifacts", [])}
    kind_total: Counter[str] = Counter(kind_of[u] for u in new_uris)
    kind_seen: Counter[str] = Counter()
    spoken: set[tuple[str, str]] = set()   # (room, agent) that already got a say
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
            "label": label,
        })
        # Attach a speech bubble for this agent if the journal had one — once per
        # agent per pause, not once per artifact (an agent with three artifacts
        # would otherwise repeat the same line three times in the quest log).
        if (aroom, agent) not in spoken:
            for s in say_by_room.get(aroom, []):
                if s["agent"] == agent:
                    spoken.add((aroom, agent))
                    out.append({"type": "say", **s})
                    break

    # Advisory gate context so far (status only — the gates still decide).
    for g in payload.get("gates", []):
        out.append({"type": "gate", "name": str(g.get("name", "")),
                    "status": _norm_status(g.get("status")), "attested": True,
                    "advisory": True})

    out.append({
        "type": "checkpoint", "boundary": boundary, "room": room,
        "title": _BOUNDARY_TITLE.get(boundary, boundary.title()),
        "summary": _checkpoint_summary(payload),
    })
    return out


# Pipeline order for the closing gate recap (unknown gates sort after, by name) —
# reads as the build sequence, not an alphabet.
_GATE_SEQ = {n: i for i, n in enumerate(
    ("spec", "build", "test", "smoke", "review", "security",
     "audit", "supply_chain", "e2e"))}


def events_for_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Closing game events from the final RunResult: the signed gate verdicts, then
    the chest opening (release) or the red door (halt)."""
    out: list[dict[str, Any]] = []
    statuses = (result.get("gate_statuses") or {}).items()
    ordered = sorted(statuses, key=lambda kv: (_GATE_SEQ.get(kv[0], len(_GATE_SEQ)), kv[0]))
    for name, status in ordered:
        out.append({"type": "gate", "name": name, "status": _norm_status(status),
                    "attested": True, "advisory": False})
    if result.get("passed"):
        out.append({"type": "release", "uri": result.get("artifact_uri") or "",
                    "mode": result.get("mode", "assistant")})
    else:
        # Name the gate(s) that stopped the build — "a gate did not PASS" makes
        # the reader scan the whole verdict list to find out which.
        bad = [f"{n} gate: {_norm_status(s).upper()}"
               for n, s in ordered if _norm_status(s) != "pass"]
        out.append({"type": "halt", "phase": result.get("phase", "unknown"),
                    "reason": ("; ".join(bad) + " (fail-closed — nothing shipped)")
                    if bad else "a gate did not PASS (fail-closed — nothing shipped)"})
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
    return f"{n_art} artifacts produced · {n_pass}/{len(gates)} gates green so far"
