"""Product Manager node (agents.md Layer 1 — T2).

Turns intent into a buildable, testable specification. Following the SOP, the
node writes a PRD, user stories, and **acceptance criteria** (each a testable
Given/When/Then with its own ``criterion_id``), declares ``required_capabilities``
(which drives downstream UX/specialist routing), marks high-risk criteria (for
stronger oracles), and prioritizes.

Input: the ``IntentBrief`` (read from the latest ``intent_brief`` artifact
handle, falling back to the raw ``request``). Output: a ``PRD`` plus the
``AcceptanceCriterion[]`` it declares — every body content-addressed behind a
``*_uri`` / handle, never inline in the channel (DS9 / LEARNINGS L4). The PRD
declares its criteria by ``criterion_id`` so traceability is load-bearing
downstream (QA derives tests with ``criterion_id`` traceability, agents.md
Layer 4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.product._common import emit_artifact, journal_entry, persist_prose
from foundry.schemas.product import PRD, AcceptanceCriterion, Criticality

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

_NODE_ID = "product_manager"
_TIER = "T2"

SOP = (
    "You are the Product Manager (Layer 1). Turn the intent into a buildable, "
    "testable specification. Follow this SOP, in order:\n"
    "1. Write the PRD body (problem, user stories, prioritization).\n"
    "2. Derive acceptance criteria: each a testable Given/When/Then, each with a "
    "stable criterion_id, each with an oracle_hint describing how a "
    "runner-evaluable test decides it.\n"
    "3. Mark high-risk criteria as 'high-risk' (they drive stronger oracles), the "
    "rest 'normal'.\n"
    "4. Flag any security/abuse criterion with security_abuse=true.\n"
    "5. Declare required_capabilities (e.g. 'ui', 'ml') — these drive routing.\n"
    "When a DESIGN SPEC is provided below, derive the PRD and acceptance criteria "
    "FROM it (it is the detailed source of truth) rather than synthesizing from the "
    "one-line request alone. Respond in the constrained JSON schema."
)

# A provided design spec is truncated to this many characters in the intent
# prompt — deterministic, bounds token cost on a large design doc (task [8]).
_SPEC_PROMPT_CAP = 24_000

# The model fills the content; the node owns the Seq (causal order), the PRD
# body's CAS handle, and the PRD's criterion_ids list, so those are excluded
# from the constrained-output schema and injected after parsing.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "prd_body": {"type": "string"},
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "criticality": {
                        "type": "string",
                        "enum": ["normal", "high-risk"],
                    },
                    "oracle_hint": {"type": "string"},
                    "security_abuse": {"type": "boolean"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "prd_body", "acceptance_criteria"],
    "additionalProperties": False,
}


def _intent_text(state: FactoryState) -> str:
    """The intent the PM works from: the raw request (the IntentBrief body lives
    out-of-band behind its handle; the request is the in-channel ground truth),
    plus the full design-spec document text when one was provided (task [8]).

    The spec is ADDITIVE and is plain input data — it GROUNDS the PRD/criteria the
    PM derives, but never replaces the acceptance-criteria or spec-gate machinery
    downstream (agents propose, only signed gates decide, L5)."""
    request = str(state.get("request", ""))
    spec = str(state.get("spec", "") or "")
    if not spec.strip():
        return request
    body = spec[:_SPEC_PROMPT_CAP]
    return (
        f"REQUEST (intent summary):\n{request}\n\n"
        f"DESIGN SPEC (detailed source of truth):\n{body}"
    )


def _build_criteria(raw: Any, seq: Seq) -> list[AcceptanceCriterion]:
    """One AcceptanceCriterion per model item — id/oracle_hint/security_abuse
    are load-bearing traceability fields, so they are always populated."""
    out: list[AcceptanceCriterion] = []
    for i, item in enumerate(raw or ()):
        criticality = Criticality(str(item.get("criticality", "normal")))
        out.append(
            AcceptanceCriterion(
                id=str(item.get("id", f"AC-{i:03d}")),
                text=str(item.get("text", "")),
                criticality=criticality,
                oracle_hint=str(item.get("oracle_hint", "")),
                security_abuse=bool(item.get("security_abuse", False)),
                seq=seq._replace(local=i),
            )
        )
    return out


def make_product_manager_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Product Manager node (IntentBrief -> PRD + criteria)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        prd_seq = rt.seq(config, _NODE_ID, "artifact:prd")
        criteria_seq = rt.seq(config, _NODE_ID, "artifact:acceptance_criteria")
        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": _intent_text(state)}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        criteria = _build_criteria(payload.get("acceptance_criteria", ()), criteria_seq)
        # Persist the PRD narrative body behind its handle so the doc set can
        # inline it (task [2]); store=None keeps the historical mint-only path.
        prd_uri = persist_prose(str(payload.get("prd_body", "")), store=rt.cas)
        prd = PRD(
            title=str(payload.get("title", "")),
            prd_uri=prd_uri,
            required_capabilities=tuple(
                str(c) for c in payload.get("required_capabilities", ())
            ),
            criterion_ids=tuple(c.id for c in criteria),
            seq=prd_seq,
        )

        artifacts = [
            emit_artifact(prd, "prd", _NODE_ID, prd_seq, store=rt.cas),
            *(
                emit_artifact(c, "acceptance_criterion", _NODE_ID, c.seq, store=rt.cas)
                for c in criteria
            ),
        ]
        # ADVISORY journal entry (plan [11]) — derived purely from data this node
        # ALREADY produced (no extra model call ⇒ zero token cost). It rides the
        # SEPARATE `agent_journal` channel and is observability ONLY: no gate or
        # router reads it, so it can never affect whether the build passes.
        # NOTE: full rollout to the remaining ~dozen nodes is MECHANICAL — same
        # three lines (derive a summary, build a journal_entry, add the channel
        # key). This node + principal_architect + reviewer_critic demonstrate the
        # convention for the representative product / architecture / quality layers.
        journal_seq = rt.seq(config, _NODE_ID, "journal")
        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        note = journal_entry(
            node_id=_NODE_ID,
            role="Product Manager",
            seq=journal_seq,
            summary=(
                f"Authored PRD {prd.title!r} with {len(criteria)} acceptance "
                f"criteria ({high_risk} high-risk)."
            ),
            assumptions=(f"Built from request: {_intent_text(state)[:120]}",),
            concerns=(
                ("No acceptance criteria derived from the intent.",) if not criteria else ()
            ),
        )

        # required_capabilities flow to their own union channel so the
        # conditional UX/specialist routers can read them downstream (arch §7).
        return {
            "artifacts": artifacts,
            "required_capabilities": list(prd.required_capabilities),
            "agent_journal": [note],
        }

    return _node
