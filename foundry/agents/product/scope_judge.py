"""Scope Judge node (Layer 1 — T3, the cheapest tier).

Classifies how much system the request is actually asking for and writes a
:class:`ScopeDecision` to the ``scope_tier`` single-writer channel. The PM and
the Principal Architect read that channel to cap what they PROPOSE (via
``agents._common.scope_directive``).

Why the node exists: without it every request gets the same treatment, so a
one-line "demo URL shortener" acquires interface contracts, a multi-lane
ownership map, and a full criteria set — the floor the product/architecture SOPs
enforce has no ceiling above it.

**Two-stage, cheapest-first.** A deterministic keyword pre-filter runs first: a
request carrying no smallness word at all cannot be DEMO, so the node returns
STANDARD *without a model call*. Only a request that DOES carry one reaches the
model, whose job is the part that actually needs judgment — telling "a simple
URL shortener" (demo) from "a simple ledger for production payroll" (not). Most
runs therefore cost zero extra tokens, and the model is spent only on the
genuinely ambiguous case.

Trust posture: this node writes NOTHING a gate reads. ``scope_tier`` is a prompt
input; ``ScopeDecision.relaxes_integrity()`` is False by construction, and the
deterministic gates a DEMO build must pass are identical to a STANDARD one
(agents propose, only signed gates decide, L5). A mis-classification cannot
weaken a release — it can only make the factory build too little.

Bias: STANDARD unless proven otherwise, in BOTH stages. The product layer runs
exactly once per run (both fix loops re-enter at architecture / implementation,
never at product), so a wrong DEMO verdict is NOT self-correcting — the criteria
stay capped for the whole run. A wrong STANDARD only costs tokens. Every
tie-break in this module resolves to STANDARD for that reason.
"""

from __future__ import annotations

import re
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
    "You are the Scope Judge. The request below contains a word suggesting it "
    "may be small. Decide whether it is genuinely a demonstration or whether "
    "that word is incidental to a real system.\n"
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

# Stage 1: the words that make DEMO even reachable. A request containing NONE of
# these is STANDARD deterministically — no model call, no tokens. Matched on word
# boundaries so "sampler" or "justify" don't trip it. Deliberately a NECESSARY
# condition only: a hit sends the request to the model, it does not decide it.
_SMALLNESS_WORDS = (
    "demo",
    "demonstration",
    "toy",
    "example",
    "sample",
    "prototype",
    "spike",
    "exercise",
    "playground",
    "scratch",
    "simple",
    "basic",
    "minimal",
    "tiny",
    "trivial",
    "quick",
    "just",
    "small",
)
_SMALLNESS_RE = re.compile(r"\b(" + "|".join(_SMALLNESS_WORDS) + r")\b", re.IGNORECASE)

# A request long enough to describe a real system is not a demo however it is
# worded — a backstop against one stray "simple" in a long brief. Deterministic,
# so it cannot itself be talked out of by the request's own text.
_DEMO_REQUEST_CHAR_CAP = 600


def _request_text(state: FactoryState) -> str:
    """What the judge classifies: the raw request. The design spec is
    deliberately EXCLUDED — supplying a spec at all is a standard-scope signal
    (see :func:`_forced_standard`), so there is no case where the spec's text
    would change the verdict, and reading it would blow this node's token cost."""
    return str(state.get("request", ""))


def _forced_standard(state: FactoryState) -> str | None:
    """The deterministic STANDARD verdict + its reason, or None to ask the model.

    Stage 1 of the two-stage judge. These rules only ever TIGHTEN (demo ->
    standard), never the reverse, so neither the model nor the request's own
    wording can talk the factory into building less than they allow."""
    request = _request_text(state)
    if str(state.get("spec", "") or "").strip():
        return "a design spec was supplied, which is a standard-scope request"
    if len(request) > _DEMO_REQUEST_CHAR_CAP:
        return (
            f"the request is longer than {_DEMO_REQUEST_CHAR_CAP} characters, "
            "so it describes a real system rather than a demo"
        )
    if not _SMALLNESS_RE.search(request):
        return "the request asks for a system without qualifying it as small"
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
