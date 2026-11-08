"""The run's outcome: :class:`RunResult`, the per-mode trust posture, and the
terminal-state distillation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from foundry.cli.defaults import _MOCK_BACKEND
from foundry.graph.factory_graph import terminal_gates_pass
from foundry.graph.supervisor import MODE_ASSISTANT
from foundry.schemas.gates import GateStatus

if TYPE_CHECKING:
    from foundry.schemas.base import ExternalAction
    from foundry.state.factory_state import FactoryState


# ---- per-mode trust posture (lifecycle.md §4 deployment-trust modes) --------
# A one-line human summary of the trust root each mode engages, surfaced in the
# run summary + --help so the operator sees WHAT authority releases the artifact.
_TRUST_POSTURE: dict[str, str] = {
    MODE_ASSISTANT: (
        "advisory review, no trust root — produces a bare open_pr a human reviews "
        "+ merges via normal CI (the reviewer's verdict is ADVISORY)"
    ),
}


# ---- result ----------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """The outcome of a smoke run — what the CLI reports.

    Holds only content-addressed handles / identifiers + the attested gate
    statuses; carries no bodies (L4) and makes no external effect.
    """

    request: str
    thread_id: str
    passed: bool
    phase: str
    mode: str = MODE_ASSISTANT
    """The release mode the run was driven in (always assistant)."""
    backend: str = _MOCK_BACKEND
    """The model backend the proxy used (mock | anthropic | openai | claude-cli | codex)."""
    artifact_uri: str | None = None
    """The reviewed FeatureDiff handle uri the PR opens against (the PR subject)."""
    open_pr_idempotency_key: str | None = None
    open_pr_payload_uri: str | None = None
    gate_statuses: dict[str, GateStatus] = field(default_factory=dict)
    """Newest-by-name attested status per gate (spec / build / test / review...)."""
    gate_findings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Findings (the human-readable WHY) for the newest verdict per gate — surfaced
    in the summary for FAILING gates so a halted real build is diagnosable."""
    output_root: str | None = None
    """Where the final accepted source tree was written (when ``output_dir`` is
    set AND the build passed); ``None`` on a halted run (no PASS, no write)."""
    draft_root: str | None = None
    """Where a best-effort DRAFT tree was written on a HALTED run (output_dir set,
    gates not all green): the latest attempt's source + a ``DRAFT-STATUS.md`` naming
    what's still failing — so a held build hands back a usable starting point, never
    empty hands. ``None`` on a PASS (use ``output_root``) or when no draft could be
    materialized."""
    draft_status: str | None = None
    """The ``DRAFT-STATUS.md`` text of a held run (gate verdicts + what's failing),
    with or without ``output_dir``; ``None`` on a PASS or when the run left no
    draft tree (e.g. it halted in planning)."""
    resume_from: str | None = None
    """The boundary a HALTED run stopped at, where a rerun picks up (``None`` on a PASS)."""
    resumable: bool = False
    """Whether the run's state is durable (``--state-dir``), so a rerun can resume it."""
    model_mode: str | None = None
    """The model mode (cloud | hybrid | local, or "custom" for an explicit level
    table); None when the run used the single-backend routing."""
    levels: dict[str, dict[str, Any]] | None = None
    """The level table the run routed by (tier -> backend/model[/base_url])."""
    hint_counts: dict[str, int] = field(default_factory=dict)
    """How often each pattern hint, planner pinned constraint and fix-loop stop
    rule fired in this process (:mod:`foundry.telemetry`)."""

    def summary(self) -> str:
        """A concise human-readable report of the run (printed by ``main``)."""
        lines = [
            f"request:   {self.request!r}",
            f"thread_id: {self.thread_id}",
            f"mode:      {self.mode}",
            f"  trust:   {_TRUST_POSTURE.get(self.mode, 'unknown')}",
            f"backend:   {self.backend}  "
            + (
                "(deterministic mock — no model call)"
                if self.backend == _MOCK_BACKEND
                else "(real model egress)"
            ),
        ]
        if self.levels:
            lines.append(f"models:    {self.model_mode or 'custom'}")
            lines += [
                f"  {tier}: {lvl['backend']} {lvl['model']}"
                for tier, lvl in sorted(self.levels.items())
            ]
        lines += [
            f"phase:     {self.phase}",
            f"result:    {'PASS' if self.passed else 'HALTED (no PR)'}",
            "gates:",
        ]
        for name in sorted(self.gate_statuses):
            lines.append(f"  - {name}: {self.gate_statuses[name].value}")
            # Surface WHY a gate didn't pass (real builds halt here; the findings
            # are the actionable detail — a missing oracle hint, a failing test, …).
            if self.gate_statuses[name] is not GateStatus.PASS:
                for finding in self.gate_findings.get(name, ()):
                    lines.append(f"      · {finding}")
        if self.passed:
            lines.append("reviewed artifact (PR subject):")
            lines.append(f"  {self.artifact_uri}")
            lines.append("open_pr action:")
            lines.append(f"  idempotency_key: {self.open_pr_idempotency_key}")
            lines.append(f"  payload_uri:     {self.open_pr_payload_uri}")
        else:
            lines.append("no open_pr action emitted (gates did not all PASS).")
        if self.resume_from:
            lines.append(
                f"halted at {self.resume_from}: rerun the same command to resume from "
                f"{self.resume_from} (each rerun gets a fresh fix budget)"
                if self.resumable
                else f"halted at {self.resume_from}: add --state-dir DIR to make a halted "
                "run resumable"
            )
        if self.output_root:
            lines.append(f"source tree written to: {self.output_root}")
        if self.draft_root:
            lines.append(
                f"DRAFT written to: {self.draft_root}  (held — see DRAFT-STATUS.md "
                "for what's failing; not shipped)"
            )
        return "\n".join(lines)


def _result(
    request: str,
    thread_id: str,
    final: FactoryState,
    *,
    mode: str = MODE_ASSISTANT,
    backend: str = _MOCK_BACKEND,
) -> RunResult:
    """Distill the terminal state into a :class:`RunResult` (no bodies, L4)."""
    statuses: dict[str, GateStatus] = {}
    findings: dict[str, tuple[str, ...]] = {}
    # newest-by-Seq per gate (the verdict that gates the release; mirrors emit guard)
    newest: dict[str, Any] = {}
    for g in final.get("gates") or ():
        cur = newest.get(g.name)
        if cur is None or g.seq > cur.seq:
            newest[g.name] = g
    for name, g in newest.items():
        statuses[name] = g.status
        if getattr(g, "findings", None):
            findings[name] = tuple(g.findings)

    # The release action: at most one open_pr on a released run.
    release: ExternalAction | None = next(
        (a for a in (final.get("actions") or ()) if a.kind == "open_pr"),
        None,
    )
    # A run "passed" only if the gates PASS AND a release action was emitted.
    passed = terminal_gates_pass(final) and release is not None
    return RunResult(
        request=request,
        thread_id=thread_id,
        mode=mode,
        backend=backend,
        passed=passed,
        phase=str(final.get("phase") or "unknown"),
        artifact_uri=release.payload_uri if release is not None else None,
        open_pr_idempotency_key=release.idempotency_key if release is not None else None,
        open_pr_payload_uri=release.payload_uri if release is not None else None,
        gate_statuses=statuses,
        gate_findings=findings,
    )
