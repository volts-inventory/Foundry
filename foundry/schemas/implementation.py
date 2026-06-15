"""Implementation-layer artifacts (agents.md Layer 3 — Implementation).

Senior Engineer → `Task[]` + `ConflictResolution[]`; Junior Engineer →
`FeatureDiff` on its own `task/<id>` branch. The review REQUEST packet lives
here; the review VERDICT does not — verdicts are `GateResult`s with a
judgment binding (gates.py; governance.md §1c). Diff bodies are
content-addressed handles, never inline (DS9).
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"  # PR opened, awaiting Reviewer/Critic
    DONE = "done"


class Task(FrozenModel):
    """A well-specified unit of work (agents.md Layer 3 — Senior Engineer SOP):
    each task gets its own branch (`task/<id>`) and traces to the acceptance
    criteria it implements."""

    id: str
    spec_uri: str  # CAS handle to the task spec body (DS9)
    criterion_ids: tuple[str, ...]  # traceability to AcceptanceCriterion.id
    branch: str  # "task/<id>" — the junior's git scope is this branch only
    status: TaskStatus = TaskStatus.PENDING
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
