"""Advisory artifact summaries for the Workshop's live telemetry, plus the retired
checkpoint records.

Supervision (stage checkpoints) was removed; :class:`CheckpointDecision` and its
enums stay ONLY so a pre-removal durable checkpoint, whose ``checkpoint_decisions``
channel holds these records, still deserializes through the msgpack allowlist
(``foundry.durability.serde``). Nothing writes or reads them any more.
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

__all__ = ["CheckpointBoundary", "CheckpointDecision", "CheckpointVerdict"]


class CheckpointBoundary(StrEnum):
    """Retired: a stage boundary a checkpoint used to pause at."""

    PRODUCT = "product"
    DESIGN = "design"
    RELEASE = "release"


class CheckpointVerdict(StrEnum):
    """Retired: a human's steering verdict at a checkpoint."""

    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_CHANGES = "needs_changes"


class CheckpointDecision(FrozenModel):
    """Retired: a recorded human steering decision (kept for deserialization)."""

    boundary: CheckpointBoundary
    verdict: CheckpointVerdict
    state_sha: str
    subject_artifact_uri: str = ""
    steered_by: str = ""
    note: str = ""
    seq: Seq


def _org_section(state: FactoryState, resolve: ArtifactResolver | None) -> dict[str, Any] | None:
    """The org chart for display (org_fanout_design.md, Wave E): present AREAS,
    their Tech-Lead DOMAINS, and each domain's units — derived from the
    OwnershipMap (Wave A ``leads_by_area``/``area_of``/``units_of``). Advisory and
    fail-soft: returns None when there is no resolver, no OwnershipMap handle, or
    the handle does not reconstitute (a telemetry read must NEVER break the build)."""
    if resolve is None:
        return None
    handles = [a for a in (state.get("artifacts") or ()) if a.kind == _OWNERSHIP_KIND]
    if not handles:
        return None
    # The ENTIRE build is wrapped: resolve, the isinstance guard, AND the Wave A
    # derivations. A telemetry read must never break the build, so any failure
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
    elif kind == "epic":
        out = " — ".join(x for x in (s("id"), s("title")) if x) or None
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
