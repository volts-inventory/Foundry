"""Senior Engineer node factory (agents.md Layer 3 — Implementation).

The Senior Engineer is the *decomposition* half of the implementation layer:
it breaks the chosen :class:`~foundry.schemas.architecture.SystemDesign` into
well-specified :class:`~foundry.schemas.implementation.Task` units, one per
branch (``task/<id>``), each tracing back to the acceptance criteria it
implements (``criterion_ids``).

Org fan-out (org_fanout_design.md, Wave B): the decomposition runs ONE focused
proxy call PER Tech-Lead — one per OwnershipMap owner (``OwnershipMap.leads()``)
— each decomposing only its own ``units_of(owner)``. Because the task id/seq/
branch are keyed off the unit and its GLOBAL merge-order index, this changes only
WHO proposes a task, never its identity or order: a single-owner map is one call,
byte-identical to the pre-fan-out Senior; a multi-owner map keeps the same ids and
the same merge order, differing only in per-domain ``criterion_ids`` enrichment.
The branch/merge ORDER is a deterministic function of the architect's
:class:`~foundry.schemas.architecture.OwnershipMap` —
``OwnershipMap.merge_order()`` — so a LangGraph resume re-derives the identical
ordering (no reliance on dict insertion or process state; LEARNINGS L8).

State-reading convention (LEARNINGS AR1-AR4, IM4): the node's DATA inputs — the
SystemDesign and OwnershipMap the architecture layer produced — are read from
``state["artifacts"]`` INSIDE the closure (found by ``Artifact.kind``), NEVER
captured as build-time factory kwargs. The artifacts channel carries only the
content-addressed HANDLES (DS9), so the typed bodies are reconstituted through
an injected ``resolve`` callable (an INFRA dependency, like ``rt.adapter``):
the orchestrator wires it to the CAS; tests wire it to an in-memory map. This
keeps the factory composable — one ``rt`` + infra, the per-run data flows in on
the state channels exactly as the architecture subgraph emitted it.

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

from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.canonical import content_digest
from foundry.schemas.architecture import OwnershipMap, SystemDesign, lane_of_area
from foundry.schemas.implementation import ConflictResolution, Task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.schemas.base import Artifact
    from foundry.schemas.journal import JournalEntry
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

    # Reconstitutes a typed body from its content-addressed artifact handle.
    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "DESIGN_KIND",
    "OWNERSHIP_KIND",
    "ArtifactResolverError",
    "leads_for_lane",
    "make_senior_engineer_node",
    "stub_conflict_resolution",
    "task_id_for_unit",
]

NODE_ID = "senior_engineer"

# The Artifact.kind tags the Principal Architect stamps on its handles
# (foundry/agents/architecture/principal_architect.py) — the seam the Senior
# reads its DATA inputs back off the artifacts channel by.
DESIGN_KIND = "system_design"
OWNERSHIP_KIND = "ownership_map"

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


class ArtifactResolverError(LookupError):
    """The architecture-layer artifact the Senior needs is absent from state.

    Raised fail-closed when ``state["artifacts"]`` carries no handle of the
    required ``kind`` (the architecture layer must run first), or when the
    injected ``resolve`` cannot reconstitute the typed body from its handle.
    """


def task_id_for_unit(unit: str) -> str:
    """Stable, process-reproducible task id for an ownership unit.

    Keyed by ``content_digest`` (LEARNINGS L7), NEVER builtin ``hash()`` — a
    resume in a fresh process must derive the identical id so the ``tasks``
    channel's keyed union coalesces the re-emission instead of duplicating it.
    Public so the Junior (Wave D) can map a task back to its unit -> owner ->
    area for the per-area budget lineage, without storing area on the Task.
    """
    return f"t-{content_digest({'unit': unit})[:12]}"


def _first_of_kind(artifacts: Sequence[Artifact], kind: str) -> Artifact:
    """The newest-by-Seq artifact handle of ``kind`` on the channel.

    The architecture layer emits one such handle; a supersession would emit a
    higher-``seq`` record under a fresh uri (L4), so we pick the latest by Seq
    deterministically rather than relying on channel order."""
    candidates = [a for a in artifacts if a.kind == kind]
    if not candidates:
        raise ArtifactResolverError(
            f"no {kind!r} artifact on state['artifacts'] — the architecture "
            "layer must produce it before the Senior Engineer runs"
        )
    return max(candidates, key=lambda a: a.seq)


def _read_design_and_ownership(
    state: FactoryState, resolve: ArtifactResolver
) -> tuple[SystemDesign, OwnershipMap]:
    """Read the Senior's DATA inputs off ``state['artifacts']`` (the convention).

    The SystemDesign + OwnershipMap come from STATE, not build-time kwargs: find
    the handles by ``kind`` and reconstitute the typed bodies via the injected
    ``resolve`` (CAS in prod, an in-memory map in tests). Fail closed if a
    handle is missing or resolves to the wrong type."""
    artifacts = state.get("artifacts") or ()
    design = resolve(_first_of_kind(artifacts, DESIGN_KIND))
    ownership = resolve(_first_of_kind(artifacts, OWNERSHIP_KIND))
    if not isinstance(design, SystemDesign):
        raise ArtifactResolverError(
            f"resolved {DESIGN_KIND!r} handle to {type(design).__name__}, "
            "expected SystemDesign"
        )
    if not isinstance(ownership, OwnershipMap):
        raise ArtifactResolverError(
            f"resolved {OWNERSHIP_KIND!r} handle to {type(ownership).__name__}, "
            "expected OwnershipMap"
        )
    return design, ownership


def leads_for_lane(ownership: OwnershipMap, lane: str | None) -> tuple[str, ...]:
    """The Tech-Leads a LANE node decomposes: those whose area folds into ``lane``
    (:func:`lane_of_area`), in :meth:`OwnershipMap.leads` order. ``None`` = every lead
    (the legacy single-node scope).

    Shared with the subgraph router so the EDGE and the NODE decide lane membership
    with one predicate and can never disagree (the architecture subgraph's AC2
    ``should_enter_<role>`` pattern) — a lane the router skips is provably a lane this
    node would have found nothing to do in."""
    return tuple(
        lead
        for lead in ownership.leads()
        if lane is None or lane_of_area(ownership.area_of(lead)) == lane
    )


def lanes_with_leads(state: FactoryState, resolve: ArtifactResolver) -> frozenset[str] | None:
    """Which lanes have a Tech-Lead to decompose, for the subgraph router.

    ``None`` means UNKNOWN — the OwnershipMap isn't readable yet. The router must then
    run the node anyway: reading it is the node's job, and its failure is the node's to
    raise (a router that swallowed it would silently skip the whole lane)."""
    try:
        _design, ownership = _read_design_and_ownership(state, resolve)
    except (ArtifactResolverError, KeyError, LookupError, ValueError):
        return None
    return frozenset(lane_of_area(ownership.area_of(lead)) for lead in ownership.leads())


def make_senior_engineer_node(
    rt: AgentRuntime,
    *,
    resolve: ArtifactResolver,
    tier: str = "T1",
    lane: str | None = None,
) -> NodeFn:
    """Build the Senior Engineer node.

    Parameters
    ----------
    resolve:
        INFRA dependency: reconstitutes a typed body (SystemDesign /
        OwnershipMap) from its content-addressed :class:`Artifact` handle. The
        orchestrator wires it to the CAS; tests wire it to an in-memory map. The
        DATA (WHICH design/ownership) is read from ``state["artifacts"]`` inside
        the closure — never a build-time kwarg (LEARNINGS IM4 / AR1-AR4).
    tier:
        Model tier for the decomposition call (T1 default — defects are born in
        implementation, agents.md Layer 3).
    lane:
        Discipline LANE (``frontend``/``backend``/``data``) this node owns. When set,
        the node decomposes ONLY the Tech-Leads whose area folds into this lane
        (:func:`lane_of_area`), so the org fan-out becomes distinct per-lane GRAPH
        nodes. The task id/seq/branch stay keyed off the unit and its GLOBAL merge
        index (``index_of`` spans the whole OwnershipMap) AND the shared internal
        ``NODE_ID``, so scoping changes only WHICH graph node proposes a task — never
        its identity, seq, order, or the keyed-union merge. Each unit is owned by
        exactly one lead in exactly one lane, so no two lane nodes collide on a seq
        key. ``None`` (default) = the whole map in one node (byte-identical legacy).

    The distinct per-lane TELEMETRY comes from the graph node NAME the subgraph
    registers (``frontend_senior`` …), NOT from this closure — the internal
    ``NODE_ID`` stays ``senior_engineer`` so content-addressing is unchanged.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        design, ownership = _read_design_and_ownership(state, resolve)
        order = ownership.merge_order()
        index_of = {unit: i for i, unit in enumerate(order)}  # GLOBAL, spans all lanes

        # ONE Tech-Lead per domain (each OwnershipMap owner; Wave A
        # `OwnershipMap.leads()`): a lead decomposes ONLY its own ownership units,
        # so its decomposition context stays focused on its slice. The task
        # id/seq/branch are GLOBAL — keyed off the unit and its merge-order index —
        # so the org fan-out changes WHO proposes a task, never its identity:
        #   * a single-owner map makes exactly one decomposition call over all
        #     units, byte-identical to the pre-fan-out Senior (regression guard);
        #   * a multi-owner map preserves the same task ids AND the same merge
        #     order (leads + units iterate in (owner, unit) order == merge_order);
        #     only the per-domain `criterion_ids` enrichment differs — the point.
        # Areas/leads are DERIVED from the OwnershipMap (Wave A); nothing about the
        # org is stored on the Task (a later wave records lineage where consumed).
        tasks: list[dict[str, Any]] = []
        notes: list[JournalEntry] = []
        for lead in leads_for_lane(ownership, lane):   # another lane owns the rest
            units = ownership.units_of(lead)
            prompt = json.dumps(
                {
                    "design_title": design.title,
                    "design_uri": design.design_uri,
                    "components": list(design.components),
                    "interfaces": list(design.interface_ids),
                    "ownership_units_in_merge_order": list(units),
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
            criteria_by_unit = _criteria_by_unit((result.parsed or {}).get("tasks", []))
            for unit in units:
                tid = task_id_for_unit(unit)
                seq = rt.seq(config, NODE_ID, f"task:{tid}", local=index_of[unit])
                spec_digest = content_digest({"unit": unit, "design": design.design_uri})
                task = Task(
                    id=tid,
                    spec_uri=f"cas://task/{spec_digest}",
                    criterion_ids=criteria_by_unit.get(unit, ()),
                    branch=f"task/{tid}",
                    seq=seq,
                )
                tasks.append(task.model_dump(mode="json"))
            # ADVISORY per-lead journal note (plan [11]; zero extra model call —
            # derived from data this lead already produced). Distinct seq key per
            # lead so `union_journal` keeps one entry per domain. Never a trust
            # input; no gate reads it.
            area = ownership.area_of(lead)
            notes.append(
                journal_entry(
                    node_id=NODE_ID,
                    role=f"Tech-Lead · {area}",
                    seq=rt.seq(config, NODE_ID, f"journal:{lead}"),
                    summary=(
                        f"Lead {lead!r} ({area}) decomposed {len(units)} "
                        "ownership unit(s) into tasks."
                    ),
                    assumptions=(f"Owns: {', '.join(units)}.",),  # a lead always owns ≥1 unit
                )
            )
        return {"tasks": tasks, "agent_journal": notes}

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
