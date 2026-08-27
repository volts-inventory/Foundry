"""Engineering / Program Manager node (agents.md Layer 1 — T2).

Owns planning, sequencing, budget, and risk. Following the SOP it decomposes the
PRD into milestones, estimates, **refines the absolute ``usd_cap``** seeded at
Triage (per-route — the cap is a function of the classified route, governance
§7), builds a risk register, and defines done-for-this-iteration.

Input: the ``PRD`` (and the ``route`` Triage classified). Output: a
``SprintPlan`` carried as a content-addressed handle on the ``artifacts``
channel; the risk-register body lives out-of-band behind ``risk_register_uri``
(DS9 / LEARNINGS L4). The refined ``usd_cap_microusd`` rides inside the
SprintPlan as the per-route budget hint — the single-writer ``budget`` ledger is
owned by the Budget Controller, so this node only *proposes* the cap, never
writes the ledger channel. Monetary values are integer micro-USD, never floats
(architecture.md §8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import skip_ui_only_author
from foundry.agents.product._common import cas_uri, emit_artifact
from foundry.schemas.product import SprintPlan

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "eng_program_mgr"
# Haiku (T3): the SprintPlan is advisory/build-dead — the budget ledger is seeded at
# triage and nothing reads the proposed cap; it only feeds the UI + checkpoint
# display, so a cheap tier is right for the narrative.
_TIER = "T3"

# Per-route default caps (micro-USD) the PLAN refines from the Triage seed
# (governance §7). A greenfield build carries the widest cap; a bugfix the
# tightest. The model proposes a refinement WITHIN this route-keyed envelope.
_ROUTE_CAP_MICROUSD: dict[str, int] = {
    "greenfield": 50_000_000,
    "evolution": 25_000_000,
    "bugfix": 5_000_000,
    "spike": 10_000_000,
}
_DEFAULT_CAP_MICROUSD = 25_000_000

SOP = (
    "You are the Engineering / Program Manager (Layer 1). Own planning, "
    "sequencing, budget, and risk. Follow this SOP, in order:\n"
    "1. Decompose the PRD into ordered milestones.\n"
    "2. Estimate, then refine the absolute usd_cap seeded at Triage (integer "
    "micro-USD; per-phase/per-loop sub-budgets apply downstream).\n"
    "3. Build a risk register (the body, content-addressed out-of-band).\n"
    "4. Define done-for-this-iteration.\n"
    "Respond in the constrained JSON schema. usd_cap_microusd is an integer "
    "number of micro-USD, never a float."
)

# The model fills the plan content; the node owns the Seq and the
# content-addressed risk-register handle, so those are injected after parsing.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "milestones": {"type": "array", "items": {"type": "string"}},
        "usd_cap_microusd": {"type": "integer", "minimum": 0},
        "risk_register_body": {"type": "string"},
        "done_definition": {"type": "string"},
    },
    "required": ["milestones", "usd_cap_microusd", "done_definition"],
    "additionalProperties": False,
}


def _route_cap(state: FactoryState) -> int:
    """The per-route budget envelope the refinement is clamped into (governance
    §7): the Triage-seeded cap is route-dependent and never silently widened."""
    route = state.get("route")
    return _ROUTE_CAP_MICROUSD.get(str(route), _DEFAULT_CAP_MICROUSD)


def make_eng_program_mgr_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Eng/Program Manager node (PRD -> SprintPlan + budget hint)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        if skip_ui_only_author(state):
            return {}  # sprint_plan is read only by the UI checkpoint; a headless run has none
        seq = rt.seq(config, _NODE_ID, "artifact:sprint_plan")
        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": state.get("request", "")}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        # Clamp the proposed refinement into the route's Triage-seeded envelope:
        # the PLAN refines DOWN the absolute cap, it never widens it (gov §7).
        proposed = int(payload.get("usd_cap_microusd", 0))
        cap = min(proposed, _route_cap(state)) if proposed > 0 else _route_cap(state)

        plan = SprintPlan(
            milestones=tuple(str(m) for m in payload.get("milestones", ())),
            usd_cap_microusd=cap,
            risk_register_uri=cas_uri(str(payload.get("risk_register_body", ""))),
            done_definition=str(payload.get("done_definition", "")),
            seq=seq,
        )

        handle = emit_artifact(plan, "sprint_plan", _NODE_ID, seq, store=rt.cas)
        return {"artifacts": [handle]}

    return _node
