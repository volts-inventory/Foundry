"""Compose one planner reply from per-section test payloads.

Graph-level tests script the factory's model calls by SOP substring. The planner
makes the single planning call, so its reply bundles what used to be several
persona replies; this keeps each test's section payloads readable on their own.
"""

from __future__ import annotations

from typing import Any

PLANNER_SOP_KEY = "You are the Planner"


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
    (``[{"unit", "criterion_ids"}]``) supplies the criterion ids per unit."""
    criteria_of = {
        t["unit"]: list(t.get("criterion_ids", ()))
        for t in (decomposition or {}).get("tasks", ())
    }
    return {
        "scope": {"tier": tier, "rationale": "test scope", "signals": []},
        "intent": intent,
        "prd": {
            "title": prd["title"],
            "body": prd.get("prd_body", ""),
            "required_capabilities": prd.get("required_capabilities", []),
        },
        "acceptance_criteria": prd.get("acceptance_criteria", []),
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
        "ownership": [
            {"unit": unit, "owner": owner, "criterion_ids": criteria_of.get(unit, [])}
            for unit, owner in design.get("ownership", {}).items()
        ],
        "pinned_constraints": design.get("pinned_constraints", []),
    }
