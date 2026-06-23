"""Release-documentation artifact models — the agent-authored doc set.

Real companies ship more than a PRD and a design doc. Foundry's upstream stages
already produce the PRD, architecture/design + ADRs, interface contracts, test
plan/report, threat model and SBOM (see the other ``foundry/schemas`` modules);
this module adds the doc types a real release also carries but that no upstream
stage authors:

* :class:`Runbook`        — operational deploy / rollback / health / on-call;
* :class:`KnownIssues`    — known issues + limitations at ship time;
* :class:`UpgradeGuide`   — breaking changes + migration steps;
* :class:`PerfCostReport` — token/compute cost of the build (numbers grounded in
  the proxy manifest, interpretation authored by the model).

These are authored per build by the Technical Writer (``foundry/docs/writers.py``)
through the model proxy — so they inherit the same config-swappable backend — and
emitted as content-addressed artifacts like every other stage's output. Unlike
the upstream PRD/SystemDesign (whose narrative bodies sit behind ``cas://``
handles), the prose lives INLINE here: these models ARE the rendered deliverable.
"""

from __future__ import annotations

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq

__all__ = ["KnownIssue", "KnownIssues", "PerfCostReport", "Runbook", "UpgradeGuide"]


class Runbook(FrozenModel):
    """Operational runbook for the built service (ops deliverable)."""

    title: str
    overview: str = ""
    prerequisites: tuple[str, ...] = ()
    deploy_steps: tuple[str, ...] = ()
    rollback_steps: tuple[str, ...] = ()
    health_checks: tuple[str, ...] = ()
    monitoring: tuple[str, ...] = ()
    oncall_escalation: tuple[str, ...] = ()
    seq: Seq


class KnownIssue(FrozenModel):
    """One known issue at ship time."""

    summary: str
    impact: str = ""
    workaround: str = ""
    severity: str = "medium"  # low | medium | high


class KnownIssues(FrozenModel):
    """Known issues + limitations page (honest ship-time caveats)."""

    title: str
    issues: tuple[KnownIssue, ...] = ()
    limitations: tuple[str, ...] = ()
    seq: Seq


class UpgradeGuide(FrozenModel):
    """Upgrade / migration guide for consumers of the previous version."""

    title: str
    from_version: str = "previous"
    to_version: str = "current"
    breaking_changes: tuple[str, ...] = ()
    migration_steps: tuple[str, ...] = ()
    rollback_notes: str = ""
    seq: Seq


class PerfCostReport(FrozenModel):
    """Performance / cost report for the build.

    The NUMBERS are injected by the writer node from the proxy's per-call
    manifest (grounded fact, not model-authored); the ``observations`` /
    ``recommendations`` are the model's interpretation — the same "node owns the
    facts, model proposes the prose" split the rest of the factory uses.
    """

    title: str
    summary: str = ""
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    model_calls: int = 0
    estimated_cost_microusd: int = 0
    observations: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    seq: Seq
