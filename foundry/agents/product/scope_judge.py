"""Scope Judge node (Layer 1 — T3, the cheapest tier).

Classifies how much system the request is actually asking for and writes a
:class:`ScopeDecision` to the ``scope_tier`` single-writer channel. The PM and
the Principal Architect read that channel to cap what they PROPOSE (via
``agents._common.scope_directive``).

Why the node exists: without it every request gets the same treatment, so a
one-line "demo URL shortener" acquires interface contracts, a multi-lane
ownership map, and a full criteria set — the floor the product/architecture SOPs
enforce has no ceiling above it.

One deterministic short-circuit only: a run supplied with a design spec is
STANDARD without a model call, because supplying a spec at all is the signal.
Everything else goes to the model. An earlier draft also gated the model call
behind a keyword regex; it was cut because a keyword list is a hard recall
ceiling ("proof of concept", "throwaway", "hello world" all read as STANDARD and
the miss is invisible), and because one T3 call is noise beside the dozens of
T1/T2 calls a run already spends.

Trust posture: this node writes nothing a gate reads. ``scope_tier`` is a prompt
input, ``ScopeDecision.relaxes_integrity()`` is False by construction, and the
gate SET a DEMO build faces is identical to a STANDARD one (agents propose, only
signed gates decide, L5). That is a claim about the gate set, NOT about assurance
strength: acceptance criteria are the specification the gates verify against, so
capping their count genuinely thins what gets checked. DEMO is a weaker build
that passes the same gates — not an equally-assured one.

Bias: STANDARD unless proven otherwise. The product layer runs exactly once per
run (both fix loops re-enter at architecture / implementation, never at product),
so a wrong DEMO verdict is NOT self-correcting — the criteria stay capped for the
whole run. A wrong STANDARD only costs tokens. Every tie-break in this module
resolves to STANDARD for that reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import journal_entry
from foundry.schemas.governance import ScopeDecision, ScopeTier

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "scope_judge"
_TIER = "T3"  # a two-way classification; the cheapest tier is the right one

SOP = (
    "You are the Scope Judge. Decide how much system the request below is asking "
    "for, so the factory builds at the right size.\n"
    "- 'demo': the request is for something to look at or learn from — a demo, "
    "toy, example, sample, prototype, spike, exercise, or a small illustrative "
    "build. Nobody depends on it.\n"
    "- 'standard': everything else, including a request that merely calls itself "
    "simple. 'A simple payroll ledger' is a real system.\n"
    "Rules, in order:\n"
    "1. A short request is not a small request. Judge the SUBJECT, not length.\n"
    "2. A request naming real users, production, deployment, scale, compliance, "
    "money, or persistence of real data is 'standard' however it is qualified.\n"
    "3. When in doubt, return 'standard'. Under-building is not recoverable "
    "later in the run; over-building only costs time.\n"
    "4. Quote the exact phrases you relied on under signals.\n"
    "Respond in the constrained JSON schema."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": ["demo", "standard"]},
        "rationale": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tier", "rationale"],
    "additionalProperties": False,
}

def _request_text(state: FactoryState) -> str:
    """What the judge classifies: the raw request. The design spec is
    deliberately EXCLUDED — supplying a spec at all already decides the verdict
    (see :func:`_forced_standard`), so its text could never change the outcome
    and reading it would only inflate this node's token cost."""
    return str(state.get("request", ""))


def _forced_standard(state: FactoryState) -> str | None:
    """The deterministic STANDARD verdict + its reason, or None to ask the model.

    One rule, and it only ever TIGHTENS (demo -> standard): a run given a design
    spec is asking for the system that spec describes, whatever the one-line
    request calls it."""
    if str(state.get("spec", "") or "").strip():
        return "a design spec was supplied, which is a standard-scope request"
    return None


def make_scope_judge_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Scope Judge node (raw request -> ``scope_tier``)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        # Already decided — an operator `--scope` pin, or a re-entry after a
        # resume. Write nothing, spend nothing. Keyed on PRESENCE, not on
        # `source`, so it is idempotent under any re-entry.
        if state.get("scope_tier") is not None:
            return {}

        seq = rt.seq(config, _NODE_ID, "scope_tier")
        forced = _forced_standard(state)
        if forced is not None:
            decision = ScopeDecision(
                tier=ScopeTier.STANDARD,
                rationale=f"Standard scope: {forced}.",
                seq=seq,
            )
        else:
            result = rt.proxy.complete(
                _NODE_ID,
                _TIER,
                SOP,
                [{"role": "user", "content": _request_text(state)}],
                purpose="ideation",
                schema=_SCHEMA,
            )
            payload = result.parsed or {}
            # An unparseable or absent completion must not silently cap the
            # build: anything that is not exactly "demo" is STANDARD.
            raw_tier = str(payload.get("tier", "")).strip().lower()
            tier = ScopeTier.DEMO if raw_tier == ScopeTier.DEMO.value else ScopeTier.STANDARD
            decision = ScopeDecision(
                tier=tier,
                rationale=str(payload.get("rationale", "")),
                signals=tuple(str(s) for s in payload.get("signals", ())),
                seq=seq,
            )

        note = journal_entry(
            node_id=_NODE_ID,
            role="Scope Judge",
            seq=rt.seq(config, _NODE_ID, "journal"),
            summary=f"Judged scope {decision.tier.value!r}: {decision.rationale}",
            assumptions=(f"Classified from request: {_request_text(state)[:120]}",),
            concerns=(
                ("Demo scope caps the build; the product layer does not re-run.",)
                if decision.tier is ScopeTier.DEMO
                else ()
            ),
        )
        return {"scope_tier": decision, "agent_journal": [note]}

    return _node
