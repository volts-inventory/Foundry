"""Planner node — the whole product + architecture plan in ONE model call.

Replaces the former persona chain (scope judge -> stakeholder proxy -> product
manager -> criteria critic -> program manager -> principal architect, plus the
per-lane lead decomposition call). Each of those was a separate ``claude -p``
launch re-reading the same request; one T1 structured call now authors the full
plan, and the node fans it out into the SAME records the old chain wrote, so
every downstream consumer (Spec gate, leads, docs, Workshop) reads unchanged
channels:

* ``artifacts``: ``intent_brief``, ``prd``, ``acceptance_criterion`` (each),
  ``criteria_findings``, ``sprint_plan``, ``system_design``, ``adr`` (each),
  ``interface_contract`` (each), ``ticket`` (each), ``ownership_map`` —
  content-addressed handles.
* ``pinned_constraints``, ``required_capabilities``, ``scope_tier`` (only when
  not already decided — it is single-writer), an advisory ``gate_triage`` on the
  ``spec`` gate, and ONE ``agent_journal`` entry.

Large plans: call 1 also splits the work into epics joined by the interface
contracts it defines. A small plan (one epic) carries its tickets inline, so it
stays one call; otherwise each epic's tickets come from their own call, run in
parallel, namespaced ``<epic>-<id>`` and merged in epic order, with each epic's
root tickets made to depend on the upstream epics' tickets implementing what it
consumes (else on their sink tickets).

The plan is a DAG of atomic tickets, each carrying its Given/When/Then scenarios
(one ``AcceptanceCriterion`` per scenario, ``ticket_id`` set). The ownership map
is DERIVED from the tickets' files (with ``unit_criteria`` per file) so the
ownership-driven scheduler keeps working unchanged.

Re-entry: ``spec_fix`` routes back here. The newest FAILing Spec-gate verdict's
findings, and the criteria already declared, go into the prompt so the revision
closes the gap instead of re-deriving the same plan.

Trust posture is unchanged: the node emits no ``GateResult`` (L5), reaches the
model only via ``rt.proxy`` (L6), and its self-review of the criteria is stamped
``JudgmentProvenance.SELF`` — the signed Spec gate still decides.
"""

from __future__ import annotations

import builtins
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from foundry.agents._common import (
    as_text,
    cas_uri,
    demo_directives,
    emit_artifact,
    journal_entry,
    persist_prose,
    scope_directive,
)
from foundry.schemas.architecture import (
    ADR,
    ConstraintScope,
    InterfaceContract,
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.gates import GateStatus, GateTriage, JudgmentProvenance
from foundry.schemas.governance import ScopeDecision, ScopeTier
from foundry.schemas.implementation import (
    BDD_REPORT_ENV,
    CONTRACTS_TICKET_ID,
    FEATURE_DIR,
    HARNESS_TICKET_ID,
    Difficulty,
    Epic,
    Specialty,
    StackDecision,
    Ticket,
    Toolchain,
    scenario_ref,
)
from foundry.schemas.product import (
    PRD,
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    IntentBrief,
    SprintPlan,
    render_scenario,
)
from foundry.state.reducers import live_constraints

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.context import SymbolIndex
    from foundry.context.retriever import Span
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

__all__ = [
    "EPIC_KIND",
    "EPIC_SCHEMA",
    "EPIC_SOP",
    "MAX_EPICS",
    "MAX_EPIC_TICKETS",
    "MAX_PLAN_TICKETS",
    "MAX_TICKETS",
    "NODE_ID",
    "PLAN_SCHEMA",
    "SOP",
    "TICKET_KIND",
    "TIER",
    "make_planner_node",
    "ticket_owner",
]

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

_TICKET_RULES = (
    "Each ticket is ONE behaviour touching roughly 1-2 files, "
    "lists in files[] every path it may write, has a specialty (frontend | backend "
    "| data | test) and a difficulty (trivial | standard | complex — it picks the "
    "model), and depends_on the ticket ids that must land first; depends_on MUST "
    "form a DAG. Name the interface contracts each ticket implements[] and "
    "consumes[] by their interfaces[].name, so tickets built in parallel code "
    "against the same signatures, and list in reads[] the files it reads. CONTRACT "
    "FIT (checked; a violation FAILS the Spec gate): every contract a ticket "
    "consumes is implemented by exactly ONE ticket, which the consumer depends_on "
    "(directly or through other tickets); every implemented contract has a consumer "
    "or a scenario on its implementing ticket. Every feature ticket gets a paired "
    "specialty='test' ticket that depends_on it and writes the step definitions for "
    "that feature's scenarios, binding the gate-written features/<feature ticket "
    "id>.feature (the gate renders one per ticket from its scenarios); the test "
    "ticket carries no scenarios of its own. Never list a .feature file in files[]: "
    "feature files are gate-owned. Every repo file a stack command names (e.g. "
    "smoke.py in `python3 smoke.py`) must be in some ticket's files[]. "
    "Where a scope cap speaks of ownership units, it means the files across your "
    "tickets.\n"
    "SCENARIOS (the acceptance criteria): each feature ticket carries its executable "
    "BDD scenarios, each with a stable id unique across the plan and at least one "
    "given, one when and one then step (plain phrases, without the keyword), and "
    "covers[]: the ids of the must-haves (MH-n) and success criteria (SC-n) it "
    "proves (empty when it proves none of them). Mark "
    "high-risk scenarios 'high-risk', the rest 'normal'. COVERAGE (checked; a "
    "violation FAILS the Spec gate): every must-have and every success criterion is "
    "listed in the covers[] of at least one scenario; every feature (non-test) "
    "ticket carries at least one scenario; exactly one feature ticket has "
    "entrypoint=true: it wires the whole app together end to end (main / server / "
    "CLI / the public API of a library), depends_on every other feature ticket "
    "(directly or through other tickets) and carries an end-to-end scenario; every "
    "other ticket has entrypoint=false. Identify how the component "
    "can be MISUSED or ABUSED (malformed/wrong-type input, invalid operations on an "
    "empty or boundary state, resource exhaustion, authn/authz for anything exposed) "
    "and author at least one scenario covering the applicable case with "
    "security_abuse=true. ALWAYS author one scenario requiring a pinned dependency "
    "manifest for the stack at the repo root (requirements.txt / uv.lock / "
    "package-lock.json / go.sum / Cargo.lock; pin dev/test deps if there are no "
    "runtime deps), on the ticket that writes that manifest.\n"
    "CRITERIA REVIEW: then review your own scenarios against the request. Under "
    "criteria_review list only REAL, in-scope gaps you could not close: missing / "
    "under-specified scenarios, and applicable security/abuse cases no scenario "
    "covers. Prefer closing a gap by adding a scenario; a listed security/abuse "
    "omission FAILS the Spec gate. Return empty arrays when coverage is complete.\n"
)

SOP = (
    "You are the Planner. In ONE reply, turn the request into a complete, buildable, "
    "testable plan: intent, product spec, acceptance criteria, delivery plan, and "
    "architecture. Fill every section of the constrained JSON schema.\n"
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
    "EPICS: group the work into epics (ids E1, E2, ... in delivery order; at most "
    f"{MAX_EPICS}). A SMALL build (one epic, at most {MAX_TICKETS} tickets) returns "
    "ONE epic and its tickets inline under tickets. A LARGER build returns its epics "
    "and leaves tickets EMPTY: each epic's tickets are then planned in a separate "
    "call that sees only this plan, so every boundary between epics MUST be an "
    "interface contract defined here — give each epic the interfaces[].name it "
    "provides[] and consumes[], the epic ids it depends_on (a DAG), and a "
    "specialty_mix hint. The LAST epic depends (directly or through other epics) on "
    "every other epic and holds the entrypoint ticket. Over the caps, work is not "
    "silently lost: the overflow FAILS the Spec gate, so consolidate instead.\n"
    "TICKETS: decompose the work into ATOMIC, Jira-style tickets. " + _TICKET_RULES
    + "When you return epics without inline tickets, criteria_review covers only "
    "plan-level gaps: each epic reviews its own scenarios.\n"
    "SPRINT PLAN: ordered milestones; usd_cap_microusd as an integer number of "
    "micro-USD (never a float); a short risk register; done-for-this-iteration.\n"
    "ARCHITECTURE: choose the component decomposition; write ADRs WITH rejected "
    "alternatives; define interface/API contracts FIRST (tickets reference them): "
    "every seam between tickets is a contract with a name, a kind (function | class "
    "| type for code seams; http | cli | event for the rest), its signature written "
    "in the chosen language, its inputs, outputs and errors, and a description. "
    "Every data type, interface and error that crosses a ticket boundary is a contract "
    "(kind type or class): the contracts file is its ONLY definition, every ticket "
    "imports it from there, and no ticket redeclares it (never plan a models module "
    "that copies contract types). Never name a method after a builtin (list, dict, "
    "set, type, id, filter, map, ...): inside the class it shadows the builtin used "
    "in annotations (`def list(self) -> list[Book]` fails the type checker); prefer "
    "list_books, get_by_id, find_all (checked for Python; a violation FAILS the Spec "
    "gate). Emit cross-cutting concerns as pinned constraints, which are injected "
    "verbatim into every build task.\n"
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
    "Cucumber runner and writes Cucumber Messages NDJSON to the file named by $"
    + BDD_REPORT_ENV + " (Python may leave bdd empty to use the built-in pytest-bdd "
    "harness); run (optional: boots the real app headless, exits 0 on success). "
    "contracts_file is the source file the code-kind contracts are written to (e.g. "
    "src/contracts.ts, contracts.py); harness_files are the package manifest and "
    "build/runner config (e.g. package.json, tsconfig.json, cucumber.json). Two "
    f"tickets are added for you: '{CONTRACTS_TICKET_ID}' writes contracts_file first "
    f"and '{HARNESS_TICKET_ID}' wires the Cucumber runner into harness_files; every "
    "ticket you write depends on them, and none may list contracts_file in files.\n"
    "When a DESIGN SPEC is provided, it is the detailed source of truth: derive the "
    "plan FROM it rather than from the one-line request alone."
)

EPIC_SOP = (
    "You are the Epic Planner. A lead planner has written the product spec, the "
    "architecture and the shared interface contracts, and split the work into "
    "epics. In ONE reply, decompose YOUR epic into ATOMIC, Jira-style tickets (at "
    f"most {MAX_EPIC_TICKETS}). " + _TICKET_RULES
    + "IDS: number tickets T01, T02, ... and scenarios AC01, AC02, ...; they are "
    "prefixed with your epic id for you. depends_on names only tickets of YOUR "
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
_GENERATED_TICKETS = frozenset({CONTRACTS_TICKET_ID, HARNESS_TICKET_ID})

# Where the contracts ticket writes, and what the harness ticket writes, when the
# plan leaves it out.
_DEFAULT_CONTRACTS_FILE: dict[str, str] = {
    "python": "contracts.py",
    "typescript": "src/contracts.ts",
    "javascript": "src/contracts.js",
    "go": "contracts/contracts.go",
    "rust": "src/contracts.rs",
    "java": "src/main/java/contracts/Contracts.java",
}
_DEFAULT_HARNESS_FILES: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml",),
    "typescript": ("package.json", "tsconfig.json", "cucumber.json"),
    "javascript": ("package.json", "cucumber.json"),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
    "java": ("pom.xml",),
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
    "required": ["id", "given", "when", "then", "security_abuse", "covers"],
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
        "id", "title", "specialty", "difficulty", "depends_on", "files", "scenarios",
        "entrypoint",
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
    "required": ["name", "kind", "signature", "inputs", "outputs", "errors", "description"],
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
        "language", "framework", "package_manager", "install", "build", "typecheck",
        "test", "bdd", "run", "contracts_file", "harness_files",
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
                "required": ["id", "title", "decision", "body"],
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
                "required": ["id", "text", "scope", "source"],
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


def _plan_schema(languages: Iterable[str]) -> dict[str, Any]:
    """:data:`PLAN_SCHEMA` with ``stack.language`` limited to ``languages``."""
    language = {"type": "string", "enum": sorted(set(languages))}
    stack = {**_STACK_SCHEMA, "properties": {**_STACK_SCHEMA["properties"], "language": language}}
    return {**PLAN_SCHEMA, "properties": {**PLAN_SCHEMA["properties"], "stack": stack}}

EPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tickets": {"type": "array", "items": _TICKET_SCHEMA},
        "criteria_review": _REVIEW_SCHEMA,
    },
    "required": ["tickets", "criteria_review"],
    "additionalProperties": False,
}


# ---- prompt assembly ---------------------------------------------------------


def _intake_text(state: FactoryState) -> str:
    """The raw request plus, when provided, the (capped) design-spec text."""
    request = str(state.get("request", ""))
    spec = str(state.get("spec", "") or "")
    if not spec.strip():
        return request
    return (
        f"REQUEST (intent summary):\n{request}\n\n"
        f"DESIGN SPEC (detailed source of truth):\n{spec[:_SPEC_PROMPT_CAP]}"
    )


def _forced_standard(state: FactoryState) -> bool:
    """A run given a design spec is asking for the system that spec describes:
    STANDARD without trusting the model's tier (only ever tightens)."""
    return bool(str(state.get("spec", "") or "").strip())


def _scope_text(state: FactoryState, *, revising: bool) -> str:
    decision = state.get("scope_tier")
    if decision is not None:
        # A revision exists to close a gate-blocking gap; its "reproduce every
        # criterion AND add" contradicts a criteria cap, so closing the gap wins.
        caps = "" if revising else (
            scope_directive(state) + scope_directive(state, architect=True)
        )
        return f"\n\nSCOPE IS FIXED: {decision.tier.value}. Echo it in scope.tier.{caps}"
    if _forced_standard(state):
        return "\n\nSCOPE IS FIXED: standard (a design spec was supplied)."
    return "\n\nIF you judge scope.tier = 'demo', these caps apply:" + demo_directives()


def _retrieve_spans(index: SymbolIndex | None, state: FactoryState) -> tuple[Span, ...]:
    """Existing-code spans via the context retriever (symbol index + one-hop
    call/def neighbours), seeded by any capabilities already on state. No index
    or no symbols is a legitimate greenfield view (empty)."""
    symbols = tuple(state.get("required_capabilities") or ())
    if index is None or not symbols:
        return ()
    return tuple(index.retrieve_or_need_context(symbols, _RETRIEVAL_K))


def latest_spec_failure(state: FactoryState) -> tuple[str, ...] | None:
    """Findings of the newest Spec-gate verdict when it FAILed, else None.

    Only the newest verdict counts: a later PASS supersedes an earlier FAIL."""
    specs = [g for g in state.get("gates") or () if g.name == _SPEC_GATE]
    if not specs:
        return None
    newest = max(specs, key=lambda g: g.seq)
    if newest.status is not GateStatus.FAIL:
        return None
    return tuple(newest.findings) or ("the Spec gate failed without findings",)


def _declared_criteria_text(state: FactoryState, rt: AgentRuntime) -> str:
    if rt.cas is None:
        return ""
    handles = sorted(
        (
            a
            for a in state.get("artifacts") or ()
            if getattr(a, "kind", None) == "acceptance_criterion"
        ),
        key=lambda a: a.seq,
    )
    current: dict[str, Any] = {}
    for a in handles:
        try:
            c = rt.cas.resolve(a)
        except Exception:
            continue
        current[str(getattr(c, "id", "?"))] = c
    # Criteria are never tombstoned: only the newest PRD's ids are still declared.
    prds = list(_newest_by_id(state, rt, "prd", PRD).values())
    if prds:
        live = set(max(prds, key=lambda p: p.seq).criterion_ids)
        if live:
            current = {cid: c for cid, c in current.items() if cid in live}
    lines: list[str] = []
    for c in current.values():
        ticket = getattr(c, "ticket_id", None)
        lines.append(
            f"- id={getattr(c, 'id', '?')}"
            + (f" (ticket={ticket})" if ticket else "")
            + f" | {getattr(c, 'text', '')} "
            f"(criticality={getattr(c, 'criticality', 'normal')}; "
            f"oracle_hint={getattr(c, 'oracle_hint', '')!r}; "
            f"security_abuse={getattr(c, 'security_abuse', False)})"
        )
    return "\n".join(lines)


def _revision_text(state: FactoryState, rt: AgentRuntime) -> str:
    """The re-entry block (spec_fix -> planner), or "" on the first pass.

    The Spec gate reads the newest record per criterion id, so the revision fixes
    a criterion a finding names by re-emitting it under the same id."""
    findings = latest_spec_failure(state)
    if findings is None:
        return ""
    prior = _declared_criteria_text(state, rt)
    goals = _goals_text(_newest_intent(state, rt))
    return (
        "\n\nREVISION PASS — the signed Spec gate FAILED your previous plan. Return "
        "the FULL plan again: reproduce every epic and every ticket (same ids) and "
        "every previously declared scenario (same id, on the same ticket, never "
        "weakened; fix in place any scenario a finding names) AND add scenarios that "
        "close every finding below.\n"
        "Spec gate findings:\n- " + "\n- ".join(findings) + "\n\n"
        f"Previously declared criteria:\n{prior or '(none resolved)'}"
        + (f"\n\nPreviously declared intent (keep these ids):{goals}" if goals else "")
    )


def _self_review_gaps(payload: dict[str, Any]) -> tuple[str, ...]:
    review = _section(payload, "criteria_review")
    return (
        *_strs(review.get("security_abuse_omissions")),
        *_strs(review.get("missing")),
    )


def _self_revision_text(gaps: tuple[str, ...], what: str = "plan") -> str:
    return (
        "\n\nSELF-REVIEW REVISION — your own criteria_review flagged these gaps. "
        f"Return the FULL {what} again with scenarios that close each one (same ids for "
        "tickets and scenarios you keep); list under criteria_review only gaps that genuinely "
        "remain.\nFlagged gaps:\n- " + "\n- ".join(gaps)
    )


def _stack_text(state: FactoryState, toolchains: Sequence[Toolchain]) -> str:
    pinned = state.get("stack")
    if pinned is not None:
        fixed = {k: v for k, v in pinned.model_dump(mode="json").items() if v and k != "notes"}
        return (
            "\n\nSTACK IS FIXED (pinned by the operator): " + as_text(fixed)
            + f"\nEcho language={pinned.language!r} in stack.language."
            + (f"\nGates: {pinned.notes}" if pinned.notes else "")
        )
    from foundry.gates.profiles import profile_examples

    langs = "\n".join(
        f"- {t.language} ({', '.join(t.binaries)}" + (f"; {t.version}" if t.version else "") + ")"
        for t in toolchains
    )
    return (
        "\n\nLANGUAGES AVAILABLE HERE (stack.language must be one of these):\n"
        f"{langs or '- python'}\n\n"
        "EXAMPLE GATE COMMANDS (hand-written profiles, for reference only; choose "
        f"your own commands):\n{profile_examples()}"
    )


def _user_content(
    state: FactoryState, rt: AgentRuntime, spans: tuple[Span, ...],
    toolchains: Sequence[Toolchain] = (),
) -> str:
    retrieved = [
        {"file": s.file, "lines": [s.line, s.end_line], "symbol": s.symbol} for s in spans
    ]
    body = _intake_text(state)
    if retrieved:
        body += "\n\nRETRIEVED CODE SPANS (existing code):\n" + as_text(retrieved)
    revision = _revision_text(state, rt)
    return (
        body + _stack_text(state, toolchains) + _scope_text(state, revising=bool(revision))
        + revision
    )


# ---- record builders -----------------------------------------------------------


# CLI backends don't grammar-enforce the schema, so one off-schema field must
# degrade to a default rather than raise and lose the whole plan.

_E = TypeVar("_E", bound=StrEnum)


def _str(raw: Any, default: str = "") -> str:
    return default if raw is None else str(raw)


def _strs(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x) for x in raw if x is not None)


def _enum(cls: type[_E], raw: Any, default: _E) -> _E:
    key = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    for member in cls:
        if key in (member.value.lower(), member.name.lower().replace("_", "-")):
            return member
    return default


def _int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(float(str(raw).strip().replace(",", "").replace("_", "")))
    except (ValueError, OverflowError):  # also NaN / inf
        return 0


def _bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1"}
    return bool(raw)


def _objs(raw: Any) -> list[dict[str, Any]]:
    # CLI backends don't grammar-enforce the schema; skip a non-object.
    return [item for item in raw or () if isinstance(item, dict)]


def _section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _steps(raw: Any) -> tuple[str, ...]:
    return tuple(x.strip() for x in _strs(raw) if x.strip())


def _uniq(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in items if x.strip()))


def _build_criterion(
    item: dict[str, Any], *, ticket_id: str, default_id: str, seq: Seq
) -> AcceptanceCriterion:
    """One scenario -> one criterion. Its oracle is the scenario itself; ``text``
    falls back to a free-text ``text`` only when no step was given."""
    cid = _str(item.get("id")).strip() or default_id
    given, when, then = (_steps(item.get(k)) for k in ("given", "when", "then"))
    return AcceptanceCriterion(
        id=cid,
        text=render_scenario(given, when, then) or _str(item.get("text")),
        criticality=_enum(Criticality, item.get("criticality"), Criticality.NORMAL),
        oracle_hint=scenario_ref(ticket_id, cid),
        security_abuse=_bool(item.get("security_abuse")),
        given=given,
        when=when,
        then=then,
        ticket_id=ticket_id,
        covers=_uniq(_strs(item.get("covers"))),
        seq=seq,
    )


def _break_cycles(deps: dict[str, list[str]]) -> list[str]:
    """Depth-first in ticket order; an edge back into the current path closes a
    cycle and is dropped. Mutates ``deps``; returns one concern per dropped edge."""
    concerns: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def visit(tid: str) -> None:
        on_path.add(tid)
        kept: list[str] = []
        for dep in deps[tid]:
            if dep in on_path:
                concerns.append(f"Dropped dependency {tid} -> {dep}: it closed a cycle.")
                continue
            if dep not in done:
                visit(dep)
            kept.append(dep)
        deps[tid] = kept
        on_path.discard(tid)
        done.add(tid)

    for tid in deps:
        if tid not in done:
            visit(tid)
    return concerns


def _safe_ticket_ids(ids: list[str]) -> dict[str, str]:
    """Raw id -> an id safe as a file name (``features/<id>.feature``, ``task/<id>``):
    other characters become ``-``; a collision gets the first free ``-<n>`` suffix,
    in plan order, so the mapping is deterministic."""
    out: dict[str, str] = {}
    safe = {
        tid for tid in ids
        if _SAFE_TICKET_ID.fullmatch(tid) and tid not in (".", "..", *_GENERATED_TICKETS)
    }
    taken = safe | _GENERATED_TICKETS
    for tid in ids:
        if tid in safe:
            out[tid] = tid
            continue
        base = re.sub(r"[^A-Za-z0-9._-]", "-", tid).strip(".") or "T"
        new, n = base, 2
        while new in taken:
            new, n = f"{base}-{n}", n + 1
        taken.add(new)
        out[tid] = new
    return out


# ---- epics -> tickets ------------------------------------------------------------

# Tags an epic call's ticket item with its epic for _link_epics; never emitted.
_EPIC_KEY = "__epic__"
_BRIEF_CAP = 6_000


@dataclass(frozen=True)
class _Epic:
    id: str
    title: str
    summary: str
    specialty_mix: tuple[str, ...]
    depends_on: tuple[str, ...]
    provides: tuple[str, ...]
    consumes: tuple[str, ...]


def _dropped_text(items: Iterable[tuple[str, dict[str, Any]]]) -> str:
    return ", ".join(
        f"{xid} ({title[:60]})" if (title := _str(item.get("title")).strip()) else xid
        for xid, item in items
    )


def _build_epics(
    raw: Any, interface_names: set[str], overflow: list[str] | None = None
) -> tuple[list[_Epic], list[str]]:
    """The plan's epics, ids made positional (``E1``..``En``) so ticket ids built
    on them stay file-safe and a re-plan can recover the epic from a ticket id.
    Over ``MAX_EPICS`` the tail is dropped (named in ``overflow``); unknown
    dependencies and contract names are dropped and a cycle loses its closing edge."""
    concerns: list[str] = []
    items = _objs(raw)
    if len(items) > MAX_EPICS:
        concerns.append(
            f"{len(items)} epics exceed the cap of {MAX_EPICS}; kept the first {MAX_EPICS}."
        )
        if overflow is not None:
            tail = items[MAX_EPICS:]
            overflow.append(
                f"{len(items)} epics exceed the cap of {MAX_EPICS}; dropped "
                + _dropped_text(
                    (_str(e.get("id")).strip() or f"epic #{MAX_EPICS + i + 1}", e)
                    for i, e in enumerate(tail)
                )
                + f": merge epics so the plan has at most {MAX_EPICS} and no work is lost."
            )
        items = items[:MAX_EPICS]
    ids = [f"E{i + 1}" for i in range(len(items))]
    alias: dict[str, str] = {}
    for eid, item in zip(ids, items, strict=True):
        alias.setdefault(_str(item.get("id")).strip() or eid, eid)
    deps: dict[str, list[str]] = {}
    contracts: dict[tuple[str, str], tuple[str, ...]] = {}
    for eid, item in zip(ids, items, strict=True):
        wanted = _uniq(alias.get(d.strip(), d.strip()) for d in _strs(item.get("depends_on")))
        unknown = [d for d in wanted if d not in ids]
        if unknown:
            concerns.append(f"Epic {eid}: dropped unknown dependencies {', '.join(unknown)}.")
        deps[eid] = [d for d in wanted if d in ids]
        for key in ("provides", "consumes"):
            named = _uniq(_strs(item.get(key)))
            bad = [x for x in named if x not in interface_names]
            if bad:
                concerns.append(f"Epic {eid}: dropped unknown {key} contracts {', '.join(bad)}.")
            contracts[eid, key] = tuple(x for x in named if x in interface_names)
    concerns += [
        c.replace("Dropped dependency", "Dropped epic dependency") for c in _break_cycles(deps)
    ]
    epics = [
        _Epic(
            id=eid,
            title=_str(item.get("title")),
            summary=_str(item.get("summary")),
            specialty_mix=_uniq(_strs(item.get("specialty_mix"))),
            depends_on=tuple(deps[eid]),
            provides=contracts[eid, "provides"],
            consumes=contracts[eid, "consumes"],
        )
        for eid, item in zip(ids, items, strict=True)
    ]
    return epics, concerns


def _ns(epic_id: str, raw_id: str) -> str:
    return raw_id if raw_id.startswith(f"{epic_id}-") else f"{epic_id}-{raw_id}"


def _epic_items(
    epic: _Epic, raw: Any, overflow: list[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """One epic call's tickets, capped at ``MAX_EPIC_TICKETS`` (the dropped tail
    named in ``overflow``), with ticket, dependency and scenario ids namespaced
    ``<epic>-<id>`` (missing ids become ``<epic>-T<nn>`` / ``<epic>-AC<nn>``) and
    each item tagged with its epic. A dependency on another epic's ticket cannot
    resolve and is dropped later: cross-epic order comes from :func:`_link_epics`."""
    concerns: list[str] = []
    items = _objs(raw)
    if len(items) > MAX_EPIC_TICKETS:
        concerns.append(
            f"Epic {epic.id}: {len(items)} tickets exceed the per-epic cap of "
            f"{MAX_EPIC_TICKETS}; kept the first {MAX_EPIC_TICKETS}."
        )
        if overflow is not None:
            overflow.append(
                f"Epic {epic.id}: {len(items)} tickets exceed the per-epic cap of "
                f"{MAX_EPIC_TICKETS}; dropped "
                + _dropped_text(
                    (_ns(epic.id, _str(t.get("id")).strip() or f"T{MAX_EPIC_TICKETS + i + 1:02d}"),
                     t)
                    for i, t in enumerate(items[MAX_EPIC_TICKETS:])
                )
                + ": consolidate this epic's tickets so no work is lost."
            )
        items = items[:MAX_EPIC_TICKETS]
    out: list[dict[str, Any]] = []
    n = 0
    for i, item in enumerate(items):
        scenarios: list[dict[str, Any]] = []
        for scenario in _objs(item.get("scenarios")):
            n += 1
            sid = _str(scenario.get("id")).strip() or f"AC{n:02d}"
            scenarios.append({**scenario, "id": _ns(epic.id, sid)})
        out.append({
            **item,
            "id": _ns(epic.id, _str(item.get("id")).strip() or f"T{i + 1:02d}"),
            "depends_on": [_ns(epic.id, d) for d in _steps(item.get("depends_on"))],
            "scenarios": scenarios,
            _EPIC_KEY: epic.id,
        })
    return out, concerns


def _link_epics(
    deps: dict[str, list[str]],
    items: dict[str, dict[str, Any]],
    epics: Sequence[_Epic],
    interface_names: set[str],
) -> None:
    """Order each epic's tickets after the epics it depends on. Its root tickets
    (no dependency inside the epic) gain deps on each upstream epic's tickets
    that implement an interface the epic consumes, else on that epic's sink
    tickets (none of its tickets depends on them). An upstream epic with no
    tickets is looked through to its own nearest upstream epics that have some,
    so the chain keeps its order. Mutates ``deps``."""
    members = {
        e.id: [tid for tid, item in items.items() if item.get(_EPIC_KEY) == e.id]
        for e in epics
    }
    by_id = {e.id: e for e in epics}

    def nearest(ups: Iterable[str], seen: set[str]) -> list[str]:
        out: list[str] = []
        for up in ups:
            if up in seen or up not in by_id:
                continue
            seen.add(up)
            out += [up] if members[up] else nearest(by_id[up].depends_on, seen)
        return out

    for epic in epics:
        mine = members[epic.id]
        needs = interface_names & {
            *epic.consumes, *(c for tid in mine for c in _strs(items[tid].get("consumes")))
        }
        upstream: list[str] = []
        for up in nearest(epic.depends_on, set()):
            theirs = members[up]
            providers = [
                tid for tid in theirs if needs & set(_strs(items[tid].get("implements")))
            ]
            if not providers:
                depended = {d for tid in theirs for d in deps[tid]}
                providers = [tid for tid in theirs if tid not in depended]
            upstream += providers
        if not upstream:
            continue
        own = set(mine)
        for tid in mine:
            if not own.intersection(deps[tid]):
                deps[tid] = list(_uniq((*deps[tid], *upstream)))


def _upstream_epics(epics: Sequence[_Epic], epic_id: str) -> set[str]:
    by_id = {e.id: e for e in epics}
    seen: set[str] = set()
    todo = list(by_id[epic_id].depends_on) if epic_id in by_id else []
    while todo:
        up = todo.pop()
        if up not in seen and up in by_id:
            seen.add(up)
            todo.extend(by_id[up].depends_on)
    return seen


def _entry_epic(epics: Sequence[_Epic]) -> str | None:
    """The epic that owns the entrypoint ticket: the last one every other epic is
    upstream of, else the last epic (the coverage check then names the gap)."""
    if not epics:
        return None
    others = {e.id for e in epics}
    sinks = [e.id for e in epics if _upstream_epics(epics, e.id) == others - {e.id}]
    return sinks[-1] if sinks else epics[-1].id


def _link_entrypoints(
    deps: dict[str, list[str]], items: dict[str, dict[str, Any]], epics: Sequence[_Epic]
) -> None:
    """An epic's entrypoint ticket cannot name other epics' tickets, so it is made
    to depend on every feature ticket of the epics upstream of its own; those never
    depend on it (cross-epic order only points upstream), so no cycle. Mutates
    ``deps``."""
    for tid, item in items.items():
        if not _bool(item.get("entrypoint")) or _is_test(item):
            continue
        upstream = _upstream_epics(epics, _str(item.get(_EPIC_KEY)))
        deps[tid] = list(_uniq((*deps[tid], *(
            other for other, it in items.items()
            if it.get(_EPIC_KEY) in upstream and not _is_test(it)
        ))))


def _is_test(item: dict[str, Any]) -> bool:
    return _enum(Specialty, item.get("specialty"), Specialty.BACKEND) is Specialty.TEST


def _tagged(epic_id: str, gaps: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"[{epic_id}] {g}" for g in gaps)


def _plan_brief(payload: dict[str, Any], intent: IntentBrief | None = None) -> str:
    """What every epic call needs of call 1's plan besides the epics and contracts."""
    prd, design = _section(payload, "prd"), _section(payload, "design")
    constraints = [f"- {_str(c.get('text'))}" for c in _objs(payload.get("pinned_constraints"))]
    stack = _section(payload, "stack")
    return (
        "PLAN (from the lead planner):\n"
        f"Stack: {_str(stack.get('language')) or '(unset)'}"
        + (f" / {_str(stack.get('framework'))}" if stack.get("framework") else "")
        + (f"; contracts file (written for you, never list it in files): "
           f"{_str(stack.get('contracts_file'))}" if stack.get("contracts_file") else "")
        + "\n"
        f"Product: {_str(prd.get('title'))}\n{_str(prd.get('body'))[:_BRIEF_CAP]}\n"
        f"Architecture: {_str(design.get('title'))}\n"
        f"{_str(design.get('body'))[:_BRIEF_CAP]}\n"
        f"Components: {', '.join(_strs(design.get('components'))) or '(none)'}"
        + ("\nPinned constraints (apply to every ticket):\n" + "\n".join(constraints)
           if constraints else "")
        + _goals_text(intent)
    )


def _interfaces_text(pairs: Iterable[tuple[str, str]]) -> str:
    return "\n".join(f"- {name}: {spec}" for name, spec in pairs) or "(none)"


def _epic_content(
    intake: str, brief: str, epics: Sequence[_Epic], epic: _Epic, interfaces: str,
    revision: str,
) -> str:
    entry = _entry_epic(epics)
    roster = "\n".join(
        f"- {e.id}: {e.title} — {e.summary}"
        + (f" (after {', '.join(e.depends_on)})" if e.depends_on else "")
        for e in epics
    )
    mine = {
        "id": epic.id, "title": epic.title, "summary": epic.summary,
        "specialty_mix": list(epic.specialty_mix), "depends_on": list(epic.depends_on),
        "provides": list(epic.provides), "consumes": list(epic.consumes),
    }
    return (
        f"{intake}\n\n{brief}\n\nEPICS (each planned in its own call):\n{roster}\n\n"
        f"YOUR EPIC: {epic.id}\n{as_text(mine)}\n"
        + ("YOUR EPIC OWNS THE ENTRYPOINT: mark exactly one feature ticket "
           "entrypoint=true that wires the whole app together end to end (main / server "
           "/ CLI / public API), depends_on every other feature ticket of your epic and "
           "carries an end-to-end scenario.\n\n" if epic.id == entry else
           "Your epic does NOT own the entrypoint: entrypoint=false on every ticket.\n\n")
        + 
        "INTERFACE CONTRACTS (shared by every epic; implements[] / consumes[] them "
        f"by name):\n{interfaces}" + revision
    )


_R = TypeVar("_R")


def _run_parallel(fn: Callable[[str], _R], args: Sequence[str]) -> list[_R]:
    """``fn`` over ``args`` on a thread pool, results in ``args`` order. The first
    failure (e.g. ``TokenCapExceeded``) cancels the calls not yet started and
    propagates once the running ones return."""
    if len(args) <= 1:
        return [fn(a) for a in args]
    with ThreadPoolExecutor(
        max_workers=min(len(args), _EPIC_WORKERS), thread_name_prefix="planner-epic"
    ) as pool:
        futures = [pool.submit(fn, a) for a in args]
        try:
            for done in as_completed(futures):
                done.result()
        except BaseException:
            for f in futures:
                f.cancel()
            raise
        return [f.result() for f in futures]


def _build_tickets(
    raw: Any,
    ticket_seq: Seq,
    criteria_seq: Seq,
    interface_names: set[str],
    *,
    cap: int = MAX_TICKETS,
    epics: Sequence[_Epic] = (),
    overflow: list[str] | None = None,
) -> tuple[list[Ticket], list[AcceptanceCriterion], list[str]]:
    """Tickets + their scenarios as criteria, made a valid DAG deterministically.

    Duplicate ticket / scenario ids: the later entry wins. Over ``cap`` the tail
    is dropped. Unknown dependencies and contract names are dropped and a cycle
    loses its closing edge. A ``test`` ticket covers its own scenarios plus those
    of the feature tickets it declares a dependency on. With ``epics`` (items tagged by
    :func:`_epic_items`), each epic's tickets are then ordered after the epics
    it depends on (:func:`_link_epics`), and each epic's entrypoint ticket after
    every feature ticket of the epics upstream of it. Tickets dropped at ``cap``
    are named in ``overflow``."""
    concerns: list[str] = []
    raw_items: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(_objs(raw)):
        tid = _str(item.get("id")).strip() or f"T-{i + 1:03d}"
        if tid in raw_items:
            concerns.append(f"Duplicate ticket id {tid}: the later entry wins.")
        raw_items[tid] = item
    if len(raw_items) > cap:
        concerns.append(
            f"{len(raw_items)} tickets exceed the cap of {cap}; kept the first {cap}."
        )
        if overflow is not None:
            overflow.append(
                f"{len(raw_items)} tickets exceed the cap of {cap}; dropped "
                + _dropped_text(list(raw_items.items())[cap:])
                + ": consolidate tickets"
                + ("" if epics else " (or split a larger build into epics)")
                + " so no work is lost."
            )
        raw_items = dict(list(raw_items.items())[:cap])
    alias = _safe_ticket_ids(list(raw_items))
    concerns += [
        f"Ticket id {old!r} renamed to {new!r} (ids name files)."
        for old, new in alias.items() if old != new
    ]
    items = {alias[tid]: item for tid, item in raw_items.items()}

    deps: dict[str, list[str]] = {}
    for tid, item in items.items():
        wanted = _uniq(alias.get(d, d) for d in _strs(item.get("depends_on")))
        unknown = [d for d in wanted if d not in items]
        if unknown:
            concerns.append(f"Ticket {tid}: dropped unknown dependencies {', '.join(unknown)}.")
        deps[tid] = [d for d in wanted if d in items]
    concerns += _break_cycles(deps)
    # Test coverage follows only the deps the plan declared: a cross-epic order
    # link makes no test ticket cover another epic's scenarios.
    declared = {tid: tuple(d) for tid, d in deps.items()}
    if epics:
        # After the cycle break, so every epic has roots to hang its implicit
        # deps on; they only point at upstream epics of a DAG, so add no cycle.
        _link_epics(deps, items, epics, interface_names)
        _link_entrypoints(deps, items, epics)

    criteria: dict[str, AcceptanceCriterion] = {}
    n = 0
    for tid, item in items.items():
        for scenario in _objs(item.get("scenarios")):
            n += 1
            c = _build_criterion(
                scenario, ticket_id=tid, default_id=f"AC-{n:03d}", seq=criteria_seq
            )
            if c.id in criteria:
                concerns.append(f"Duplicate scenario id {c.id}: the later entry wins.")
            criteria[c.id] = c
    ordered = [
        c.model_copy(update={"seq": criteria_seq._replace(local=i)})
        for i, c in enumerate(criteria.values())
    ]

    specialty = {
        tid: _enum(Specialty, item.get("specialty"), Specialty.BACKEND)
        for tid, item in items.items()
    }
    own = {tid: tuple(c.id for c in ordered if c.ticket_id == tid) for tid in items}
    files: dict[str, tuple[str, ...]] = {}
    for tid, item in items.items():
        planned = _uniq(_strs(item.get("files")))
        files[tid] = tuple(f for f in planned if not f.endswith(".feature"))
        if files[tid] != planned:
            concerns.append(
                f"Ticket {tid}: dropped feature file(s) "
                f"{', '.join(f for f in planned if f.endswith('.feature'))} from its files: "
                f"the gate writes {FEATURE_DIR}/<ticket>.feature from the scenarios."
            )
    tickets: list[Ticket] = []
    for i, (tid, item) in enumerate(items.items()):
        scenario_ids = own[tid]
        if specialty[tid] is Specialty.TEST:
            scenario_ids = _uniq(
                (*scenario_ids, *(c for d in declared[tid] if specialty[d] is not Specialty.TEST
                                  for c in own[d]))
            )
        contracts: dict[str, tuple[str, ...]] = {}
        for key in ("implements", "consumes"):
            named = _uniq(_strs(item.get(key)))
            unknown = [x for x in named if x not in interface_names]
            if unknown:
                concerns.append(
                    f"Ticket {tid}: dropped unknown {key} contracts {', '.join(unknown)}."
                )
            contracts[key] = tuple(x for x in named if x in interface_names)
        tickets.append(
            Ticket(
                id=tid,
                title=_str(item.get("title")),
                description=_str(item.get("description")),
                specialty=specialty[tid],
                difficulty=_enum(Difficulty, item.get("difficulty"), Difficulty.STANDARD),
                depends_on=tuple(deps[tid]),
                files=files[tid],
                reads=_uniq(_strs(item.get("reads"))),
                scenario_ids=scenario_ids,
                implements=contracts["implements"],
                consumes=contracts["consumes"],
                entrypoint=_bool(item.get("entrypoint")),
                seq=ticket_seq._replace(local=i),
            )
        )

    tested = {d for t in tickets if t.specialty is Specialty.TEST for d in declared[t.id]}
    concerns += [
        f"Ticket {t.id} has scenarios but no test ticket depends on it."
        for t in tickets
        if t.specialty is not Specialty.TEST and own[t.id] and t.id not in tested
    ]
    return tickets, ordered, concerns


def _build_adrs(raw: Any, seq: Seq, *, store: ArtifactStore | None) -> list[ADR]:
    return [
        ADR(
            id=_str(item.get("id"), f"ADR-{i:03d}"),
            title=_str(item.get("title")),
            decision=_str(item.get("decision")),
            body_uri=persist_prose(_str(item.get("body")), store=store),
            rejected_alternatives=_strs(item.get("rejected_alternatives")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def _contract_kind(item: dict[str, Any]) -> str:
    kind = _str(item.get("kind")).strip().lower()
    return kind if kind in CONTRACT_KINDS else ""


def _contract_text(item: dict[str, Any]) -> str:
    """A contract's body: kind + signature, description, inputs/outputs/errors.
    A contract given only as free-text ``spec`` keeps that text unchanged."""
    signature = _str(item.get("signature")).strip()
    kind = _contract_kind(item)
    lines = [f"[{kind}] {signature}" if kind else signature] if signature else []
    description = _str(item.get("description")).strip() or _str(item.get("spec"))
    if description:
        lines.append(description)
    for key in ("inputs", "outputs", "errors"):
        if values := _steps(item.get(key)):
            lines.append(f"{key}: " + "; ".join(values))
    return "\n".join(lines)


def _build_interfaces(
    raw: Any, seq: Seq, *, store: ArtifactStore | None = None
) -> list[InterfaceContract]:
    return [
        InterfaceContract(
            name=_str(item.get("name"), f"iface-{i}"),
            # Same handle as cas_uri(body); persisted so a targeted re-plan can
            # hand the contract text to the epics it re-runs.
            spec_uri=persist_prose(_contract_text(item), store=store),
            kind=_contract_kind(item),
            signature=_str(item.get("signature")).strip(),
            consumers=_strs(item.get("consumers")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


# ---- stack, generated tickets, contract fit ----------------------------------


def _stack_decision(
    payload: dict[str, Any], languages: Iterable[str]
) -> tuple[StackDecision | None, list[str]]:
    """The plan's stack, or None (the gates then infer a profile from the request)
    when it names no language or one this host lacks."""
    raw = _section(payload, "stack")
    language = _str(raw.get("language")).strip().lower()
    available = sorted(set(languages))
    if language not in available:
        return None, [
            f"Stack language {language or '(none)'!r} is not available here "
            f"({', '.join(available)}); the gates infer a profile from the request."
        ]
    stack = StackDecision(
        language=language,
        **{k: _str(raw.get(k)).strip() for k in (
            "framework", "package_manager", "install", "build", "typecheck", "test",
            "bdd", "run", "notes",
        )},
    )
    if language != "python" and not stack.test and not stack.bdd:
        # The pytest harness cannot test another language: without a command of its
        # own the stack has no test gate at all.
        return None, [
            f"The {language} stack declares neither a test nor a bdd command; it was "
            "dropped and the gates infer a profile from the request."
        ]
    concerns: list[str] = []
    if language != "python" and not stack.bdd:
        concerns.append(
            f"The {language} stack has no bdd command: its scenarios are not executed."
        )
    if language != "python" and not stack.test:
        concerns.append(f"The {language} stack has no test command.")
    return stack, concerns


def _plan_paths(raw: Any, what: str, concerns: list[str]) -> tuple[str, ...]:
    """Repo-relative file paths from the plan; gate inputs, directories, absolute
    paths and ``..`` are dropped with a concern."""
    kept: list[str] = []
    for path in _uniq(_strs(raw)):
        parts = path.split("/")
        if (path.startswith("/") or path.endswith("/") or ".." in parts
                or path == "criteria.json" or parts[0] == FEATURE_DIR):
            concerns.append(f"Dropped {what} path {path!r}: not a project file.")
        else:
            kept.append(path)
    return tuple(kept)


# How to write contract stubs a strict type checker accepts with no implementation.
_CONTRACT_STUB_RULES: dict[str, str] = {
    "python": (
        "declare services as typing.Protocol classes (mypy accepts a `...` body "
        "there, but reports a plain class or function with a `...` body as a "
        "missing return) or as ABCs with @abstractmethod, and data as dataclasses; "
        "never name a method after a builtin used in annotations (`list`, `type`, "
        "`id`, `dict`, ...) — inside the class body it shadows the builtin, so "
        "`-> list[X]` breaks — or annotate with `builtins.list`."
    ),
    "typescript": (
        "use only `interface` and `type` declarations (plus `enum`/`const` values "
        "the contracts name), never a function or class body."
    ),
    "": "declarations only — interfaces, types and signatures with no bodies the "
        "compiler would reject.",
}


def _generated_tickets(
    interfaces: Sequence[InterfaceContract],
    raw_stack: dict[str, Any],
    stack: StackDecision | None,
    seq: Seq,
) -> tuple[list[Ticket], list[str]]:
    """The planner's own tickets: ``contracts`` writes every code-kind contract as
    real code when there are any; ``bdd-harness`` wires the stack's Cucumber runner
    when it has a ``bdd`` command. Both have no dependencies (wave 0), so the first
    BUILD check already has the manifest, the config and the contracts to compile."""
    concerns: list[str] = []
    language = stack.language if stack else _str(raw_stack.get("language")).strip().lower()
    out: list[Ticket] = []
    code = [i for i in interfaces if i.kind in CODE_CONTRACT_KINDS]
    owned: tuple[str, ...] = ()
    if code:
        owned = _plan_paths(raw_stack.get("contracts_file"), "contracts", concerns)
        if not owned and language in _DEFAULT_CONTRACTS_FILE:
            owned = (_DEFAULT_CONTRACTS_FILE[language],)
        if not owned:
            concerns.append(
                f"{len(code)} code contract(s) but no contracts file for language "
                f"{language or '(none)'!r}: they are not written as code."
            )
        else:
            out.append(Ticket(
                id=CONTRACTS_TICKET_ID,
                title="Define contracts",
                description=(
                    f"Write every interface contract this ticket implements as real "
                    f"{language or 'project-language'} code in {', '.join(owned)}: the "
                    "exact signatures, types, classes and interfaces (TypeScript "
                    "interfaces/types, Python Protocols and dataclasses, Go interfaces, "
                    "...) with no behaviour beyond what the language needs to compile: "
                    "declarations only, no logic, helpers, I/O or validation. Every "
                    "other ticket imports these definitions and none may change or "
                    "redeclare them, so match each contract exactly. The file must pass "
                    "the stack's build and typecheck on its own: "
                    + _CONTRACT_STUB_RULES.get(language, _CONTRACT_STUB_RULES[""])
                ),
                specialty=Specialty.BACKEND,
                difficulty=Difficulty.TRIVIAL,
                files=owned,
                implements=tuple(i.name for i in code),
                seq=seq,
            ))
    if stack is not None and stack.bdd:
        files = _plan_paths(raw_stack.get("harness_files"), "harness", concerns) or (
            _DEFAULT_HARNESS_FILES.get(language, ())
        )
        commands = "; ".join(
            f"{k} `{v}`" for k, v in (
                ("install", stack.install), ("build", stack.build),
                ("typecheck", stack.typecheck), ("test", stack.test),
            ) if v
        )
        out.append(Ticket(
            id=HARNESS_TICKET_ID,
            title="Wire the BDD runner",
            description=(
                f"Add the official Cucumber runner for {language} as a dev dependency "
                "in the package manifest and write its config so the BDD command "
                f"`{stack.bdd}` runs every {FEATURE_DIR}/*.feature file and writes "
                f"Cucumber Messages (NDJSON) to the file named by ${BDD_REPORT_ENV}. "
                "Set up the project skeleton so these commands work"
                + (f": {commands}." if commands else ".")
                + " Write no step definitions: the test tickets do."
            ),
            specialty=Specialty.TEST,
            difficulty=Difficulty.STANDARD,
            files=tuple(f for f in files if f not in owned),
            seq=seq,
        ))
    return out, concerns


def _attach_generated(
    tickets: Sequence[Ticket], generated: Sequence[Ticket], seq: Seq
) -> tuple[list[Ticket], list[str]]:
    """Put the generated tickets first, make every other ticket depend on them, and
    take the contract files out of every other ticket's write scope (they are
    gate-owned once the contracts ticket is built)."""
    concerns: list[str] = []
    gen_ids = tuple(t.id for t in generated)
    owned = {f for t in generated if t.id == CONTRACTS_TICKET_ID for f in t.files}
    ordered = list(generated)
    for t in tickets:
        files = tuple(f for f in t.files if f not in owned)
        if files != t.files:
            concerns.append(
                f"Ticket {t.id}: dropped the gate-owned contract file(s) "
                f"{', '.join(sorted(set(t.files) - set(files)))} from its files."
            )
        ordered.append(t.model_copy(update={
            "files": files, "depends_on": _uniq((*gen_ids, *t.depends_on)),
        }))
    return [
        t.model_copy(update={"seq": seq._replace(local=i)}) for i, t in enumerate(ordered)
    ], concerns


# Script files a stack command can name as its own repo file (`python3 smoke.py`).
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".sh", ".rb", ".lua",
                    ".pl", ".php")


def stack_files(stack: StackDecision) -> tuple[str, ...]:
    """Repo-relative script files the stack's run / test / bdd commands execute."""
    out: list[str] = []
    for cmd in (stack.run, stack.test, stack.bdd):
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        for tok in tokens:
            path = tok.removeprefix("./")
            if (path.endswith(_SCRIPT_SUFFIXES) and not path.startswith(("-", "/"))
                    and not any(ch in path for ch in "$=*:")
                    and ".." not in path.split("/") and path not in out):
                out.append(path)
    return tuple(out)


def _own_stack_files(
    tickets: Sequence[Ticket], stack: StackDecision | None
) -> tuple[list[Ticket], list[str], tuple[str, ...]]:
    """Give every script a stack command runs (``smoke.py`` for ``python3 smoke.py``)
    an owner: one no ticket writes is added to the entrypoint ticket, which wires the
    app end to end — else the run command fails on a file nothing builds. With no
    entrypoint ticket it is a coverage finding (a re-plan). Returns (tickets,
    concerns, findings)."""
    if stack is None:
        return list(tickets), [], ()
    tickets, init_concerns = _own_package_inits(tickets, stack)

    def owned(path: str) -> bool:
        return any(f == path or (f.endswith("/") and path.startswith(f))
                   for t in tickets for f in t.files)

    orphans = [f for f in stack_files(stack) if not owned(f)]
    if not orphans:
        return list(tickets), init_concerns, ()
    entry = next((t for t in tickets if t.entrypoint and t.id not in _GENERATED_TICKETS), None)
    if entry is None:
        return list(tickets), init_concerns, tuple(
            f"Stack command file {f} is written by no ticket: put it in the files[] of "
            "the ticket that wires the app (the entrypoint ticket)." for f in orphans
        )
    note = (" Also write " + ", ".join(orphans) + ": the stack's commands run "
            + ("it" if len(orphans) == 1 else "them")
            + (f" (run: `{stack.run}`), which must boot the real app headless and exit 0"
               if stack.run else "") + ".")
    updated = entry.model_copy(update={
        "files": (*entry.files, *orphans), "description": entry.description + note,
    })
    return ([updated if t.id == entry.id else t for t in tickets],
            [*init_concerns,
             f"Ticket {entry.id}: added stack command file(s) {', '.join(orphans)} that no "
             "ticket wrote."], ())


def _own_package_inits(
    tickets: Sequence[Ticket], stack: StackDecision
) -> tuple[list[Ticket], list[str]]:
    """Give every Python source directory's ``__init__.py`` an owner. Without one
    mypy maps ``finance/contracts.py`` to both ``contracts`` and ``finance.contracts``
    and fails, and no ticket may write it outside its files. The contracts ticket
    (wave 0) takes them, else the first ticket writing into that directory. Test
    directories are left to pytest's own rootdir handling."""
    if stack.language != "python":
        return list(tickets), []
    owned = {f for t in tickets for f in t.files}
    dirs: dict[str, str] = {}  # missing __init__.py -> first ticket writing below it
    for t in tickets:
        for f in t.files:
            if not f.endswith(".py"):
                continue
            parts = f.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                if any(p.startswith("test") for p in parts[:i]):
                    break
                init = "/".join((*parts[:i], "__init__.py"))
                if init not in owned:
                    dirs.setdefault(init, t.id)
    if not dirs:
        return list(tickets), []
    holder = CONTRACTS_TICKET_ID if any(t.id == CONTRACTS_TICKET_ID for t in tickets) else None
    added: dict[str, list[str]] = {}
    for init, first in dirs.items():
        added.setdefault(holder or first, []).append(init)
    out = [
        t.model_copy(update={
            "files": (*t.files, *added[t.id]),
            "description": t.description + " Also create the package marker(s) "
            + ", ".join(added[t.id]) + " (empty unless this ticket needs exports there).",
        }) if t.id in added else t
        for t in tickets
    ]
    return out, [f"Ticket {tid}: added package marker(s) {', '.join(inits)} that no "
                 "ticket wrote." for tid, inits in added.items()]


def _contract_findings(
    tickets: Sequence[Ticket], criteria: Sequence[AcceptanceCriterion],
    declared: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """The plan-time contract fit check over the model's tickets: every consumed
    contract has exactly one implementing ticket and the consumer depends on it;
    every implemented contract has a consumer or a scenario on its implementer.
    ``declared`` names type contracts the generated contracts ticket writes in full
    (data types, error classes): consuming one needs no implementing ticket."""
    work = [t for t in tickets if t.id not in _GENERATED_TICKETS]
    deps = {t.id: t.depends_on for t in tickets}
    producers: dict[str, list[str]] = {}
    for t in work:
        for name in t.implements:
            producers.setdefault(name, []).append(t.id)

    def ancestors(tid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(deps.get(tid, ()))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        return seen

    out = [
        f"Contract {name} is implemented by {len(ids)} tickets ({', '.join(ids)}); "
        "exactly one ticket may implement it."
        for name, ids in producers.items() if len(ids) > 1
    ]
    consumed: set[str] = set()
    for t in work:
        for name in t.consumes:
            if name in t.implements:
                continue
            consumed.add(name)
            ids = producers.get(name, [])
            if not ids and name in declared:
                continue
            if not ids:
                out.append(f"Ticket {t.id} consumes contract {name}, but no ticket implements it.")
            elif len(ids) == 1 and ids[0] not in ancestors(t.id):
                out.append(
                    f"Ticket {t.id} consumes contract {name} but does not depend on "
                    f"{ids[0]}, which implements it."
                )
    with_scenarios = {c.ticket_id for c in criteria}
    out += [
        f"Contract {name} (implemented by {', '.join(ids)}) has no consumer and no "
        "scenario on its implementing ticket."
        for name, ids in producers.items()
        if name not in consumed and not with_scenarios.intersection(ids)
    ]
    return tuple(out)


# Builtin types a method name can shadow inside its class body, breaking every
# annotation there that uses the builtin (`def list(self) -> list[Book]`).
_BUILTIN_TYPES = frozenset(
    n for n in dir(builtins) if isinstance(getattr(builtins, n), type) and n.islower()
)
_PY_METHOD = re.compile(r"\bdef\s+([A-Za-z_]\w*)\s*\(")


def _builtin_method_findings(
    interfaces: Sequence[InterfaceContract], language: str
) -> tuple[str, ...]:
    """Python contract signatures that name a method after a builtin type the same
    signature also uses in an annotation: mypy then resolves the annotation to the
    method and rejects it. Re-planning renames the method (list_books, get_by_id)."""
    if language != "python":
        return ()
    out: list[str] = []
    for i in interfaces:
        if i.kind not in CODE_CONTRACT_KINDS:
            continue
        clashes = sorted({
            name for name in _PY_METHOD.findall(i.signature)
            if name in _BUILTIN_TYPES
            and re.search(rf"(?:->|:)\s*(?:[\w.]+\[)*{name}\b", i.signature)
        })
        if clashes:
            out.append(
                f"Contract {i.name} names method(s) {', '.join(clashes)} after a builtin "
                "its own annotations use, which the type checker rejects; rename them "
                "(e.g. list_books, get_by_id)."
            )
    return tuple(out)


# ---- coverage: intent -> scenarios -> tickets -> one entrypoint --------------

_GOAL_ID = re.compile(r"(MH|SC)[-_ ]?0*(\d+)", re.IGNORECASE)


def _goals(raw: Any, prefix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(ids, texts)`` of the must-haves (``MH``) or success criteria (``SC``).

    An item is ``{id, text}`` or a bare string. A declared ``<prefix>-<n>`` id is
    normalised (``mh1`` -> ``MH-1``) and kept when unused; otherwise the item gets
    the first free positional ``<prefix>-<n>``, so ids are deterministic and the
    two lists never share one."""
    entries = raw if isinstance(raw, (list, tuple)) else _strs(raw)
    ids: list[str] = []
    texts: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            text = _str(item.get("text") or item.get("description")).strip()
            wanted = _str(item.get("id")).strip()
        else:
            text, wanted = ("" if item is None else str(item).strip()), ""
        if not text:
            continue
        m = _GOAL_ID.fullmatch(wanted)
        gid = f"{prefix}-{int(m.group(2))}" if m and m.group(1).upper() == prefix else ""
        if not gid or gid in ids:
            n = len(ids) + 1
            while f"{prefix}-{n}" in ids:
                n += 1
            gid = f"{prefix}-{n}"
        ids.append(gid)
        texts.append(text)
    return tuple(ids), tuple(texts)


def _goal_of(raw: str, known: dict[str, str]) -> str | None:
    """The goal id a scenario's ``covers`` entry names, or None."""
    key = raw.strip()
    m = _GOAL_ID.fullmatch(key)
    if m:
        key = f"{m.group(1).upper()}-{int(m.group(2))}"
    return known.get(key.lower())


def _goal_rows(brief: IntentBrief | None) -> list[tuple[str, str, str]]:
    """``(kind, id, text)`` of every must-have and success criterion."""
    if brief is None:
        return []
    return [
        *(("Must-have", g, t) for g, t in zip(brief.must_have_ids, brief.must_haves,
                                               strict=False)),
        *(("Success criterion", g, t) for g, t in zip(
            brief.success_criterion_ids, brief.success_criteria, strict=False)),
    ]


def _goals_text(brief: IntentBrief | None) -> str:
    if brief is None:
        return ""
    lines = [f"- {gid} ({kind.lower()}): {text}" for kind, gid, text in _goal_rows(brief)]
    return (
        f"\nDeliverable: {brief.deliverable or 'app'}"
        + ("\nMUST-HAVES AND SUCCESS CRITERIA (cite these ids in scenario covers[]; each "
           "needs at least one covering scenario across the plan):\n" + "\n".join(lines)
           if lines else "")
    )


def _coverage_findings(
    tickets: Sequence[Ticket],
    criteria: Sequence[AcceptanceCriterion],
    brief: IntentBrief | None,
    *,
    run: str | None,
) -> tuple[str, ...]:
    """The plan-time coverage check: every must-have and success criterion is
    covered by a scenario on a live ticket; every feature ticket has scenarios; a
    feature ticket marked ``entrypoint`` depends (transitively) on every other
    feature ticket; and an app-like deliverable has a ``run`` command (``run``
    None: not checked, e.g. an operator-pinned profile)."""
    goals = _goal_rows(brief)
    known = {g.lower(): g for _, g, _ in goals}
    live = {t.id for t in tickets}
    out: list[str] = []
    covered: set[str] = set()
    owned: set[str] = set()
    for c in criteria:
        if c.ticket_id not in live:
            out.append(
                f"Scenario {c.id} is not owned by a live ticket"
                + (f" ({c.ticket_id} is not in the plan)" if c.ticket_id else "")
                + "; put it on a ticket of the plan."
            )
            continue
        owned.add(c.ticket_id)
        for raw in c.covers:
            gid = _goal_of(raw, known)
            if gid is None:
                out.append(
                    f"Scenario {c.id} covers unknown id {raw!r}; cite must-have / "
                    "success-criterion ids ("
                    + (", ".join(g for _, g, _ in goals) or "none declared") + ")."
                )
            else:
                covered.add(gid)
    out += [
        f"{kind} {gid} ({text[:80]!r}) has no covering scenario; add one whose covers "
        f"lists {gid}."
        for kind, gid, text in goals if gid not in covered
    ]
    features = [
        t for t in tickets
        if t.id not in _GENERATED_TICKETS and t.specialty is not Specialty.TEST
    ]
    out += [
        f"Feature ticket {t.id} has no scenarios; give it at least one (or fold it "
        "into the ticket whose behaviour it serves)."
        for t in features if t.id not in owned
    ]
    entries = [t for t in features if t.entrypoint]
    if features and not entries:
        out.append(
            "No entrypoint ticket: mark one feature ticket entrypoint=true that wires the "
            "app together end to end and depends_on every other feature ticket."
        )
    deps = {t.id: t.depends_on for t in tickets}
    gaps: dict[str, list[str]] = {}
    for e in entries:
        seen: set[str] = set()
        todo = list(deps[e.id])
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        gaps[e.id] = [t.id for t in features if t.id != e.id and t.id not in seen]
    if gaps and all(gaps.values()):
        out += [
            f"Entrypoint ticket {tid} does not depend (directly or transitively) on "
            f"feature ticket(s) {', '.join(missing)}; it must wire every feature in."
            for tid, missing in gaps.items()
        ]
    deliverable = brief.deliverable if brief is not None else ""
    if run is not None and not run.strip() and deliverable != "library":
        out.append(
            "stack.run is empty but the deliverable is "
            f"{deliverable or 'an app (undeclared)'}: give a run command that boots the "
            "real app headless and exits 0, or declare deliverable 'library' if nothing runs."
        )
    return tuple(out)


def _build_constraints(raw: Any, seq: Seq) -> list[PinnedConstraint]:
    return [
        PinnedConstraint(
            id=_str(item.get("id"), f"PC-{i:03d}"),
            text=_str(item.get("text")),
            # Unknown scope is injected as global: the fail-safe direction.
            scope=_enum(ConstraintScope, item.get("scope"), ConstraintScope.GLOBAL),
            source=_str(item.get("source"), NODE_ID),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def ticket_owner(ticket: Ticket) -> str:
    """Owner name for a ticket's files: the specialty leads it, so area
    classification (whole-token) lands frontend/data tickets in their lane."""
    return f"{ticket.specialty.value}-{ticket.id}"


def _ownership_from_tickets(tickets: list[Ticket], seq: Seq) -> OwnershipMap:
    """Compatibility view for the ownership-driven scheduler: every ticket file
    is a unit. A file shared by tickets goes to the later ticket and carries the
    union of their scenario ids."""
    owners: dict[str, str] = {}
    unit_criteria: dict[str, tuple[str, ...]] = {}
    for t in tickets:
        for path in t.files:
            owners[path] = ticket_owner(t)
            merged = _uniq((*unit_criteria.get(path, ()), *t.scenario_ids))
            if merged:
                unit_criteria[path] = merged
    return OwnershipMap(owners=owners, unit_criteria=unit_criteria, seq=seq)


def _retired_constraints(
    state: FactoryState, emitted: list[PinnedConstraint], seq: Seq
) -> list[PinnedConstraint]:
    """Tombstones for this node's prior live constraints a re-plan dropped.

    A record whose ``supersedes`` names its own id retires that id in
    ``live_constraints``; the newer Seq makes it the id's latest record."""
    kept = {c.id for c in emitted}
    stale = sorted(
        (
            c
            for c in live_constraints(list(state.get("pinned_constraints") or ()))
            if c.seq.node_id == NODE_ID and c.id not in kept
        ),
        key=lambda c: c.id,
    )
    return [
        c.model_copy(
            update={"supersedes": c.id, "seq": seq._replace(local=len(emitted) + i)}
        )
        for i, c in enumerate(stale)
    ]


_M = TypeVar("_M")


def _newest_by_id(
    state: FactoryState, rt: AgentRuntime, kind: str, cls: type[_M]
) -> dict[str, _M]:
    """This node's newest record per id (``name`` for contracts) of ``kind``, in
    Seq order of each id's first record; empty without a store."""
    if rt.cas is None:
        return {}
    handles = sorted(
        (a for a in state.get("artifacts") or () if a.kind == kind and a.seq.node_id == NODE_ID),
        key=lambda a: a.seq,
    )
    newest: dict[str, _M] = {}
    for a in handles:
        try:
            body = rt.cas.resolve(a)
        except Exception:
            continue
        if isinstance(body, cls):
            newest[str(getattr(body, "id", None) or getattr(body, "name", ""))] = body
    return newest


def _prose(rt: AgentRuntime, uri: str) -> str:
    try:
        return str(getattr(rt.cas.get_body(uri), "text", "")) if rt.cas else ""
    except Exception:
        return ""


@dataclass(frozen=True)
class _PriorPlan:
    """An earlier epic-mode pass, recovered from this node's artifacts so a
    Spec-gate re-plan can re-run only the epics its findings name."""

    epics: tuple[_Epic, ...]
    epic_of: dict[str, str]  # live ticket id -> epic id
    tickets: tuple[Ticket, ...]  # live, Seq order (the planner's own tickets aside)
    generated: tuple[Ticket, ...]  # the planner's own tickets, carried as they were
    criteria: dict[str, AcceptanceCriterion]  # newest per id, live tickets only
    prd: PRD
    critique: CriteriaCritique | None
    interfaces: tuple[tuple[str, str], ...]  # (name, spec)
    declared: frozenset[str]  # type-kind contracts the contracts ticket writes in full
    brief: str
    intent: IntentBrief | None


def _latest_epics(state: FactoryState, rt: AgentRuntime) -> list[_Epic]:
    """The epics of this node's newest call-1 pass (one Seq but for ``local``), so
    an epic an earlier pass had and a later one dropped never comes back."""
    if rt.cas is None:
        return []
    handles = sorted(
        (a for a in state.get("artifacts") or ()
         if a.kind == EPIC_KIND and a.seq.node_id == NODE_ID),
        key=lambda a: a.seq,
    )
    if not handles:
        return []
    newest = handles[-1].seq._replace(local=0)
    out: list[_Epic] = []
    for a in handles:
        if a.seq._replace(local=0) != newest:
            continue
        try:
            body = rt.cas.resolve(a)
        except Exception:
            continue
        if isinstance(body, Epic):
            out.append(_Epic(
                id=body.id, title=body.title, summary=body.summary,
                specialty_mix=body.specialty_mix, depends_on=body.depends_on,
                provides=body.provides, consumes=body.consumes,
            ))
    return out


def _epics_from_tickets(live: Sequence[Ticket], epic_of: dict[str, str]) -> list[_Epic]:
    """Epics rebuilt from ticket-id prefixes, for a pass that predates the
    ``epic`` records: titles and declared contracts are approximations."""
    order = sorted(set(epic_of.values()), key=lambda e: int(e[1:]))
    epics: list[_Epic] = []
    for eid in order:
        mine = [t for t in live if epic_of[t.id] == eid]
        upstream = {epic_of[d] for t in mine for d in t.depends_on if epic_of.get(d, eid) != eid}
        epics.append(_Epic(
            id=eid,
            title=eid,
            summary="; ".join(t.title for t in mine)[:_BRIEF_CAP],
            specialty_mix=_uniq(t.specialty.value for t in mine),
            depends_on=tuple(e for e in order if e in upstream),
            provides=_uniq(x for t in mine for x in t.implements),
            consumes=_uniq(x for t in mine for x in t.consumes),
        ))
    return epics


def _newest_intent(state: FactoryState, rt: AgentRuntime) -> IntentBrief | None:
    briefs = list(_newest_by_id(state, rt, "intent_brief", IntentBrief).values())
    return max(briefs, key=lambda b: b.seq) if briefs else None


def _prior_plan(state: FactoryState, rt: AgentRuntime) -> _PriorPlan | None:
    """None unless every live planner ticket carries the prefix of a known epic
    (an inline plan has no epics to target) and the PRD resolves."""
    every = sorted(
        (t for t in _newest_by_id(state, rt, TICKET_KIND, Ticket).values()
         if t.supersedes != t.id),
        key=lambda t: t.seq,
    )
    live = [t for t in every if t.id not in _GENERATED_TICKETS]
    prds = list(_newest_by_id(state, rt, "prd", PRD).values())
    if not live or not prds:
        return None
    epic_of: dict[str, str] = {}
    for t in live:
        m = _EPIC_OF_TICKET.match(t.id)
        if m is None:
            return None
        epic_of[t.id] = m.group(1)
    epics = _latest_epics(state, rt) or _epics_from_tickets(live, epic_of)
    if not set(epic_of.values()) <= {e.id for e in epics}:
        return None
    prd = max(prds, key=lambda p: p.seq)
    # Criteria are never tombstoned: only the current PRD's ids are live, so a
    # scenario a later pass dropped stays dropped.
    current = set(prd.criterion_ids)
    criteria = {
        cid: c for cid, c in
        _newest_by_id(state, rt, "acceptance_criterion", AcceptanceCriterion).items()
        if c.ticket_id in epic_of and cid in current
    }
    critiques = list(_newest_by_id(state, rt, "criteria_findings", CriteriaCritique).values())
    designs = list(_newest_by_id(state, rt, "system_design", SystemDesign).values())
    design = max(designs, key=lambda d: d.seq) if designs else None
    contracts = _newest_by_id(state, rt, "interface_contract", InterfaceContract)
    interfaces = tuple((name, _prose(rt, i.spec_uri)) for name, i in contracts.items())
    declared = frozenset(name for name, i in contracts.items() if i.kind == "type")
    intent = _newest_intent(state, rt)
    brief = (
        "PLAN (from the previous pass):\n"
        f"Product: {prd.title}\n{_prose(rt, prd.prd_uri)[:_BRIEF_CAP]}"
        + (f"\nArchitecture: {design.title}\n{_prose(rt, design.design_uri)[:_BRIEF_CAP]}\n"
           f"Components: {', '.join(design.components) or '(none)'}" if design else "")
        + _goals_text(intent)
    )
    return _PriorPlan(
        epics=tuple(epics), epic_of=epic_of, tickets=tuple(live),
        generated=tuple(t for t in every if t.id in _GENERATED_TICKETS), criteria=criteria,
        prd=prd, critique=max(critiques, key=lambda c: c.seq) if critiques else None,
        interfaces=interfaces, declared=declared, brief=brief, intent=intent,
    )


def _target_epics(findings: tuple[str, ...], prior: _PriorPlan) -> tuple[str, ...] | None:
    """The epics a Spec-gate failure names by epic, ticket or criterion id, or
    None when any finding names none of them: a plan-level issue re-plans all."""
    ids = {e.id for e in prior.epics}
    hit: set[str] = set()
    for finding in findings:
        tokens = {tok.strip(".") for tok in _SAFE_TICKET_ID.findall(finding)}
        named = {t for t in tokens if t in ids}
        named |= {prior.epic_of[t] for t in tokens if t in prior.epic_of}
        named |= {
            prior.epic_of[c.ticket_id] for cid, c in prior.criteria.items()
            if cid in tokens and c.ticket_id in prior.epic_of
        }
        if not named:
            return None
        hit |= named
    return tuple(e.id for e in prior.epics if e.id in hit)


def _prior_items(prior: _PriorPlan, epic_id: str) -> list[dict[str, Any]]:
    """An untouched epic's live tickets back as raw items (scenarios from their
    criteria, cross-epic deps left to :func:`_link_epics`), so the merge
    re-derives every dependency against the re-planned epics."""
    return [
        {
            "id": t.id, "title": t.title, "description": t.description,
            "specialty": t.specialty.value, "difficulty": t.difficulty.value,
            "depends_on": [d for d in t.depends_on if prior.epic_of.get(d) == epic_id],
            "files": list(t.files), "reads": list(t.reads),
            "implements": list(t.implements), "consumes": list(t.consumes),
            "entrypoint": t.entrypoint,
            "scenarios": [
                {"id": c.id, "text": c.text, "given": list(c.given), "when": list(c.when),
                 "then": list(c.then), "criticality": c.criticality.value,
                 "security_abuse": c.security_abuse, "covers": list(c.covers)}
                for c in prior.criteria.values() if c.ticket_id == t.id
            ],
            _EPIC_KEY: epic_id,
        }
        for t in prior.tickets if prior.epic_of[t.id] == epic_id
    ]


def _norm_title(title: str) -> str:
    return " ".join(title.lower().split())


def _match_prior_epics(epics: Sequence[_Epic], prior: _PriorPlan) -> dict[str, str]:
    """New epic id -> the previous pass's epic it continues, by title or by its
    declared contracts. A re-plan's call 1 may reorder or split epics, so a
    position never pairs them, and an ambiguous match pairs nothing."""

    def contracts(e: _Epic) -> tuple[frozenset[str], frozenset[str]]:
        return frozenset(e.provides), frozenset(e.consumes)

    found: dict[str, str] = {}
    for e in epics:
        hits = {
            p.id for p in prior.epics
            if (_norm_title(e.title) and _norm_title(p.title) == _norm_title(e.title))
            or ((e.provides or e.consumes) and contracts(p) == contracts(e))
        }
        if len(hits) == 1:
            found[e.id] = hits.pop()
    claimed = Counter(found.values())
    return {new: old for new, old in found.items() if claimed[old] == 1}


def _epic_revision_text(
    findings: tuple[str, ...], prior: _PriorPlan | None, epic_id: str,
    continues: str | None,
) -> str:
    """The revision block of one epic call. ``continues`` is the previous pass's
    epic this one carries on: its tickets are reproduced under this epic's id.
    Without one, the previous plan's tickets are listed for reference only."""
    head = (
        "\n\nREVISION PASS — the signed Spec gate FAILED the previous plan. Return this "
        "epic's FULL ticket list again"
    )
    tail = "Spec gate findings:\n- " + "\n- ".join(findings) + "\n\n"
    if prior is None or continues is None:
        refs = "\n".join(
            f"- {t.id} [{t.specialty.value}] {t.title}" for t in prior.tickets
        )[:_BRIEF_CAP] if prior is not None else ""
        return (
            head + ", numbered afresh, AND add scenarios that close every finding below "
            "that concerns this epic.\n" + tail
            + "The previous plan's tickets (reference only — the epics were re-split, so "
            f"do not reuse these ids):\n{refs or '(none)'}"
        )

    def rebased(xid: str) -> str:
        return f"{epic_id}-{xid.removeprefix(f'{continues}-')}"

    lines: list[str] = []
    for t in (t for t in prior.tickets if prior.epic_of[t.id] == continues):
        lines.append(
            f"- {rebased(t.id)} [{t.specialty.value}] {t.title} files={list(t.files)}"
        )
        lines += [
            f"    - {rebased(c.id)}: {c.text} (criticality={c.criticality.value}; "
            f"security_abuse={c.security_abuse})"
            for c in prior.criteria.values() if c.ticket_id == t.id
        ]
    return (
        head + ": reproduce every ticket and scenario below (same ids, never weakened; "
        "fix in place any a finding names) AND add scenarios that close every finding "
        "below that concerns this epic.\n" + tail
        + f"This epic's previous tickets:\n{chr(10).join(lines) or '(none)'}"
    )


def _retired_tickets(
    state: FactoryState, rt: AgentRuntime, emitted: list[Ticket], seq: Seq
) -> list[Ticket]:
    """Tombstones for this node's prior live tickets a re-plan dropped, the same
    pattern as :func:`_retired_constraints`: ``supersedes`` names the ticket's own id
    and the newer Seq makes it the id's latest record, so ``read_tickets`` drops it."""
    newest = _newest_by_id(state, rt, TICKET_KIND, Ticket)
    kept = {t.id for t in emitted}
    stale = sorted(
        (t for t in newest.values() if t.supersedes != t.id and t.id not in kept),
        key=lambda t: t.id,
    )
    return [
        t.model_copy(update={
            "supersedes": t.id, "version": t.version + 1,
            "seq": seq._replace(local=len(emitted) + i),
        })
        for i, t in enumerate(stale)
    ]


def _route_cap(state: FactoryState) -> int:
    return _ROUTE_CAP_MICROUSD.get(str(state.get("route")), _DEFAULT_CAP_MICROUSD)


def _scope_decision(
    state: FactoryState, payload: dict[str, Any], seq: Seq
) -> ScopeDecision:
    if _forced_standard(state):
        return ScopeDecision(
            tier=ScopeTier.STANDARD,
            rationale="Standard scope: a design spec was supplied.",
            seq=seq,
        )
    scope = _section(payload, "scope")
    # Anything that is not exactly "demo" is STANDARD: an absent or garbled
    # answer must never silently cap the build.
    raw_tier = _str(scope.get("tier")).strip().lower()
    tier = ScopeTier.DEMO if raw_tier == ScopeTier.DEMO.value else ScopeTier.STANDARD
    return ScopeDecision(
        tier=tier,
        rationale=_str(scope.get("rationale")),
        signals=_strs(scope.get("signals")),
        seq=seq,
    )


def _triage(critique: CriteriaCritique, prd_uri: str, seq: Seq) -> GateTriage:
    """The advisory the Spec gate's operators read; the gate re-derives the FAIL
    from the materialized omissions itself (L5)."""
    if critique.coverage_findings or critique.contract_findings:
        explanation = (
            "BLOCKING: the "
            + " and ".join(
                f"{what} check failed — {'; '.join(found)}"
                for what, found in (
                    ("contract fit", critique.contract_findings),
                    ("coverage", critique.coverage_findings),
                ) if found
            )
            + (f" Security/abuse omissions: {'; '.join(critique.security_abuse_omissions)}."
               if critique.security_abuse_omissions else "")
            + " Any of these fails the Spec gate."
        )
    elif critique.spec_gate_blocking():
        explanation = (
            "BLOCKING: applicable security/abuse acceptance criteria are omitted — "
            f"{'; '.join(critique.security_abuse_omissions)}. "
            "Omission of applicable security/abuse criteria fails the Spec gate."
        )
    elif critique.missing:
        explanation = (
            "Non-blocking: missing or under-specified criteria — "
            f"{'; '.join(critique.missing)}."
        )
    else:
        explanation = "No criteria-completeness omissions found."
    return GateTriage(
        name=_SPEC_GATE, run_against=prd_uri, explanation=explanation,
        produced_by=NODE_ID, seq=seq,
    )


# ---- the node ------------------------------------------------------------------


def make_planner_node(
    rt: AgentRuntime,
    index: SymbolIndex | None = None,
    toolchains: Sequence[Toolchain] | None = None,
) -> NodeFn:
    """Build the Planner node (T1). Call 1 writes the plan and its epics; a small
    plan carries its tickets inline (one call), a larger one gets one ticket call
    per epic, run in parallel. Each call self-revises at most once when its own
    criteria review flags gaps.

    ``index`` is the context retriever probed for existing code spans (``None``
    is greenfield — empty retrieval). ``toolchains`` are the languages offered
    for the stack (``None`` probes this host once)."""

    def _call(sop: str, schema: dict[str, Any], content: str) -> dict[str, Any]:
        completion = rt.proxy.complete(
            NODE_ID,
            TIER,
            sop,
            [{"role": "user", "content": content}],
            purpose="ideation",
            schema=schema,
        )
        return completion.parsed or {}

    def _plan_epic(content: str) -> tuple[dict[str, Any], bool]:
        payload = _call(EPIC_SOP, EPIC_SCHEMA, content)
        gaps = _self_review_gaps(payload)
        if gaps:
            revision = _self_revision_text(gaps, "ticket list for your epic")
            payload = _call(EPIC_SOP, EPIC_SCHEMA, content + revision)
        return payload, bool(gaps)

    def _plan_epics(
        epics: Sequence[_Epic], contents: Sequence[str], overflow: list[str]
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...], list[str], list[str]]:
        """Run the epic calls; returns their raw items, tagged review gaps
        (missing, omissions), the self-revised epic ids and concerns. Tickets an
        epic loses at its cap are named in ``overflow``."""
        results = _run_parallel(_plan_epic, contents)
        raw: list[dict[str, Any]] = []
        missing: list[str] = []
        omissions: list[str] = []
        revised: list[str] = []
        concerns: list[str] = []
        for epic, (payload, self_revised) in zip(epics, results, strict=True):
            items, item_concerns = _epic_items(epic, payload.get("tickets"), overflow)
            raw += items
            concerns += item_concerns
            review = _section(payload, "criteria_review")
            missing += _tagged(epic.id, _strs(review.get("missing")))
            omissions += _tagged(epic.id, _strs(review.get("security_abuse_omissions")))
            if self_revised:
                revised.append(epic.id)
        return raw, tuple(missing), tuple(omissions), revised, concerns

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        findings = latest_spec_failure(state)
        prior = _prior_plan(state, rt) if findings else None
        targets = _target_epics(findings, prior) if findings and prior else None
        if findings and prior and targets:
            return _replan_epics(state, config, prior, targets, findings)
        spans = _retrieve_spans(index, state)
        if toolchains is None:
            from foundry.toolchains import available_toolchains

            tools: tuple[Toolchain, ...] = available_toolchains()
        else:
            tools = tuple(toolchains)
        languages = {t.language for t in tools}
        pinned = state.get("stack")
        schema = _plan_schema(languages | ({pinned.language} if pinned else set()))
        content = _user_content(state, rt, spans, tools)
        payload = _call(SOP, schema, content)
        # One free in-node revision when the self-review flags gaps, so they don't
        # spend the shared Spec-gate fix budget; never more than one.
        gaps = _self_review_gaps(payload)
        if gaps:
            payload = _call(SOP, schema, content + _self_revision_text(gaps))
        store = rt.cas

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        intent = _section(payload, "intent")
        brief_seq = seq("artifact:intent_brief")
        must_have_ids, must_haves = _goals(intent.get("must_haves"), "MH")
        success_ids, success = _goals(intent.get("success_criteria"), "SC")
        deliverable = _str(intent.get("deliverable")).strip().lower()
        brief = IntentBrief(
            intent=_str(intent.get("intent")),
            assumptions=_strs(intent.get("assumptions")),
            unknowns=_strs(intent.get("unknowns")),
            success_criteria=success,
            must_haves=must_haves,
            nice_to_haves=_strs(intent.get("nice_to_haves")),
            must_have_ids=must_have_ids,
            success_criterion_ids=success_ids,
            deliverable=deliverable if deliverable in DELIVERABLES else "",
            seq=brief_seq,
        )

        prd_section = _section(payload, "prd")
        prd_seq = seq("artifact:prd")
        interfaces = _build_interfaces(
            payload.get("interfaces"), seq("artifact:interfaces"), store=store
        )
        names = {i.name for i in interfaces}
        overflow: list[str] = []
        epics, epic_concerns = _build_epics(payload.get("epics"), names, overflow)
        inline = _objs(payload.get("tickets"))
        epic_missing: tuple[str, ...] = ()
        epic_omissions: tuple[str, ...] = ()
        revised_epics: list[str] = []
        tickets_seq = seq("artifact:tickets")
        criteria_seq = seq("artifact:acceptance_criteria")
        per_epic = bool(epics) and (len(epics) > 1 or not inline)
        if per_epic:
            if inline:
                epic_concerns.append(
                    f"Ignored {len(inline)} inline ticket(s): the plan has "
                    f"{len(epics)} epics, each planned in its own call."
                )
            intake = _intake_text(state)
            plan_brief = _plan_brief(payload, brief)
            iface_text = _interfaces_text(
                (_str(i.get("name")), _contract_text(i))
                for i in _objs(payload.get("interfaces"))
            )
            continues = _match_prior_epics(epics, prior) if prior else {}
            contents = [
                _epic_content(
                    intake, plan_brief, epics, e, iface_text,
                    _epic_revision_text(findings, prior, e.id, continues.get(e.id))
                    if findings else "",
                )
                for e in epics
            ]
            raw, epic_missing, epic_omissions, revised_epics, more = _plan_epics(
                epics, contents, overflow
            )
            epic_concerns += more
            tickets, criteria, ticket_concerns = _build_tickets(
                raw, tickets_seq, criteria_seq, names, cap=MAX_PLAN_TICKETS, epics=epics,
                overflow=overflow,
            )
        else:
            tickets, criteria, ticket_concerns = _build_tickets(
                payload.get("tickets"), tickets_seq, criteria_seq, names, overflow=overflow
            )
        # stack is single-writer: an operator pin or an earlier pass wins.
        decided, stack_concerns = (
            _stack_decision(payload, languages) if pinned is None else (None, [])
        )
        generated, more_concerns = _generated_tickets(
            interfaces, _section(payload, "stack"), pinned or decided, tickets_seq
        )
        tickets, attach_concerns = _attach_generated(tickets, generated, tickets_seq)
        ticket_concerns += [*stack_concerns, *more_concerns, *attach_concerns]
        raw_stack = _section(payload, "stack")
        chosen_stack = pinned or decided
        contract_findings = (
            *_contract_findings(
                tickets, criteria,
                frozenset(i.name for i in interfaces if i.kind == "type"),
            ),
            *_builtin_method_findings(
                interfaces,
                chosen_stack.language if chosen_stack is not None
                else _str(raw_stack.get("language")).strip().lower(),
            ),
        )
        raw_run = _str(raw_stack.get("run")).strip()
        stack_update = decided
        if (pinned is not None and not pinned.profile and not pinned.run and raw_run
                and _str(raw_stack.get("language")).strip().lower() == pinned.language):
            # A planner-chosen stack (no operator profile) from an earlier pass may
            # gain its run command, so a missing-run finding is fixable by re-plan.
            stack_update = pinned.model_copy(update={"run": raw_run})
        current_stack = stack_update or pinned
        tickets, own_concerns, stack_findings = _own_stack_files(tickets, current_stack)
        ticket_concerns += own_concerns
        coverage_findings = (*overflow, *stack_findings, *_coverage_findings(
            tickets, criteria, brief,
            run=None if current_stack is not None and current_stack.profile
            else (current_stack.run if current_stack is not None else raw_run),
        ))
        # Only a Spec-gate re-plan replaces an earlier plan of this run.
        dropped = (
            _retired_tickets(state, rt, tickets, tickets_seq)
            if latest_spec_failure(state) else []
        )
        prd = PRD(
            title=_str(prd_section.get("title")),
            prd_uri=persist_prose(_str(prd_section.get("body")), store=store),
            required_capabilities=_strs(prd_section.get("required_capabilities")),
            criterion_ids=tuple(c.id for c in criteria),
            seq=prd_seq,
        )

        review = _section(payload, "criteria_review")
        critique_seq = seq("artifact:criteria_findings")
        critique = CriteriaCritique(
            prd_uri=prd.prd_uri,
            missing=(*_strs(review.get("missing")), *epic_missing),
            security_abuse_omissions=(
                *_strs(review.get("security_abuse_omissions")), *epic_omissions
            ),
            # The plan reviews its own criteria: same call, same lineage — say so.
            judgment_provenance=JudgmentProvenance.SELF,
            seq=critique_seq,
            contract_findings=contract_findings,
            coverage_findings=coverage_findings,
        )

        sprint = _section(payload, "sprint_plan")
        sprint_seq = seq("artifact:sprint_plan")
        proposed = _int(sprint.get("usd_cap_microusd"))
        cap = min(proposed, _route_cap(state)) if proposed > 0 else _route_cap(state)
        plan = SprintPlan(
            milestones=_strs(sprint.get("milestones")),
            usd_cap_microusd=cap,
            risk_register_uri=cas_uri(_str(sprint.get("risk_register"))),
            done_definition=_str(sprint.get("done_definition")),
            seq=sprint_seq,
        )

        adrs = _build_adrs(payload.get("adrs"), seq("artifact:adrs"), store=store)
        ownership_seq = seq("artifact:ownership_map")
        ownership = _ownership_from_tickets(tickets, ownership_seq)
        design_section = _section(payload, "design")
        design_seq = seq("artifact:system_design")
        design = SystemDesign(
            title=_str(design_section.get("title")),
            design_uri=persist_prose(_str(design_section.get("body")), store=store),
            components=_strs(design_section.get("components")),
            adr_ids=tuple(a.id for a in adrs),
            interface_ids=tuple(i.name for i in interfaces),
            seq=design_seq,
        )
        constraints_seq = seq("pinned_constraints")
        constraints = _build_constraints(payload.get("pinned_constraints"), constraints_seq)
        retired = _retired_constraints(state, constraints, constraints_seq)

        epics_seq = seq("artifact:epics")
        epic_records = [
            Epic(
                id=e.id, title=e.title, summary=e.summary, specialty_mix=e.specialty_mix,
                depends_on=e.depends_on, provides=e.provides, consumes=e.consumes,
                seq=epics_seq._replace(local=i),
            )
            for i, e in enumerate(epics if per_epic else ())
        ]
        artifacts = [
            emit_artifact(brief, "intent_brief", NODE_ID, brief_seq, store=store),
            *(emit_artifact(e, EPIC_KIND, NODE_ID, e.seq, store=store) for e in epic_records),
            emit_artifact(prd, "prd", NODE_ID, prd_seq, store=store),
            *(
                emit_artifact(c, "acceptance_criterion", NODE_ID, c.seq, store=store)
                for c in criteria
            ),
            emit_artifact(critique, "criteria_findings", NODE_ID, critique_seq, store=store),
            emit_artifact(plan, "sprint_plan", NODE_ID, sprint_seq, store=store),
            emit_artifact(design, "system_design", NODE_ID, design_seq, store=store),
            *(emit_artifact(a, "adr", NODE_ID, a.seq, store=store) for a in adrs),
            *(
                emit_artifact(i, "interface_contract", NODE_ID, i.seq, store=store)
                for i in interfaces
            ),
            *(
                emit_artifact(t, TICKET_KIND, NODE_ID, t.seq, store=store)
                for t in (*tickets, *dropped)
            ),
            emit_artifact(ownership, "ownership_map", NODE_ID, ownership_seq, store=store),
        ]

        out: dict[str, Any] = {
            "artifacts": artifacts,
            "pinned_constraints": [*constraints, *retired],
            "required_capabilities": list(prd.required_capabilities),
            "gate_triage": [_triage(critique, prd.prd_uri, seq("gate_triage:spec"))],
        }
        if stack_update is not None:
            out["stack"] = stack_update
        # scope_tier is single-writer: an operator pin or an earlier pass wins.
        scope = state.get("scope_tier")
        if scope is None:
            scope = _scope_decision(state, payload, seq("scope_tier"))
            out["scope_tier"] = scope

        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        concerns: list[str] = [*epic_concerns, *ticket_concerns]
        if dropped:
            concerns.append("Re-plan retired ticket(s) " + ", ".join(t.id for t in dropped) + ".")
        if not criteria:
            concerns.append("No acceptance criteria derived from the intent.")
        if not ownership.owners:
            concerns.append("Empty ownership map — no build tasks will be created.")
        if scope.tier is ScopeTier.DEMO:
            concerns.append("Demo scope caps the build.")
        if any(o.strip() for o in critique.security_abuse_omissions):
            concerns.append("Self-review left security/abuse omissions open.")
        if contract_findings:
            concerns.append(f"Contract fit check: {len(contract_findings)} finding(s).")
        if coverage_findings:
            concerns.append(f"Coverage check: {len(coverage_findings)} finding(s).")
        stack = current_stack
        epic_note = (
            f" Tickets planned per epic in {len(epics)} parallel call(s)"
            f" ({', '.join(e.id for e in epics)})."
            + (f" Self-revised epic(s) {', '.join(revised_epics)} once." if revised_epics else "")
            if per_epic else ""
        )
        out["agent_journal"] = [
            journal_entry(
                node_id=NODE_ID,
                role=ROLE,
                seq=seq("journal"),
                summary=(
                    f"Planned {prd.title!r} ({scope.tier.value} scope): "
                    f"{len(criteria)} acceptance criteria ({high_risk} high-risk), "
                    f"{len(adrs)} ADRs, {len(interfaces)} interface contracts, "
                    f"{len(tickets)} tickets, "
                    f"{len(ownership.owners)} ownership units, {len(constraints)} "
                    "pinned constraints."
                    + (f" Stack: {stack.language}"
                       + (f" ({stack.profile} profile)." if stack.profile else ".")
                       if stack else "")
                    + epic_note
                    + (" Revised after a Spec-gate failure." if findings else "")
                    + (" Self-revised once after its own criteria review." if gaps else "")
                ),
                assumptions=(
                    *brief.assumptions,
                    f"Retrieved {len(spans)} code span(s) for impact analysis.",
                ),
                concerns=tuple(concerns),
            )
        ]
        return out

    def _replan_epics(
        state: FactoryState,
        config: RunnableConfig,
        prior: _PriorPlan,
        targets: tuple[str, ...],
        findings: tuple[str, ...],
    ) -> dict[str, Any]:
        """Spec-gate re-plan of only the epics the findings name: call 1 and the
        other epics stand; every dependency is re-derived over the merged set.
        Re-emits what the re-planned tickets change (tickets + tombstones,
        criteria, PRD criterion ids, critique, ownership); the plan-level
        records of the earlier pass stay current."""
        store = rt.cas

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        stack = state.get("stack")
        chosen = [e for e in prior.epics if e.id in targets]
        intake = _intake_text(state)
        iface_text = _interfaces_text(prior.interfaces)
        contents = [
            _epic_content(
                intake, prior.brief, prior.epics, e, iface_text,
                _epic_revision_text(
                    tuple(f for f in findings if e.id in (_target_epics((f,), prior) or ())),
                    prior, e.id, e.id,
                ),
            )
            for e in chosen
        ]
        overflow: list[str] = []
        fresh, missing, omissions, revised, concerns = _plan_epics(chosen, contents, overflow)
        raw = [
            item
            for e in prior.epics
            for item in (
                [i for i in fresh if i[_EPIC_KEY] == e.id] if e.id in targets
                else _prior_items(prior, e.id)
            )
        ]
        tickets_seq = seq("artifact:tickets")
        tickets, criteria, ticket_concerns = _build_tickets(
            raw, tickets_seq, seq("artifact:acceptance_criteria"),
            {name for name, _ in prior.interfaces}, cap=MAX_PLAN_TICKETS, epics=prior.epics,
            overflow=overflow,
        )
        tickets, attach_concerns = _attach_generated(tickets, prior.generated, tickets_seq)
        tickets, own_concerns, stack_findings = _own_stack_files(tickets, stack)
        ticket_concerns += [*attach_concerns, *own_concerns]
        dropped = _retired_tickets(state, rt, tickets, tickets_seq)
        prd_seq = seq("artifact:prd")
        prd = prior.prd.model_copy(
            update={"criterion_ids": tuple(c.id for c in criteria), "seq": prd_seq}
        )

        def kept(gaps: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(g for g in gaps if not any(g.startswith(f"[{t}] ") for t in targets))

        old = prior.critique
        critique_seq = seq("artifact:criteria_findings")
        critique = CriteriaCritique(
            prd_uri=prd.prd_uri,
            missing=(*kept(old.missing if old else ()), *missing),
            security_abuse_omissions=(
                *kept(old.security_abuse_omissions if old else ()), *omissions
            ),
            judgment_provenance=JudgmentProvenance.SELF,
            seq=critique_seq,
            contract_findings=_contract_findings(tickets, criteria, prior.declared),
            coverage_findings=(*overflow, *stack_findings, *_coverage_findings(
                tickets, criteria, prior.intent,
                run=None if stack is None or stack.profile else stack.run,
            )),
        )
        ownership_seq = seq("artifact:ownership_map")
        ownership = _ownership_from_tickets(tickets, ownership_seq)
        artifacts = [
            emit_artifact(prd, "prd", NODE_ID, prd_seq, store=store),
            *(
                emit_artifact(c, "acceptance_criterion", NODE_ID, c.seq, store=store)
                for c in criteria
            ),
            emit_artifact(critique, "criteria_findings", NODE_ID, critique_seq, store=store),
            *(
                emit_artifact(t, TICKET_KIND, NODE_ID, t.seq, store=store)
                for t in (*tickets, *dropped)
            ),
            emit_artifact(ownership, "ownership_map", NODE_ID, ownership_seq, store=store),
        ]
        concerns += ticket_concerns
        if dropped:
            concerns.append("Re-plan retired ticket(s) " + ", ".join(t.id for t in dropped) + ".")
        if critique.spec_gate_blocking():
            concerns.append("Self-review left security/abuse omissions open.")
        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        return {
            "artifacts": artifacts,
            "gate_triage": [_triage(critique, prd.prd_uri, seq("gate_triage:spec"))],
            "agent_journal": [
                journal_entry(
                    node_id=NODE_ID,
                    role=ROLE,
                    seq=seq("journal"),
                    summary=(
                        f"Re-planned epic(s) {', '.join(targets)} of {prd.title!r}: "
                        f"{len(criteria)} acceptance criteria ({high_risk} high-risk), "
                        f"{len(tickets)} tickets across {len(prior.epics)} epics, "
                        f"{len(ownership.owners)} ownership units. "
                        "Revised after a Spec-gate failure."
                        + (f" Self-revised epic(s) {', '.join(revised)} once." if revised else "")
                    ),
                    concerns=tuple(concerns),
                )
            ],
        }

    return _node
