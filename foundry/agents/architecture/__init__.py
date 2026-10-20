"""Layer-2 (Architecture & Design) agent node factories (agents.md Layer 2).

Exports the two deterministic conditional Layer-2 nodes (domain specialist / UX
design) and their entry predicates, following the shared
``make_<agent>_node(rt) -> NodeFn`` convention (``_runtime.py``). The design
itself is authored by :mod:`foundry.agents.planner`.
"""

from __future__ import annotations

from foundry.agents.architecture.conditional import (
    make_domain_specialist_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)

__all__ = [
    "make_domain_specialist_node",
    "make_ux_design_node",
    "should_enter_domain_specialist",
    "should_enter_ux_design",
]
