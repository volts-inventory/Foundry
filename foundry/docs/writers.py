"""Technical Writer — authors the release docs no upstream stage produces.

Runbook, Known-Issues, Upgrade-Guide and Perf/Cost report are authored here via
``rt.proxy`` (so they inherit the same config-swappable backend as every other
model call) and returned as typed :mod:`foundry.schemas.docs` bodies. The node
owns the facts the model must not invent — the ``seq``, the document title, and
(for the perf/cost report) the token/cost NUMBERS, which come from the proxy's
per-call manifest, not the model.

Documentation is BEST-EFFORT: a model/transport failure degrades a single doc to
an empty (clearly-marked) body rather than failing the build. The build's
correctness is decided by the gates, never by whether the writer reached the
model.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.agents._common import as_text
from foundry.proxy import ProxyUnavailable, TransportError, VerdictChannelError
from foundry.schemas.docs import (
    KnownIssue,
    KnownIssues,
    PerfCostReport,
    Runbook,
    UpgradeGuide,
)
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime

__all__ = ["TechnicalWriter"]

_RUNBOOK_SOP = (
    "You are a Technical Writer authoring an OPERATIONAL RUNBOOK for the service "
    "just built. Be concrete and operational: prerequisites, ordered deploy "
    "steps, rollback steps, health checks, monitoring signals, on-call "
    "escalation. Ground every step in the provided build context. Reply with "
    "JSON only."
)
_KNOWN_ISSUES_SOP = (
    "You are a Technical Writer authoring a KNOWN-ISSUES & LIMITATIONS page at "
    "ship time. Be honest: list real known issues (summary, impact, workaround, "
    "severity low|medium|high) and scope limitations implied by the build "
    "context. Do not invent issues that the context does not support. Reply with "
    "JSON only."
)
_UPGRADE_SOP = (
    "You are a Technical Writer authoring an UPGRADE / MIGRATION GUIDE. List "
    "breaking changes and the ordered migration steps a consumer of the previous "
    "version must take, plus rollback notes. Reply with JSON only."
)
_PERF_COST_SOP = (
    "You are a Technical Writer authoring the PERFORMANCE & COST report for this "
    "build. The token and cost NUMBERS are supplied to you as ground truth — do "
    "NOT restate or alter them. Write a short summary, then observations and "
    "recommendations interpreting the build's cost profile. Reply with JSON only."
)

_RUNBOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "prerequisites": {"type": "array", "items": {"type": "string"}},
        "deploy_steps": {"type": "array", "items": {"type": "string"}},
        "rollback_steps": {"type": "array", "items": {"type": "string"}},
        "health_checks": {"type": "array", "items": {"type": "string"}},
        "monitoring": {"type": "array", "items": {"type": "string"}},
        "oncall_escalation": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_KNOWN_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "impact": {"type": "string"},
                    "workaround": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_UPGRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from_version": {"type": "string"},
        "to_version": {"type": "string"},
        "breaking_changes": {"type": "array", "items": {"type": "string"}},
        "migration_steps": {"type": "array", "items": {"type": "string"}},
        "rollback_notes": {"type": "string"},
    },
    "additionalProperties": False,
}
_PERF_COST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _strs(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v).strip())
    return ()


class TechnicalWriter:
    """Authors the four release docs via the proxy; best-effort, never fatal."""

    def __init__(self, rt: AgentRuntime, *, run_id: str, tier: str = "T3") -> None:
        self._rt = rt
        self._run_id = run_id
        self._tier = tier

    def _seq(self, node_id: str, key: str) -> Seq:
        epoch = self._rt.epochs.bump(self._run_id, f"{node_id}/{key}")
        return Seq(self._run_id, epoch, 0, node_id, 0)

    def _author(
        self, node_id: str, sop: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """One best-effort proxy call. A MODEL/TRANSPORT failure (unavailable
        backend, unparseable reply, off-grammar verdict) degrades to an empty
        payload so doc assembly never breaks — but a wiring bug (bad tier,
        renamed kwarg) is NOT swallowed: those raise AttributeError/TypeError/
        ValueError and surface."""
        try:
            result = self._rt.proxy.complete(
                node_id,
                self._tier,
                sop,
                [{"role": "user", "content": as_text(context)}],
                purpose="ideation",
                schema=schema,
            )
        except (ProxyUnavailable, TransportError, VerdictChannelError, json.JSONDecodeError):
            return {}
        parsed = result.parsed
        return parsed if isinstance(parsed, dict) else {}

    def runbook(self, title: str, context: dict[str, Any]) -> Runbook:
        p = self._author("tech_writer_runbook", _RUNBOOK_SOP, context, _RUNBOOK_SCHEMA)
        return Runbook(
            title=title,
            overview=str(p.get("overview", "")),
            prerequisites=_strs(p.get("prerequisites")),
            deploy_steps=_strs(p.get("deploy_steps")),
            rollback_steps=_strs(p.get("rollback_steps")),
            health_checks=_strs(p.get("health_checks")),
            monitoring=_strs(p.get("monitoring")),
            oncall_escalation=_strs(p.get("oncall_escalation")),
            seq=self._seq("tech_writer_runbook", "artifact:runbook"),
        )

    def known_issues(self, title: str, context: dict[str, Any]) -> KnownIssues:
        p = self._author(
            "tech_writer_known_issues", _KNOWN_ISSUES_SOP, context, _KNOWN_ISSUES_SCHEMA
        )
        issues: list[KnownIssue] = []
        for raw in p.get("issues", ()) if isinstance(p.get("issues"), list) else ():
            if not isinstance(raw, dict) or not str(raw.get("summary", "")).strip():
                continue
            sev = str(raw.get("severity", "medium"))
            issues.append(
                KnownIssue(
                    summary=str(raw["summary"]),
                    impact=str(raw.get("impact", "")),
                    workaround=str(raw.get("workaround", "")),
                    severity=sev if sev in ("low", "medium", "high") else "medium",
                )
            )
        return KnownIssues(
            title=title,
            issues=tuple(issues),
            limitations=_strs(p.get("limitations")),
            seq=self._seq("tech_writer_known_issues", "artifact:known_issues"),
        )

    def upgrade_guide(self, title: str, context: dict[str, Any]) -> UpgradeGuide:
        p = self._author("tech_writer_upgrade", _UPGRADE_SOP, context, _UPGRADE_SCHEMA)
        return UpgradeGuide(
            title=title,
            from_version=str(p.get("from_version", "previous")),
            to_version=str(p.get("to_version", "current")),
            breaking_changes=_strs(p.get("breaking_changes")),
            migration_steps=_strs(p.get("migration_steps")),
            rollback_notes=str(p.get("rollback_notes", "")),
            seq=self._seq("tech_writer_upgrade", "artifact:upgrade"),
        )

    def perf_cost(
        self, title: str, context: dict[str, Any], *, usage: dict[str, int]
    ) -> PerfCostReport:
        """The token/cost NUMBERS come from ``usage`` (the proxy manifest); the
        model only authors the prose interpretation."""
        p = self._author("tech_writer_perf_cost", _PERF_COST_SOP, context, _PERF_COST_SCHEMA)
        return PerfCostReport(
            title=title,
            summary=str(p.get("summary", "")),
            total_tokens_in=int(usage.get("total_tokens_in", 0)),
            total_tokens_out=int(usage.get("total_tokens_out", 0)),
            model_calls=int(usage.get("model_calls", 0)),
            estimated_cost_microusd=int(usage.get("estimated_cost_microusd", 0)),
            observations=_strs(p.get("observations")),
            recommendations=_strs(p.get("recommendations")),
            seq=self._seq("tech_writer_perf_cost", "artifact:perf_cost"),
        )
