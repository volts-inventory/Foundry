"""Planner constants, SOP prompts and the structured-output JSON schemas."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from foundry.schemas.implementation import (
    BDD_REPORT_ENV,
    CONTRACTS_TICKET_ID,
    HARNESS_TICKET_ID,
    SKELETON_TASK_ID,
    Difficulty,
    Specialty,
)

NODE_ID = "planner"


ROLE = "Planner"


TIER = "T1"  # everything downstream inherits this plan; never auto-downgrade


_SPEC_GATE = "spec"


TICKET_KIND = "ticket"


EPIC_KIND = "epic"


_SAFE_TICKET_ID = re.compile(r"[A-Za-z0-9._-]+")


_EPIC_OF_TICKET = re.compile(r"(E\d+)-")


# Deterministic, bounds token cost on a large design doc; the full spec stays on
# the `spec` channel.
_SPEC_PROMPT_CAP = 24_000


_RETRIEVAL_K = 24


# Hard ceilings, each ticket being a coding session: inline tickets of a small
# plan, epics per plan, tickets per epic call, and tickets across all epics.
MAX_TICKETS = 40


MAX_EPICS = 12


MAX_EPIC_TICKETS = 25


MAX_PLAN_TICKETS = 200


_EPIC_WORKERS = 8


# Per-route budget envelopes (micro-USD) the sprint plan's cap is clamped into
# (governance §7): the plan may refine the Triage-seeded cap down, never widen it.
_ROUTE_CAP_MICROUSD: dict[str, int] = {
    "greenfield": 50_000_000,
    "evolution": 25_000_000,
    "bugfix": 5_000_000,
    "spike": 10_000_000,
}


_DEFAULT_CAP_MICROUSD = 25_000_000


# Where the contracts ticket writes when the plan leaves it out.
_DEFAULT_CONTRACTS_FILE: dict[str, str] = {
    "python": "contracts.py",
    "typescript": "src/contracts.ts",
    "javascript": "src/contracts.js",
    "go": "contracts/contracts.go",
    "rust": "src/contracts.rs",
    "java": "src/main/java/contracts/Contracts.java",
}


_TICKET_RULES = (
    "Cover every case the request needs. Keep each ticket within the TICKET SIZE "
    "ceiling for the model its difficulty runs on (the numbers for this run follow "
    "the request; checked: a ticket over it FAILS the Spec gate and must be split). "
    "Templates and static assets count only toward its "
    "looser files-in-all limit, so never split a web layer per page when it fits. "
    "A feature ticket owns at least one scenario: work no scenario of its own "
    "proves belongs in the ticket whose behaviour it serves. Never split off work that only one "
    "other ticket uses: give it to that ticket (a ticket only one other ticket "
    "depends on is merged into it when both fit the ceiling; never into the "
    "entrypoint, whose job is wiring the features in). Each ticket is a "
    "separate agent session and gate run, and every hand-off between tickets is a "
    "chance to mismatch. A ticket is one coherent slice of the product: its closely "
    "related behaviours and their files; tickets meet at interface contracts. State "
    "what each ticket must do and why, not how to implement it. NAMES: name every "
    "ticket id and module after its domain (ticket stats -> stats.py, ticket cli -> "
    "features/cli.feature), never after a ticket number (T3, T3.feature, "
    "test_T2.py): the gate names a ticket's feature file and step module after its "
    "id. "
    "Each ticket "
    "lists in files[] every path it may write, has a specialty (frontend | backend "
    "| data | test) and a difficulty (trivial | standard | complex — it picks the "
    "model), and depends_on the ticket ids that must land first; depends_on MUST "
    "form a DAG. Name the interface contracts each ticket implements[] and "
    "consumes[] by their interfaces[].name, so tickets built in parallel code "
    "against the same signatures. ADDED FOR YOU (never write them): the dependency "
    "on the one ticket implementing a contract a ticket consumes, the entrypoint's "
    "dependencies on every feature ticket, and every ticket's dependency on the "
    "planner's own tickets; reads[] lists only files a ticket reads beyond the "
    "contracts file and its dependencies' files (it is shown those), usually none. "
    "CONTRACT "
    "FIT (checked; a violation FAILS the Spec gate): every contract a ticket "
    "consumes is implemented by exactly ONE ticket, which the consumer depends_on "
    "(directly or through other tickets). Every implemented contract should have a "
    "consumer or a scenario on its implementing ticket. STEP DEFINITIONS bind the "
    "gate-written features/<feature ticket id>.feature (the gate renders one per "
    "ticket from its scenarios). On a Python stack with no bdd command (pytest-bdd), "
    "the feature ticket that builds the behaviour binds its own scenarios from ONE "
    "step module tests/test_<ticket id>.py that is added to its files[] for you: "
    "never list a test module for them, and never plan a test-only ticket that binds "
    "feature tickets' scenarios (checked and repaired), so each wave's check runs "
    "them as soon as it lands. "
    "So give each ticket only scenarios its own code and its dependencies can pass "
    "the moment it lands: a user-level scenario (an HTTP request, a CLI run) belongs "
    "on the ticket that exposes that surface, never on a models-only ticket. "
    "On any other stack, unless a DEMO scope cap says so, every feature ticket gets "
    "a paired specialty='test' ticket that depends_on it and writes the step "
    "definitions for that feature's scenarios in ONE step file; the test ticket "
    "carries no scenarios of its own. Never list a .feature file in files[]: "
    "feature files are gate-owned. Every repo file a stack command names (e.g. "
    "smoke.py in `python3 smoke.py`) must be in some ticket's files[]. "
    "Where a scope cap speaks of ownership units, it means the files across your "
    "tickets.\n"
    "SCENARIOS (the acceptance criteria): each feature ticket carries its executable "
    "BDD scenarios, each with a stable id unique across the plan and at least one "
    "given, one when and one then step (plain phrases, without the keyword), and "
    "covers[]: the ids of the must-haves (MH-n) and success criteria (SC-n) it "
    "proves (empty when it proves none of them). Mark "
    "high-risk scenarios 'high-risk' (omit criticality otherwise). COVERAGE (checked; a "
    "violation FAILS the Spec gate): every must-have and every success criterion is "
    "listed in the covers[] of at least one scenario; every feature (non-test) "
    "ticket carries at least one scenario; exactly one feature ticket has "
    "entrypoint=true: it wires the whole app together end to end (main / server / "
    "CLI / the public API of a library), depends_on every other feature ticket "
    "(directly or through other tickets) and carries an end-to-end scenario; omit "
    "entrypoint on every other ticket. Identify how the component "
    "can be MISUSED or ABUSED (malformed/wrong-type input, invalid operations on an "
    "empty or boundary state, resource exhaustion, authn/authz for anything exposed) "
    "and author at least one scenario covering the applicable case with "
    "security_abuse=true (omit security_abuse on the rest). ALWAYS author one "
    "scenario requiring a pinned dependency manifest for the stack at the repo root "
    "(requirements.txt / uv.lock / "
    "package-lock.json / go.sum / Cargo.lock; pin dev/test deps if there are no "
    "runtime deps), on the ticket that writes that manifest; it checks the manifest "
    "itself (present, every entry pinned), never that installing succeeds: the gates "
    "run offline (checked).\n"
    "CRITERIA REVIEW: then review your own scenarios against the request. Under "
    "criteria_review list only REAL, in-scope gaps you could not close: missing / "
    "under-specified scenarios, and applicable security/abuse cases no scenario "
    "covers. Prefer closing a gap by adding a scenario; a listed security/abuse "
    "omission FAILS the Spec gate. Return empty arrays when coverage is complete.\n"
)


_SCOPE_RULES = (
    "SCOPE (scope.tier): 'demo' only when the request is for something to look at or "
    "learn from (demo, toy, example, prototype, spike, exercise) that nobody depends "
    "on; 'standard' otherwise. Judge the subject, not the length; real users, "
    "production, deployment, money, compliance or persisted real data mean "
    "'standard'. When in doubt, 'standard'. Quote the phrases you relied on.\n"
    "INTENT: restate the intent in your own words; list assumptions and open "
    "unknowns; define success in user terms; flag must-haves vs nice-to-haves. Give "
    "each must-have an id MH-1, MH-2, ... and each success criterion an id SC-1, "
    "SC-2, ... (keep an id once given); scenarios cite them in covers[]. Declare "
    "deliverable: 'library' (only code other programs import), 'cli', 'service' or "
    "'app'.\n"
    "PRD: the problem, user stories and prioritization; declare "
    "required_capabilities (e.g. 'ui', 'ml') — they drive specialist routing.\n"
)


_DESIGN_RULES = (
    "SPRINT PLAN: ordered milestones; usd_cap_microusd as an integer number of "
    "micro-USD (never a float); a short risk register; done-for-this-iteration.\n"
    "ARCHITECTURE: choose the component decomposition; write ADRs WITH rejected "
    "alternatives; define interface/API contracts FIRST (tickets reference them): "
    "every seam between tickets is a contract with a name, a kind (function | class "
    "| type for code seams; http | cli | event for the rest), its signature written "
    "in the chosen language and a description; give inputs, outputs and errors only "
    "where the signature does not already state them (an http | cli | event "
    "contract; the errors a function raises). "
    "Every data type, interface and error that crosses a ticket boundary is a contract "
    "(kind type or class): the contracts file is its ONLY definition, every ticket "
    "imports it from there, and no ticket redeclares it (never plan a models module "
    "that copies contract types). Never name a method after a builtin (list, dict, "
    "set, type, id, filter, map, ...): inside the class it shadows the builtin used "
    "in annotations (`def list(self) -> list[Book]` fails the type checker); prefer "
    "list_books, get_by_id, find_all (checked for Python; a violation FAILS the Spec "
    "gate). A class contract the entrypoint ticket implements that composes other "
    "tickets' class contracts (the app / composition root) declares how it receives "
    "them: an __init__ (constructor) taking them, or a factory function in the "
    "contract that takes them and returns it (checked; a violation FAILS the Spec "
    "gate). Emit cross-cutting concerns as pinned constraints (their text; id, scope "
    "and source are filled in when omitted), which are injected verbatim into every "
    "build task.\n"
    "STACK: choose the stack yourself from the request: stack.language MUST be one "
    "of the LANGUAGES AVAILABLE HERE; pick the framework and package manager. Give "
    "the shell commands the gates run from the repo root, in a sandbox that can "
    "write only inside the repo: install (the ONLY step with network: it must fetch "
    "or vendor every dependency into the repo, e.g. `npm install --no-audit "
    "--no-fund`, `go mod vendor`, `cargo vendor`; it must work with no lockfile in "
    "the repo, since none may have been generated yet; Python leaves it empty), then "
    "build, typecheck (the language's "
    "own type checker over the whole project, e.g. `npx tsc --noEmit`, `go vet "
    "./...`, `python3 -m mypy .`; it proves every ticket matches the contracts), "
    "test, bdd and run, all OFFLINE; run is REQUIRED unless the deliverable is a "
    "library (checked). bdd runs every features/*.feature scenario "
    "with the language's OFFICIAL "
    "Cucumber runner and writes its report (Cucumber Messages NDJSON; godog and "
    "cucumber-rs: their Cucumber JSON) to the file named by $"
    + BDD_REPORT_ENV
    + " (Python may leave bdd empty to use the built-in pytest-bdd "
    "harness); run (optional: boots the real app headless, exits 0 on success; for "
    "a web server it is a smoke script, e.g. `node smoke.js` / `python3 smoke.py`, "
    "that starts the app on port 0, makes one request, stops it and exits 0 — never "
    "the bare server entry such as `node app.js`, `npm start`, `flask run` or "
    "`uvicorn ...`, which never exits; checked). "
    "contracts_file is the source file the code-kind contracts are written to (omit "
    "it for the language default below); harness_files are build/runner config "
    "beyond the package manifest and the runner's own config, which are included "
    "for you (usually none); "
    "cucumber-js step files go under features/step_definitions/. The ids "
    f"'{CONTRACTS_TICKET_ID}' and '{HARNESS_TICKET_ID}' are reserved: never plan a "
    f"ticket with either. '{CONTRACTS_TICKET_ID}' is added for you and writes "
    "contracts_file first; only when stack.bdd is set, "
    f"'{HARNESS_TICKET_ID}' is added too and wires the Cucumber runner into "
    "harness_files. Every ticket depends on them for you, and none may list "
    "contracts_file in files. Every Python package directory's __init__.py is added "
    "for you. "
    "Default contracts_file per language: "
    + ", ".join(f"{lang} {path}" for lang, path in _DEFAULT_CONTRACTS_FILE.items())
    + ". The code-kind contract signatures are compiled together as that file at plan "
    "time (Python with mypy, TypeScript with tsc), so write each as complete, valid "
    "declarations of the chosen language that reference only each other and the "
    "standard library.\n"
    "When a DESIGN SPEC is provided, it is the detailed source of truth: derive the "
    "plan FROM it rather than from the one-line request alone."
)


SOP = (
    "You are the Planner. In ONE reply, turn the request into a complete, buildable, "
    "testable plan: intent, product spec, acceptance criteria, delivery plan, and "
    "architecture. Fill every section of the constrained JSON schema.\n"
    + _SCOPE_RULES
    + "EPICS: group the work into epics (ids E1, E2, ... in delivery order; at most "
    f"{MAX_EPICS}). A SMALL build (one epic, at most {MAX_TICKETS} tickets) returns "
    "ONE epic and its tickets inline under tickets. A LARGER build returns its epics "
    "and leaves tickets EMPTY: each epic's tickets are then planned in a separate "
    "call that sees only this plan, so every boundary between epics MUST be an "
    "interface contract defined here — give each epic the interfaces[].name it "
    "provides[] and consumes[], the epic ids it depends_on (a DAG), and a "
    "specialty_mix hint. The LAST epic depends (directly or through other epics) on "
    "every other epic and holds the entrypoint ticket. Work over the caps is dropped "
    "(and reported), so consolidate instead.\n"
    "TICKETS: decompose the work into Jira-style tickets. "
    + _TICKET_RULES
    + "When you return epics without inline tickets, criteria_review covers only "
    "plan-level gaps: each epic reviews its own scenarios.\n" + _DESIGN_RULES
)


# Call 1 of a staged plan (a local planner level): the design alone; every epic's
# tickets and scenarios come from their own call, so one bad epic is redone alone.
DESIGN_SOP = (
    "You are the Planner. In ONE reply, write the DESIGN of a complete, buildable, "
    "testable plan: intent, product spec, delivery plan, architecture, stack, the "
    "shared interface contracts and the epics. The tickets and their scenarios (the "
    "acceptance criteria) are NOT yours: each epic's are planned in a separate call "
    "that sees only this design. Fill every section of the constrained JSON schema.\n"
    + _SCOPE_RULES
    + "EPICS: split the work into epics (ids E1, E2, ... in delivery order; at most "
    f"{MAX_EPICS}); a small build is ONE epic. Each epic becomes at most "
    f"{MAX_EPIC_TICKETS} Jira-style tickets, each a coding session within the TICKET "
    "SIZE ceiling that follows the request. Every boundary between epics MUST be an "
    "interface contract defined here — give each epic the interfaces[].name it "
    "provides[] and consumes[], the epic ids it depends_on (a DAG), and a "
    "specialty_mix hint; its summary names the behaviours and the must-have (MH-n) "
    "and success-criterion (SC-n) ids it delivers, so the epics cover every one. The "
    "LAST epic depends (directly or through other epics) on every other epic and "
    "holds the entrypoint ticket, which wires the whole app together. Work over the "
    "caps is dropped (and reported), so consolidate instead.\n" + _DESIGN_RULES
)


EPIC_SOP = (
    "You are the Epic Planner. A lead planner has written the product spec, the "
    "architecture and the shared interface contracts, and split the work into "
    "epics. In ONE reply, decompose YOUR epic into Jira-style tickets (at "
    f"most {MAX_EPIC_TICKETS}). "
    + _TICKET_RULES
    + "IDS: name each ticket after its domain (catalog, cart-api; never T01) and "
    "number scenarios AC01, AC02, ...; both are prefixed with your epic id for you. "
    "depends_on names only tickets of YOUR "
    "epic: work in other epics is ordered for you through the epic dependencies "
    "and the interfaces your tickets consume. Build only your epic's share. "
    "COVERAGE across epics: cite in covers[] the must-have / success-criterion ids "
    "your scenarios prove, and cover every one your epic delivers. Mark "
    "entrypoint=true on one ticket ONLY when told your epic owns the entrypoint: "
    "it is made to depend on every other epic's feature tickets for you."
)


_STR_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


# Contract kinds; the code kinds are written as real code by the contracts ticket.
CODE_CONTRACT_KINDS = ("function", "class", "type")


CONTRACT_KINDS = (*CODE_CONTRACT_KINDS, "http", "cli", "event")


_GENERATED_TICKETS = frozenset({CONTRACTS_TICKET_ID, HARNESS_TICKET_ID, SKELETON_TASK_ID})


# What the harness ticket always writes, on top of the plan's harness_files: the
# manifest and lockfile it adds the runner to, and the runner's entry file, so its
# description never asks for a file outside its scope. A feature ticket may list
# them too (to add its own deps): overlapping scopes run in separate waves.
_DEFAULT_HARNESS_FILES: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml",),
    "typescript": ("package.json", "package-lock.json", "tsconfig.json", "cucumber.json"),
    "javascript": ("package.json", "package-lock.json", "cucumber.json"),
    "go": ("go.mod", "go.sum", "bdd/godog_test.go"),
    "rust": ("Cargo.toml", "Cargo.lock", "tests/cucumber.rs"),
    "java": ("pom.xml", "src/test/java/RunCucumberTest.java"),
}


_SCENARIO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "given": _STR_ARRAY,
        "when": _STR_ARRAY,
        "then": _STR_ARRAY,
        "criticality": {"type": "string", "enum": ["normal", "high-risk"]},
        "security_abuse": {"type": "boolean"},
        "covers": _STR_ARRAY,
    },
    "required": ["id", "given", "when", "then", "covers"],
    "additionalProperties": False,
}


_TICKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "specialty": {"type": "string", "enum": [m.value for m in Specialty]},
        "difficulty": {"type": "string", "enum": [m.value for m in Difficulty]},
        "depends_on": _STR_ARRAY,
        "files": _STR_ARRAY,
        "reads": _STR_ARRAY,
        "implements": _STR_ARRAY,
        "consumes": _STR_ARRAY,
        "scenarios": {"type": "array", "items": _SCENARIO_SCHEMA},
        "entrypoint": {"type": "boolean"},
    },
    "required": [
        "id",
        "title",
        "specialty",
        "difficulty",
        "depends_on",
        "files",
        "scenarios",
    ],
    "additionalProperties": False,
}


_INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": list(CONTRACT_KINDS)},
        "signature": {"type": "string"},
        "inputs": _STR_ARRAY,
        "outputs": _STR_ARRAY,
        "errors": _STR_ARRAY,
        "description": {"type": "string"},
        "consumers": _STR_ARRAY,
    },
    "required": ["name", "kind", "signature", "description"],
    "additionalProperties": False,
}


_STACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "framework": {"type": "string"},
        "package_manager": {"type": "string"},
        "install": {"type": "string"},
        "build": {"type": "string"},
        "typecheck": {"type": "string"},
        "test": {"type": "string"},
        "bdd": {"type": "string"},
        "run": {"type": "string"},
        "notes": {"type": "string"},
        "contracts_file": {"type": "string"},
        "harness_files": _STR_ARRAY,
    },
    "required": [
        "language",
        "framework",
        "package_manager",
        "install",
        "build",
        "typecheck",
        "test",
        "bdd",
        "run",
    ],
    "additionalProperties": False,
}


_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "missing": _STR_ARRAY,
        "security_abuse_omissions": _STR_ARRAY,
    },
    "required": ["missing", "security_abuse_omissions"],
    "additionalProperties": False,
}


_GOAL_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"],
        "additionalProperties": False,
    },
}


DELIVERABLES = ("app", "library", "cli", "service")


_EPIC_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "specialty_mix": _STR_ARRAY,
        "depends_on": _STR_ARRAY,
        "provides": _STR_ARRAY,
        "consumes": _STR_ARRAY,
    },
    "required": ["id", "title", "summary", "depends_on", "provides", "consumes"],
    "additionalProperties": False,
}


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": ["demo", "standard"]},
                "rationale": {"type": "string"},
                "signals": _STR_ARRAY,
            },
            "required": ["tier", "rationale"],
            "additionalProperties": False,
        },
        "intent": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "assumptions": _STR_ARRAY,
                "unknowns": _STR_ARRAY,
                "success_criteria": _GOAL_SCHEMA,
                "must_haves": _GOAL_SCHEMA,
                "nice_to_haves": _STR_ARRAY,
                "deliverable": {"type": "string", "enum": list(DELIVERABLES)},
            },
            "required": ["intent", "success_criteria", "must_haves", "deliverable"],
            "additionalProperties": False,
        },
        "prd": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "required_capabilities": _STR_ARRAY,
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "criteria_review": _REVIEW_SCHEMA,
        "sprint_plan": {
            "type": "object",
            "properties": {
                "milestones": _STR_ARRAY,
                "usd_cap_microusd": {"type": "integer", "minimum": 0},
                "risk_register": {"type": "string"},
                "done_definition": {"type": "string"},
            },
            "required": ["milestones", "usd_cap_microusd", "done_definition"],
            "additionalProperties": False,
        },
        "design": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "components": _STR_ARRAY,
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "adrs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "decision": {"type": "string"},
                    "body": {"type": "string"},
                    "rejected_alternatives": _STR_ARRAY,
                },
                "required": ["title", "decision", "body"],
                "additionalProperties": False,
            },
        },
        "stack": _STACK_SCHEMA,
        "interfaces": {"type": "array", "items": _INTERFACE_SCHEMA},
        "tickets": {"type": "array", "items": _TICKET_SCHEMA},
        "epics": {"type": "array", "items": _EPIC_ITEM_SCHEMA},
        "pinned_constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "local"]},
                    "source": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "intent",
        "prd",
        "criteria_review",
        "sprint_plan",
        "design",
        "adrs",
        "stack",
        "interfaces",
        "tickets",
        "epics",
    ],
    "additionalProperties": False,
}


# Staged call 1 (:data:`DESIGN_SOP`): the plan less what the epic calls write.
_EPIC_OWNED = ("tickets", "criteria_review")


DESIGN_SCHEMA: dict[str, Any] = {
    **PLAN_SCHEMA,
    "properties": {k: v for k, v in PLAN_SCHEMA["properties"].items() if k not in _EPIC_OWNED},
    "required": [k for k in PLAN_SCHEMA["required"] if k not in _EPIC_OWNED],
}


def _plan_schema(languages: Iterable[str], *, design: bool = False) -> dict[str, Any]:
    """:data:`PLAN_SCHEMA` (``design``: :data:`DESIGN_SCHEMA`) with ``stack.language``
    limited to ``languages``."""
    base = DESIGN_SCHEMA if design else PLAN_SCHEMA
    language = {"type": "string", "enum": sorted(set(languages))}
    stack = {**_STACK_SCHEMA, "properties": {**_STACK_SCHEMA["properties"], "language": language}}
    return {**base, "properties": {**base["properties"], "stack": stack}}


EPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tickets": {"type": "array", "items": _TICKET_SCHEMA},
        "criteria_review": _REVIEW_SCHEMA,
    },
    "required": ["tickets", "criteria_review"],
    "additionalProperties": False,
}


# Not "You are the Planner": scripted test backends key the plan call on that.
DELTA_SOP = (
    "You are the Plan Patcher. The plan check FAILED a plan with the findings below. "
    "Do NOT return the whole plan: in ONE reply return a DELTA that closes every "
    "finding and changes nothing else; add a ticket only when no existing one can "
    "carry the fix.\n"
    "- tickets: each ticket you add or change, with every required field, under its "
    "id. A changed ticket keeps every scenario you do not list: list a scenario (same "
    "id) to change it or a new id to add one; a scenario listed on another ticket "
    "moves there.\n"
    "- remove_tickets: ids of tickets to delete (move any work they carry first).\n"
    "- remove_scenarios: ids of scenarios to delete.\n"
    "- criteria_review: gaps that remain after your delta (empty arrays when none).\n"
    "The interface contracts are fixed: close a contract-fit finding through the "
    "tickets' implements[], consumes[] and depends_on. The planner's own tickets "
    f"'{CONTRACTS_TICKET_ID}' and '{HARNESS_TICKET_ID}' are added for you: never name "
    "them.\nThe plan's rules: " + _TICKET_RULES
)


DELTA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tickets": {"type": "array", "items": _TICKET_SCHEMA},
        "remove_tickets": _STR_ARRAY,
        "remove_scenarios": _STR_ARRAY,
        "criteria_review": _REVIEW_SCHEMA,
    },
    "required": ["tickets", "remove_tickets", "remove_scenarios", "criteria_review"],
    "additionalProperties": False,
}
