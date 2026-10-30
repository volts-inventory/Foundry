"""Product & planning artifacts (agents.md Layer 1 — Product & Planning).

Client Proxy → `IntentBrief`; Product Manager → `PRD` + `AcceptanceCriterion[]`;
Criteria-Completeness Critic → `CriteriaCritique` (`criteria_findings`);
Eng/Program Manager → `SprintPlan`. Large bodies never live inline — they sit
in the CAS behind `*_uri` handles (DS9). Records are superseded, never edited.
"""

from __future__ import annotations

from enum import StrEnum

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import JudgmentProvenance
from foundry.state.seq import Seq


class Criticality(StrEnum):
    """PM marks high-risk criteria for stronger oracles (agents.md Layer 1):
    high-risk drives T1 oracle authoring, negative oracles, and cross-family
    review (governance.md §1b)."""

    NORMAL = "normal"
    HIGH_RISK = "high-risk"


class IntentBrief(FrozenModel):
    """Client Proxy output: holds the "why" and resolves ambiguity
    (agents.md Layer 1 — Stakeholder/Client Proxy SOP)."""

    intent: str  # restated intent
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()  # success in user terms
    must_haves: tuple[str, ...] = ()
    nice_to_haves: tuple[str, ...] = ()
    # Stable ids the plan's scenarios cite in ``covers`` (aligned by index with
    # ``must_haves`` / ``success_criteria``), and what the build delivers
    # (app | library | cli | service; "" when undeclared).
    must_have_ids: tuple[str, ...] = ()
    success_criterion_ids: tuple[str, ...] = ()
    deliverable: str = ""
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class PRD(FrozenModel):
    """Product Manager output (agents.md Layer 1). The PRD body (user stories,
    prioritization) is content-addressed (DS9); `required_capabilities` drives
    UX/specialist conditional routing (architecture.md §7)."""

    title: str
    prd_uri: str  # CAS handle to the full PRD body
    required_capabilities: tuple[str, ...] = ()
    criterion_ids: tuple[str, ...] = ()  # the acceptance criteria this PRD declares
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class AcceptanceCriterion(FrozenModel):
    """One testable Given/When/Then criterion (agents.md Layer 1 — PM SOP).

    Any diff touching acceptance criteria is a deterministic Guardian trip →
    HITL (agents.md Layer 7), so edits arrive only as supersessions.
    """

    id: str  # `criterion_id` — the traceability key for tests/tasks
    text: str  # Given/When/Then
    criticality: Criticality = Criticality.NORMAL
    oracle_hint: str = ""  # testable-oracle hint: how a runner-evaluable test decides it
    security_abuse: bool = False  # security/abuse criterion (Spec-gate relevant)
    # Structured scenario; ``text`` stays the rendered "Given … When … Then …" so
    # every plain-text consumer keeps working.
    given: tuple[str, ...] = ()
    when: tuple[str, ...] = ()
    then: tuple[str, ...] = ()
    ticket_id: str | None = None
    covers: tuple[str, ...] = ()  # the must-have / success-criterion ids it proves
    version: int = 1
    supersedes: str | None = None
    seq: Seq


def render_scenario(given: tuple[str, ...], when: tuple[str, ...], then: tuple[str, ...]) -> str:
    """The one-line ``text`` form of a scenario."""
    parts = []
    for word, steps in (("Given", given), ("When", when), ("Then", then)):
        if steps:
            parts.append(f"{word} " + " and ".join(steps))
    return " ".join(parts)


class CriteriaCritique(FrozenModel):
    """Criteria-Completeness Critic output (`criteria_findings`, agents.md
    Layer 1): stops "100% coverage of weak criteria" before the Spec gate.

    This is a judgment seam: under the solo-lineage self-judge profile the
    critique may be same-lineage, but only openly — it carries the
    `judgment_provenance` stamp (governance.md §1e, implementation-plan §0.2).
    """

    prd_uri: str  # the PRD reviewed
    missing: tuple[str, ...] = ()  # missing / under-specified criteria findings
    security_abuse_omissions: tuple[str, ...] = ()  # omitted security/abuse cases
    judgment_provenance: JudgmentProvenance
    seq: Seq
    # The planner's deterministic contract fit check (a consumed contract with no
    # or several producers, a consumer not ordered after its producer, a contract
    # signature that does not compile): computed, not judged, and blocking like an
    # omission.
    contract_findings: tuple[str, ...] = ()
    # The planner's deterministic coverage check (an uncovered must-have or success
    # criterion, a feature ticket without scenarios, no end-to-end entrypoint, no
    # run command): computed, and blocking the same way.
    coverage_findings: tuple[str, ...] = ()

    def spec_gate_blocking(self) -> bool:
        """Omission of applicable security/abuse criteria FAILS the Spec gate
        (agents.md Layer 1 — Criteria-Completeness Critic SOP), as does a
        contract fit or coverage finding.

        Blank/whitespace-only entries are ignored so this predicate matches the
        Spec gate's own re-derivation byte-for-byte (``SpecGate._critique_blocks``
        requires ``o.strip()`` truthy) — the materialize decision and the gate
        decision apply the SAME rule to a degenerate omission list."""
        return any(
            o.strip() for o in (
                *self.security_abuse_omissions, *self.contract_findings,
                *self.coverage_findings,
            )
        )


class SprintPlan(FrozenModel):
    """Eng/Program Manager output (agents.md Layer 1): milestones, refined
    absolute budget cap, risk register, done-for-this-iteration. Monetary
    values are integer micro-USD, never floats (architecture.md §8)."""

    milestones: tuple[str, ...] = ()
    usd_cap_microusd: int  # refined absolute cap seeded at Triage (governance.md §7)
    risk_register_uri: str  # CAS handle (DS9)
    done_definition: str  # done-for-this-iteration
    version: int = 1
    supersedes: str | None = None
    seq: Seq
