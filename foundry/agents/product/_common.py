"""Shared helpers for the Product-layer node factories.

Two concerns recur across all four product nodes and live here so each factory
stays a faithful SOP + a thin model-call body:

* **Out-of-band bodies (DS9 / LEARNINGS L4).** A product node's real output is a
  rich typed model (``IntentBrief`` / ``PRD`` / ``AcceptanceCriterion`` /
  ``CriteriaCritique`` / ``SprintPlan``). The ``artifacts`` channel is
  ``list[Artifact]`` folded by ``merge_by_uri`` (keys on ``.uri``), and these
  typed models are NOT ``Artifact`` subclasses and carry no ``.uri`` — so a node
  never puts a body inline in the channel. Instead :func:`emit_artifact` stores
  the typed body content-addressed and writes a small :class:`Artifact` *handle*
  (``cas://sha256/<hex>``) to the channel. The handle's ``uri`` is the body's
  content digest, so an identical re-emission on resume coalesces under
  ``merge_by_uri`` (idempotent, L7 — ``content_digest``, never ``hash()``).

* **Deterministic Seq + content-addressed ``*_uri`` rebinding.** The model emits
  the artifact's *content* fields; the node owns the causal-order ``Seq`` (via
  ``rt.seq``) and any content-addressed ``*_uri`` body handle. The pattern is to
  ``model_validate`` the proxy's parsed dict with a placeholder seq/uri, then
  rebind via ``model_copy`` — see each factory. :func:`cas_uri` mints the
  process-stable handle for those large bodies.
"""

from __future__ import annotations

# CR5: the artifact-handle helpers are agents-level shared primitives. They live
# in ``foundry.agents._common`` and are re-exported here so the product factories'
# existing import path (``from foundry.agents.product._common import ...``) stays
# stable; there is no second copy to drift.
from foundry.agents._common import cas_uri, content_schema, emit_artifact, persist_prose

__all__ = ["cas_uri", "content_schema", "emit_artifact", "persist_prose"]
