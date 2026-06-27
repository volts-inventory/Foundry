"""Shared helpers for the Security-layer node factories (agents.md Layer 5).

Two concerns recur across appsec_engineer / audit_compliance /
supply_chain_reviewer and live here so each factory stays a faithful SOP + a
thin model-call body (and there is no byte-duplicate to drift, like the
product/quality ``_common`` modules):

* **The ``run_against`` ref off the config seam.** Every Security node binds its
  advisory to the integrated tree the gate-wiring wave pinned. The ref is the
  config-pinned ``run_against`` (falling back to the integrated ``state_sha``),
  with the run id as a last-resort fallback so a standalone invocation still
  yields a coherent (if degenerate) binding ref. :func:`run_against` is that one
  resolution; ``supply_chain_reviewer`` additionally resolves a *base* tree and
  keeps its own ``_tree_refs`` wrapper.

* **The blocking-severity advisory.** A HIGH/CRITICAL finding flags the advisory
  BLOCKING in its explanation — but the agent never mints the verdict bit; the
  gate runner re-derives and signs it (LEARNINGS L5). The severity floor is
  :data:`BLOCKING_SEVERITIES`; :func:`blocking_triage` formats the shared
  "BLOCKING: … / Non-blocking … / <empty>" :class:`GateTriage` body, with the
  per-gate wording supplied by each factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import cas_uri, emit_artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.security import Severity

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


__all__ = [
    "BLOCKING_SEVERITIES",
    "blocking_triage",
    "cas_uri",
    "emit_artifact",
    "run_against",
]

# Severities that make an advisory BLOCKING (the runner still owns the verdict).
BLOCKING_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})


def run_against(config: RunnableConfig) -> str:
    """The integrated tree / state ref a Security advisory is bound to.

    Primary source is the config-pinned ``run_against`` (the gate-wiring wave
    sets the integrated ``tree_sha``), falling back to the integrated
    ``state_sha``; falls back to the run id so a standalone invocation still
    yields a coherent (if degenerate) binding ref."""
    configurable = config.get("configurable") or {}
    pinned = configurable.get("run_against") or configurable.get("state_sha")
    if pinned:
        return str(pinned)
    return str(configurable.get("thread_id", ""))


def blocking_triage(
    *,
    name: str,
    severity: Severity,
    findings: tuple[str, ...],
    run_against: str,
    seq: Any,
    produced_by: str,
    noun: str,
    blocking_suffix: str,
    empty: str,
) -> GateTriage:
    """The advisory the Security gate consumes (HIGH/CRITICAL flagged BLOCKING).

    A HIGH/CRITICAL finding marks the explanation BLOCKING; the runner still
    owns the verdict bit (LEARNINGS L5). ``noun`` (e.g. ``"security"`` /
    ``"audit"``), ``blocking_suffix`` (the gate-specific "the runner must
    confirm …" clause) and ``empty`` (the no-findings message) carry the
    per-gate wording."""
    if severity in BLOCKING_SEVERITIES and findings:
        explanation = (
            f"BLOCKING: {severity.value}-severity {noun} finding(s) — "
            f"{'; '.join(findings)}. {blocking_suffix}"
        )
    elif findings:
        explanation = (
            f"Non-blocking ({severity.value}): {noun} findings — "
            f"{'; '.join(findings)}."
        )
    else:
        explanation = empty
    return GateTriage(
        name=name,
        run_against=run_against,
        explanation=explanation,
        produced_by=produced_by,
        seq=seq,
    )
