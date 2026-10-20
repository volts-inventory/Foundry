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
  ``interface_contract`` (each), ``ownership_map`` — content-addressed handles.
* ``pinned_constraints``, ``required_capabilities``, ``scope_tier`` (only when
  not already decided — it is single-writer), an advisory ``gate_triage`` on the
  ``spec`` gate, and ONE ``agent_journal`` entry.

The ownership map also carries ``unit_criteria`` (criterion ids per ownership
unit), so the per-lane leads decompose deterministically with no model call.

Re-entry: ``spec_fix`` routes back here. The newest FAILing Spec-gate verdict's
findings, and the criteria already declared, go into the prompt so the revision
closes the gap instead of re-deriving the same plan.

Trust posture is unchanged: the node emits no ``GateResult`` (L5), reaches the
model only via ``rt.proxy`` (L6), and its self-review of the criteria is stamped
``JudgmentProvenance.SELF`` — the signed Spec gate still decides.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from foundry.agents._common import (
    as_text,
    cas_uri,
    demo_directives,
    emit_artifact,
    journal_entry,
    persist_prose,
    scope_directive,
)
from foundry.schemas.architecture import (
    ADR,
    ConstraintScope,
    InterfaceContract,
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.gates import GateStatus, GateTriage, JudgmentProvenance
from foundry.schemas.governance import ScopeDecision, ScopeTier
from foundry.schemas.product import (
    PRD,
    AcceptanceCriterion,
    CriteriaCritique,
    Criticality,
    IntentBrief,
    SprintPlan,
)
from foundry.state.reducers import live_constraints

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.context import SymbolIndex
    from foundry.context.retriever import Span
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.state.factory_state import FactoryState
    from foundry.state.seq import Seq

__all__ = ["NODE_ID", "PLAN_SCHEMA", "SOP", "TIER", "make_planner_node"]

NODE_ID = "planner"
ROLE = "Planner"
TIER = "T1"  # everything downstream inherits this plan; never auto-downgrade
_SPEC_GATE = "spec"

# Deterministic, bounds token cost on a large design doc; the full spec stays on
# the `spec` channel.
_SPEC_PROMPT_CAP = 24_000
_RETRIEVAL_K = 24

# Per-route budget envelopes (micro-USD) the sprint plan's cap is clamped into
# (governance §7): the plan may refine the Triage-seeded cap down, never widen it.
_ROUTE_CAP_MICROUSD: dict[str, int] = {
    "greenfield": 50_000_000,
    "evolution": 25_000_000,
    "bugfix": 5_000_000,
    "spike": 10_000_000,
}
_DEFAULT_CAP_MICROUSD = 25_000_000

SOP = (
    "You are the Planner. In ONE reply, turn the request into a complete, buildable, "
    "testable plan: intent, product spec, acceptance criteria, delivery plan, and "
    "architecture. Fill every section of the constrained JSON schema.\n"
    "SCOPE (scope.tier): 'demo' only when the request is for something to look at or "
    "learn from (demo, toy, example, prototype, spike, exercise) that nobody depends "
    "on; 'standard' otherwise. Judge the subject, not the length; real users, "
    "production, deployment, money, compliance or persisted real data mean "
    "'standard'. When in doubt, 'standard'. Quote the phrases you relied on.\n"
    "INTENT: restate the intent in your own words; list assumptions and open "
    "unknowns; define success in user terms; flag must-haves vs nice-to-haves.\n"
    "PRD: the problem, user stories and prioritization; declare "
    "required_capabilities (e.g. 'ui', 'ml') — they drive specialist routing.\n"
    "ACCEPTANCE CRITERIA: each a testable Given/When/Then with a stable id and a "
    "non-empty oracle_hint saying how a runner-evaluable test decides it. Mark "
    "high-risk criteria 'high-risk', the rest 'normal'. Identify how the component "
    "can be MISUSED or ABUSED (malformed/wrong-type input, invalid operations on an "
    "empty or boundary state, resource exhaustion, authn/authz for anything exposed) "
    "and author at least one criterion covering the applicable case with "
    "security_abuse=true. ALWAYS author one criterion requiring a pinned dependency "
    "manifest for the stack at the repo root (requirements.txt / uv.lock / "
    "package-lock.json / go.sum / Cargo.lock; pin dev/test deps if there are no "
    "runtime deps), with an oracle_hint that a recognized manifest is present.\n"
    "CRITERIA REVIEW: then review your own criteria against the request. Under "
    "criteria_review list only REAL, in-scope gaps you could not close: missing / "
    "under-specified criteria, and applicable security/abuse cases no criterion "
    "covers. Prefer closing a gap by adding a criterion; a listed security/abuse "
    "omission FAILS the Spec gate. Return empty arrays when coverage is complete.\n"
    "SPRINT PLAN: ordered milestones; usd_cap_microusd as an integer number of "
    "micro-USD (never a float); a short risk register; done-for-this-iteration.\n"
    "ARCHITECTURE: choose the component decomposition; write ADRs WITH rejected "
    "alternatives; define interface/API contracts; assign every file/interface "
    "ownership unit an owner (units become independently built tasks, merged in "
    "owner order) and list, per unit, the acceptance-criterion ids it implements; "
    "emit cross-cutting concerns as pinned constraints, which are injected verbatim "
    "into every build task.\n"
    "When a DESIGN SPEC is provided, it is the detailed source of truth: derive the "
    "plan FROM it rather than from the one-line request alone."
)

_STR_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": ["demo", "standard"]},
                "rationale": {"type": "string"},
                "signals": _STR_ARRAY,
            },
            "required": ["tier", "rationale"],
            "additionalProperties": False,
        },
        "intent": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "assumptions": _STR_ARRAY,
                "unknowns": _STR_ARRAY,
                "success_criteria": _STR_ARRAY,
                "must_haves": _STR_ARRAY,
                "nice_to_haves": _STR_ARRAY,
            },
            "required": ["intent"],
            "additionalProperties": False,
        },
        "prd": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "required_capabilities": _STR_ARRAY,
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "acceptance_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "criticality": {"type": "string", "enum": ["normal", "high-risk"]},
                    "oracle_hint": {"type": "string"},
                    "security_abuse": {"type": "boolean"},
                },
                "required": ["id", "text", "oracle_hint"],
                "additionalProperties": False,
            },
        },
        "criteria_review": {
            "type": "object",
            "properties": {
                "missing": _STR_ARRAY,
                "security_abuse_omissions": _STR_ARRAY,
            },
            "required": ["missing", "security_abuse_omissions"],
            "additionalProperties": False,
        },
        "sprint_plan": {
            "type": "object",
            "properties": {
                "milestones": _STR_ARRAY,
                "usd_cap_microusd": {"type": "integer", "minimum": 0},
                "risk_register": {"type": "string"},
                "done_definition": {"type": "string"},
            },
            "required": ["milestones", "usd_cap_microusd", "done_definition"],
            "additionalProperties": False,
        },
        "design": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "components": _STR_ARRAY,
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "adrs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "decision": {"type": "string"},
                    "body": {"type": "string"},
                    "rejected_alternatives": _STR_ARRAY,
                },
                "required": ["id", "title", "decision", "body"],
                "additionalProperties": False,
            },
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "spec": {"type": "string"},
                    "consumers": _STR_ARRAY,
                },
                "required": ["name", "spec"],
                "additionalProperties": False,
            },
        },
        "ownership": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string"},
                    "owner": {"type": "string"},
                    "criterion_ids": _STR_ARRAY,
                },
                "required": ["unit", "owner", "criterion_ids"],
                "additionalProperties": False,
            },
        },
        "pinned_constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "local"]},
                    "source": {"type": "string"},
                },
                "required": ["id", "text", "scope", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "intent",
        "prd",
        "acceptance_criteria",
        "criteria_review",
        "sprint_plan",
        "design",
        "adrs",
        "interfaces",
        "ownership",
    ],
    "additionalProperties": False,
}


# ---- prompt assembly ---------------------------------------------------------


def _intake_text(state: FactoryState) -> str:
    """The raw request plus, when provided, the (capped) design-spec text."""
    request = str(state.get("request", ""))
    spec = str(state.get("spec", "") or "")
    if not spec.strip():
        return request
    return (
        f"REQUEST (intent summary):\n{request}\n\n"
        f"DESIGN SPEC (detailed source of truth):\n{spec[:_SPEC_PROMPT_CAP]}"
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
        caps = "" if revising else (
            scope_directive(state) + scope_directive(state, architect=True)
        )
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
    lines: list[str] = []
    for c in current.values():
        lines.append(
            f"- id={getattr(c, 'id', '?')} | {getattr(c, 'text', '')} "
            f"(criticality={getattr(c, 'criticality', 'normal')}; "
            f"oracle_hint={getattr(c, 'oracle_hint', '')!r}; "
            f"security_abuse={getattr(c, 'security_abuse', False)})"
        )
    return "\n".join(lines)


def _revision_text(state: FactoryState, rt: AgentRuntime) -> str:
    """The re-entry block (spec_fix -> planner), or "" on the first pass.

    The Spec gate reads the newest record per criterion id, so the revision fixes
    a criterion a finding names by re-emitting it under the same id."""
    findings = latest_spec_failure(state)
    if findings is None:
        return ""
    prior = _declared_criteria_text(state, rt)
    return (
        "\n\nREVISION PASS — the signed Spec gate FAILED your previous plan. Return "
        "the FULL plan again: reproduce every previously declared criterion (same "
        "id, never weakened; fix in place any criterion a finding names) AND add "
        "criteria that close every finding below.\n"
        "Spec gate findings:\n- " + "\n- ".join(findings) + "\n\n"
        f"Previously declared criteria:\n{prior or '(none resolved)'}"
    )


def _self_review_gaps(payload: dict[str, Any]) -> tuple[str, ...]:
    review = _section(payload, "criteria_review")
    return (
        *_strs(review.get("security_abuse_omissions")),
        *_strs(review.get("missing")),
    )


def _self_revision_text(gaps: tuple[str, ...]) -> str:
    return (
        "\n\nSELF-REVIEW REVISION — your own criteria_review flagged these gaps. "
        "Return the FULL plan again with criteria that close each one (same ids for "
        "criteria you keep); list under criteria_review only gaps that genuinely "
        "remain.\nFlagged gaps:\n- " + "\n- ".join(gaps)
    )


def _user_content(
    state: FactoryState, rt: AgentRuntime, spans: tuple[Span, ...]
) -> str:
    retrieved = [
        {"file": s.file, "lines": [s.line, s.end_line], "symbol": s.symbol} for s in spans
    ]
    body = _intake_text(state)
    if retrieved:
        body += "\n\nRETRIEVED CODE SPANS (existing code):\n" + as_text(retrieved)
    revision = _revision_text(state, rt)
    return body + _scope_text(state, revising=bool(revision)) + revision


# ---- record builders -----------------------------------------------------------


# CLI backends don't grammar-enforce the schema, so one off-schema field must
# degrade to a default rather than raise and lose the whole plan.

_E = TypeVar("_E", bound=StrEnum)


def _str(raw: Any, default: str = "") -> str:
    return default if raw is None else str(raw)


def _strs(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x) for x in raw if x is not None)


def _enum(cls: type[_E], raw: Any, default: _E) -> _E:
    key = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    for member in cls:
        if key in (member.value.lower(), member.name.lower().replace("_", "-")):
            return member
    return default


def _int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(float(str(raw).strip().replace(",", "").replace("_", "")))
    except (ValueError, OverflowError):  # also NaN / inf
        return 0


def _bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1"}
    return bool(raw)


def _objs(raw: Any) -> list[dict[str, Any]]:
    # CLI backends don't grammar-enforce the schema; skip a non-object.
    return [item for item in raw or () if isinstance(item, dict)]


def _section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _build_criteria(raw: Any, seq: Seq) -> list[AcceptanceCriterion]:
    out: list[AcceptanceCriterion] = []
    for i, item in enumerate(_objs(raw)):
        out.append(
            AcceptanceCriterion(
                id=_str(item.get("id"), f"AC-{i:03d}"),
                text=_str(item.get("text")),
                criticality=_enum(Criticality, item.get("criticality"), Criticality.NORMAL),
                oracle_hint=_str(item.get("oracle_hint")),
                security_abuse=_bool(item.get("security_abuse")),
                seq=seq._replace(local=i),
            )
        )
    return out


def _build_adrs(raw: Any, seq: Seq, *, store: ArtifactStore | None) -> list[ADR]:
    return [
        ADR(
            id=_str(item.get("id"), f"ADR-{i:03d}"),
            title=_str(item.get("title")),
            decision=_str(item.get("decision")),
            body_uri=persist_prose(_str(item.get("body")), store=store),
            rejected_alternatives=_strs(item.get("rejected_alternatives")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def _build_interfaces(raw: Any, seq: Seq) -> list[InterfaceContract]:
    return [
        InterfaceContract(
            name=_str(item.get("name"), f"iface-{i}"),
            spec_uri=cas_uri(_str(item.get("spec"))),
            consumers=_strs(item.get("consumers")),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def _build_constraints(raw: Any, seq: Seq) -> list[PinnedConstraint]:
    return [
        PinnedConstraint(
            id=_str(item.get("id"), f"PC-{i:03d}"),
            text=_str(item.get("text")),
            # Unknown scope is injected as global: the fail-safe direction.
            scope=_enum(ConstraintScope, item.get("scope"), ConstraintScope.GLOBAL),
            source=_str(item.get("source"), NODE_ID),
            seq=seq._replace(local=i),
        )
        for i, item in enumerate(_objs(raw))
    ]


def _build_ownership(raw: Any, seq: Seq, criterion_ids: set[str]) -> OwnershipMap:
    """A later entry for the same unit replaces the earlier one wholesale; ids
    naming no declared criterion are dropped."""
    owners: dict[str, str] = {}
    unit_criteria: dict[str, tuple[str, ...]] = {}
    for item in _objs(raw):
        unit = _str(item.get("unit")).strip()
        if not unit:
            continue
        owners[unit] = _str(item.get("owner")) or "owner"
        cids = tuple(c for c in _strs(item.get("criterion_ids")) if c in criterion_ids)
        if cids:
            unit_criteria[unit] = cids
        else:
            unit_criteria.pop(unit, None)
    return OwnershipMap(owners=owners, unit_criteria=unit_criteria, seq=seq)


def _retired_constraints(
    state: FactoryState, emitted: list[PinnedConstraint], seq: Seq
) -> list[PinnedConstraint]:
    """Tombstones for this node's prior live constraints a re-plan dropped.

    A record whose ``supersedes`` names its own id retires that id in
    ``live_constraints``; the newer Seq makes it the id's latest record."""
    kept = {c.id for c in emitted}
    stale = sorted(
        (
            c
            for c in live_constraints(list(state.get("pinned_constraints") or ()))
            if c.seq.node_id == NODE_ID and c.id not in kept
        ),
        key=lambda c: c.id,
    )
    return [
        c.model_copy(
            update={"supersedes": c.id, "seq": seq._replace(local=len(emitted) + i)}
        )
        for i, c in enumerate(stale)
    ]


def _route_cap(state: FactoryState) -> int:
    return _ROUTE_CAP_MICROUSD.get(str(state.get("route")), _DEFAULT_CAP_MICROUSD)


def _scope_decision(
    state: FactoryState, payload: dict[str, Any], seq: Seq
) -> ScopeDecision:
    if _forced_standard(state):
        return ScopeDecision(
            tier=ScopeTier.STANDARD,
            rationale="Standard scope: a design spec was supplied.",
            seq=seq,
        )
    scope = _section(payload, "scope")
    # Anything that is not exactly "demo" is STANDARD: an absent or garbled
    # answer must never silently cap the build.
    raw_tier = _str(scope.get("tier")).strip().lower()
    tier = ScopeTier.DEMO if raw_tier == ScopeTier.DEMO.value else ScopeTier.STANDARD
    return ScopeDecision(
        tier=tier,
        rationale=_str(scope.get("rationale")),
        signals=_strs(scope.get("signals")),
        seq=seq,
    )


def _triage(critique: CriteriaCritique, prd_uri: str, seq: Seq) -> GateTriage:
    """The advisory the Spec gate's operators read; the gate re-derives the FAIL
    from the materialized omissions itself (L5)."""
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
        name=_SPEC_GATE, run_against=prd_uri, explanation=explanation,
        produced_by=NODE_ID, seq=seq,
    )


# ---- the node ------------------------------------------------------------------


def make_planner_node(rt: AgentRuntime, index: SymbolIndex | None = None) -> NodeFn:
    """Build the Planner node (T1, one structured model call per pass, plus at
    most one revision call when its own criteria review flags gaps).

    ``index`` is the context retriever probed for existing code spans (``None``
    is greenfield — empty retrieval)."""

    def _plan(content: str) -> dict[str, Any]:
        completion = rt.proxy.complete(
            NODE_ID,
            TIER,
            SOP,
            [{"role": "user", "content": content}],
            purpose="ideation",
            schema=PLAN_SCHEMA,
        )
        return completion.parsed or {}

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        spans = _retrieve_spans(index, state)
        content = _user_content(state, rt, spans)
        payload = _plan(content)
        # One free in-node revision when the self-review flags gaps, so they don't
        # spend the shared Spec-gate fix budget; never more than one.
        gaps = _self_review_gaps(payload)
        if gaps:
            payload = _plan(content + _self_revision_text(gaps))
        store = rt.cas

        def seq(key: str) -> Seq:
            return rt.seq(config, NODE_ID, key)

        intent = _section(payload, "intent")
        brief_seq = seq("artifact:intent_brief")
        brief = IntentBrief(
            intent=_str(intent.get("intent")),
            assumptions=_strs(intent.get("assumptions")),
            unknowns=_strs(intent.get("unknowns")),
            success_criteria=_strs(intent.get("success_criteria")),
            must_haves=_strs(intent.get("must_haves")),
            nice_to_haves=_strs(intent.get("nice_to_haves")),
            seq=brief_seq,
        )

        prd_section = _section(payload, "prd")
        prd_seq = seq("artifact:prd")
        criteria = _build_criteria(
            payload.get("acceptance_criteria"), seq("artifact:acceptance_criteria")
        )
        prd = PRD(
            title=_str(prd_section.get("title")),
            prd_uri=persist_prose(_str(prd_section.get("body")), store=store),
            required_capabilities=_strs(prd_section.get("required_capabilities")),
            criterion_ids=tuple(c.id for c in criteria),
            seq=prd_seq,
        )

        review = _section(payload, "criteria_review")
        critique_seq = seq("artifact:criteria_findings")
        critique = CriteriaCritique(
            prd_uri=prd.prd_uri,
            missing=_strs(review.get("missing")),
            security_abuse_omissions=_strs(review.get("security_abuse_omissions")),
            # The plan reviews its own criteria: same call, same lineage — say so.
            judgment_provenance=JudgmentProvenance.SELF,
            seq=critique_seq,
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
        interfaces = _build_interfaces(payload.get("interfaces"), seq("artifact:interfaces"))
        ownership_seq = seq("artifact:ownership_map")
        ownership = _build_ownership(
            payload.get("ownership"), ownership_seq, {c.id for c in criteria}
        )
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
        constraints = _build_constraints(payload.get("pinned_constraints"), constraints_seq)
        retired = _retired_constraints(state, constraints, constraints_seq)

        artifacts = [
            emit_artifact(brief, "intent_brief", NODE_ID, brief_seq, store=store),
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
            emit_artifact(ownership, "ownership_map", NODE_ID, ownership_seq, store=store),
        ]

        out: dict[str, Any] = {
            "artifacts": artifacts,
            "pinned_constraints": [*constraints, *retired],
            "required_capabilities": list(prd.required_capabilities),
            "gate_triage": [_triage(critique, prd.prd_uri, seq("gate_triage:spec"))],
        }
        # scope_tier is single-writer: an operator pin or an earlier pass wins.
        scope = state.get("scope_tier")
        if scope is None:
            scope = _scope_decision(state, payload, seq("scope_tier"))
            out["scope_tier"] = scope

        high_risk = sum(1 for c in criteria if c.criticality is Criticality.HIGH_RISK)
        concerns: list[str] = []
        if not criteria:
            concerns.append("No acceptance criteria derived from the intent.")
        if not ownership.owners:
            concerns.append("Empty ownership map — no build tasks will be created.")
        if scope.tier is ScopeTier.DEMO:
            concerns.append("Demo scope caps the build.")
        if critique.spec_gate_blocking():
            concerns.append("Self-review left security/abuse omissions open.")
        revision = latest_spec_failure(state)
        out["agent_journal"] = [
            journal_entry(
                node_id=NODE_ID,
                role=ROLE,
                seq=seq("journal"),
                summary=(
                    f"Planned {prd.title!r} ({scope.tier.value} scope): "
                    f"{len(criteria)} acceptance criteria ({high_risk} high-risk), "
                    f"{len(adrs)} ADRs, {len(interfaces)} interface contracts, "
                    f"{len(ownership.owners)} ownership units, {len(constraints)} "
                    "pinned constraints."
                    + (" Revised after a Spec-gate failure." if revision else "")
                    + (" Self-revised once after its own criteria review." if gaps else "")
                ),
                assumptions=(
                    *brief.assumptions,
                    f"Retrieved {len(spans)} code span(s) for impact analysis.",
                ),
                concerns=tuple(concerns),
            )
        ]
        return out

    return _node
