"""Stakeholder / Client Proxy node (agents.md Layer 1 — T2).

Stands in for the customer: holds the "why" and resolves ambiguity. Restates
intent, lists assumptions & unknowns, defines success in user terms, and flags
must-have vs nice-to-have. Input: the raw ``request``. Output: an
``IntentBrief`` (the body, wrapped as a content-addressed ``Artifact`` handle on
the ``artifacts`` channel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.product._common import content_schema, emit_artifact
from foundry.schemas.product import IntentBrief

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "stakeholder_proxy"
_TIER = "T2"

SOP = (
    "You are the Stakeholder / Client Proxy. Stand in for the customer; hold the "
    '"why" and resolve ambiguity. Follow this SOP, in order:\n'
    "1. Restate the intent in your own words.\n"
    "2. List assumptions and open unknowns.\n"
    "3. Define success in user-facing terms (not implementation terms).\n"
    "4. Flag each requirement as a must-have or a nice-to-have.\n"
    "Emit an IntentBrief. If a material ambiguity cannot be resolved from the "
    "request alone, record it under unknowns (it may trigger HITL)."
)

# Content fields the model fills; the node owns `seq` (causal order), so it is
# excluded from the constrained-output schema and injected after parsing.
_SCHEMA = content_schema(IntentBrief)


def make_stakeholder_proxy_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Stakeholder/Client Proxy node (raw request -> IntentBrief)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        seq = rt.seq(config, _NODE_ID, "artifact:intent_brief")
        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": state["request"]}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        parsed = dict(result.parsed or {})
        brief = IntentBrief.model_validate({**parsed, "seq": seq})
        handle = emit_artifact(brief, "intent_brief", _NODE_ID, seq)
        return {"artifacts": [handle]}

    return _node
