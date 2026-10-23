"""Implementation-layer artifacts (agents.md Layer 3 — Implementation).

Senior Engineer → `Task[]` + `ConflictResolution[]`; Junior Engineer →
`FeatureDiff` on its own `task/<id>` branch. The review REQUEST packet lives
here; the review VERDICT does not — verdicts are `GateResult`s with a
judgment binding (gates.py; governance.md §1c). Diff bodies are
content-addressed handles, never inline (DS9).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypedDict

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"  # PR opened, awaiting Reviewer/Critic
    DONE = "done"


class Specialty(StrEnum):
    """Which specialist agent builds a ticket."""

    FRONTEND = "frontend"
    BACKEND = "backend"
    DATA = "data"
    TEST = "test"


class Difficulty(StrEnum):
    """Planner's rating of a ticket; mapped to a model tier, never to a model id."""

    TRIVIAL = "trivial"
    STANDARD = "standard"
    COMPLEX = "complex"


# Default difficulty -> proxy tier (T1 strongest). Overridable per run.
DEFAULT_TICKET_TIERS: dict[Difficulty, str] = {
    Difficulty.TRIVIAL: "T3",
    Difficulty.STANDARD: "T2",
    Difficulty.COMPLEX: "T1",
}

FEATURE_DIR = "features"

# Tickets the planner emits itself, ahead of the model's tickets: the wave-0 one
# that writes every typed contract as code (its files are gate-owned once built),
# and the one that wires the stack's Cucumber runner.
CONTRACTS_TICKET_ID = "contracts"
HARNESS_TICKET_ID = "bdd-harness"

# Env var the gate sets to a path it owns; the planned BDD command must write its
# Cucumber Messages (NDJSON) report there.
BDD_REPORT_ENV = "FOUNDRY_BDD_REPORT"


class Toolchain(FrozenModel):
    """A language toolchain detected on this machine (offered to the planner)."""

    language: str  # "python", "typescript", "go", ...
    binaries: tuple[str, ...]  # the executables found, e.g. ("node", "npm")
    version: str = ""


class StackDecision(FrozenModel):
    """The planner's choice of stack and the commands the gates run for it.
    Commands are shell strings run by the gates in the sandbox; only ``install``
    gets network."""

    language: str
    framework: str = ""
    package_manager: str = ""
    install: str = ""
    build: str = ""
    typecheck: str = ""
    test: str = ""
    bdd: str = ""  # must write Cucumber Messages to $FOUNDRY_BDD_REPORT
    run: str = ""  # optional smoke command
    profile: str = ""  # a pinned/legacy gate profile name, when one was used
    notes: str = ""


def feature_path(ticket_id: str) -> str:
    """Repo-relative path of a ticket's executable BDD feature file."""
    return f"{FEATURE_DIR}/{ticket_id}.feature"


def scenario_ref(ticket_id: str, criterion_id: str) -> str:
    """A criterion's oracle: its scenario, which is named after the criterion id."""
    return f"{feature_path(ticket_id)}::{criterion_id}"


class Ticket(FrozenModel):
    """An atomic, Jira-style unit of work the Planner emits. Its acceptance
    scenarios are the AcceptanceCriterion records listed in ``scenario_ids``."""

    id: str  # "T-001"
    title: str
    description: str = ""
    specialty: Specialty
    difficulty: Difficulty = Difficulty.STANDARD
    depends_on: tuple[str, ...] = ()  # ticket ids; must form a DAG
    files: tuple[str, ...] = ()  # the only paths this ticket may write (outputs)
    reads: tuple[str, ...] = ()  # files it reads (inputs)
    scenario_ids: tuple[str, ...] = ()  # AcceptanceCriterion ids
    implements: tuple[str, ...] = ()  # InterfaceContract ids it provides
    consumes: tuple[str, ...] = ()  # InterfaceContract ids it calls
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class Epic(FrozenModel):
    """One epic of an epic-mode plan as the Planner's call 1 split it, kept so a
    targeted Spec-gate re-plan re-runs it with its own title, summary and
    declared contracts. Every epic of one pass shares a Seq but for ``local``."""

    id: str  # positional: "E1".."En"
    title: str = ""
    summary: str = ""
    specialty_mix: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()  # epic ids; a DAG
    provides: tuple[str, ...] = ()  # InterfaceContract names
    consumes: tuple[str, ...] = ()
    seq: Seq


class TaskEvent(TypedDict):
    """Live per-ticket telemetry (pool -> run(task_cb=...) -> Workshop)."""

    phase: Literal["start", "done", "failed"]
    ticket_id: str
    title: str
    specialty: str
    tier: str


class Task(FrozenModel):
    """A well-specified unit of work (agents.md Layer 3 — Senior Engineer SOP):
    each task gets its own branch (`task/<id>`) and traces to the acceptance
    criteria it implements."""

    id: str
    spec_uri: str  # CAS handle to the task spec body (DS9)
    criterion_ids: tuple[str, ...]  # traceability to AcceptanceCriterion.id
    branch: str  # "task/<id>" — the junior's git scope is this branch only
    status: TaskStatus = TaskStatus.PENDING
    # Ticket-driven fields; None/() on tasks derived from a plain ownership unit.
    ticket_id: str | None = None
    specialty: Specialty | None = None
    tier: str | None = None  # proxy tier ("T1".."T3"); carried to the adapter
    depends_on: tuple[str, ...] = ()  # task ids that must finish first
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class FeatureDiff(FrozenModel):
    """Junior Engineer output: the feature diff / PR on its branch
    (agents.md Layer 3). Re-runs reset the branch to `base_tree_sha` first
    (idempotent retry), so a retried diff supersedes its predecessor."""

    diff_uri: str  # content-addressed diff handle (DS9)
    task_id: str
    branch: str
    base_tree_sha: str
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class ReviewRequest(FrozenModel):
    """The review REQUEST packet — NOT the verdict (agents.md Layer 4 —
    Reviewer/Critic; verdicts are `GateResult`s).

    Carries the server/orchestrator-issued single-use `verdict_nonce` that the
    eventual judgment verdict's binding must consume — the anti-replay,
    anti-status-FLIP challenge of architecture.md §3 / governance.md §1c.
    """

    diff_uri: str  # subject_artifact_uri of the eventual judgment verdict
    task_id: str
    run_against: str  # per-branch "task/<id>" | integrated state_sha (arch §4)
    verdict_nonce: str  # server-issued, single-use; never agent-minted
    seq: Seq


class ConflictResolution(FrozenModel):
    """Senior Engineer integration output (agents.md Layer 3): merge conflicts
    resolved using the `ownership_map` and the conflicting hunks."""

    task_ids: tuple[str, ...]  # the conflicting tasks
    paths: tuple[str, ...]  # conflicting files
    base_tree_sha: str
    resolved_diff_uri: str  # CAS handle to the resolution diff (DS9)
    rationale: str = ""  # ownership-map citation
    seq: Seq
