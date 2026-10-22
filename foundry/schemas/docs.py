"""Prose bodies behind the planner's ``*_uri`` handles (PRD / design / ADR /
interface-contract narrative)."""

from __future__ import annotations

from foundry.schemas.base import FrozenModel

__all__ = ["ProseBody"]


class ProseBody(FrozenModel):
    """The out-of-band narrative body behind a ``*_uri`` handle.

    Lets the body round-trip through the
    :class:`~foundry.durability.artifact_store.ArtifactStore` (an allowlisted
    frozen model) so a consumer (e.g. a ticket's interface-contract context) can
    resolve the text, not just the handle.

    It carries NO ``seq``: it is keyed by the body digest the producing node
    already minted as the handle (``cas_uri(text)``), NOT by causal order — the
    node persists it via ``put_body_at(handle_uri, ProseBody(text=text))`` so
    ``content_identity()`` is irrelevant to its storage key.
    """

    text: str = ""
