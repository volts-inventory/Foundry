"""Shared agent-layer helpers (CR5 dedup).

Every agent layer (product / quality / architecture) needs the same two
primitives, so they live here once instead of being byte-copied per layer:

* :func:`emit_artifact` — wrap a typed frozen body as a content-addressed
  :class:`Artifact` *handle*. The body lives out-of-band behind the handle's
  ``uri`` (DS9 / LEARNINGS L4); only the small handle rides the ``artifacts``
  channel (``list[Artifact]`` folded by ``merge_by_uri``, keyed on ``.uri``).
  The ``uri`` is the body's ``content_identity()`` so distinct records get
  distinct handles and identical re-emissions on resume coalesce (idempotent,
  L7 — ``content_digest``, never builtin ``hash()``).

* :func:`cas_uri` — the ``cas://sha256/<hex>`` handle for an arbitrary
  out-of-band body (process-stable digest, L7).

:func:`content_schema` derives a constrained-output schema with node-owned
fields (``seq`` etc.) dropped; :func:`as_text` is the canonical, sort-stable
JSON serialization used to build deterministic prompts / body digests.
"""

from __future__ import annotations

import json
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
    """Wrap a typed frozen body as a content-addressed :class:`Artifact` handle.

    The body itself (the ``model``) lives out-of-band behind the returned
    handle's ``uri``; only the handle rides the ``artifacts`` channel (DS9 / L4).
    ``uri`` is the body's content identity, so DISTINCT records get distinct
    handles and identical re-emissions coalesce under ``merge_by_uri``.
    """
    return Artifact(
        kind=kind,
        uri=f"cas://sha256/{model.content_identity()}",
        produced_by=produced_by,
        seq=seq,
    )


def as_text(obj: Any) -> str:
    """Canonical, sort-stable JSON serialization (deterministic prompts/digests)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
