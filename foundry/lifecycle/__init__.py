"""Product-lifecycle modules wired into the Workshop-UI workflow.

The pipeline ships a release; the LIFECYCLE owns software over its life. The
UI-wired concerns live here:

* :mod:`foundry.lifecycle.milestones` — plans an ordered milestone list and
  runs the full factory per milestone, chaining each released tree as the next
  milestone's base.
* :mod:`foundry.lifecycle.autopilot` — Director proposes, a design panel votes,
  the winner becomes the next gated iteration (fail-closed on panel decline).
* :mod:`foundry.lifecycle.git_export` — exports a released tree's build stages
  as a git history.

This package is import-cheap (no heavy imports at module import time).
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
