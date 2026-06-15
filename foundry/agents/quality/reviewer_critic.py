"""Reviewer / Critic — the optimizer's evaluator (agents.md Layer 4, T1).

SOP: review the diff for correctness, design adherence, readability, and the
*test* diffs (guard against weakened assertions) → emit a verdict
``approve | request-changes`` with specific findings.

Trust model (LEARNINGS L5 / AR4): the reviewer is the ONE quality node that
touches the ``gates`` channel — but it never *mints* a verdict. It calls
``rt.proxy.judge(...)``, and the proxy:

* extracts ``status`` from the grammar-constrained verdict channel (a
  compromised adapter cannot have free-text "approve" authentically signed),
* stamps the judgment binding OUTSIDE the adapter via the injected ZoneBSigner
  (the agent holds no key), and
* returns a :class:`JudgmentResult` whose ``.gate_result`` already carries the
  binding + ``judgment_provenance`` stamp.

The node writes that proxy-stamped ``GateResult`` to ``gates``. ``_merge_gates``
admits it only because it carries a valid binding + an unconsumed server-issued
``verdict_nonce`` + no negative latch — none of which the agent forged. L5 is
honored: the agent emitted a verdict it could never have signed itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.agents.quality._common import (
    server_issued_nonce,
    state_sha,
    subject_artifact_uri,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.state.factory_state import FactoryState

_NODE_ID = "reviewer_critic"
_GATE_NAME = "review"  # a JUDGMENT_VERDICTS gate (reducers.py)

_SOP = (
    "You are the Reviewer/Critic, the evaluator side of the evaluator-optimizer "
    "loop. Review the diff for correctness, design adherence, readability, and "
    "scrutinize the TEST diffs for weakened assertions. Return a verdict: "
    "approve only if the change is correct and the tests still bind the "
    "criteria; otherwise request-changes with specific, actionable findings."
)


def make_reviewer_critic_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the reviewer judgment node (LEARNINGS AR3).

    Issues a verdict via ``rt.proxy.judge`` with a server-issued nonce and a
    ``Seq`` whose ``node_id == _NODE_ID`` (the binding signs ``seq.node_id``;
    ``judge`` rejects a mismatch). Writes the proxy-stamped ``GateResult`` to
    the ``gates`` channel and carries ``judgment_provenance`` forward.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        # Seq.node_id MUST equal the node_id passed to judge() — the binding
        # signs seq.node_id, so judge() rejects a mismatched record (proxy L5).
        seq = rt.seq(config, _NODE_ID, "verdict:review")

        artifacts = state.get("artifacts") or []
        state_subject = artifacts[-1].uri if artifacts else None
        subject_uri = subject_artifact_uri(config, state_subject)
        run_against = state_sha(config, fallback=seq.run_id)
        # Server-issued, single-use; the agent consumes but never mints it.
        nonce = server_issued_nonce(config)

        result = rt.proxy.judge(
            _NODE_ID,
            subject_uri,
            run_against,
            nonce,
            _SOP,
            [
                {
                    "role": "user",
                    "content": (
                        "Review the artifact under "
                        f"{subject_uri or '(unspecified subject)'} against the "
                        "interfaces and acceptance criteria. Emit your verdict."
                    ),
                }
            ],
            seq=seq,
            tier="T1",
            gate_name=_GATE_NAME,
        )

        # The verdict the proxy stamped (binding + provenance) — the agent never
        # signed it. _merge_gates admits it on a valid binding + fresh nonce +
        # no latch; it is dropped fail-closed otherwise (L5).
        return {"gates": [result.gate_result]}

    return _node
