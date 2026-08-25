"""Stakeholder / Client Proxy node (agents.md Layer 1 — T2).

Stands in for the customer: holds the "why" and resolves ambiguity. Restates
intent, lists assumptions & unknowns, defines success in user terms, and flags
must-have vs nice-to-have. Input: the raw ``request``. Output: an
``IntentBrief`` (the body, wrapped as a content-addressed ``Artifact`` handle on
the ``artifacts`` channel).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import skip_advisory_in_autonomous
from foundry.agents.product._common import content_schema, emit_artifact
from foundry.schemas.product import IntentBrief

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "stakeholder_proxy"
# Haiku (T3): the IntentBrief is advisory/build-dead — no gate or downstream agent
# reads it (the PM works from request/spec); it only feeds the UI + checkpoint
# display, so a cheap tier is right for the narrative.
_TIER = "T3"

SOP = (
    "You are the Stakeholder / Client Proxy. Stand in for the customer; hold the "
    '"why" and resolve ambiguity. Follow this SOP, in order:\n'
    "1. Restate the intent in your own words.\n"
    "2. List assumptions and open unknowns.\n"
    "3. Define success in user-facing terms (not implementation terms).\n"
    "4. Flag each requirement as a must-have or a nice-to-have.\n"
    "Emit an IntentBrief. If a material ambiguity cannot be resolved from the "
    "request alone, record it under unknowns (it may trigger HITL). When a "
    "DESIGN SPEC is provided below, GROUND the intent in it (it is the detailed "
    "source of truth; the request only summarizes intent) rather than synthesizing "
    "from the one-line request alone."
)

# Content fields the model fills; the node owns `seq` (causal order), so it is
# excluded from the constrained-output schema and injected after parsing.
_SCHEMA = content_schema(IntentBrief)

# A provided design spec is truncated to this many characters in the intake
# prompt — deterministic, bounds token cost on a large design doc (task [8]).
# The full spec stays on the `spec` channel; this only caps what the model reads.
_SPEC_PROMPT_CAP = 24_000


def _intake_text(state: FactoryState) -> str:
    """The intake the Proxy works from: the raw ``request`` plus, when provided,
    the full design-spec document text (task [8]).

    The spec is ADDITIVE — a short ``request`` still summarizes intent while the
    spec grounds the detail. It is plain input data (no new trust surface): it
    INFORMS the IntentBrief, but the acceptance-criteria / spec-gate machinery
    downstream is unchanged.
    """
    request = str(state.get("request", ""))
    spec = str(state.get("spec", "") or "")
    if not spec.strip():
        return request
    body = spec[:_SPEC_PROMPT_CAP]
    return (
        f"REQUEST (intent summary):\n{request}\n\n"
        f"DESIGN SPEC (detailed source of truth):\n{body}"
    )


def make_stakeholder_proxy_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Stakeholder/Client Proxy node (raw request -> IntentBrief)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        if skip_advisory_in_autonomous(state):
            return {}  # intent_brief is read only by the UI checkpoint; a headless run has none
        seq = rt.seq(config, _NODE_ID, "artifact:intent_brief")
        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": _intake_text(state)}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        parsed = dict(result.parsed or {})
        brief = IntentBrief.model_validate({**parsed, "seq": seq})
        handle = emit_artifact(brief, "intent_brief", _NODE_ID, seq, store=rt.cas)
        return {"artifacts": [handle]}

    return _node
