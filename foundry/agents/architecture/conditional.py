"""Conditional Layer-2 nodes: Domain Specialist + UX/Design (agents.md).

Both are *conditional* nodes (agents.md Layer 2): they contribute only when the
PM-declared ``required_capabilities`` match a registry capability. Otherwise the
node is a no-op — it returns ``{}`` (no channel writes), so every downstream
consumer tolerates its ABSENCE (the artifacts are declared optional).

The next wave wires these into the role subgraph with conditional edges; this
module ships the node factories AND the matching entry predicates
(``should_enter_<role>``) those edges branch on, so the predicate and the no-op
guard inside the node agree on one source of truth.

* Domain Specialist (T1/T2) — entered when capabilities intersect a domain pack
  (e.g. ``regulated``, ``ml``, ``fintech``, ``healthcare``); emits a domain
  design addendum (here: an addendum artifact handle). Consumers treat it as
  optional.
* UX / Design (T2) — entered for UI projects: when ``required_capabilities``
  contains ``ui`` (or ``frontend``); emits a ``ux_spec`` artifact handle.

Neither emits a ``GateResult`` (LEARNINGS L5) nor imports the Anthropic SDK
(LEARNINGS L6 — all egress via ``rt.proxy``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.canonical import content_digest
from foundry.schemas.base import Artifact

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

DOMAIN_SPECIALIST_NODE_ID = "domain_specialist"
UX_DESIGN_NODE_ID = "ux_design"

# Capability triggers (agents.md Layer 2 / architecture.md §7). Broad on
# purpose: a false entry costs one cheap addendum, a missed entry drops needed
# expertise from the design.
_DOMAIN_CAPABILITIES = frozenset(
    {"regulated", "ml", "data", "mobile", "embedded", "fintech", "healthcare"}
)
_UX_CAPABILITIES = frozenset({"ui", "frontend", "ux"})

# Conditional nodes default to T2 (cheaper) unless escalated by the registry;
# domain packs may pin T1. Kept here so the subgraph wiring reads one source.
DOMAIN_TIER = "T2"
UX_TIER = "T2"


def _capabilities(state: FactoryState) -> frozenset[str]:
    return frozenset(state.get("required_capabilities") or ())


def should_enter_domain_specialist(state: FactoryState) -> bool:
    """True when ``required_capabilities`` intersect a domain-pack capability.

    The conditional edge the subgraph adds branches on exactly this predicate;
    the node's own guard reuses it so edge and node never disagree.
    """
    return bool(_capabilities(state) & _DOMAIN_CAPABILITIES)


def should_enter_ux_design(state: FactoryState) -> bool:
    """True for UI projects: ``required_capabilities`` contains a UI capability."""
    return bool(_capabilities(state) & _UX_CAPABILITIES)


def make_domain_specialist_node(rt: AgentRuntime) -> NodeFn:
    """Domain Specialist (conditional). No-op unless a domain capability matches.

    On a match it retrieves nothing new (it consumes the SystemDesign) and emits
    a domain design addendum artifact handle; downstream treats it as optional.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        if not should_enter_domain_specialist(state):
            return {}  # conditional no-op: no writes, absence tolerated downstream
        seq = rt.seq(config, DOMAIN_SPECIALIST_NODE_ID, "artifact:domain_addendum")
        matched = sorted(_capabilities(state) & _DOMAIN_CAPABILITIES)
        addendum = _addendum("domain_addendum", matched, seq, DOMAIN_SPECIALIST_NODE_ID)
        return {"artifacts": [addendum]}

    return _node


def make_ux_design_node(rt: AgentRuntime) -> NodeFn:
    """UX / Design (conditional). No-op unless a UI capability is required.

    On a match it emits a ``ux_spec`` artifact handle; downstream tolerates its
    absence on non-UI projects.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        if not should_enter_ux_design(state):
            return {}  # conditional no-op: no writes, absence tolerated downstream
        seq = rt.seq(config, UX_DESIGN_NODE_ID, "artifact:ux_spec")
        matched = sorted(_capabilities(state) & _UX_CAPABILITIES)
        return {"artifacts": [_addendum("ux_spec", matched, seq, UX_DESIGN_NODE_ID)]}

    return _node


def _addendum(kind: str, matched: list[str], seq: Seq, produced_by: str) -> Artifact:
    """A content-addressed handle for a conditional addendum body (DS9 / L4).

    The body digest is process-stable (LEARNINGS L7: ``content_digest``).
    """
    body = as_text({"kind": kind, "matched_capabilities": matched})
    return Artifact(
        kind=kind,
        uri=f"cas://{kind}/{content_digest(body)}",
        produced_by=produced_by,
        seq=seq,
    )
