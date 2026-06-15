"""Senior Engineer node factory (agents.md Layer 3 — Implementation).

The Senior Engineer is the *decomposition* half of the implementation layer:
it breaks the chosen :class:`~foundry.schemas.architecture.SystemDesign` into
well-specified :class:`~foundry.schemas.implementation.Task` units, one per
branch (``task/<id>``), each tracing back to the acceptance criteria it
implements (``criterion_ids``). The branch/merge ORDER is a deterministic
function of the architect's
:class:`~foundry.schemas.architecture.OwnershipMap` —
``OwnershipMap.merge_order()`` — so a LangGraph resume re-derives the identical
ordering (no reliance on dict insertion or process state; LEARNINGS L8).

Convention (LEARNINGS AR1-AR4): a ``make_senior_engineer_node(rt, ...) ->
NodeFn`` factory returns a closure ``(state, config) -> channel-writes`` that
mints a deterministic :class:`~foundry.state.seq.Seq` via ``rt.seq`` and reaches
the single model egress through ``rt.proxy.complete`` (L6). It writes the
decomposed tasks to the ``tasks`` channel (``list[dict]`` keyed by ``id``);
nothing here mints a ``GateResult`` — the Senior's review verdict is produced on
the trusted ``rt.proxy.judge`` path, never minted by the agent (L5).

The conflict-resolution path (integration merge using the ownership map) is
typed but STUBBED here: it returns a deterministic
:class:`~foundry.schemas.implementation.ConflictResolution` skeleton so callers
and the graph wiring can depend on the shape before the integration loop lands.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.canonical import content_digest
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.implementation import ConflictResolution, Task

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

__all__ = [
    "make_senior_engineer_node",
    "stub_conflict_resolution",
]

NODE_ID = "senior_engineer"

_SOP = (
    "You are the Senior Engineer. Decompose the system design into a set of "
    "well-specified, independently-buildable tasks. Each task implements one or "
    "more acceptance criteria and is scoped to a single ownership unit so it can "
    "be built on its own branch. Reply with JSON only."
)

# Grammar-constrained decomposition channel: the proxy json.loads this off the
# structured-output channel, so the adapter cannot smuggle free text past it.
_DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string"},
                    "title": {"type": "string"},
                    "criterion_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["unit", "title", "criterion_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}


def _task_id(unit: str) -> str:
    """Stable, process-reproducible task id for an ownership unit.

    Keyed by ``content_digest`` (LEARNINGS L7), NEVER builtin ``hash()`` — a
    resume in a fresh process must derive the identical id so the ``tasks``
    channel's keyed union coalesces the re-emission instead of duplicating it.
    """
    return f"t-{content_digest({'unit': unit})[:12]}"


def make_senior_engineer_node(
    rt: AgentRuntime,
    *,
    design: SystemDesign,
    ownership: OwnershipMap,
    tier: str = "T1",
) -> NodeFn:
    """Build the Senior Engineer node.

    Parameters
    ----------
    design / ownership:
        The architect's outputs (resolved from their CAS handles by the
        orchestrator). ``ownership.merge_order()`` gives the deterministic
        branch/merge order the emitted tasks are sequenced by.
    tier:
        Model tier for the decomposition call (T1 default — defects are born in
        implementation, agents.md Layer 3).
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        order = ownership.merge_order()
        # Map each ownership unit to the criteria its task should implement,
        # asking the proxy (single egress, L6) for the decomposition.
        prompt = json.dumps(
            {
                "design_title": design.title,
                "design_uri": design.design_uri,
                "components": list(design.components),
                "interfaces": list(design.interface_ids),
                "ownership_units_in_merge_order": list(order),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        result = rt.proxy.complete(
            NODE_ID,
            tier,
            _SOP,
            [{"role": "user", "content": prompt}],
            purpose="ideation",
            schema=_DECOMPOSITION_SCHEMA,
        )
        proposed = (result.parsed or {}).get("tasks", [])
        criteria_by_unit = _criteria_by_unit(proposed)

        # Emit one Task per ownership unit, IN merge order, so a downstream
        # consumer iterating the channel by Seq sees the deterministic order.
        # `local` carries the merge-order index (the Seq tail-break), so the
        # ordering is recoverable from the records alone.
        tasks: list[dict[str, Any]] = []
        for index, unit in enumerate(order):
            tid = _task_id(unit)
            seq = rt.seq(config, NODE_ID, f"task:{tid}", local=index)
            spec_digest = content_digest({"unit": unit, "design": design.design_uri})
            task = Task(
                id=tid,
                spec_uri=f"cas://task/{spec_digest}",
                criterion_ids=criteria_by_unit.get(unit, ()),
                branch=f"task/{tid}",
                seq=seq,
            )
            tasks.append(task.model_dump(mode="json"))
        return {"tasks": tasks}

    return _node


def _criteria_by_unit(proposed: list[Any]) -> dict[str, tuple[str, ...]]:
    """Index the model's proposed criterion_ids by ownership unit.

    Defensive: ignores malformed entries (the merge order, not the model, is
    the source of truth for WHICH tasks exist — the model only enriches each
    task with its criteria traceability)."""
    out: dict[str, tuple[str, ...]] = {}
    for entry in proposed:
        if not isinstance(entry, dict):
            continue
        unit = entry.get("unit")
        if not isinstance(unit, str):
            continue
        cids = entry.get("criterion_ids", [])
        out[unit] = tuple(str(c) for c in cids) if isinstance(cids, list) else ()
    return out


def stub_conflict_resolution(
    *,
    task_ids: tuple[str, ...],
    paths: tuple[str, ...],
    base_tree_sha: str,
    ownership: OwnershipMap,
    seq: Seq,
) -> ConflictResolution:
    """Typed-but-stubbed integration conflict resolution (agents.md Layer 3).

    The real path resolves merge conflicts using the ownership map and the
    conflicting hunks; here it returns a deterministic skeleton (empty
    resolution diff, ownership-citing rationale) so callers can depend on the
    shape before the integration loop lands. Content-addressed (DS9) and
    deterministic so a resume re-derives the identical record."""
    citation = ", ".join(f"{p}->{ownership.owner_of(p) or '?'}" for p in paths)
    diff_digest = content_digest({"tasks": list(task_ids), "paths": list(paths)})
    return ConflictResolution(
        task_ids=task_ids,
        paths=paths,
        base_tree_sha=base_tree_sha,
        resolved_diff_uri=f"cas://conflict/{diff_digest}",
        rationale=f"STUB: ownership-map order resolves [{citation}]",
        seq=seq,
    )
