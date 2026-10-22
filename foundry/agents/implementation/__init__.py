"""Implementation layer (agents.md Layer 3) — node factories.

Two ``make_<agent>_node(rt: AgentRuntime, ...) -> NodeFn`` factories, following
the shared seam (LEARNINGS AR1-AR4):

* :func:`make_senior_engineer_node` — decompose ``SystemDesign`` +
  ``OwnershipMap`` into a deterministic, ownership-ordered ``Task[]`` (one
  ``task/<id>`` branch each, with ``criterion_ids`` traceability), emitted to
  the ``tasks`` channel via ``rt.proxy.complete`` (single egress, L6). The
  integration conflict-resolution path is typed but stubbed
  (:func:`stub_conflict_resolution`).
* :func:`make_engineering_node` — the ticket path: tickets -> tasks, built wave
  by wave through a per-specialty-limited pool (``tickets.py``).
* :func:`make_junior_engineer_node` — take ONE ``Task``, build a ``TaskPacket``
  with the pinned constraints injected VERBATIM + FIRST (arch §2), run the
  swappable substrate (``rt.adapter``) inside a ``Sandbox``, and emit a
  content-addressed ``FeatureDiff`` to ``artifacts``. Idempotent retry resets
  the worktree to the base tree (supersession, not edit — L4) and keys the diff
  by ``content_digest`` (L7), never builtin ``hash()``.

Neither node mints a ``GateResult`` — the adapter returns advisory
``GateTriage`` only (L5); lineage is proxy-observed, never self-reported.
"""

from __future__ import annotations

from foundry.agents.implementation.junior_engineer import (
    idempotency_key,
    make_fixer_node,
    make_junior_engineer_node,
    pinned_texts,
    ready_tasks,
    tasks_for_lane,
)
from foundry.agents.implementation.senior_engineer import (
    DESIGN_KIND,
    OWNERSHIP_KIND,
    ArtifactResolverError,
    lanes_with_leads,
    leads_for_lane,
    make_senior_engineer_node,
    stub_conflict_resolution,
)
from foundry.agents.implementation.tickets import (
    DEFAULT_WORKERS,
    TICKET_KIND,
    has_tickets,
    make_engineering_node,
    next_wave,
    read_tickets,
    task_from_ticket,
)

__all__ = [
    "DEFAULT_WORKERS",
    "DESIGN_KIND",
    "OWNERSHIP_KIND",
    "TICKET_KIND",
    "ArtifactResolverError",
    "has_tickets",
    "idempotency_key",
    "lanes_with_leads",
    "leads_for_lane",
    "make_engineering_node",
    "make_fixer_node",
    "make_junior_engineer_node",
    "make_senior_engineer_node",
    "next_wave",
    "pinned_texts",
    "read_tickets",
    "ready_tasks",
    "stub_conflict_resolution",
    "task_from_ticket",
    "tasks_for_lane",
]
