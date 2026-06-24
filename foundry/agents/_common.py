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
from typing import TYPE_CHECKING, Any

from foundry.canonical import content_digest
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.journal import JournalEntry
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.durability.artifact_store import ArtifactStore


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


def persist_prose(text: str, *, store: ArtifactStore | None = None) -> str:
    """Mint the ``cas://`` handle for a narrative body AND persist the body.

    The PM/architect author prose (PRD body, design body, ADR rationale) that the
    doc set wants to inline. Historically the node only computed ``cas_uri(text)``
    (the handle) and DISCARDED ``text``, so the assembler resolved nothing and
    ``render.py`` emitted a dead handle (task [2]). This stores the body as an
    allowlisted :class:`~foundry.schemas.docs.ProseBody` UNDER that exact handle
    (``put_body_at``), so ``store.get_body(uri)`` round-trips the prose.

    Returns the ``cas_uri(text)`` handle UNCHANGED — identical to the historical
    ``*_uri`` value — so identity is byte-stable and the change is purely
    additive. When ``store`` is ``None`` (synthetic-resolver tests) this is
    mint-only: EXACTLY the historical behaviour.
    """
    uri = cas_uri(text)
    if store is not None:
        # Imported here (not at module top) so the agent-layer common helpers
        # don't take a hard import dep on the docs schema package.
        from foundry.schemas.docs import ProseBody

        store.put_body_at(uri, ProseBody(text=text))
    return uri


def emit_artifact(
    model: FrozenModel,
    kind: str,
    produced_by: str,
    seq: Seq,
    *,
    store: ArtifactStore | None = None,
) -> Artifact:
    """Wrap a typed frozen body as a content-addressed :class:`Artifact` handle.

    The body itself (the ``model``) lives out-of-band behind the returned
    handle's ``uri``; only the handle rides the ``artifacts`` channel (DS9 / L4).
    ``uri`` is the body's content identity, so DISTINCT records get distinct
    handles and identical re-emissions coalesce under ``merge_by_uri``.

    When ``store`` is provided (``rt.cas`` is set), the body is ALSO persisted
    via ``store.put_body`` so a real run's ``resolve`` can reconstitute it
    (DECISIONS D6) — the stored uri is IDENTICAL to the handle's, since both are
    ``cas://sha256/<model.content_identity()>``. When ``store`` is ``None`` this
    is mint-only — EXACTLY the historical behavior, so the synthetic-resolver
    tests are untouched (the change is purely additive / backward-compatible).
    """
    uri = f"cas://sha256/{model.content_identity()}"
    if store is not None:
        stored = store.put_body(model)
        # Invariant: the handle uri MUST equal the stored uri (both are the
        # body's content identity) — else a resolve would miss the body.
        assert stored == uri, f"store uri {stored!r} != handle uri {uri!r}"
    return Artifact(kind=kind, uri=uri, produced_by=produced_by, seq=seq)


def as_text(obj: Any) -> str:
    """Canonical, sort-stable JSON serialization (deterministic prompts/digests)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def journal_entry(
    *,
    node_id: str,
    role: str,
    seq: Seq,
    summary: str,
    assumptions: tuple[str, ...] = (),
    concerns: tuple[str, ...] = (),
    confidence: float | None = None,
) -> JournalEntry:
    """Build one ADVISORY :class:`JournalEntry` for the ``agent_journal`` channel.

    Reusable across every agent node: a node returns ``{"agent_journal": [entry]}``
    on its OWN channel — never on ``gates``/``gate_triage`` — to record how its turn
    went for the "interview the agent" affordance (:mod:`foundry.agents.interview`).

    HARD CONSTRAINT (plan [11]): the journal is STRICTLY ADVISORY observability —
    NEVER a trust input. No gate, ``merge_gates``, ``route_after_gates``, or
    ``admission`` reads ``agent_journal``; an entry cannot influence whether a
    build passes (agents propose, only signed gates decide, L5).

    Token cost is zero by default: the caller derives ``summary``/``assumptions``/
    ``concerns`` from data the node ALREADY has (the parsed completion, the
    artifacts it just minted) — NOT from a new model call. A richer model-authored
    narrative must be opt-in / cost-gated by the caller, default off.
    """
    return JournalEntry(
        node_id=node_id,
        role=role,
        summary=summary,
        assumptions=assumptions,
        concerns=concerns,
        confidence=confidence,
        seq=seq,
    )
