"""The bounded ideation loop + the spec-document renderer.

See the package docstring (:mod:`foundry.ideation`) for the ADVISORY trust
posture — everything here produces only plain text/markdown and has no
channel-write surface. The loop's single side effect on the outside world is
calling ``rt.proxy.complete(purpose="ideation")``; it inherits the proxy's
swappable backend and credential, and adds no egress of its own.

The deliverable is a markdown SPEC DOCUMENT shaped so task [8]'s ``--spec``
ingest (``foundry.cli.run(spec=...)``) can consume it: a human-readable design
spec with a Capabilities section and a numbered Acceptance Criteria section
(Given/When/Then with oracle hints) that the planner node can GROUND its
PRD + criteria in. It never replaces the criteria the planner derives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime

__all__ = [
    "IDEATION_SCHEMA",
    "IdeationResult",
    "IdeationRound",
    "ideate",
    "render_spec_markdown",
]

_NODE_ID = "ideation"
# T2 (Sonnet-class) — exploration over a rough idea; the proxy enables adaptive
# thinking for ideation-purpose calls on reasoning models. Never a gate/judge.
_TIER = "T2"

# Bounds (turn cap): ideation is front-of-funnel exploration, not an unbounded
# agent — the loop is hard-capped so token cost stays predictable.
_DEFAULT_ROUNDS = 3
_MAX_ROUNDS = 8

SOP = (
    "You are an Ideation / Brainstorm partner helping a human turn a rough idea "
    "into a buildable design spec. You ASSIST authoring; you decide nothing — the "
    "real build re-derives and gates everything downstream. Follow this SOP each "
    "round, in order:\n"
    "1. Restate the idea in one or two sentences (the working title + summary).\n"
    "2. Ask the most important clarifying questions still open (assumptions you "
    "had to make, ambiguities a human should resolve).\n"
    "3. Propose the capabilities the system needs (short noun phrases).\n"
    "4. Sketch acceptance criteria: each a testable Given/When/Then with a stable "
    "id and an oracle_hint describing how a runner-evaluable test would decide it; "
    "flag security/abuse criteria.\n"
    "5. Sketch a rough architecture (a few bullet points: components + data flow).\n"
    "On later rounds, REFINE the prior round's proposal using the feedback/notes "
    "provided rather than starting over. Respond ONLY in the constrained JSON "
    "schema."
)

# Constrained-output schema for one ideation round. This is NOT the verdict
# schema (no approve/request-changes status enum), so the proxy/test-double route
# it through the free-text/completions path — it never shadows a judgment verdict.
IDEATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "oracle_hint": {"type": "string"},
                    "security_abuse": {"type": "boolean"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
        "architecture_sketch": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class IdeationRound:
    """One round's structured proposal (plain data — no Seq, no channel record).

    Mirrors :data:`IDEATION_SCHEMA`. ``acceptance_criteria`` items are plain
    dicts (``id`` / ``text`` / ``oracle_hint`` / ``security_abuse``) so the
    renderer can lay them out as the spec's testable criteria.
    """

    title: str
    summary: str
    clarifying_questions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    acceptance_criteria: tuple[dict[str, Any], ...] = ()
    architecture_sketch: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> IdeationRound:
        """Build a round from a (possibly partial) model JSON payload.

        Missing/degenerate fields default to empty — the loop is robust to a
        terse model response and a blank round simply contributes nothing to the
        final spec rather than crashing."""
        data = payload or {}
        criteria: list[dict[str, Any]] = []
        for i, item in enumerate(data.get("acceptance_criteria") or ()):
            if not isinstance(item, dict):
                continue
            criteria.append(
                {
                    "id": str(item.get("id", f"AC-{i:03d}")),
                    "text": str(item.get("text", "")),
                    "oracle_hint": str(item.get("oracle_hint", "")),
                    "security_abuse": bool(item.get("security_abuse", False)),
                }
            )
        return cls(
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            clarifying_questions=tuple(str(q) for q in data.get("clarifying_questions") or ()),
            assumptions=tuple(str(a) for a in data.get("assumptions") or ()),
            capabilities=tuple(str(c) for c in data.get("capabilities") or ()),
            acceptance_criteria=tuple(criteria),
            architecture_sketch=tuple(str(s) for s in data.get("architecture_sketch") or ()),
        )


@dataclass(frozen=True, slots=True)
class IdeationResult:
    """The deliverable of an ideation session (plain data — no trust surface).

    Carries the original ``idea``, every :class:`IdeationRound` (oldest first),
    and the rendered markdown ``spec_markdown`` — the handoff artifact for task
    [8]'s ``--spec`` ingest. The spec document is rendered from the last
    (most-refined) round. This is NOT a channel record: it holds no ``Seq``,
    writes to no reducer, and round-trips through no store.
    """

    idea: str
    rounds: tuple[IdeationRound, ...]
    spec_markdown: str


def _round_prompt(idea: str, prior: IdeationRound | None, round_no: int, total: int) -> str:
    """The user message for one round: the idea, plus the prior round's proposal
    and the open questions to fold in when refining."""
    if prior is None:
        return (
            f"ROUGH IDEA:\n{idea}\n\n"
            f"This is ideation round {round_no} of {total}. Produce a first-pass "
            "design proposal per the SOP."
        )
    open_qs = "\n".join(f"- {q}" for q in prior.clarifying_questions) or "- (none)"
    crit = "\n".join(f"- {c['id']}: {c['text']}" for c in prior.acceptance_criteria) or "- (none)"
    return (
        f"ROUGH IDEA:\n{idea}\n\n"
        f"This is ideation round {round_no} of {total}. REFINE the prior proposal "
        f"below — resolve the open questions with reasonable assumptions, tighten "
        f"the acceptance criteria, and fill gaps.\n\n"
        f"PRIOR TITLE: {prior.title}\n"
        f"PRIOR SUMMARY: {prior.summary}\n"
        f"OPEN QUESTIONS:\n{open_qs}\n"
        f"PRIOR ACCEPTANCE CRITERIA:\n{crit}"
    )


def ideate(rt: AgentRuntime, idea: str, *, rounds: int = _DEFAULT_ROUNDS) -> IdeationResult:
    """Run a bounded, iterative ideation loop and return an :class:`IdeationResult`.

    ``rounds`` is the turn cap (clamped to ``[1, _MAX_ROUNDS]``): each round is one
    ``rt.proxy.complete(purpose="ideation")`` call that proposes/refines the spec.
    The result's ``spec_markdown`` is the handoff artifact for task [8]'s ingest.

    ADVISORY: this function makes no decision and writes to no channel — it only
    reads model text through the proxy and renders markdown (see module docstring).
    """
    if not idea or not idea.strip():
        raise ValueError("ideate() requires a non-empty idea")
    n = max(1, min(int(rounds), _MAX_ROUNDS))

    history: list[IdeationRound] = []
    prior: IdeationRound | None = None
    for i in range(n):
        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": _round_prompt(idea, prior, i + 1, n)}],
            purpose="ideation",
            schema=IDEATION_SCHEMA,
        )
        rnd = IdeationRound.from_payload(result.parsed)
        history.append(rnd)
        prior = rnd

    spec_markdown = render_spec_markdown(idea, history[-1])
    return IdeationResult(idea=idea, rounds=tuple(history), spec_markdown=spec_markdown)


def render_spec_markdown(idea: str, rnd: IdeationRound) -> str:
    """Render a round into a markdown SPEC DOCUMENT — the handoff for task [8].

    The layout is what ``--spec`` ingest grounds intent in: a title + summary, the
    capabilities, a numbered Acceptance Criteria section (Given/When/Then with
    oracle hints, security/abuse flagged), an architecture sketch, and the open
    questions/assumptions a human should review before building. It is plain
    markdown — no trust metadata, no signatures: an authoring artifact only.
    """
    title = rnd.title.strip() or "Design Spec"
    lines: list[str] = [f"# {title}", ""]

    summary = rnd.summary.strip() or idea.strip()
    lines += ["## Summary", "", summary, ""]

    lines += ["## Capabilities", ""]
    if rnd.capabilities:
        lines += [f"- {c}" for c in rnd.capabilities]
    else:
        lines.append("- _(none proposed)_")
    lines.append("")

    lines += ["## Acceptance Criteria", ""]
    if rnd.acceptance_criteria:
        for i, c in enumerate(rnd.acceptance_criteria, start=1):
            cid = str(c.get("id", f"AC-{i:03d}"))
            flag = " *(security/abuse)*" if c.get("security_abuse") else ""
            lines.append(f"{i}. **{cid}**{flag}: {c.get('text', '')}")
            hint = str(c.get("oracle_hint", "")).strip()
            if hint:
                lines.append(f"   - Oracle: {hint}")
    else:
        lines.append("_(none proposed)_")
    lines.append("")

    lines += ["## Architecture Sketch", ""]
    if rnd.architecture_sketch:
        lines += [f"- {s}" for s in rnd.architecture_sketch]
    else:
        lines.append("- _(none proposed)_")
    lines.append("")

    lines += ["## Open Questions", ""]
    if rnd.clarifying_questions:
        lines += [f"- {q}" for q in rnd.clarifying_questions]
    else:
        lines.append("- _(none)_")
    lines.append("")

    lines += ["## Assumptions", ""]
    if rnd.assumptions:
        lines += [f"- {a}" for a in rnd.assumptions]
    else:
        lines.append("- _(none)_")
    lines.append("")

    lines += [
        "---",
        "",
        "_Advisory ideation draft. This document informs a build's spec; it does "
        "not override the spec gate, criteria derivation, or any verdict. Review "
        "and edit before feeding to `foundry --spec`._",
        "",
    ]
    return "\n".join(lines)
