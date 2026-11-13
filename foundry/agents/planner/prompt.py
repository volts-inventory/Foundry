"""Prompt assembly: the request, spec, scope, revision and stack text of call 1."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text, demo_directives, scope_directive
from foundry.agents.planner.build import _TRUNCATED
from foundry.agents.planner.checks import _goals_text
from foundry.agents.planner.readers import _newest_by_id, _newest_intent, _section, _strs
from foundry.agents.planner.schema import _RETRIEVAL_K, _SPEC_GATE, _SPEC_PROMPT_CAP
from foundry.base_tree import BaseView, read_base, render_base_view
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import Toolchain
from foundry.schemas.product import PRD

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.context import SymbolIndex
    from foundry.context.retriever import Span
    from foundry.state.factory_state import FactoryState


def _intake_text(state: FactoryState) -> str:
    """The raw request plus, when provided, the (capped) design-spec text and, for
    a change build, the base tree view with the change-mode rules."""
    request = str(state.get("request", ""))
    spec = str(state.get("spec", "") or "")
    text = (
        request
        if not spec.strip()
        else (
            f"REQUEST (intent summary):\n{request}\n\n"
            f"DESIGN SPEC (detailed source of truth):\n{spec[:_SPEC_PROMPT_CAP]}"
        )
    )
    base = _base(state)
    return text if base is None else f"{text}\n\n{_change_text(base)}"


def _base(state: FactoryState) -> BaseView | None:
    """The base tree of a change build, or None for a new project."""
    base_dir = state.get("base_dir")
    return read_base(base_dir) if base_dir else None


def _change_text(base: BaseView) -> str:
    taken = ", ".join(sorted(base.feature_names)) or "(none)"
    return (
        render_base_view(base) + "\n\n"
        "CHANGE MODE — the REQUEST is a change to the EXISTING PROJECT above. These "
        "rules override any conflicting rule:\n"
        "- Keep the existing stack (language, framework, package manager and every "
        "command) exactly as it is and echo it in stack.\n"
        "- Plan tickets ONLY for files you add or change. List an existing file in a "
        "ticket's files[] only when that ticket must modify it; untouched files stay "
        "as they are and need no ticket.\n"
        "- interfaces[] holds only NEW contracts. The existing contracts are already "
        "implemented: consume them by name with no implementing ticket, and never "
        "redeclare one (checked). New code contracts extend the existing contracts "
        "file.\n"
        "- Scenarios and must-haves cover only the NEW behaviour. The existing feature "
        "files stay in force and must keep passing, so never break the behaviour they "
        f"describe. Existing feature names (do not reuse as ticket ids): {taken}.\n"
        "- An entrypoint ticket is needed only when the change must re-wire the app; it "
        "then modifies the existing entry file. A dependency-manifest scenario is "
        "needed only when the change adds a dependency."
    )


def _forced_standard(state: FactoryState) -> bool:
    """A run given a design spec is asking for the system that spec describes:
    STANDARD without trusting the model's tier (only ever tightens)."""
    return bool(str(state.get("spec", "") or "").strip())


def _scope_text(state: FactoryState, *, revising: bool) -> str:
    decision = state.get("scope_tier")
    if decision is not None:
        # A revision exists to close a gate-blocking gap; its "reproduce every
        # criterion AND add" contradicts a criteria cap, so closing the gap wins.
        caps = "" if revising else (scope_directive(state) + scope_directive(state, architect=True))
        return f"\n\nSCOPE IS FIXED: {decision.tier.value}. Echo it in scope.tier.{caps}"
    if _forced_standard(state):
        return "\n\nSCOPE IS FIXED: standard (a design spec was supplied)."
    return "\n\nIF you judge scope.tier = 'demo', these caps apply:" + demo_directives()


def _retrieve_spans(index: SymbolIndex | None, state: FactoryState) -> tuple[Span, ...]:
    """Existing-code spans via the context retriever (symbol index + one-hop
    call/def neighbours), seeded by any capabilities already on state. No index
    or no symbols is a legitimate greenfield view (empty)."""
    symbols = tuple(state.get("required_capabilities") or ())
    if index is None or not symbols:
        return ()
    return tuple(index.retrieve_or_need_context(symbols, _RETRIEVAL_K))


def latest_spec_failure(state: FactoryState) -> tuple[str, ...] | None:
    """Findings of the newest Spec-gate verdict when it FAILed, else None.

    Only the newest verdict counts: a later PASS supersedes an earlier FAIL."""
    specs = [g for g in state.get("gates") or () if g.name == _SPEC_GATE]
    if not specs:
        return None
    newest = max(specs, key=lambda g: g.seq)
    if newest.status is not GateStatus.FAIL:
        return None
    return tuple(newest.findings) or ("the Spec gate failed without findings",)


def _declared_criteria_text(state: FactoryState, rt: AgentRuntime) -> str:
    if rt.cas is None:
        return ""
    handles = sorted(
        (
            a
            for a in state.get("artifacts") or ()
            if getattr(a, "kind", None) == "acceptance_criterion"
        ),
        key=lambda a: a.seq,
    )
    current: dict[str, Any] = {}
    for a in handles:
        try:
            c = rt.cas.resolve(a)
        except Exception:
            continue
        current[str(getattr(c, "id", "?"))] = c
    # Criteria are never tombstoned: only the newest PRD's ids are still declared.
    prds = list(_newest_by_id(state, rt, "prd", PRD).values())
    if prds:
        live = set(max(prds, key=lambda p: p.seq).criterion_ids)
        if live:
            current = {cid: c for cid, c in current.items() if cid in live}
    return _criteria_text(current.values())


def _criteria_text(criteria: Iterable[Any]) -> str:
    lines: list[str] = []
    for c in criteria:
        ticket = getattr(c, "ticket_id", None)
        lines.append(
            f"- id={getattr(c, 'id', '?')}"
            + (f" (ticket={ticket})" if ticket else "")
            + f" | {getattr(c, 'text', '')} "
            f"(criticality={getattr(c, 'criticality', 'normal')}; "
            f"oracle_hint={getattr(c, 'oracle_hint', '')!r}; "
            f"security_abuse={getattr(c, 'security_abuse', False)})"
        )
    return "\n".join(lines)


# Who failed the plan a revision block answers: (subject, findings label).
_GATE = ("the signed Spec gate", "Spec gate findings")


_CHECK = (
    "the planner's own plan check (the checks the signed Spec gate runs)",
    "Plan check findings",
)


def _revision_block(
    findings: Iterable[str],
    criteria: str,
    goals: str,
    source: tuple[str, str] = _GATE,
) -> str:
    """A full-plan revision block. The Spec gate reads the newest record per
    criterion id, so the revision fixes a criterion a finding names by re-emitting
    it under the same id."""
    return (
        f"\n\nREVISION PASS — {source[0]} FAILED your previous plan. Return "
        "the FULL plan again: reproduce every epic and every ticket (same ids) and "
        "every previously declared scenario (same id, on the same ticket, never "
        "weakened; fix in place any scenario a finding names) AND add scenarios that "
        "close every finding below.\n"
        f"{source[1]}:\n- " + "\n- ".join(findings) + "\n\n"
        f"Previously declared criteria:\n{criteria or '(none resolved)'}"
        + (f"\n\nPreviously declared intent (keep these ids):{goals}" if goals else "")
    )


_SELF = ("your own criteria_review", "Flagged gaps")


def _repair_block(
    findings: Iterable[str], plan: str, source: tuple[str, str] = _GATE, what: str = "plan"
) -> str:
    """A repair of the failed ``plan`` (shown whole) rather than a re-plan from
    scratch: it is an edit of what the findings name, so it must not re-derive the rest."""
    return (
        f"\n\nREPAIR PASS — {source[0]} faulted your previous {what}, shown in full below. "
        "Correct it in place: keep every id (epics, tickets, scenarios, must-haves, "
        "success criteria) and everything no finding faults exactly as it is, but "
        "restructure what a finding shows is wrong in the design itself (give a contract "
        "its one implementing ticket, add the missing dependency, split or merge the "
        f"tickets involved). Return the FULL {what}, not only the changes; list under "
        "criteria_review only gaps that genuinely remain.\n"
        + (
            f"A field below ending in '{_TRUNCATED.strip()}' was cut for length: write it "
            "out in full again, never copy the cut text.\n"
            if _TRUNCATED in plan
            else ""
        )
        + f"{source[1]}:\n- "
        + "\n- ".join(findings)
        + f"\n\nYOUR PREVIOUS {what.upper()}:\n{plan}"
    )


def _payload_text(payload: dict[str, Any], tickets: list[dict[str, Any]] | None = None) -> str:
    """A plan reply, whole and uncut, for its repair (the model wrote it, so it
    fits, and every field is to be reproduced): the review dropped (the findings
    replace it), ``tickets`` (the checked ones) in place of the raw ones when given."""
    shown = {k: v for k, v in payload.items() if k != "criteria_review"}
    if tickets is not None:
        shown["tickets"] = tickets
    return as_text(shown)


def _revision_text(state: FactoryState, rt: AgentRuntime, plan: str = "") -> str:
    """The re-entry block (spec_fix -> planner), or "" on the first pass. With the
    previous pass's ``plan`` it is a repair of that plan instead."""
    findings = latest_spec_failure(state)
    if findings is None:
        return ""
    if plan:
        return _repair_block(findings, plan)
    return _revision_block(
        findings, _declared_criteria_text(state, rt), _goals_text(_newest_intent(state, rt))
    )


# A review "gap" the review itself rules outside the spec or defers ("the spec does
# not mention CSRF ... flagged for a future iteration"): not a gap of this plan.
_OUT_OF_SPEC = re.compile(
    r"(?i)\bout[- ]of[- ]scope\b"
    r"|\bnot (?:in|part of|required by|requested (?:by|in)|mentioned (?:in|by)"
    r"|specified (?:in|by)|asked for (?:in|by)) the (?:design )?(?:spec\w*|request|requirements)"
    r"|\b(?:spec\w*|request|requirements) (?:does not|doesn't|do not|don't|never) "
    r"(?:mention|ask|require|request|specify|call for|name)"
    r"|\b(?:future|later|next|subsequent) (?:iteration|release|version|phase)"
    r"|\bdeferred\b"
)


def _review_missing(review: dict[str, Any]) -> tuple[str, ...]:
    """``review``'s in-scope missing gaps: one it says the spec does not ask for, or
    defers, is left out (see :func:`_review_deferred`)."""
    return tuple(m for m in _strs(review.get("missing")) if not _OUT_OF_SPEC.search(m))


def _review_deferred(review: dict[str, Any]) -> tuple[str, ...]:
    """What ``review`` notes beyond the spec: its out_of_scope entries and the
    missing ones it rules out of scope itself. Never blocking."""
    return (
        *_strs(review.get("out_of_scope")),
        *(m for m in _strs(review.get("missing")) if _OUT_OF_SPEC.search(m)),
    )


def _deferred_concerns(review: dict[str, Any]) -> list[str]:
    return [f"Out of scope (self-review): {d}" for d in _review_deferred(review)]


def _self_review_gaps(payload: dict[str, Any]) -> tuple[str, ...]:
    review = _section(payload, "criteria_review")
    return (*_strs(review.get("security_abuse_omissions")), *_review_missing(review))


def _self_revision_text(gaps: tuple[str, ...], what: str = "plan") -> str:
    return (
        "\n\nSELF-REVIEW REVISION — your own criteria_review flagged these gaps. "
        f"Return the FULL {what} again with scenarios that close each one (same ids for "
        "tickets and scenarios you keep); list under criteria_review only gaps that genuinely "
        "remain.\nFlagged gaps:\n- " + "\n- ".join(gaps)
    )


def _stack_text(state: FactoryState, toolchains: Sequence[Toolchain]) -> str:
    pinned = state.get("stack")
    if pinned is not None:
        fixed = {k: v for k, v in pinned.model_dump(mode="json").items() if v and k != "notes"}
        return (
            "\n\nSTACK IS FIXED (pinned by the operator): "
            + as_text(fixed)
            + f"\nEcho language={pinned.language!r} in stack.language."
            + (f"\nGates: {pinned.notes}" if pinned.notes else "")
        )
    from foundry.gates.profiles import profile_examples

    langs = "\n".join(
        f"- {t.language} ({', '.join(t.binaries)}" + (f"; {t.version}" if t.version else "") + ")"
        for t in toolchains
    )
    return (
        "\n\nLANGUAGES AVAILABLE HERE (stack.language must be one of these):\n"
        f"{langs or '- python'}\n\n"
        "EXAMPLE GATE COMMANDS (hand-written profiles, for reference only; choose "
        f"your own commands):\n{profile_examples()}"
    )


def _user_content(
    state: FactoryState,
    rt: AgentRuntime,
    spans: tuple[Span, ...],
    toolchains: Sequence[Toolchain] = (),
    plan: str = "",
) -> str:
    retrieved = [{"file": s.file, "lines": [s.line, s.end_line], "symbol": s.symbol} for s in spans]
    body = _intake_text(state)
    if retrieved:
        body += "\n\nRETRIEVED CODE SPANS (existing code):\n" + as_text(retrieved)
    revision = _revision_text(state, rt, plan)
    return (
        body
        + _stack_text(state, toolchains)
        + _scope_text(state, revising=bool(revision))
        + revision
    )
