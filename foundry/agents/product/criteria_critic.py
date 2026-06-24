"""Criteria-Completeness Critic node (agents.md Layer 1 — T1, independent role).

Stops "100% coverage of weak criteria": independently critiques the PM's
``acceptance_criteria`` for completeness against the PRD — **especially
security/abuse criteria** — before the Spec gate. Following the SOP it
enumerates missing / under-specified criteria, flags omitted security/abuse
cases, and requires additions.

Output: a ``CriteriaCritique`` (``criteria_findings``) carried as a
content-addressed handle on the ``artifacts`` channel, AND an advisory
``GateTriage`` on the ``gate_triage`` channel naming the ``spec`` gate. When the
critic finds an applicable security/abuse omission, the triage explanation marks
it BLOCKING — but the critic NEVER mints a ``GateResult`` (LEARNINGS L5): the
Spec gate runner computes and signs the actual verdict. The critic also stamps
``judgment_provenance`` from the proxy's operating profile (self vs independent,
governance §1e / plan §0.2) so a same-lineage critique is recorded openly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.product._common import emit_artifact
from foundry.schemas.gates import GateTriage, JudgmentProvenance
from foundry.schemas.product import CriteriaCritique

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

_NODE_ID = "criteria_completeness_critic"
_TIER = "T1"  # most-consequential completeness judgment never auto-downgrades
_SPEC_GATE = "spec"

SOP = (
    "You are the Criteria-Completeness Critic (Layer 1), an INDEPENDENT reviewer "
    "of the Product Manager's acceptance criteria. Stop '100% coverage of weak "
    "criteria.' Follow this SOP, in order:\n"
    "1. Read the PRD and the declared acceptance criteria.\n"
    "2. Enumerate missing or under-specified criteria.\n"
    "3. Flag omitted security/abuse cases that the PRD's scope makes applicable.\n"
    "4. Require additions. Omission of applicable security/abuse criteria FAILS "
    "the Spec gate — list every such omission under security_abuse_omissions.\n"
    "Respond in the constrained JSON schema. You produce findings only; you do "
    "NOT decide the gate verdict."
)

# The model fills the findings; the node owns the Seq, the reviewed PRD's uri,
# and the judgment_provenance stamp (from the proxy profile, not the model).
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "missing": {"type": "array", "items": {"type": "string"}},
        "security_abuse_omissions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["missing", "security_abuse_omissions"],
    "additionalProperties": False,
}


def _prd_uri(state: FactoryState) -> str:
    """The reviewed PRD's content-addressed handle, read from the latest ``prd``
    artifact on the channel (empty if none yet — a degenerate greenfield view)."""
    artifacts: list[Artifact] = list(state.get("artifacts") or ())
    prds = [a for a in artifacts if a.kind == "prd"]
    if not prds:
        return ""
    return max(prds, key=lambda a: a.seq).uri


def _provenance(rt: AgentRuntime) -> JudgmentProvenance:
    """Record the proxy's operating profile openly (governance §1e / plan §0.2):
    a same-lineage self-judge critique is SELF; a decorrelated one INDEPENDENT."""
    return (
        JudgmentProvenance.SELF
        if rt.proxy.profile == "self_judge"
        else JudgmentProvenance.INDEPENDENT
    )


def make_criteria_completeness_critic_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Criteria-Completeness Critic node (PRD + criteria ->
    CriteriaCritique + an advisory, never a GateResult — LEARNINGS L5)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        critique_seq = rt.seq(config, _NODE_ID, "artifact:criteria_findings")
        triage_seq = rt.seq(config, _NODE_ID, "gate_triage:spec")
        prd_uri = _prd_uri(state)

        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": state.get("request", "")}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        critique = CriteriaCritique(
            prd_uri=prd_uri,
            missing=tuple(str(m) for m in payload.get("missing", ())),
            security_abuse_omissions=tuple(
                str(o) for o in payload.get("security_abuse_omissions", ())
            ),
            judgment_provenance=_provenance(rt),
            seq=critique_seq,
        )

        handle = emit_artifact(critique, "criteria_findings", _NODE_ID, critique_seq, store=rt.cas)
        triage = _triage(critique, prd_uri, triage_seq)
        # Agents write GateTriage (advisory), never a GateResult — the Spec gate
        # runner computes + signs the verdict from these findings (LEARNINGS L5).
        return {"artifacts": [handle], "gate_triage": [triage]}

    return _node


def _triage(critique: CriteriaCritique, prd_uri: str, seq: Any) -> GateTriage:
    """The advisory the Spec gate consumes. A security/abuse omission is flagged
    BLOCKING in the explanation (``CriteriaCritique.spec_gate_blocking``); the
    runner still owns the verdict bit."""
    if critique.spec_gate_blocking():
        explanation = (
            "BLOCKING: applicable security/abuse acceptance criteria are omitted — "
            f"{'; '.join(critique.security_abuse_omissions)}. "
            "Omission of applicable security/abuse criteria fails the Spec gate."
        )
    elif critique.missing:
        explanation = (
            "Non-blocking: missing or under-specified criteria — "
            f"{'; '.join(critique.missing)}."
        )
    else:
        explanation = "No criteria-completeness omissions found."
    return GateTriage(
        name=_SPEC_GATE,
        run_against=prd_uri,
        explanation=explanation,
        produced_by=_NODE_ID,
        seq=seq,
    )
