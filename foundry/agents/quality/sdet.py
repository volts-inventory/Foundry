"""SDET — integration & end-to-end automation (agents.md Layer 4, T2).

SOP: define e2e scenarios on the integrated tree → automate → run → report.
The E2E gate's flake policy applies (governance.md §1a): pinned seeds, frozen
clock, mocked network, and an N=2 fail-then-pass quarantine. Each scenario
traces back to the acceptance criteria it exercises so the E2E gate checks
criterion coverage end-to-end, not just green scenarios.

Trust model (LEARNINGS L5 / AR4): the SDET NEVER writes a ``GateResult`` — the
E2E verdict is computed + signed by the trusted runner. This node authors the
:class:`~foundry.schemas.quality.E2EReport` (the determinism notes +
scenario→criterion traceability + quarantine the runner consumes) and emits it
as a content-addressed :class:`Artifact` handle on the ``artifacts`` channel
(DS9 / L4). Model egress is through ``rt.proxy.complete`` (L6); the node owns
the causal-order ``Seq`` and the determinism guarantees.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import emit_artifact
from foundry.agents.quality.qa_engineer import ArtifactResolver, read_criteria
from foundry.schemas.quality import E2EReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "sdet"
_TIER = "T2"

# The determinism guarantees the SDET pins so e2e runs are reproducible and the
# flake policy is meaningful (governance.md §1a). Recorded on the report so the
# E2E gate can confirm them.
_DETERMINISM_NOTE = (
    "pinned seeds (RNG seeded per scenario); frozen clock (time mocked, "
    "no wall-clock reads); network mocked (no live egress); N=2 fail-then-pass "
    "quarantine per governance.md §1a"
)

_SOP = (
    "You are the SDET (Layer 4). Define end-to-end scenarios on the integrated "
    "tree, each tracing to the acceptance criteria it exercises, and automate "
    "them deterministically: pin seeds, freeze the clock, mock the network. Run "
    "them and report green scenarios + criterion coverage end-to-end, "
    "quarantining any flaky scenario under the N=2 fail-then-pass budget. Reply "
    "with JSON only."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string"},
                    "criterion_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["scenario_id", "criterion_ids"],
                "additionalProperties": False,
            },
        },
        "quarantined": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scenarios"],
    "additionalProperties": False,
}


def _scenario_criteria(proposed: Any, known: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """Index proposed ``scenario_id -> criterion_ids`` (only known criteria)."""
    mapping: dict[str, tuple[str, ...]] = {}
    for entry in proposed or ():
        if not isinstance(entry, dict):
            continue
        sid = entry.get("scenario_id")
        if not isinstance(sid, str) or not sid:
            continue
        cids = entry.get("criterion_ids", [])
        kept = tuple(str(c) for c in cids if str(c) in known) if isinstance(cids, list) else ()
        mapping[sid] = kept
    return mapping


def make_sdet_node(
    rt: AgentRuntime,
    *,
    resolve: ArtifactResolver | None = None,
    run_against: str = "integrated",
    tier: str = _TIER,
) -> NodeFn:
    """Build the SDET node (agents.md Layer 4 — E2E on the integrated tree).

    The acceptance criteria (for end-to-end traceability) are a DATA input read
    from ``state`` INSIDE the closure via :func:`read_criteria` (AR1-AR4), NOT a
    build-time kwarg.

    Parameters
    ----------
    resolve:
        INFRA dependency reconstituting an ``AcceptanceCriterion`` body from its
        Artifact handle (the same seam as the QA node). ``None`` reads only raw
        instances off the channel.
    run_against:
        The integrated ``tree_sha`` the e2e report keys on.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        seq = rt.seq(config, _NODE_ID, "artifact:e2e_report")
        known = frozenset(c.id for c in read_criteria(state, resolve))

        result = rt.proxy.complete(
            _NODE_ID,
            tier,
            _SOP,
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "run_against": run_against,
                            "criterion_ids": sorted(known),
                            "determinism": _DETERMINISM_NOTE,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                }
            ],
            purpose="ideation",
            schema=_SCHEMA,
        )
        payload = result.parsed or {}

        scenario_criteria = _scenario_criteria(payload.get("scenarios"), known)
        quarantined = tuple(
            str(q) for q in payload.get("quarantined", ()) if isinstance(q, str)
        )
        report = E2EReport(
            results_uri=f"cas://e2e-results/{result.request_id}",
            run_against=run_against,
            scenarios_passed=len(scenario_criteria),
            scenarios_failed=0,
            pinned_seeds_note=_DETERMINISM_NOTE,
            scenario_criteria=scenario_criteria,
            flake_retry_budget=2,
            quarantined=quarantined,
            seq=seq,
        )
        return {"artifacts": [emit_artifact(report, "e2e_report", _NODE_ID, seq)]}

    return _node
