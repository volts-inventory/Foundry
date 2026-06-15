"""Layer-2 (Architecture & Design) agent node factories (agents.md Layer 2).

Exports the principal architect node factory plus the two conditional Layer-2
nodes (domain specialist / UX design) and their entry predicates, all following
the shared ``make_<agent>_node(rt) -> NodeFn`` convention (``_runtime.py``).
"""

from __future__ import annotations

from foundry.agents.architecture.conditional import (
    make_domain_specialist_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)
from foundry.agents.architecture.principal_architect import (
    make_principal_architect_node,
)

__all__ = [
    "make_domain_specialist_node",
    "make_principal_architect_node",
    "make_ux_design_node",
    "should_enter_domain_specialist",
    "should_enter_ux_design",
]
