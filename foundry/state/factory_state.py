"""`FactoryState` — the typed shared state with per-channel merge semantics.

(architecture.md §3) Single-writer channels have no reducer; fan-in channels
use the commutative reducers from `reducers.py`. Large/append-only data is
held out-of-band by content-addressed handle (`history_ref`, artifact URIs)
to bound checkpoint size (DS9).
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from foundry.schemas.architecture import PinnedConstraint
from foundry.schemas.base import Artifact, BudgetLedger, ExternalAction, LogRef
from foundry.schemas.gates import GateResult, GateTriage
from foundry.schemas.governance import ScopeDecision, SignOffRecord, TierDecision
from foundry.schemas.journal import JournalEntry
from foundry.state import reducers


class FactoryState(TypedDict, total=False):
    request: str  # single-writer
    # OPTIONAL full design-spec document text (task [8]). Additive input: a short
    # `request` still summarizes intent; when present, `spec` carries the detailed
    # design-spec doc(s) text that GROUNDS the plan — the planner derives the
    # IntentBrief/PRD/criteria/design FROM it (read by `planner._intake_text`).
    # Single-writer (seeded at graph entry). It is plain INPUT DATA — it INFORMS
    # the PRD/criteria but never bypasses the acceptance-criteria derivation or
    # any signed gate (same trust posture as `request`: agents propose, only
    # gates decide, L5).
    spec: str  # single-writer
    route: str | None  # single-writer (triage)
    phase: str  # single-writer (supervisor)
    # How much system the request is asking for. Single-writer — unlike
    # `risk_tier` (written by the parent-level triage node) this one is written
    # by the `planner` node INSIDE the planner subgraph, only when absent (an
    # operator pin or an earlier pass wins), so a spec_fix re-entry never changes
    # it. PROMPT INPUT ONLY: read by the planner to cap what it
    # proposes. Never read by a gate, router, or admission path — no gate is
    # skipped and no verdict weakened at DEMO. It DOES thin the criteria those
    # gates verify against, though; see ScopeDecision for that honest limit.
    scope_tier: ScopeDecision | None  # single-writer (planner, or --scope pin)
    required_capabilities: Annotated[list[str], reducers.union]
    pinned_constraints: Annotated[list[PinnedConstraint], reducers.merge_constraints]
    artifacts: Annotated[list[Artifact], reducers.merge_by_uri]
    gates: Annotated[list[GateResult], reducers.merge_gates]
    gate_triage: Annotated[list[GateTriage], reducers.union_triage]
    # ADVISORY observability ONLY (plan [11]). A SEPARATE channel from gates: an
    # agent's work-journal note, folded additively by `union_journal`. NEVER read
    # by any gate, by `merge_gates`, by `route_after_gates`, or by `admission` —
    # the journal cannot influence whether a build passes (agents propose, only
    # signed gates decide, L5). Same posture as `gate_triage`, weaker still.
    agent_journal: Annotated[list[JournalEntry], reducers.union_journal]
    tasks: Annotated[list[dict], reducers.union_by_id]
    budget: BudgetLedger  # single-writer (Budget Controller)
    actions: Annotated[list[ExternalAction], reducers.union_actions]
    history_ref: LogRef  # handle; raw events live out-of-band
    # --- Phase-2 (Gated mode) channels (governance.md §1e/§2) -----------------
    mode: str  # single-writer: always "assistant" (supervisor triage)
    risk_tier: TierDecision | None  # single-writer (triage tiering, gov §1e)
    sign_offs: Annotated[list[SignOffRecord], reducers.union_sign_offs]
