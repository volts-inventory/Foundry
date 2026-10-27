"""Compose one planner reply from per-section test payloads.

Graph-level tests script the factory's model calls by SOP substring. The planner
makes the single planning call, so its reply bundles what used to be several
persona replies; this keeps each test's section payloads readable on their own.
"""

from __future__ import annotations

from typing import Any

PLANNER_SOP_KEY = "You are the Planner"

# Python without a bdd command: the gates keep the pinned pytest-bdd harness.
PYTHON_STACK: dict[str, Any] = {
    "language": "python", "framework": "", "package_manager": "pip", "install": "",
    "build": "", "typecheck": "", "test": "pytest", "bdd": "", "run": "", "notes": "",
    "contracts_file": "", "harness_files": [],
}


def _scenario(criterion: dict[str, Any]) -> dict[str, Any]:
    """A legacy text criterion as a scenario: its text stays the rendered form."""
    out = {k: criterion[k] for k in ("id", "criticality", "security_abuse") if k in criterion}
    return {**out, "text": criterion.get("text", ""), "given": [], "when": [], "then": []}


def _goal_ids(intent: dict[str, Any]) -> list[str]:
    """The ids the planner gives the intent's must-haves / success criteria when
    they are bare strings: positional ``MH-<n>`` / ``SC-<n>``."""
    return [
        *(f"MH-{i}" for i, _ in enumerate(intent.get("must_haves", ()), start=1)),
        *(f"SC-{i}" for i, _ in enumerate(intent.get("success_criteria", ()), start=1)),
    ]


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
    names ride a file-less ticket so they still reach the Spec gate.

    Full coverage by construction: the first scenario covers every must-have and
    success criterion, the last ticket is the entrypoint (depending on every
    other) and the deliverable defaults to a library (no run command needed)."""
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
    scenarios = [s for t in tickets for s in t["scenarios"]]
    if scenarios:
        scenarios[0]["covers"] = _goal_ids(intent)
    if tickets:
        tickets[-1]["entrypoint"] = True
        tickets[-1]["depends_on"] = [t["id"] for t in tickets[:-1]]
    return {
        "scope": {"tier": tier, "rationale": "test scope", "signals": []},
        "intent": {"deliverable": "library", **intent},
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
        "stack": PYTHON_STACK,
        "interfaces": design.get("interfaces", []),
        # A single epic with its tickets inline: the planner makes one call.
        "epics": [{
            "id": "E1", "title": prd["title"], "summary": prd.get("prd_body", ""),
            "depends_on": [], "provides": [], "consumes": [],
        }],
        "tickets": tickets,
        "pinned_constraints": design.get("pinned_constraints", []),
    }


# ---- a large plan: three epics joined by interface contracts -------------------

EPIC_PLANNER_SOP_KEY = "You are the Epic Planner"


def _ticket(tid: str, specialty: str, *deps: str, **extra: Any) -> dict[str, Any]:
    return {"id": tid, "title": f"{specialty} {tid}", "specialty": specialty,
            "difficulty": "standard", "depends_on": list(deps), "files": [f"{tid}.py"],
            "scenarios": [], **extra}


def _scn(cid: str, *covers: str, abuse: bool = False) -> dict[str, Any]:
    return {"id": cid, "given": ["g"], "when": ["w"], "then": ["t"], "security_abuse": abuse,
            "covers": list(covers)}


def _review(*missing: str) -> dict[str, Any]:
    return {"missing": list(missing), "security_abuse_omissions": []}


def multi_epic_plan() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Call 1's reply (three epics, no inline tickets) and each epic call's reply.

    E1 (storage) provides ``Store.save`` / ``Store.load``; E2 (api) depends on E1
    and consumes ``Store.load``, so its root ticket waits for E1's ``Store.load``
    implementer; E3 (web) depends on E2 but consumes none of E2's contracts, so its
    root waits for E2's sink ticket. E3's ``web page`` id is not file-safe; it is
    the entrypoint (E3 is downstream of every epic). Coverage is complete: each
    must-have / success criterion is covered by a scenario in some epic."""
    plan = {
        "scope": {"tier": "standard", "rationale": "a real product", "signals": []},
        "intent": {
            "intent": "A notes product with storage, an API and a web UI.",
            "must_haves": [{"id": "MH-1", "text": "notes persist"},
                           {"id": "MH-2", "text": "notes are served over HTTP"}],
            "success_criteria": [{"id": "SC-1", "text": "a user sees a note in the browser"}],
            "deliverable": "app",
        },
        "prd": {"title": "Notes", "body": "Users keep notes.", "required_capabilities": []},
        "criteria_review": _review(),
        "sprint_plan": {"milestones": ["M1"], "usd_cap_microusd": 1_000_000,
                        "done_definition": "green"},
        "design": {"title": "Notes", "body": "web -> api -> store",
                   "components": ["store", "api", "web"]},
        "adrs": [],
        "stack": {**PYTHON_STACK, "run": "python -m notes --check"},
        "interfaces": [
            {"name": "Store.save", "spec": "save(note) -> id"},
            {"name": "Store.load", "spec": "load(id) -> note"},
            {"name": "Api.http", "spec": "GET /notes/{id}"},
        ],
        "epics": [
            {"id": "E1", "title": "Storage", "summary": "persist notes",
             "specialty_mix": ["data", "test"], "depends_on": [],
             "provides": ["Store.save", "Store.load"], "consumes": []},
            {"id": "E2", "title": "API", "summary": "serve notes",
             "specialty_mix": ["backend", "test"], "depends_on": ["E1"],
             "provides": ["Api.http"], "consumes": ["Store.load"]},
            {"id": "E3", "title": "Web", "summary": "show notes",
             "specialty_mix": ["frontend", "test"], "depends_on": ["E2"],
             "provides": [], "consumes": []},
        ],
        "tickets": [],
        "pinned_constraints": [],
    }
    replies = {
        "E1": {
            "tickets": [
                _ticket("T01", "data", implements=["Store.save"], scenarios=[_scn("AC01", "MH-1")]),
                _ticket("T02", "data", "T01", implements=["Store.load"],
                        scenarios=[_scn("AC02", abuse=True)]),
                _ticket("T03", "test", "T01", "T02"),
            ],
            "criteria_review": _review(),
        },
        "E2": {
            "tickets": [
                _ticket("T01", "backend", implements=["Api.http"], consumes=["Store.load"],
                        scenarios=[_scn("AC01", "MH-2")]),
                _ticket("T02", "test", "T01"),
            ],
            "criteria_review": _review(),
        },
        "E3": {
            "tickets": [
                _ticket("web page", "frontend", scenarios=[_scn("AC01", "SC-1")],
                        entrypoint=True),
                _ticket("T02", "test", "web page"),
            ],
            "criteria_review": _review(),
        },
    }
    return plan, replies
