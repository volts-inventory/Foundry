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

from typing import Any

from foundry.canonical import content_digest
from foundry.schemas.base import Artifact, FrozenModel
from foundry.state.seq import Seq


def content_schema(model: type[FrozenModel], *, drop: tuple[str, ...] = ("seq",)) -> dict[str, Any]:
    """JSON schema for a model's *content* fields, with node-owned fields dropped.

    The node — not the model call — owns the causal-order ``seq`` (and any
    content-addressed ``*_uri`` body handle), so those fields are removed from
    the constrained-output schema the proxy enforces and injected after parsing.
    """
    schema = model.model_json_schema()
    props = {k: v for k, v in schema.get("properties", {}).items() if k not in drop}
    required = [r for r in schema.get("required", []) if r not in drop]
    return {**schema, "properties": props, "required": required}


def cas_uri(body: Any) -> str:
    """Content-addressed ``cas://sha256/<hex>`` handle for an out-of-band body.

    Process-stable (``content_digest``, never builtin ``hash()`` — LEARNINGS L7),
    so the same body yields the same handle across a kill-then-resume and the
    keyed channel union dedupes the re-emission.
    """
    return f"cas://sha256/{content_digest(body)}"


def emit_artifact(model: FrozenModel, kind: str, produced_by: str, seq: Seq) -> Artifact:
    """Wrap a typed product body as a content-addressed :class:`Artifact` handle.

    The body itself (the ``model``) lives out-of-band behind the returned
    handle's ``uri``; only the handle rides the ``artifacts`` channel (DS9 / L4).
    ``uri`` is the body's content identity so identical re-emissions coalesce.
    """
    return Artifact(
        kind=kind,
        uri=f"cas://sha256/{model.content_identity()}",
        produced_by=produced_by,
        seq=seq,
    )
