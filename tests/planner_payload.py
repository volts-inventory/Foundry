"""Compose one planner reply from per-section test payloads.

Graph-level tests script the factory's model calls by SOP substring. The planner
makes the single planning call, so its reply bundles what used to be several
persona replies; this keeps each test's section payloads readable on their own.
"""

from __future__ import annotations

from typing import Any

PLANNER_SOP_KEY = "You are the Planner"


def _scenario(criterion: dict[str, Any]) -> dict[str, Any]:
    """A legacy text criterion as a scenario: its text stays the rendered form."""
    out = {k: criterion[k] for k in ("id", "criticality", "security_abuse") if k in criterion}
    return {**out, "text": criterion.get("text", ""), "given": [], "when": [], "then": []}


def plan_payload(
    *,
    intent: dict[str, Any],
    prd: dict[str, Any],
    critique: dict[str, Any],
    sprint: dict[str, Any],
    design: dict[str, Any],
    decomposition: dict[str, Any] | None = None,
    tier: str = "standard",
) -> dict[str, Any]:
    """``design["ownership"]`` is ``{unit: owner}``; ``decomposition["tasks"]``
    (``[{"unit", "criterion_ids"}]``) supplies the criterion ids per unit.

    Each unit becomes one backend ticket owning that file and the scenarios for
    its criteria (a criterion goes to the first unit naming it); criteria no unit
    names ride a file-less ticket so they still reach the Spec gate."""
    criteria_of = {
        t["unit"]: list(t.get("criterion_ids", ()))
        for t in (decomposition or {}).get("tasks", ())
    }
    criteria = {c["id"]: c for c in prd.get("acceptance_criteria", [])}
    placed: set[str] = set()
    tickets: list[dict[str, Any]] = []
    for i, unit in enumerate(design.get("ownership", {}), start=1):
        mine = [c for c in criteria_of.get(unit, []) if c in criteria and c not in placed]
        placed.update(mine)
        tickets.append({
            "id": f"T-{i:03d}",
            "title": f"build {unit}",
            "specialty": "backend",
            "difficulty": "standard",
            "depends_on": [],
            "files": [unit],
            "scenarios": [_scenario(criteria[c]) for c in mine],
        })
    orphans = [c for cid, c in criteria.items() if cid not in placed]
    if orphans:
        tickets.append({
            "id": "T-SPEC",
            "title": "cross-cutting acceptance",
            "specialty": "backend",
            "difficulty": "standard",
            "depends_on": [],
            "files": [],
            "scenarios": [_scenario(c) for c in orphans],
        })
    return {
        "scope": {"tier": tier, "rationale": "test scope", "signals": []},
        "intent": intent,
        "prd": {
            "title": prd["title"],
            "body": prd.get("prd_body", ""),
            "required_capabilities": prd.get("required_capabilities", []),
        },
        "criteria_review": critique,
        "sprint_plan": {
            "milestones": sprint.get("milestones", []),
            "usd_cap_microusd": sprint.get("usd_cap_microusd", 0),
            "risk_register": sprint.get("risk_register_body", ""),
            "done_definition": sprint.get("done_definition", ""),
        },
        "design": {
            "title": design["title"],
            "body": design.get("design_body", ""),
            "components": design.get("components", []),
        },
        "adrs": design.get("adrs", []),
        "interfaces": design.get("interfaces", []),
        "tickets": tickets,
        "pinned_constraints": design.get("pinned_constraints", []),
    }
