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
from foundry.state import reducers


class FactoryState(TypedDict, total=False):
    request: str  # single-writer
    route: str | None  # single-writer (triage)
    phase: str  # single-writer (supervisor)
    required_capabilities: Annotated[list[str], reducers.union]
    pinned_constraints: Annotated[list[PinnedConstraint], reducers.merge_constraints]
    artifacts: Annotated[list[Artifact], reducers.merge_by_uri]
    gates: Annotated[list[GateResult], reducers.merge_gates]
    gate_triage: Annotated[list[GateTriage], reducers.union_triage]
    tasks: Annotated[list[dict], reducers.union_by_id]
    budget: BudgetLedger  # single-writer (Budget Controller)
    actions: Annotated[list[ExternalAction], reducers.union_actions]
    history_ref: LogRef  # handle; raw events live out-of-band
