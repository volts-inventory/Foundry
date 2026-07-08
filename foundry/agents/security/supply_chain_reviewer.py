"""Supply-Chain Reviewer node (agents.md Layer 5 — T1, AppSec + DevOps).

The build the trusted builder will execute is agent-authored, so it is reviewed
as a high-privilege artifact. SOP (agents.md Layer 5): diff
CI/IaC/Dockerfile/lockfile/manifest/scanner-config against baseline -> enforce
policy (no build-time network/hooks, no privilege/egress widening, no
signing/OIDC-trust changes without HITL) -> SAST over build scripts + IaC.

Output: a content-addressed :class:`SupplyChainReport` handle on ``artifacts``,
AND an advisory :class:`GateTriage` on the ``gate_triage`` channel naming the
``supply_chain`` gate. A policy violation makes the advisory BLOCKING, and a
``trust_root_touched`` change (signing/OIDC) escalates HITL (governance §1e) —
but the agent NEVER mints a ``GateResult`` (LEARNINGS L5): the Supply-chain gate
runner (assurance fan-out) computes and signs the verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents._common import cas_uri, emit_artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.security import SupplyChainReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

import os as _os  # noqa: E402

_NODE_ID = "supply_chain_reviewer"
# Defaults to Opus (T1); FOUNDRY_SECURITY_TIER=T2 opts into Sonnet (frugal).
_TIER = _os.environ.get("FOUNDRY_SECURITY_TIER", "T1").strip() or "T1"
_SUPPLY_CHAIN_GATE = "supply_chain"

SOP = (
    "You are the Supply-Chain Reviewer (Layer 5). The build the trusted builder "
    "will execute is agent-authored — review it as a high-privilege artifact. "
    "Follow this SOP, in order:\n"
    "1. Diff CI/IaC/Dockerfile/lockfile/manifest/scanner-config against the "
    "baseline tree.\n"
    "2. Enforce policy: no build-time network/hooks, no privilege/egress "
    "widening, no signing/OIDC-trust changes without HITL — list every "
    "policy_violation.\n"
    "3. Set trust_root_touched=true iff signing/OIDC-trust config changed "
    "(requires HITL).\n"
    "4. Run SAST over build scripts + IaC and summarize the report.\n"
    "Respond in the constrained JSON schema. You produce an advisory report "
    "only; you do NOT decide the gate verdict."
)

# The model fills the review content; the node owns the Seq, the report body CAS
# handle, and the base/proposed tree refs.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "report_body": {"type": "string"},
        "policy_violations": {"type": "array", "items": {"type": "string"}},
        "trust_root_touched": {"type": "boolean"},
    },
    "required": ["report_body", "policy_violations"],
    "additionalProperties": False,
}


def _tree_refs(config: RunnableConfig) -> tuple[str, str]:
    """(base_tree_sha, tree_sha): the reviewed baseline and the proposed tree.

    Config-pinned (the gate-wiring wave sets the integrated refs); the proposed
    tree falls back to the run id, the baseline to ``base_tree_sha`` or empty."""
    configurable = config.get("configurable") or {}
    base = str(configurable.get("base_tree_sha", ""))
    proposed = str(
        configurable.get("run_against")
        or configurable.get("state_sha")
        or configurable.get("thread_id", "")
    )
    return base, proposed


def make_supply_chain_reviewer_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the Supply-Chain Reviewer node (LEARNINGS AR3).

    Emits a ``SupplyChainReport`` handle + an advisory ``GateTriage`` — never a
    ``GateResult`` (L5)."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        report_seq = rt.seq(config, _NODE_ID, "artifact:supply_chain_report")
        triage_seq = rt.seq(config, _NODE_ID, "gate_triage:supply_chain")
        base_tree_sha, tree_sha = _tree_refs(config)

        result = rt.proxy.complete(
            _NODE_ID,
            _TIER,
            SOP,
            [{"role": "user", "content": state.get("request", "")}],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        violations = tuple(str(v) for v in payload.get("policy_violations", ()))
        trust_root_touched = bool(payload.get("trust_root_touched", False))
        report = SupplyChainReport(
            base_tree_sha=base_tree_sha,
            tree_sha=tree_sha,
            report_uri=cas_uri(str(payload.get("report_body", ""))),
            policy_violations=violations,
            trust_root_touched=trust_root_touched,
            seq=report_seq,
        )
        handle = emit_artifact(
            report, "supply_chain_report", _NODE_ID, report_seq, store=rt.cas
        )
        triage = _triage(report, triage_seq)

        # ADVISORY only — the Supply-chain gate runner (assurance fan-out)
        # computes + signs the verdict (LEARNINGS L5).
        return {"artifacts": [handle], "gate_triage": [triage]}

    return _node


def _triage(report: SupplyChainReport, seq: Any) -> GateTriage:
    """The advisory the Supply-chain gate consumes. A policy violation or a
    trust-root change is flagged BLOCKING; the runner still owns the verdict
    bit (LEARNINGS L5)."""
    if report.trust_root_touched:
        explanation = (
            "BLOCKING: signing/OIDC-trust-root change requires HITL "
            f"(governance §1e). Policy violations: "
            f"{'; '.join(report.policy_violations) or 'none'}."
        )
    elif report.policy_violations:
        explanation = (
            "BLOCKING: supply-chain policy violation(s) — "
            f"{'; '.join(report.policy_violations)}."
        )
    else:
        explanation = "No supply-chain policy violations in the build diff."
    return GateTriage(
        name=_SUPPLY_CHAIN_GATE,
        run_against=report.tree_sha,
        explanation=explanation,
        produced_by=_NODE_ID,
        seq=seq,
    )
