"""`CheckpointDecision` — the human steering decision at a stage boundary. (plan [10])

The checkpoint/HITL mode pauses a build IN STAGES at the stage boundaries (after
product/spec, after architecture/design, after implementation+quality / before
release) so a human can INSPECT that stage's outputs and steer the run —
*approve* (continue), *reject* (halt), or *needs-changes* (route the stage back
for rework).

HARD TRUST CONSTRAINT (the whole point of [10]): a checkpoint decision is HUMAN
STEERING LAYERED ON TOP of the deterministic gates — it can HALT a run or send a
stage back for rework, but a checkpoint *approve* can NEVER turn a gate FAIL into
a pass, weaken a signed gate verdict, or substitute for the Design/Release
sign-off. The gates remain the SOLE trust authority (LEARNINGS L5: agents — and
humans steering them — propose; only signed gates decide). A ``CheckpointDecision``
lives on its OWN state channel (``checkpoint_decisions``), is read by NO gate, by
``merge_gates``, by ``route_after_gates``, by ``release_allowed``, or by
``admission``; the only thing an *approve* does structurally is let the run
PROCEED to the gate boundary — where the runner-attested verdict still decides.
A *reject* routes to END (no PR / no release); the gates are untouched either way.

Steering is AUTHORSHIP, not approval (lifecycle.md §2): a human who attaches a
steering ``note`` makes a substantive content contribution, so ``steered_by``
records that human in the author set for segregation-of-duties — by the enforced
"approver ∉ author set" predicate they cannot also serve as the Release sign-off
approver-of-record for the same artifact. The checkpoint approve therefore gates
HUMAN judgment ("looks on track"), never machine correctness.

Like every channel record it is a frozen, content-addressed model carrying a
causal-order :class:`Seq`; the union reducer keys on a stable per-decision
identity so a LangGraph resume re-fold coalesces the re-emission (idempotent, L7).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from foundry.schemas.architecture import OwnershipMap
from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Callable

    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

    # Reconstitutes a typed body from its content-addressed handle (CAS in prod).
    ArtifactResolver = Callable[[Artifact], Any]

# The Artifact.kind the planner stamps on the OwnershipMap handle.
# Held as a literal here (not imported from the agents layer) to keep schemas
# free of an agents dependency.
_OWNERSHIP_KIND = "ownership_map"

__all__ = [
    "CHECKPOINT_BOUNDARIES",
    "CheckpointBoundary",
    "CheckpointDecision",
    "CheckpointVerdict",
    "inspection_payload",
]


class CheckpointBoundary(StrEnum):
    """The three stage boundaries a checkpoint can pause at (plan [10]).

    Ordered by the SOP phase sequence: after the product/spec stage, after the
    architecture/design stage, and after implementation+quality but BEFORE the
    release boundary. The release checkpoint sits AFTER the Build+Test gate has
    run, so a human inspecting it sees the attested gate verdicts — and an
    *approve* there still cannot release a build whose gates FAIL (the emit guard
    re-checks the gates regardless)."""

    PRODUCT = "product"  # after product/spec (PRD + acceptance criteria)
    DESIGN = "design"  # after architecture/design (SystemDesign + constraints)
    RELEASE = "release"  # after implementation+quality, before release/emit


# The default ordered boundary set ``checkpoints=True`` enables (all three).
CHECKPOINT_BOUNDARIES: tuple[CheckpointBoundary, ...] = (
    CheckpointBoundary.PRODUCT,
    CheckpointBoundary.DESIGN,
    CheckpointBoundary.RELEASE,
)


class CheckpointVerdict(StrEnum):
    """A human's steering verdict at a checkpoint (plan [10], lifecycle.md §2).

    APPROVE continues the run to the next stage (the gates still decide release);
    REJECT halts the run (no PR / no release); NEEDS_CHANGES routes the stage back
    for bounded rework. NONE of these can turn a gate FAIL into a pass — they only
    HALT, hold, or let the run proceed to the (unchanged) gate boundary."""

    APPROVE = "approve"  # continue — proceed to the gate boundary (gates still decide)
    REJECT = "reject"  # halt the run (no PR / no release)
    NEEDS_CHANGES = "needs_changes"  # route the stage back for bounded rework


class CheckpointDecision(FrozenModel):
    """A recorded human steering decision at a stage boundary. NOT a trust input.

    Advisory-to-trust posture (see module doc): the only structural effect of an
    APPROVE is to let the run proceed to the gate boundary; a REJECT/NEEDS_CHANGES
    halts or re-routes. It can never mint or weaken a gate verdict. ``steered_by``
    + a non-empty ``note`` records steering AUTHORSHIP (lifecycle.md §2) so the
    same human cannot later be the Release approver-of-record (segregation of
    duties)."""

    boundary: CheckpointBoundary  # which stage boundary this decision was made at
    verdict: CheckpointVerdict
    state_sha: str  # the integrated state the decision was made against (provenance)
    subject_artifact_uri: str = ""  # the stage's decision-point artifact (advisory)
    steered_by: str = ""  # the human identity that steered (author-set, lifecycle §2)
    note: str = ""  # optional steering direction (substantive contribution → authorship)
    seq: Seq

    def proceeds(self) -> bool:
        """True iff this decision lets the run CONTINUE to the gate boundary.

        Only APPROVE proceeds; REJECT/NEEDS_CHANGES do not. This is HUMAN steering
        — proceeding merely reaches the gate boundary, where the runner-attested
        verdict still decides the release (it never makes a FAIL pass)."""
        return self.verdict is CheckpointVerdict.APPROVE

    def union_key(self) -> tuple[str, str]:
        """Stable per-decision identity for the ``checkpoint_decisions`` union.

        Keyed on ``(boundary, content_identity())``: a fresh decision at a
        boundary coexists with a prior one (e.g. a re-checkpoint after rework),
        while a byte-identical re-emission on a LangGraph resume re-fold coalesces
        to one record (idempotent). ``content_identity()`` spans the whole
        canonical record (including ``seq``), so genuinely distinct decisions are
        distinct identities — correct for an append-only steering log."""
        return (str(self.boundary), self.content_identity())


def _org_section(state: FactoryState, resolve: ArtifactResolver | None) -> dict[str, Any] | None:
    """The org chart for display (org_fanout_design.md, Wave E): present AREAS,
    their Tech-Lead DOMAINS, and each domain's units — derived from the
    OwnershipMap (Wave A ``leads_by_area``/``area_of``/``units_of``). Advisory and
    fail-soft: returns None when there is no resolver, no OwnershipMap handle, or
    the handle does not reconstitute (a telemetry read must NEVER break a pause)."""
    if resolve is None:
        return None
    handles = [a for a in (state.get("artifacts") or ()) if a.kind == _OWNERSHIP_KIND]
    if not handles:
        return None
    # The ENTIRE build is wrapped: resolve, the isinstance guard, AND the Wave A
    # derivations. A telemetry read must never break the pause, so any failure
    # (resolver error, wrong-type body, a malformed map) just omits "org".
    # ``max`` orders by Seq, which is correct within a run (how supersession
    # works); cross-run recency is irrelevant for this advisory display.
    try:
        ownership = resolve(max(handles, key=lambda a: a.seq))
        if not isinstance(ownership, OwnershipMap):
            return None
        return {
            "areas": [
                {"area": area, "leads": list(leads)}
                for area, leads in ownership.leads_by_area().items()
            ],
            "domains": [
                {
                    "owner": owner,
                    "area": ownership.area_of(owner),
                    "units": list(ownership.units_of(owner)),
                }
                for owner in ownership.leads()
            ],
        }
    except Exception:
        return None


def _artifact_detail(kind: str, body: object, *, max_len: int = 200) -> str | None:
    """A SHORT, human one-line summary of an artifact's CONTENT for the advisory
    display (never the full body — L4): the PRD's title, an acceptance criterion's
    text, the sprint plan's milestone count, etc. Best-effort and defensive — returns
    None when nothing useful is extractable (the row then shows just its label).

    ``max_len`` caps the returned string (trimmed at a word boundary with an ellipsis);
    callers that render into more space (e.g. the live event log's expandable box) pass
    a larger value so a long intent/criterion isn't cut mid-thought."""
    def s(attr: str) -> str | None:
        v = getattr(body, attr, None)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def count(attr: str) -> int:
        v = getattr(body, attr, None)
        try:
            return len(v)  # type: ignore[arg-type]
        except Exception:
            return 0

    if kind == "ticket":
        # Machine-readable for the Workshop's Tickets tab, so never length-trimmed.
        return json.dumps({
            "id": s("id"), "title": s("title"),
            "specialty": str(getattr(body, "specialty", "") or ""),
            "difficulty": str(getattr(body, "difficulty", "") or ""),
            "depends_on": list(getattr(body, "depends_on", ()) or ()),
            # A re-plan's tombstone (supersedes its own id): the ticket was dropped.
            "retired": getattr(body, "supersedes", None) == s("id"),
        }, separators=(",", ":"))
    out: str | None = None
    if kind == "acceptance_criterion":
        out = s("text")
        crit = s("criticality")
        if out and crit:
            out = f"[{crit}] {out}"
    elif kind == "prd":
        caps = count("required_capabilities")
        out = (s("title") or "PRD") + (f" — {caps} capabilities" if caps else "")
    elif kind == "intent_brief":
        out = s("intent")
        mh = count("must_haves")
        if out and mh:
            out = f"{out} ({mh} must-haves)"
    elif kind == "sprint_plan":
        out = f"{count('milestones')} milestone(s)"
        dd = s("done_definition")
        if dd:
            out += f" — done when: {dd}"
    elif kind == "system_design":
        comps = count("components")
        out = (s("title") or "System Design") + (f" — {comps} components" if comps else "")
    elif kind == "adr":
        out = s("title") or s("decision")
    elif kind == "interface_contract":
        out = s("name")
        cons = count("consumers")
        if out and cons:
            out = f"{out} ({cons} consumers)"
    elif kind == "ownership_map":
        owners = getattr(body, "owners", None) or {}
        try:
            out = f"{len(set(owners.values()))} domain(s) across {len(owners)} unit(s)"
        except Exception:
            out = None
    elif kind == "test_plan":
        out = f"{count('test_criteria')} test criteria"
    elif kind == "test_report":
        out = f"{getattr(body, 'passed', 0)} passed / {getattr(body, 'failed', 0)} failed"
    elif kind == "e2e_report":
        out = (f"{getattr(body, 'scenarios_passed', 0)} scenarios passed / "
               f"{getattr(body, 'scenarios_failed', 0)} failed")
    elif kind == "threat_model":
        out = f"{count('oracle_bindings')} oracle bindings, {count('residual_item_ids')} residual items"
    elif kind == "audit_finding":
        out = f"severity {getattr(body, 'severity', '?')} — {count('findings')} finding(s)"
    elif kind == "supply_chain_report":
        pv = count("policy_violations")
        out = f"{pv} policy violation(s)" if pv else "clean supply-chain scan"
    if not out:  # generic fallback: the first descriptive string field on the body
        for attr in ("title", "name", "text", "summary", "intent", "decision", "description"):
            out = s(attr)
            if out:
                break
    if not out:
        return None
    out = " ".join(str(out).split())  # collapse whitespace to one line
    if len(out) > max_len:  # trim at a WORD boundary, never mid-word
        out = out[:max_len].rsplit(" ", 1)[0] + "…"
    return out


def inspection_payload(
    state: FactoryState,
    *,
    boundary: CheckpointBoundary,
    state_sha: str,
    resolve: ArtifactResolver | None = None,
) -> dict[str, object]:
    """Build the READ-ONLY inspection payload surfaced to the human at a pause.

    Advisory only (plan [10]): the payload is what the operator inspects to judge
    "is this on the right track?" — the stage's artifact handles, the attested
    gate verdicts SO FAR (status only, advisory context), and the advisory
    ``agent_journal`` entries from [11] (note ``foundry.agents.interview.interview``
    can turn those entries into prose for the operator). It carries NO bodies (L4)
    and writing it influences NOTHING — it is a pure read of state for display.

    The payload is a plain JSON-able dict so it can ride the LangGraph
    ``interrupt()`` value out to the caller, who renders it and feeds back a
    :class:`CheckpointDecision` via ``Command(resume=...)``."""
    artifacts = []
    for a in (state.get("artifacts") or ()):
        entry: dict[str, object] = {"kind": a.kind, "uri": a.uri, "produced_by": a.produced_by}
        if resolve is not None:  # advisory CONTENT one-liner (never the full body, L4)
            try:
                detail = _artifact_detail(a.kind, resolve(a))
                if detail:
                    entry["detail"] = detail
            except Exception:
                pass
        artifacts.append(entry)
    gates = [
        {"name": g.name, "status": str(g.status), "run_against": g.run_against}
        for g in (state.get("gates") or ())
    ]
    journal = [
        {
            "node_id": e.node_id,
            "role": e.role,
            "summary": e.summary,
            "assumptions": list(e.assumptions),
            "concerns": list(e.concerns),
            "confidence": e.confidence,
        }
        for e in (state.get("agent_journal") or ())
    ]
    payload: dict[str, object] = {
        "boundary": str(boundary),
        "state_sha": state_sha,
        "phase": state.get("phase"),
        "route": state.get("route"),
        "mode": state.get("mode"),
        "artifacts": artifacts,
        "gates": gates,  # advisory context — the gates still decide, never this view
        "agent_journal": journal,  # advisory rationale ([11]); interview() reads these
        "note": (
            "READ-ONLY inspection. Approve only attests human judgment "
            "('on track'); it can never turn a gate FAIL into a pass — the "
            "gates remain the sole trust authority."
        ),
    }
    org = _org_section(state, resolve)
    if org is not None:
        # Advisory org chart (Wave E): areas -> Tech-Lead domains -> units. Pure
        # display (the workshop UI renders the org); influences nothing.
        payload["org"] = org
    return payload
