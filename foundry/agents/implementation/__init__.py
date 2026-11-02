"""Implementation layer (agents.md Layer 3) — node factories.

* :func:`make_engineering_node` — the planner's tickets -> tasks, built wave by
  wave through a per-specialty-limited pool (``tickets.py``).
* :func:`make_fixer_node` — one coding session that repairs the integrated tree
  after a failed build/test gate.

Neither node mints a ``GateResult`` — the adapter returns advisory
``GateTriage`` only (L5); lineage is proxy-observed, never self-reported.
"""

from __future__ import annotations

from foundry.agents.implementation.junior_engineer import (
    idempotency_key,
    make_fixer_node,
    pinned_texts,
    ready_tasks,
)
from foundry.agents.implementation.senior_engineer import DESIGN_KIND, OWNERSHIP_KIND
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
    "has_tickets",
    "idempotency_key",
    "make_engineering_node",
    "make_fixer_node",
    "next_wave",
    "pinned_texts",
    "read_tickets",
    "ready_tasks",
    "task_from_ticket",
]
