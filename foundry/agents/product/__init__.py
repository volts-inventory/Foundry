"""Product & Planning layer (agents.md Layer 1) — node factories.

Five ``make_<agent>_node(rt: AgentRuntime) -> NodeFn`` factories, in SOP order:

* :func:`make_scope_judge_node` — raw request -> ``scope_tier`` (how much system
  the request asks for; caps what the PM/architect propose, never what the gates
  require).
* :func:`make_stakeholder_proxy_node` — raw request -> ``IntentBrief``.
* :func:`make_product_manager_node` — intent -> ``PRD`` + ``AcceptanceCriterion[]``.
* :func:`make_criteria_completeness_critic_node` — PRD + criteria ->
  ``CriteriaCritique`` and an advisory ``GateTriage`` that flags any
  security/abuse omission as Spec-gate blocking (the Spec gate computes the
  actual verdict; the critic never mints a ``GateResult`` — LEARNINGS L5).
* :func:`make_eng_program_mgr_node` — PRD -> ``SprintPlan`` (+ budget hint).

Every node follows the shared seam (LEARNINGS AR1-AR4): mint a deterministic
``Seq`` via ``rt.seq``, call ``rt.proxy.complete`` for the single egress (L6),
content-address large bodies behind ``*_uri`` handles (DS9 / L4), and write
channel deltas only — never a ``GateResult`` to ``gates``.
"""

from __future__ import annotations

from foundry.agents.product.criteria_critic import (
    make_criteria_completeness_critic_node,
)
from foundry.agents.product.eng_program_mgr import make_eng_program_mgr_node
from foundry.agents.product.product_manager import make_product_manager_node
from foundry.agents.product.scope_judge import make_scope_judge_node
from foundry.agents.product.stakeholder_proxy import make_stakeholder_proxy_node

__all__ = [
    "make_criteria_completeness_critic_node",
    "make_eng_program_mgr_node",
    "make_product_manager_node",
    "make_scope_judge_node",
    "make_stakeholder_proxy_node",
]
