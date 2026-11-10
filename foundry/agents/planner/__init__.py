"""Planner node — the whole product + architecture plan in ONE model call.

Replaces the former persona chain (scope judge -> stakeholder proxy -> product
manager -> criteria critic -> program manager -> principal architect, plus the
per-lane lead decomposition call). Each of those was a separate ``claude -p``
launch re-reading the same request; one T1 structured call now authors the full
plan, and the node fans it out into the SAME records the old chain wrote, so
every downstream consumer (Spec gate, leads, docs, Workshop) reads unchanged
channels:

* ``artifacts``: ``intent_brief``, ``prd``, ``acceptance_criterion`` (each),
  ``criteria_findings``, ``sprint_plan``, ``system_design``, ``adr`` (each),
  ``interface_contract`` (each), ``ticket`` (each), ``ownership_map`` —
  content-addressed handles.
* ``pinned_constraints``, ``required_capabilities``, ``scope_tier`` (only when
  not already decided — it is single-writer), an advisory ``gate_triage`` on the
  ``spec`` gate, and ONE ``agent_journal`` entry.

Large plans: call 1 also splits the work into epics joined by the interface
contracts it defines. A small plan (one epic) carries its tickets inline, so it
stays one call; otherwise each epic's tickets come from their own call, run in
parallel, namespaced ``<epic>-<id>`` and merged in epic order, with each epic's
root tickets made to depend on the upstream epics' tickets implementing what it
consumes (else on their sink tickets).

The plan is a DAG of atomic tickets, each carrying its Given/When/Then scenarios
(one ``AcceptanceCriterion`` per scenario, ``ticket_id`` set). The ownership map
is DERIVED from the tickets' files (with ``unit_criteria`` per file) so the
ownership-driven scheduler keeps working unchanged.

Re-entry: ``spec_fix`` routes back here. The newest FAILing Spec-gate verdict's
findings, and the criteria already declared, go into the prompt so the revision
closes the gap instead of re-deriving the same plan.

Patch, don't re-plan: where a revision would otherwise re-run call 1 (the
in-node plan check, or a Spec-gate failure no epic re-run can target), a finding
the tickets alone can close first gets ONE small delta call: the findings, the
tickets they name and a summary of the rest in; ticket upserts / removals out,
merged by id and put through the same assembly. A delta that does not merge, or
leaves the merged plan failing a check, falls back to the full re-plan.

Repair before re-planning: every revision (the self-review's, the in-node plan
check's, a Spec-gate re-entry's, an epic's) is first a REPAIR — the failed plan
itself plus its findings in, the full corrected plan out — at medium thinking
effort, as is the delta. A repair still leaving a finding gets ONE fresh
revision at the default effort, so a revision is at most two calls; the plan
check keeps whichever draft has the fewest findings, a tie going to the later.
An epic with no previous one to repair is planned at the default effort.

Interface re-plan: a ticket the stub-signature guard left to a re-plan (mid-build,
the Spec gate already passed) gets one small call revising only the contracts it
implements (:mod:`foundry.agents.planner.interface`), not a re-plan.

Trust posture is unchanged: the node emits no ``GateResult`` (L5), reaches the
model only via ``rt.proxy`` (L6), and its self-review of the criteria is stamped
``JudgmentProvenance.SELF`` — the signed Spec gate still decides.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from foundry import progress
from foundry.agents._common import cas_uri, emit_artifact, journal_entry, persist_prose
from foundry.agents.implementation.tickets.tasks import pending_interface_replans
from foundry.agents.plan_contracts import CompileResult
from foundry.agents.planner.assembly import _assemble, _Fit, _Frame
from foundry.agents.planner.build import (
    _DROPPED_KEY,
    _EPIC_KEY,
    _build_adrs,
    _build_criterion,
    _build_epics,
    _build_interfaces,
    _build_tickets,
    _contract_text,
    _Epic,
    _epic_content,
    _epic_items,
    _interfaces_text,
    _plan_brief,
    _run_parallel,
    _safe_ticket_ids,
    _tagged,
)
from foundry.agents.planner.checks import (
    _bdd_quoting,
    _builtin_method_findings,
    _compile,
    _contract_findings,
    _coverage_findings,
    _declaration_only,
    _goals,
    _goals_text,
    _offline_scenario_findings,
    _stack_command_findings,
    finding_kind,
)
from foundry.agents.planner.constraints import (
    _build_constraints,
    _ownership_from_tickets,
    _retired_constraints,
    _route_cap,
    _scope_decision,
    _stack_constraints,
    _triage,
    ticket_owner,
)
from foundry.agents.planner.delta import (
    _affected,
    _delta_text,
    _merge_delta,
    _patch_note,
    _patchable,
    _rejected,
    _structural_findings,
)
from foundry.agents.planner.folds import (
    _add_contract_deps,
    _add_read_deps,
    _drop_self_consumes,
    _fold_test_bindings,
    _one_step_module_per_ticket,
    _own_scenario_bindings,
    _own_stack_files,
    _wire_entrypoint,
    stack_files,
)
from foundry.agents.planner.interface import revise_interfaces
from foundry.agents.planner.prior import (
    _Draft,
    _draft_prior,
    _epic_revision_text,
    _kept_ids,
    _match_prior_epics,
    _named_epics,
    _not_of,
    _prior_items,
    _prior_plan,
    _prior_plan_text,
    _PriorPlan,
    _retired_tickets,
    _revision_note,
    _target_epics,
    _ticket_epics,
    _ticket_items,
    _untagged,
    _with_fit,
)
from foundry.agents.planner.prompt import (
    _CHECK,
    _GATE,
    _SELF,
    _base,
    _criteria_text,
    _declared_criteria_text,
    _intake_text,
    _payload_text,
    _repair_block,
    _retrieve_spans,
    _revision_block,
    _self_review_gaps,
    _self_revision_text,
    _user_content,
    latest_spec_failure,
)
from foundry.agents.planner.readers import _int, _objs, _section, _str, _strs
from foundry.agents.planner.schema import (
    DELIVERABLES,
    DELTA_SCHEMA,
    DELTA_SOP,
    EPIC_KIND,
    EPIC_SCHEMA,
    EPIC_SOP,
    MAX_EPIC_TICKETS,
    MAX_EPICS,
    MAX_PLAN_TICKETS,
    MAX_TICKETS,
    NODE_ID,
    PLAN_SCHEMA,
    ROLE,
    SOP,
    TICKET_KIND,
    TIER,
    _plan_schema,
)
from foundry.agents.planner.sizing import size_text, ticket_ceilings
from foundry.agents.planner.stack import _generated_tickets, _python_deps_rule, _stack_decision
from foundry.proxy.model_provider_proxy import ProxyUnavailable, StructuredOutputError
from foundry.schemas.architecture import SystemDesign
from foundry.schemas.gates import JudgmentProvenance
from foundry.schemas.governance import ScopeTier
from foundry.schemas.implementation import Epic, Toolchain
from foundry.schemas.product import PRD, CriteriaCritique, Criticality, IntentBrief, SprintPlan
from foundry.telemetry import count

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.context import SymbolIndex
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

__all__ = [
    "DELTA_SCHEMA",
    "DELTA_SOP",
    "EPIC_KIND",
    "EPIC_SCHEMA",
    "EPIC_SOP",
    "MAX_EPICS",
    "MAX_EPIC_TICKETS",
    "MAX_PLAN_TICKETS",
    "MAX_TICKETS",
    "NODE_ID",
    "PLAN_SCHEMA",
    "SOP",
    "TICKET_KIND",
    "TIER",
    "_DROPPED_KEY",
    "_add_contract_deps",
    "_add_read_deps",
    "_bdd_quoting",
    "_build_criterion",
    "_build_tickets",
    "_builtin_method_findings",
    "_contract_findings",
    "_coverage_findings",
    "_declared_criteria_text",
    "_drop_self_consumes",
    "_fold_test_bindings",
    "_offline_scenario_findings",
    "_one_step_module_per_ticket",
    "_own_scenario_bindings",
    "_own_stack_files",
    "_python_deps_rule",
    "_safe_ticket_ids",
    "_wire_entrypoint",
    "make_planner_node",
    "stack_files",
    "ticket_owner",
]


Effort = Literal["medium", "xhigh"] | None

# A repair corrects a plan it is shown, so it needs far less thinking than inventing
# one: the first plan call keeps the default (xhigh locally), every repair and delta
# runs at medium.
_REPAIR_EFFORT: Effort = "medium"


def _count_findings(findings: Iterable[str]) -> None:
    for f in findings:
        count(f"planner.finding.{finding_kind(f)}")


def _count_gaps(payload: dict[str, Any]) -> None:
    review = _section(payload, "criteria_review")
    for _ in _strs(review.get("security_abuse_omissions")):
        count("planner.finding.security-gap")
    for _ in _strs(review.get("missing")):
        count("planner.finding.review-missing")


def _outcome(event: str, where: str, findings: Sequence[str]) -> None:
    """Count ``planner.<event>`` and log it with the findings it leaves or closed."""
    count(f"planner.{event}")
    progress.log(
        f"planner {event.replace('.', ' ')} ({where}): {len(findings)} finding(s)"
        + (": " + " | ".join(findings)[:400] if findings else "")
    )


def _count_folds(fit: _Fit) -> None:
    """Count ``planner.fold.<name>`` once per emitted plan whose assembly that fold
    changed: drafts a revision replaced are not counted."""
    for name in fit.folds:
        count(f"planner.fold.{name}")


def _open(
    fit: _Fit,
    missing: Sequence[str],
    omissions: Sequence[str],
    findings: Sequence[str],
    prior: _PriorPlan,
) -> tuple[str, ...]:
    """What a repair of ``prior`` against the Spec gate's ``findings`` leaves failing:
    its check findings, review gaps, and the structural criteria problems the gate
    named or the repair introduced (one the gate let pass, e.g. an optional
    security criterion, stays allowed)."""
    known = set(_structural_findings(tuple(prior.criteria.values())))
    return (
        *fit.blocking,
        *fit.overflow,
        *omissions,
        *missing,
        *(
            s
            for s in _structural_findings(fit.criteria)
            if s not in known or any(f.startswith(s) for f in findings)
        ),
    )


def _best(*fits: _Fit) -> int:
    """The index of the fit with the fewest blocking findings; a tie goes to the
    later one, a revision over what it revised."""
    return min(range(len(fits)), key=lambda i: (len(fits[i].blocking), -i))


def make_planner_node(
    rt: AgentRuntime,
    index: SymbolIndex | None = None,
    toolchains: Sequence[Toolchain] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
) -> NodeFn:
    """Build the Planner node (T1). Call 1 writes the plan and its epics; a small
    plan carries its tickets inline (one call), a larger one gets one ticket call
    per epic, run in parallel. Each call self-revises at most once when its own
    criteria review flags gaps.

    Before emitting, the node repairs what is mechanical (:func:`_assemble`) and
    runs the Spec gate's deterministic checks itself, plus a plan-time compile of
    the code contracts. Findings left get ONE in-node revision — only the epics
    they name, else the whole plan — and the better of the two plans is emitted;
    only what is still left reaches the Spec gate.

    ``index`` is the context retriever probed for existing code spans (``None``
    is greenfield — empty retrieval). ``toolchains`` are the languages offered
    for the stack (``None`` probes this host once). ``ticket_tiers`` is the run's
    difficulty -> tier table: with the proxy's local tiers it sets each ticket's
    size ceiling (local or cloud), stated in every planning prompt and checked."""

    ceilings = ticket_ceilings(rt.proxy.local_tiers, ticket_tiers)
    sizing = size_text(ceilings)

    def _call(
        sop: str, schema: dict[str, Any], content: str, effort: Effort = None
    ) -> dict[str, Any]:
        completion = rt.proxy.complete(
            NODE_ID,
            TIER,
            sop,
            [{"role": "user", "content": content}],
            purpose="ideation",
            schema=schema,
            effort=effort,
        )
        return completion.parsed or {}

    def _self_revise(
        sop: str,
        schema: dict[str, Any],
        content: str,
        payload: dict[str, Any],
        gaps: tuple[str, ...],
        what: str,
    ) -> dict[str, Any]:
        """The one self-review revision: a repair of ``payload`` first; a repair
        whose own review still flags a gap gives way to today's fresh revision."""
        _count_gaps(payload)
        repaired = _call(
            sop,
            schema,
            content + _repair_block(gaps, _payload_text(payload), _SELF, what),
            _REPAIR_EFFORT,
        )
        left = _self_review_gaps(repaired)
        if not left:
            _outcome("repair.closed", f"self-review of the {what}", gaps)
            return repaired
        _outcome("repair.fallback", f"self-review of the {what}", left)
        return _call(sop, schema, content + _self_revision_text(gaps, what))

    def _plan_epic(
        content: str, self_review: bool, effort: Effort = None
    ) -> tuple[dict[str, Any], bool]:
        payload = _call(EPIC_SOP, EPIC_SCHEMA, content, effort)
        gaps = _self_review_gaps(payload) if self_review else ()
        if gaps:
            payload = _self_revise(
                EPIC_SOP, EPIC_SCHEMA, content, payload, gaps, "ticket list for your epic"
            )
        return payload, bool(gaps)

    def _plan_epics(
        epics: Sequence[_Epic],
        contents: Sequence[str],
        overflow: list[str],
        *,
        self_review: bool = True,
        efforts: Sequence[Effort] = (),
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...], list[str], list[str]]:
        """Run the epic calls, each at its ``efforts`` entry (default: the default
        effort); returns their raw items, tagged review gaps (missing, omissions),
        the self-revised epic ids and concerns. Tickets an epic loses at its cap
        are named in ``overflow``."""
        calls = list(zip(contents, efforts or [None] * len(contents), strict=True))
        results = _run_parallel(lambda ce: _plan_epic(ce[0], self_review, ce[1]), calls)
        raw: list[dict[str, Any]] = []
        missing: list[str] = []
        omissions: list[str] = []
        revised: list[str] = []
        concerns: list[str] = []
        for epic, (payload, self_revised) in zip(epics, results, strict=True):
            items, item_concerns = _epic_items(epic, payload.get("tickets"), overflow)
            raw += items
            concerns += item_concerns
            review = _section(payload, "criteria_review")
            missing += _tagged(epic.id, _strs(review.get("missing")))
            omissions += _tagged(epic.id, _strs(review.get("security_abuse_omissions")))
            if self_revised:
                revised.append(epic.id)
        return raw, tuple(missing), tuple(omissions), revised, concerns

    def _rerun_epics(
        epics: Sequence[_Epic],
        raw: Sequence[dict[str, Any]],
        targets: tuple[str, ...],
        findings: tuple[str, ...],
        mem: _PriorPlan,
        inputs: tuple[str, str, str],
        overflow: list[str],
        *,
        repair: bool,
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...], list[str]]:
        """The in-node revision of only the epics ``targets`` names, each handed the
        findings naming it; every other epic's items stand. A ``repair`` corrects
        each epic's tickets in place at lower effort. Returns the merged items, the
        re-run epics' review gaps and concerns."""
        intake, brief, iface_text = inputs
        chosen = [e for e in epics if e.id in targets]
        contents = [
            _epic_content(
                intake,
                brief,
                epics,
                e,
                iface_text,
                _epic_revision_text(
                    tuple(f for f in findings if e.id in (_target_epics((f,), mem) or ())),
                    mem,
                    e.id,
                    e.id,
                    _CHECK,
                    repair=repair,
                ),
            )
            for e in chosen
        ]
        fresh, missing, omissions, _, concerns = _plan_epics(
            chosen,
            contents,
            overflow,
            self_review=False,
            efforts=[_REPAIR_EFFORT if repair else None] * len(chosen),
        )
        merged = [
            item
            for e in epics
            for item in (
                [i for i in fresh if i.get(_EPIC_KEY) == e.id]
                if e.id in targets
                else [i for i in raw if i.get(_EPIC_KEY) == e.id]
            )
        ]
        return merged, missing, omissions, concerns

    def _patch(
        state: FactoryState,
        findings: Sequence[str],
        items: Sequence[dict[str, Any]],
        contracts: Sequence[tuple[str, str]],
        frame: _Frame,
        structural: frozenset[str],
    ) -> _Fit | None:
        """One delta call closing ``findings``, merged into ``items`` and put through
        the same assembly as a full plan; the merged plan's fit. None (re-plan in
        full) for a delta that does not merge or flags a security/abuse omission,
        or a merged plan still failing a check, dropping work at a cap, or with a
        gate-checked criteria problem outside ``structural``. The caller keeps the
        plan's own criteria review: the delta never saw it, so it cannot clear it."""
        epic_ids = frozenset(e.id for e in frame.epics)
        shown = _affected(findings, items, (n for n, _ in contracts))
        try:
            delta = _call(
                DELTA_SOP,
                DELTA_SCHEMA,
                _delta_text(
                    str(state.get("request", "")) + sizing,
                    findings,
                    items,
                    shown,
                    contracts,
                    frame.brief,
                    epic_ids,
                ),
                _REPAIR_EFFORT,
            )
        except ProxyUnavailable as exc:
            # Only an unparseable reply falls back: a transport outage stays one.
            if not isinstance(exc.__cause__, StructuredOutputError):
                raise
            _rejected("its reply did not parse")
            return None
        merged = _merge_delta(items, delta, epic_ids, shown)
        if merged is None:
            return None
        if _strs(_section(delta, "criteria_review").get("security_abuse_omissions")):
            _rejected("it reports a security/abuse omission")
            return None
        fit = _assemble(merged, frame)
        if fit.blocking:
            _rejected(
                f"{len(fit.blocking)} finding(s) still open: " + " | ".join(fit.blocking)[:400]
            )
            return None
        if fit.overflow:
            _rejected("the merged plan drops work at a cap")
            return None
        if new := set(_structural_findings(fit.criteria)) - structural:
            _rejected("new criteria problem(s): " + " | ".join(sorted(new))[:400])
            return None
        _outcome("delta.accepted", "tickets", findings)
        return fit

    def _draft(
        state: FactoryState,
        payload: dict[str, Any],
        seqs: dict[str, Seq],
        languages: set[str],
        findings: tuple[str, ...] | None,
        prior: _PriorPlan | None,
        source: tuple[str, str] = _GATE,
        *,
        self_review: bool = True,
        repair: bool = False,
    ) -> _Draft:
        """Call 1's reply -> the epic calls (when it has epics) -> repaired, checked
        tickets. ``findings``/``prior`` make the epic calls revisions, ``repair``
        lower-effort repairs of the tickets ``prior`` carries."""
        pinned = state.get("stack")
        intent = _section(payload, "intent")
        must_have_ids, must_haves = _goals(intent.get("must_haves"), "MH")
        success_ids, success = _goals(intent.get("success_criteria"), "SC")
        deliverable = _str(intent.get("deliverable")).strip().lower()
        brief = IntentBrief(
            intent=_str(intent.get("intent")),
            assumptions=_strs(intent.get("assumptions")),
            unknowns=_strs(intent.get("unknowns")),
            success_criteria=success,
            must_haves=must_haves,
            nice_to_haves=_strs(intent.get("nice_to_haves")),
            must_have_ids=must_have_ids,
            success_criterion_ids=success_ids,
            deliverable=deliverable if deliverable in DELIVERABLES else "",
            seq=seqs["brief"],
        )
        interfaces = _build_interfaces(payload.get("interfaces"), seqs["interfaces"], store=rt.cas)
        base = _base(state)
        existing = base.contract_names if base is not None else frozenset()
        names = {i.name for i in interfaces} | existing
        overflow: list[str] = []
        epics, concerns = _build_epics(payload.get("epics"), names, overflow)
        inline = _objs(payload.get("tickets"))
        review = _section(payload, "criteria_review")
        missing = _strs(review.get("missing"))
        omissions = _strs(review.get("security_abuse_omissions"))
        revised: list[str] = []
        inputs = ("", "", "")
        raw: list[dict[str, Any]] = inline
        per_epic = bool(epics) and (len(epics) > 1 or not inline)
        if per_epic:
            if inline:
                concerns.append(
                    f"Ignored {len(inline)} inline ticket(s): the plan has "
                    f"{len(epics)} epics, each planned in its own call."
                )
            inputs = (
                _intake_text(state) + sizing,
                _plan_brief(payload, brief),
                _interfaces_text(
                    (_str(i.get("name")), _contract_text(i))
                    for i in _objs(payload.get("interfaces"))
                ),
            )
            continues = _match_prior_epics(epics, prior) if prior else {}
            contents = [
                _epic_content(
                    inputs[0],
                    inputs[1],
                    epics,
                    e,
                    inputs[2],
                    _epic_revision_text(
                        findings, prior, e.id, continues.get(e.id), source, repair=repair
                    )
                    if findings
                    else "",
                )
                for e in epics
            ]
            raw, epic_missing, epic_omissions, revised, more = _plan_epics(
                epics,
                contents,
                overflow,
                self_review=self_review,
                # An epic with no previous one to repair is planned afresh.
                efforts=[_REPAIR_EFFORT if repair and continues.get(e.id) else None for e in epics],
            )
            missing += epic_missing
            omissions += epic_omissions
            concerns += more
        # stack is single-writer: an operator pin or an earlier pass wins.
        decided, stack_concerns = (
            _stack_decision(payload, languages) if pinned is None else (None, [])
        )
        raw_stack = _section(payload, "stack")
        generated, gen_concerns = _generated_tickets(
            interfaces, raw_stack, pinned or decided, seqs["tickets"], base
        )
        concerns += [*stack_concerns, *gen_concerns]
        raw_run = _str(raw_stack.get("run")).strip()
        stack_update = decided
        if (
            pinned is not None
            and not pinned.profile
            and not pinned.run
            and raw_run
            and _str(raw_stack.get("language")).strip().lower() == pinned.language
        ):
            # A planner-chosen stack (no operator profile) from an earlier pass may
            # gain its run command, so a missing-run finding is fixable by re-plan.
            stack_update = pinned.model_copy(update={"run": raw_run})
        current = stack_update or pinned
        chosen = pinned or decided
        language = (
            chosen.language
            if chosen is not None
            else _str(raw_stack.get("language")).strip().lower()
        )
        frame = _Frame(
            names=names,
            epics=epics if per_epic else (),
            cap=MAX_PLAN_TICKETS if per_epic else MAX_TICKETS,
            generated=generated,
            stack=current,
            brief=brief,
            run=None
            if current is not None and current.profile
            else (current.run if current is not None else raw_run),
            declared=frozenset(i.name for i in interfaces if _declaration_only(i)) | existing,
            interfaces=interfaces,
            language=language,
            compiled=_compile(interfaces, language, current, base),
            tickets_seq=seqs["tickets"],
            criteria_seq=seqs["criteria"],
            base=base,
            stack_findings=_stack_command_findings(decided),
            ceilings=ceilings,
            keep=_kept_ids(state, rt),
        )
        return _Draft(
            payload=payload,
            brief=brief,
            interfaces=interfaces,
            epics=epics,
            per_epic=per_epic,
            raw=raw,
            epic_overflow=tuple(overflow),
            epic_inputs=inputs,
            missing=missing,
            omissions=omissions,
            revised_epics=tuple(revised),
            concerns=tuple(concerns),
            decided=decided,
            stack_update=stack_update,
            frame=frame,
            fit=_assemble(raw, frame, overflow),
        )

    def _self_check(
        state: FactoryState,
        draft: _Draft,
        content: str,
        schema: dict[str, Any],
        seqs: dict[str, Seq],
        languages: set[str],
    ) -> tuple[_Draft, str]:
        """The one in-node revision of a plan the checks still fault: an epic plan
        re-runs only the epics the findings name; a finding naming none (or an
        inline plan) re-plans call 1 with every finding. Each revision is first a
        repair of the failed plan at lower effort; a repair leaving any finding gets
        one fresh revision at the default effort. Of the original, the repair and
        the fresh revision the one with the fewest findings is kept, a tie going to
        the later. Returns the draft and a journal note."""
        blocking = draft.fit.blocking
        if not blocking:
            return draft, ""
        notes = (*blocking, *draft.fit.overflow)
        _count_findings(notes)
        mem = _draft_prior(draft, seqs["prd"]) if draft.per_epic else None
        targets = _target_epics(blocking, mem) if mem is not None else None
        if mem is not None and targets:
            what = f"epic(s) {', '.join(targets)}"

            def rerun(repair: bool) -> _Draft:
                over = _not_of(draft.epic_overflow, targets)
                raw, missing, omissions, concerns = _rerun_epics(
                    draft.epics,
                    draft.raw,
                    targets,
                    notes,
                    mem,
                    draft.epic_inputs,
                    over,
                    repair=repair,
                )
                return replace(
                    draft,
                    raw=raw,
                    epic_overflow=tuple(over),
                    missing=(*_untagged(draft.missing, targets), *missing),
                    omissions=(*_untagged(draft.omissions, targets), *omissions),
                    concerns=(*draft.concerns, *concerns),
                    fit=_assemble(raw, draft.frame, over),
                )

            repaired = rerun(repair=True)
            replanned: _Draft | None = None
            if left := (*repaired.fit.blocking, *repaired.fit.overflow):
                _outcome("repair.fallback", what, left)
                replanned = rerun(repair=False)
            else:
                _outcome("repair.closed", what, blocking)
        else:
            items = _ticket_items(
                draft.fit.tickets,
                draft.fit.criteria,
                _ticket_epics(draft.fit.tickets) if draft.per_epic else {},
            )
            # An open review gap needs the full re-plan: the delta never sees it.
            if draft.missing or draft.omissions:
                progress.log("planner delta skipped: the review has an open gap")
            elif not _patchable(blocking, items, draft.frame, draft.fit.overflow):
                progress.log("planner delta skipped: a finding needs more than the tickets changed")
            else:
                fit = _patch(
                    state,
                    notes,
                    items,
                    [
                        (_str(i.get("name")), _contract_text(i))
                        for i in _objs(draft.payload.get("interfaces"))
                    ],
                    draft.frame,
                    frozenset(_structural_findings(draft.fit.criteria)),
                )
                if fit is not None:
                    return replace(draft, fit=fit), _patch_note(len(blocking))
            what = "the plan"
            # An epic plan's tickets reach their own epic calls, not call 1.
            plan = _payload_text(draft.payload, None if draft.per_epic else items)
            repaired = _draft(
                state,
                _call(SOP, schema, content + _repair_block(notes, plan, _CHECK), _REPAIR_EFFORT),
                seqs,
                languages,
                notes,
                mem,
                _CHECK,
                self_review=False,
                repair=True,
            )
            replanned = None
            if left := (*repaired.fit.blocking, *repaired.fit.overflow):
                _outcome("repair.fallback", what, left)
                text = _revision_block(
                    notes, _criteria_text(draft.fit.criteria), _goals_text(draft.brief), _CHECK
                )
                replanned = _draft(
                    state,
                    _call(SOP, schema, content + text),
                    seqs,
                    languages,
                    notes,
                    mem,
                    _CHECK,
                    self_review=False,
                )
            else:
                _outcome("repair.closed", what, blocking)
        revisions = (repaired,) if replanned is None else (repaired, replanned)
        pick = _best(draft.fit, *(r.fit for r in revisions))
        chosen = (draft, *revisions)[pick]
        if replanned is not None:
            event = ("replan.kept_original", "replan.kept_repair", "replan.used")[pick]
            _outcome(event, what, chosen.fit.blocking)
        before = len(blocking)
        if pick == 0:
            after = min(len(r.fit.blocking) for r in revisions)
            return draft, _revision_note(what, before, after, kept=True)
        return chosen, _revision_note(what, before, len(chosen.fit.blocking), kept=False)

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        findings = latest_spec_failure(state)
        if not findings and (asked := pending_interface_replans(state)):
            return revise_interfaces(rt, state, config, asked, _call)
        if findings:
            _count_findings(findings)
        prior = _prior_plan(state, rt) if findings else None
        targets = _target_epics(findings, prior) if findings and prior else None
        if findings and prior and targets:
            return _replan_epics(state, config, prior, targets, findings)
        earlier = (prior or _prior_plan(state, rt, inline=True)) if findings else None
        if findings:
            patched = _patch_replan(state, config, earlier, findings)
            if patched is not None:
                return patched
        spans = _retrieve_spans(index, state)
        if toolchains is None:
            from foundry.toolchains import available_toolchains

            tools: tuple[Toolchain, ...] = available_toolchains()
        else:
            tools = tuple(toolchains)
        languages = {t.language for t in tools}
        pinned = state.get("stack")
        schema = _plan_schema(languages | ({pinned.language} if pinned else set()))
        content = _user_content(state, rt, spans, tools) + sizing
        store = rt.cas

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        seqs = {
            "brief": seq("artifact:intent_brief"),
            "prd": seq("artifact:prd"),
            "interfaces": seq("artifact:interfaces"),
            "tickets": seq("artifact:tickets"),
            "criteria": seq("artifact:acceptance_criteria"),
        }
        gaps: tuple[str, ...] = ()
        draft: _Draft | None = None
        if earlier is not None:
            # A Spec-gate re-plan repairs the previous pass first; only a repair the
            # gate would still fail falls back to the fresh re-plan below.
            repaired = _draft(
                state,
                _call(
                    SOP,
                    schema,
                    _user_content(state, rt, spans, tools, _prior_plan_text(earlier)) + sizing,
                    _REPAIR_EFFORT,
                ),
                seqs,
                languages,
                findings,
                prior,
                self_review=False,
                repair=True,
            )
            if left := _open(
                repaired.fit, repaired.missing, repaired.omissions, findings or (), earlier
            ):
                _outcome("repair.fallback", "Spec-gate re-plan", left)
            else:
                _outcome("repair.closed", "Spec-gate re-plan", findings or ())
                draft = repaired
        if draft is None:
            payload = _call(SOP, schema, content)
            # One free in-node revision when the self-review flags gaps, so they don't
            # spend the shared Spec-gate fix budget: a repair, then at most one fresh call.
            gaps = _self_review_gaps(payload)
            if gaps:
                payload = _self_revise(SOP, schema, content, payload, gaps, "plan")
            draft = _draft(state, payload, seqs, languages, findings, prior)
        draft, check_note = _self_check(state, draft, content, schema, seqs, languages)
        _count_folds(draft.fit)
        payload, brief, interfaces, epics = (
            draft.payload,
            draft.brief,
            draft.interfaces,
            draft.epics,
        )
        per_epic, fit, tickets_seq = draft.per_epic, draft.fit, seqs["tickets"]
        tickets, criteria = fit.tickets, fit.criteria
        # Only a Spec-gate re-plan replaces an earlier plan of this run.
        dropped = (
            _retired_tickets(state, rt, tickets, tickets_seq) if latest_spec_failure(state) else []
        )
        prd_section = _section(payload, "prd")
        prd_seq = seqs["prd"]
        prd = PRD(
            title=_str(prd_section.get("title")),
            prd_uri=persist_prose(_str(prd_section.get("body")), store=store),
            required_capabilities=_strs(prd_section.get("required_capabilities")),
            criterion_ids=tuple(c.id for c in criteria),
            seq=prd_seq,
        )

        critique_seq = seq("artifact:criteria_findings")
        critique = CriteriaCritique(
            prd_uri=prd.prd_uri,
            missing=draft.missing,
            security_abuse_omissions=draft.omissions,
            # The plan reviews its own criteria: same call, same lineage — say so.
            judgment_provenance=JudgmentProvenance.SELF,
            seq=critique_seq,
            contract_findings=fit.contract_findings,
            coverage_findings=fit.coverage_findings,
        )

        sprint = _section(payload, "sprint_plan")
        sprint_seq = seq("artifact:sprint_plan")
        proposed = _int(sprint.get("usd_cap_microusd"))
        cap = min(proposed, _route_cap(state)) if proposed > 0 else _route_cap(state)
        plan = SprintPlan(
            milestones=_strs(sprint.get("milestones")),
            usd_cap_microusd=cap,
            risk_register_uri=cas_uri(_str(sprint.get("risk_register"))),
            done_definition=_str(sprint.get("done_definition")),
            seq=sprint_seq,
        )

        adrs = _build_adrs(payload.get("adrs"), seq("artifact:adrs"), store=store)
        ownership_seq = seq("artifact:ownership_map")
        ownership = _ownership_from_tickets(tickets, ownership_seq)
        design_section = _section(payload, "design")
        design_seq = seq("artifact:system_design")
        design = SystemDesign(
            title=_str(design_section.get("title")),
            design_uri=persist_prose(_str(design_section.get("body")), store=store),
            components=_strs(design_section.get("components")),
            adr_ids=tuple(a.id for a in adrs),
            interface_ids=tuple(i.name for i in interfaces),
            seq=design_seq,
        )
        constraints_seq = seq("pinned_constraints")
        constraints = _build_constraints(
            [
                *_objs(payload.get("pinned_constraints")),
                *_stack_constraints(draft.frame.stack, payload),
            ],
            constraints_seq,
        )
        retired = _retired_constraints(state, constraints, constraints_seq)

        epics_seq = seq("artifact:epics")
        epic_records = [
            Epic(
                id=e.id,
                title=e.title,
                summary=e.summary,
                specialty_mix=e.specialty_mix,
                depends_on=e.depends_on,
                provides=e.provides,
                consumes=e.consumes,
                seq=epics_seq._replace(local=i),
            )
            for i, e in enumerate(epics if per_epic else ())
        ]
        artifacts = [
            emit_artifact(brief, "intent_brief", NODE_ID, seqs["brief"], store=store),
            *(emit_artifact(e, EPIC_KIND, NODE_ID, e.seq, store=store) for e in epic_records),
            emit_artifact(prd, "prd", NODE_ID, prd_seq, store=store),
            *(
                emit_artifact(c, "acceptance_criterion", NODE_ID, c.seq, store=store)
                for c in criteria
            ),
            emit_artifact(critique, "criteria_findings", NODE_ID, critique_seq, store=store),
            emit_artifact(plan, "sprint_plan", NODE_ID, sprint_seq, store=store),
            emit_artifact(design, "system_design", NODE_ID, design_seq, store=store),
            *(emit_artifact(a, "adr", NODE_ID, a.seq, store=store) for a in adrs),
            *(
                emit_artifact(i, "interface_contract", NODE_ID, i.seq, store=store)
                for i in interfaces
            ),
            *(
                emit_artifact(t, TICKET_KIND, NODE_ID, t.seq, store=store)
                for t in (*tickets, *dropped)
            ),
            emit_artifact(ownership, "ownership_map", NODE_ID, ownership_seq, store=store),
        ]

        out: dict[str, Any] = {
            "artifacts": artifacts,
            "pinned_constraints": [*constraints, *retired],
            "required_capabilities": list(prd.required_capabilities),
            "gate_triage": [_triage(critique, prd.prd_uri, seq("gate_triage:spec"))],
        }
        if draft.stack_update is not None:
            out["stack"] = draft.stack_update
        # scope_tier is single-writer: an operator pin or an earlier pass wins.
        scope = state.get("scope_tier")
        if scope is None:
            scope = _scope_decision(state, payload, seq("scope_tier"))
            out["scope_tier"] = scope

        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        concerns: list[str] = [*draft.concerns, *fit.concerns]
        if dropped:
            concerns.append("Re-plan retired ticket(s) " + ", ".join(t.id for t in dropped) + ".")
        if not criteria:
            concerns.append("No acceptance criteria derived from the intent.")
        if not ownership.owners:
            concerns.append("Empty ownership map — no build tasks will be created.")
        if scope.tier is ScopeTier.DEMO:
            concerns.append("Demo scope caps the build.")
        if any(o.strip() for o in critique.security_abuse_omissions):
            concerns.append("Self-review left security/abuse omissions open.")
        if fit.contract_findings:
            concerns.append(f"Contract fit check: {len(fit.contract_findings)} finding(s).")
        if fit.coverage_findings:
            concerns.append(f"Coverage check: {len(fit.coverage_findings)} finding(s).")
        stack = draft.frame.stack
        epic_note = (
            f" Tickets planned per epic in {len(epics)} parallel call(s)"
            f" ({', '.join(e.id for e in epics)})."
            + (
                f" Self-revised epic(s) {', '.join(draft.revised_epics)} once."
                if draft.revised_epics
                else ""
            )
            if per_epic
            else ""
        )
        out["agent_journal"] = [
            journal_entry(
                node_id=NODE_ID,
                role=ROLE,
                seq=seq("journal"),
                summary=(
                    f"Planned {prd.title!r} ({scope.tier.value} scope): "
                    f"{len(criteria)} acceptance criteria ({high_risk} high-risk), "
                    f"{len(adrs)} ADRs, {len(interfaces)} interface contracts, "
                    f"{len(tickets)} tickets, "
                    f"{len(ownership.owners)} ownership units, {len(constraints)} "
                    "pinned constraints."
                    + (
                        f" Stack: {stack.language}"
                        + (f" ({stack.profile} profile)." if stack.profile else ".")
                        if stack
                        else ""
                    )
                    + epic_note
                    + (" Revised after a Spec-gate failure." if findings else "")
                    + (" Self-revised once after its own criteria review." if gaps else "")
                    + check_note
                ),
                assumptions=(
                    *brief.assumptions,
                    f"Retrieved {len(spans)} code span(s) for impact analysis.",
                ),
                concerns=tuple(concerns),
            )
        ]
        return out

    def _replan_epics(
        state: FactoryState,
        config: RunnableConfig,
        prior: _PriorPlan,
        targets: tuple[str, ...],
        findings: tuple[str, ...],
    ) -> dict[str, Any]:
        """Spec-gate re-plan of only the epics the findings name: call 1 and the
        other epics stand; every dependency is re-derived over the merged set.
        Re-emits what the re-planned tickets change (tickets + tombstones,
        criteria, PRD criterion ids, critique, ownership); the plan-level
        records of the earlier pass stay current. The epic calls repair the epics'
        previous tickets at lower effort; what the merged plan still leaves open
        (the test :func:`_open` applies to a call-1 repair) gets one fresh in-node
        re-run, at the default effort, of the epics it names."""

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        chosen = [e for e in prior.epics if e.id in targets]
        inputs = (
            _intake_text(state) + sizing,
            prior.brief,
            _interfaces_text(prior.interfaces),
        )
        contents = [
            _epic_content(
                inputs[0],
                inputs[1],
                prior.epics,
                e,
                inputs[2],
                _epic_revision_text(
                    tuple(f for f in findings if e.id in (_target_epics((f,), prior) or ())),
                    prior,
                    e.id,
                    e.id,
                    repair=True,
                ),
            )
            for e in chosen
        ]
        overflow: list[str] = []
        fresh, missing, omissions, revised, concerns = _plan_epics(
            chosen, contents, overflow, efforts=[_REPAIR_EFFORT] * len(chosen)
        )
        raw = [
            item
            for e in prior.epics
            for item in (
                [i for i in fresh if i[_EPIC_KEY] == e.id]
                if e.id in targets
                else _prior_items(prior, e.id)
            )
        ]
        tickets_seq = seq("artifact:tickets")
        frame = _prior_frame(state, prior, tickets_seq, seq("artifact:acceptance_criteria"))
        fit = _assemble(raw, frame, overflow)
        rerun: tuple[str, ...] = ()
        check_note = ""
        where = f"Spec-gate re-plan of epic(s) {', '.join(targets)}"
        left = _open(fit, missing, omissions, findings, prior)
        if not left:
            _outcome("repair.closed", where, findings)
        else:
            _count_findings((*fit.blocking, *fit.overflow))
            mem = _with_fit(prior, fit)
            rerun = _named_epics(left, mem)
            if not rerun:
                progress.log(
                    f"planner repair ({where}) left {len(left)} finding(s) naming no epic: "
                    + " | ".join(left)[:400]
                )
            else:
                _outcome("repair.fallback", where, left)
                over = _not_of(overflow, rerun)
                raw2, missing2, omissions2, concerns2 = _rerun_epics(
                    prior.epics, raw, rerun, left, mem, inputs, over, repair=False
                )
                fit2 = _assemble(raw2, frame, over)
                before, after = len(fit.blocking), len(fit2.blocking)
                kept = _best(fit, fit2) == 0
                _outcome(
                    "replan.kept_repair" if kept else "replan.used",
                    f"epic(s) {', '.join(rerun)}",
                    fit2.blocking,
                )
                check_note = _revision_note(f"epic(s) {', '.join(rerun)}", before, after, kept)
                if not kept:
                    fit = fit2
                    missing = (*_untagged(missing, rerun), *missing2)
                    omissions = (*_untagged(omissions, rerun), *omissions2)
                    concerns += concerns2
                else:
                    rerun = ()
        replanned = (*targets, *rerun)
        old = prior.critique
        return _emit_replan(
            config,
            state,
            prior,
            fit,
            tickets_seq,
            missing=(*_untagged(old.missing if old else (), replanned), *missing),
            omissions=(
                *_untagged(old.security_abuse_omissions if old else (), replanned),
                *omissions,
            ),
            what=f"Re-planned epic(s) {', '.join(targets)}",
            note=(f" Self-revised epic(s) {', '.join(revised)} once." if revised else "")
            + check_note,
            concerns=concerns,
        )

    def _prior_frame(
        state: FactoryState, prior: _PriorPlan, tickets_seq: Seq, criteria_seq: Seq
    ) -> _Frame:
        stack = state.get("stack")
        base = _base(state)
        existing = base.contract_names if base is not None else frozenset()
        return _Frame(
            names={name for name, _ in prior.interfaces} | existing,
            epics=prior.epics,
            cap=MAX_PLAN_TICKETS if prior.epics else MAX_TICKETS,
            generated=prior.generated,
            stack=stack,
            brief=prior.intent,
            run=None if stack is None or stack.profile else stack.run,
            declared=prior.declared | existing,
            # The contracts are the earlier pass's, already checked there.
            interfaces=(),
            language="",
            compiled=CompileResult(),
            tickets_seq=tickets_seq,
            criteria_seq=criteria_seq,
            base=base,
            ceilings=ceilings,
            keep=_kept_ids(state, rt),
        )

    def _patch_replan(
        state: FactoryState,
        config: RunnableConfig,
        prior: _PriorPlan | None,
        findings: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """A Spec-gate re-plan as one delta over the previous pass's tickets, its
        plan-level records and contracts standing. None (re-plan in full) when a
        check finding needs more than the tickets changed, or when the merged plan
        would still fail the gate: a check finding, a criteria problem or a
        security/abuse omission left open."""
        if prior is None:
            return None
        old = prior.critique
        # The delta never sees the review: an open gap needs the full re-plan.
        if old is not None and (old.security_abuse_omissions or old.missing):
            progress.log("planner delta skipped: the review has an open gap")
            return None
        checked = (*old.contract_findings, *old.coverage_findings) if old else ()
        items = _ticket_items(prior.tickets, prior.criteria.values(), prior.epic_of)
        # Placeholder Seqs until the patch is accepted: a fallback mints its own.
        frame = _prior_frame(state, prior, prior.prd.seq, prior.prd.seq)
        if not _patchable(checked, items, frame, ()):
            progress.log("planner delta skipped: a finding needs more than the tickets changed")
            return None
        fit = _patch(state, findings, items, prior.interfaces, frame, frozenset())
        if fit is None:
            return None
        tickets_seq = rt.seq(config, NODE_ID, "artifact:tickets")
        criteria_seq = rt.seq(config, NODE_ID, "artifact:acceptance_criteria")
        fit = replace(
            fit,
            tickets=[
                t.model_copy(update={"seq": tickets_seq._replace(local=t.seq.local)})
                for t in fit.tickets
            ],
            criteria=[
                c.model_copy(update={"seq": criteria_seq._replace(local=c.seq.local)})
                for c in fit.criteria
            ],
        )
        return _emit_replan(
            config,
            state,
            prior,
            fit,
            tickets_seq,
            missing=(),
            omissions=(),
            what="Patched the plan",
            note=" Patched with one delta call instead of a full re-plan.",
            concerns=[],
        )

    def _emit_replan(
        config: RunnableConfig,
        state: FactoryState,
        prior: _PriorPlan,
        fit: _Fit,
        tickets_seq: Seq,
        *,
        missing: tuple[str, ...],
        omissions: tuple[str, ...],
        what: str,
        note: str,
        concerns: list[str],
    ) -> dict[str, Any]:
        """Re-emit what a re-plan of the tickets changes (tickets + tombstones,
        criteria, PRD criterion ids, critique, ownership); the plan-level records
        of the earlier pass stay current."""
        store = rt.cas

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        _count_folds(fit)
        tickets, criteria = fit.tickets, fit.criteria
        dropped = _retired_tickets(state, rt, tickets, tickets_seq)
        prd_seq = seq("artifact:prd")
        prd = prior.prd.model_copy(
            update={"criterion_ids": tuple(c.id for c in criteria), "seq": prd_seq}
        )
        critique_seq = seq("artifact:criteria_findings")
        critique = CriteriaCritique(
            prd_uri=prd.prd_uri,
            missing=missing,
            security_abuse_omissions=omissions,
            judgment_provenance=JudgmentProvenance.SELF,
            seq=critique_seq,
            contract_findings=fit.contract_findings,
            coverage_findings=fit.coverage_findings,
        )
        ownership_seq = seq("artifact:ownership_map")
        ownership = _ownership_from_tickets(tickets, ownership_seq)
        artifacts = [
            emit_artifact(prd, "prd", NODE_ID, prd_seq, store=store),
            *(
                emit_artifact(c, "acceptance_criterion", NODE_ID, c.seq, store=store)
                for c in criteria
            ),
            emit_artifact(critique, "criteria_findings", NODE_ID, critique_seq, store=store),
            *(
                emit_artifact(t, TICKET_KIND, NODE_ID, t.seq, store=store)
                for t in (*tickets, *dropped)
            ),
            emit_artifact(ownership, "ownership_map", NODE_ID, ownership_seq, store=store),
        ]
        concerns = [*concerns, *fit.concerns]
        if dropped:
            concerns.append("Re-plan retired ticket(s) " + ", ".join(t.id for t in dropped) + ".")
        if critique.spec_gate_blocking():
            concerns.append("Self-review left security/abuse omissions open.")
        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        return {
            "artifacts": artifacts,
            "gate_triage": [_triage(critique, prd.prd_uri, seq("gate_triage:spec"))],
            "agent_journal": [
                journal_entry(
                    node_id=NODE_ID,
                    role=ROLE,
                    seq=seq("journal"),
                    summary=(
                        f"{what} of {prd.title!r}: "
                        f"{len(criteria)} acceptance criteria ({high_risk} high-risk), "
                        f"{len(tickets)} tickets"
                        + (f" across {len(prior.epics)} epics" if prior.epics else "")
                        + f", {len(ownership.owners)} ownership units. "
                        "Revised after a Spec-gate failure." + note
                    ),
                    concerns=tuple(concerns),
                )
            ],
        }

    return _node
