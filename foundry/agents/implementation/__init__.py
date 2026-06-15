"""Implementation layer (agents.md Layer 3) — node factories.

Two ``make_<agent>_node(rt: AgentRuntime, ...) -> NodeFn`` factories, following
the shared seam (LEARNINGS AR1-AR4):

* :func:`make_senior_engineer_node` — decompose ``SystemDesign`` +
  ``OwnershipMap`` into a deterministic, ownership-ordered ``Task[]`` (one
  ``task/<id>`` branch each, with ``criterion_ids`` traceability), emitted to
  the ``tasks`` channel via ``rt.proxy.complete`` (single egress, L6). The
  integration conflict-resolution path is typed but stubbed
  (:func:`stub_conflict_resolution`).
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
    make_junior_engineer_node,
    pinned_texts,
)
from foundry.agents.implementation.senior_engineer import (
    make_senior_engineer_node,
    stub_conflict_resolution,
)

__all__ = [
    "idempotency_key",
    "make_junior_engineer_node",
    "make_senior_engineer_node",
    "pinned_texts",
    "stub_conflict_resolution",
]
