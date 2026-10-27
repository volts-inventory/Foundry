"""Shared implementation-layer identifiers.

``NODE_ID`` is the seq namespace ticket tasks are minted under (``tickets.py``):
changing it would change every ticket task's ``Seq`` and so its merge order on
resume. The ``*_KIND`` tags are the ``Artifact.kind`` values the planner stamps
on its SystemDesign / OwnershipMap handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from foundry.schemas.base import Artifact

    # Reconstitutes a typed body from its content-addressed artifact handle.
    ArtifactResolver = Callable[[Artifact], Any]

__all__ = ["DESIGN_KIND", "NODE_ID", "OWNERSHIP_KIND"]

NODE_ID = "senior_engineer"

DESIGN_KIND = "system_design"
OWNERSHIP_KIND = "ownership_map"
