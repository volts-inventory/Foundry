"""Pure markdown renderers for the per-build documentation set.

Each function turns a typed artifact body (or a small collection of them) into a
:class:`RenderedDoc` — a path + title + markdown body. No I/O, no model calls,
no state: rendering is deterministic and unit-testable in isolation. The
upstream artifacts (PRD, SystemDesign, ADRs, …) are rendered from their
STRUCTURED fields — the upstream prose bodies behind ``prd_uri`` / ``design_uri``
are content-addressed handles that the factory does not persist, so we render
what is actually durable (title, components, criteria text, decisions) and note
where a narrative body would live.
"""

from __future__ import annotations

from dataclasses import dataclass

from foundry.schemas.architecture import ADR, InterfaceContract, OwnershipMap, SystemDesign
from foundry.schemas.docs import KnownIssues, PerfCostReport, Runbook, UpgradeGuide
from foundry.schemas.product import PRD, AcceptanceCriterion
from foundry.schemas.quality import E2EReport, TestPlan, TestReport

__all__ = [
    "RenderedDoc",
    "render_acceptance",
    "render_adrs",
    "render_design",
    "render_e2e_report",
    "render_interfaces",
    "render_known_issues",
    "render_ownership",
    "render_perf_cost",
    "render_prd",
    "render_runbook",
    "render_test_plan",
    "render_test_report",
    "render_upgrade",
]


@dataclass(frozen=True, slots=True)
class RenderedDoc:
    """One rendered document destined for the docs/ tree."""

    path: str  # repo-relative path, e.g. "docs/architecture/DESIGN.md"
    title: str
    body: str  # markdown


def _bullets(items: tuple[str, ...] | list[str], *, empty: str = "_None recorded._") -> str:
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return empty
    return "\n".join(f"- {i}" for i in items)


def _h(title: str) -> str:
    return f"# {title}\n"


def _cell(value: object) -> str:
    """Escape a value for a markdown table cell — model-authored text may contain
    ``|`` or newlines that would otherwise inject columns / break the row."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


# ---- upstream artifacts (rendered from structured fields) -------------------


def render_prd(prd: PRD, criteria: tuple[AcceptanceCriterion, ...]) -> RenderedDoc:
    parts = [
        _h(f"Product Requirements — {prd.title or 'Untitled'}"),
        f"> Narrative body: `{prd.prd_uri}`\n",
        "## Required capabilities\n",
        _bullets(prd.required_capabilities),
        "\n## Acceptance criteria\n",
    ]
    if criteria:
        parts.append("| ID | Criticality | Security/abuse | Criterion |")
        parts.append("|----|-------------|----------------|-----------|")
        for c in sorted(criteria, key=lambda c: c.id):
            parts.append(
                f"| {_cell(c.id)} | {c.criticality} | "
                f"{'yes' if c.security_abuse else 'no'} | {_cell(c.text)} |"
            )
    else:
        parts.append(f"_Declared criteria: {', '.join(prd.criterion_ids) or 'none'}._")
    return RenderedDoc("docs/PRD.md", "Product Requirements", "\n".join(parts) + "\n")


def render_design(design: SystemDesign) -> RenderedDoc:
    parts = [
        _h(f"System Design — {design.title or 'Untitled'}"),
        f"> Narrative body: `{design.design_uri}`\n",
        "## Components\n",
        _bullets(design.components),
        "\n## Architecture Decision Records\n",
        _bullets(design.adr_ids, empty="_No ADRs recorded._"),
        "\n## Interfaces\n",
        _bullets(design.interface_ids, empty="_No interfaces recorded._"),
    ]
    return RenderedDoc(
        "docs/architecture/DESIGN.md", "System Design", "\n".join(parts) + "\n"
    )


def render_adrs(adrs: tuple[ADR, ...]) -> RenderedDoc:
    parts = [_h("Architecture Decision Records")]
    if not adrs:
        parts.append("_No ADRs recorded._")
    for adr in sorted(adrs, key=lambda a: a.id):
        parts.append(f"## {adr.id} — {adr.title}\n")
        parts.append(f"**Status:** {adr.status}\n")
        parts.append(f"**Decision:** {adr.decision}\n")
        parts.append(f"> Full rationale: `{adr.body_uri}`\n")
        parts.append("**Rejected alternatives:**\n")
        parts.append(_bullets(adr.rejected_alternatives, empty="_None recorded._"))
        parts.append("")
    return RenderedDoc(
        "docs/architecture/ADRS.md", "Architecture Decision Records", "\n".join(parts) + "\n"
    )


def render_interfaces(interfaces: tuple[InterfaceContract, ...]) -> RenderedDoc:
    parts = [_h("Interface Contracts")]
    if not interfaces:
        parts.append("_No interface contracts recorded._")
    for itf in sorted(interfaces, key=lambda i: i.name):
        parts.append(f"## `{itf.name}`\n")
        parts.append(f"> Spec body: `{itf.spec_uri}`\n")
        parts.append(f"**Consumers:** {', '.join(itf.consumers) or 'none'}\n")
    return RenderedDoc(
        "docs/architecture/INTERFACES.md", "Interface Contracts", "\n".join(parts) + "\n"
    )


def render_ownership(ownership: OwnershipMap) -> RenderedDoc:
    parts = [_h("Ownership Map"), "| Path / interface | Owner |", "|------------------|-------|"]
    for path, owner in sorted(ownership.owners.items()):
        parts.append(f"| {_cell(path)} | {_cell(owner)} |")
    if not ownership.owners:
        parts.append("| _none_ | _none_ |")
    return RenderedDoc("docs/architecture/OWNERSHIP.md", "Ownership Map", "\n".join(parts) + "\n")


def render_acceptance(criteria: tuple[AcceptanceCriterion, ...]) -> RenderedDoc:
    parts = [_h("Acceptance Criteria")]
    if not criteria:
        parts.append("_No acceptance criteria recorded._")
    for c in sorted(criteria, key=lambda c: c.id):
        parts.append(f"## {c.id} ({c.criticality})\n")
        parts.append(f"{c.text}\n")
        if c.oracle_hint:
            parts.append(f"**Oracle hint:** {c.oracle_hint}\n")
        if c.security_abuse:
            parts.append("**Security/abuse criterion.**\n")
    return RenderedDoc("docs/REQUIREMENTS.md", "Acceptance Criteria", "\n".join(parts) + "\n")


def render_test_plan(plan: TestPlan) -> RenderedDoc:
    parts = [
        _h("Test Plan"),
        f"> Plan / test sources: `{plan.plan_uri}`\n",
        "## Test → criteria traceability\n",
        "| Test | Criteria exercised |",
        "|------|--------------------|",
    ]
    for test_id in sorted(plan.test_criteria):
        parts.append(f"| {_cell(test_id)} | {_cell(', '.join(plan.test_criteria[test_id]))} |")
    if not plan.test_criteria:
        parts.append("| _none_ | _none_ |")
    parts.append(f"\n_Criteria covered: {len(plan.covered_criteria())}._")
    return RenderedDoc("docs/qa/TEST-PLAN.md", "Test Plan", "\n".join(parts) + "\n")


def render_test_report(report: TestReport) -> RenderedDoc:
    parts = [
        _h("Test Report"),
        f"- **Run against:** `{report.run_against}`",
        f"- **Passed:** {report.passed}",
        f"- **Failed:** {report.failed}",
        f"- **Quarantined (flaky):** {', '.join(report.quarantined) or 'none'}",
        f"\n> Raw results: `{report.results_uri}`",
    ]
    return RenderedDoc("docs/qa/TEST-REPORT.md", "Test Report", "\n".join(parts) + "\n")


def render_e2e_report(report: E2EReport) -> RenderedDoc:
    parts = [
        _h("End-to-End Report"),
        f"- **Run against:** `{report.run_against}`",
        f"- **Scenarios passed:** {report.scenarios_passed}",
        f"- **Scenarios failed:** {report.scenarios_failed}",
        f"- **Quarantined (flaky):** {', '.join(report.quarantined) or 'none'}",
        f"- **Determinism:** {report.pinned_seeds_note or 'not recorded'}",
        f"\n> Raw scenario results: `{report.results_uri}`",
    ]
    return RenderedDoc("docs/qa/E2E-REPORT.md", "End-to-End Report", "\n".join(parts) + "\n")


# ---- agent-authored docs ----------------------------------------------------


def render_runbook(rb: Runbook) -> RenderedDoc:
    parts = [
        _h(f"Runbook — {rb.title or 'Service'}"),
        rb.overview or "_No overview authored._",
        "\n## Prerequisites\n",
        _bullets(rb.prerequisites),
        "\n## Deploy\n",
        _bullets(rb.deploy_steps),
        "\n## Rollback\n",
        _bullets(rb.rollback_steps),
        "\n## Health checks\n",
        _bullets(rb.health_checks),
        "\n## Monitoring\n",
        _bullets(rb.monitoring),
        "\n## On-call escalation\n",
        _bullets(rb.oncall_escalation),
    ]
    return RenderedDoc("docs/ops/RUNBOOK.md", "Runbook", "\n".join(parts) + "\n")


def render_known_issues(ki: KnownIssues) -> RenderedDoc:
    parts = [_h(ki.title or "Known Issues & Limitations"), "## Known issues\n"]
    if ki.issues:
        parts.append("| Severity | Summary | Impact | Workaround |")
        parts.append("|----------|---------|--------|------------|")
        for issue in ki.issues:
            parts.append(
                f"| {_cell(issue.severity)} | {_cell(issue.summary)} "
                f"| {_cell(issue.impact) or '—'} | {_cell(issue.workaround) or '—'} |"
            )
    else:
        parts.append("_No known issues recorded._")
    parts.append("\n## Limitations\n")
    parts.append(_bullets(ki.limitations))
    return RenderedDoc(
        "docs/KNOWN-ISSUES.md", "Known Issues & Limitations", "\n".join(parts) + "\n"
    )


def render_upgrade(ug: UpgradeGuide) -> RenderedDoc:
    parts = [
        _h(ug.title or "Upgrade Guide"),
        f"**From:** {ug.from_version} → **To:** {ug.to_version}\n",
        "## Breaking changes\n",
        _bullets(ug.breaking_changes, empty="_None._"),
        "\n## Migration steps\n",
        _bullets(ug.migration_steps),
        "\n## Rollback\n",
        ug.rollback_notes or "_No rollback notes authored._",
    ]
    return RenderedDoc("docs/UPGRADE.md", "Upgrade Guide", "\n".join(parts) + "\n")


def render_perf_cost(pc: PerfCostReport) -> RenderedDoc:
    heading = f"Performance & Cost Report — {pc.title}" if pc.title else "Performance & Cost Report"
    parts = [
        _h(heading),
        pc.summary or "",
        "\n## Build cost (grounded in the proxy manifest)\n",
        "_Scope: the build itself, excluding documentation generation. The "
        "BUILD-MANIFEST reports the all-in total including this doc set._\n",
        f"- **Model calls:** {pc.model_calls}",
        f"- **Tokens in:** {pc.total_tokens_in:,}",
        f"- **Tokens out:** {pc.total_tokens_out:,}",
        f"- **Estimated cost:** ${pc.estimated_cost_microusd / 1_000_000:.4f} "
        f"({pc.estimated_cost_microusd} µUSD)",
        "\n## Observations\n",
        _bullets(pc.observations),
        "\n## Recommendations\n",
        _bullets(pc.recommendations),
    ]
    return RenderedDoc("docs/PERF-COST.md", "Performance & Cost Report", "\n".join(parts) + "\n")
