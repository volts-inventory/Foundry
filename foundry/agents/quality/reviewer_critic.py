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
    journal_entry,
    server_issued_nonce,
    state_sha,
    subject_artifact_uri,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

_NODE_ID = "reviewer_critic"
_GATE_NAME = "review"  # a JUDGMENT_VERDICTS gate (reducers.py)

# The kind the Junior Engineer stamps on the diff handle it emits to ``artifacts``
# (foundry/agents/implementation/junior_engineer.py) — the artifact this reviewer
# judges when the runner has not pinned an explicit subject on the config.
_FEATURE_DIFF_KIND = "feature_diff"


def _newest_feature_diff_uri(artifacts: Sequence[Artifact]) -> str | None:
    """The diff under review: the newest-by-``Seq`` ``feature_diff`` handle, if any.

    C-R1-03: the channel folds by ``merge_by_uri`` (insertion-order, NOT seq), so
    ``artifacts[-1]`` is not deterministically the diff under review. Select by
    KIND and pick the latest by ``Seq`` (mirrors ``senior_engineer._first_of_kind``)
    so a supersession (higher-``seq`` handle under a fresh uri, L4) deterministically
    wins. Returns ``None`` when no diff handle is present (the config-pinned subject
    is the primary source; the node fails closed if neither yields one)."""
    candidates = [a for a in artifacts if getattr(a, "kind", None) == _FEATURE_DIFF_KIND]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.seq).uri

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

        # Server-issued, single-use; the agent consumes but never mints it. Read
        # first so a missing nonce fails closed before anything else (the most
        # fundamental trust input of a verdict).
        nonce = server_issued_nonce(config)

        # Primary source is the config-pinned subject (the runner sets it); the
        # state fallback selects the feature_diff by KIND, newest-by-seq (C-R1-03)
        # — never insertion-order ``artifacts[-1]``.
        state_subject = _newest_feature_diff_uri(state.get("artifacts") or [])
        subject_uri = subject_artifact_uri(config, state_subject)
        # DT2 / fail-closed: a verdict needs a concrete subject to bind to.
        # `subject_artifact_uri` falls back to "" when neither the config seam
        # nor state names one — that yields a coherent-but-empty binding the gate
        # runner can't meaningfully latch. The supervisor / gate-wiring wave
        # (T-M1-13) MUST pin `config['configurable']['subject_artifact_uri']` for
        # every judgment node (exactly like `verdict_nonce`/`state_sha`); until
        # then we fail closed rather than emit a verdict over an empty subject.
        if not subject_uri:
            raise ValueError(
                "reviewer_critic has no subject to judge: neither "
                "config['configurable']['subject_artifact_uri'] nor a state "
                "artifact is present. The supervisor/gate-wiring wave (T-M1-13) "
                "must pin subject_artifact_uri on the run config for judgment nodes."
            )
        run_against = state_sha(config, fallback=seq.run_id)

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

        # ADVISORY journal entry (plan [11]). It merely ECHOES the verdict the
        # proxy already stamped — derived from `result.gate_result` (zero extra
        # cost, no new model call) — onto the SEPARATE `agent_journal` channel.
        # CRITICAL: this is observability only. The TRUST path is the signed
        # `gates` write below; `_merge_gates` reads ONLY that. The journal is
        # read by no gate/router, so even if its text disagreed with the verdict
        # it could not change the outcome (rollout to other nodes is mechanical).
        journal_seq = rt.seq(config, _NODE_ID, "journal")
        note = journal_entry(
            node_id=_NODE_ID,
            role="Reviewer / Critic",
            seq=journal_seq,
            summary=(
                f"Reviewed {subject_uri} -> verdict "
                f"{result.gate_result.status.value}."
            ),
            assumptions=(f"Judged against state {run_against}.",),
            concerns=tuple(result.gate_result.findings),
        )

        # The verdict the proxy stamped (binding + provenance) — the agent never
        # signed it. _merge_gates admits it on a valid binding + fresh nonce +
        # no latch; it is dropped fail-closed otherwise (L5).
        return {"gates": [result.gate_result], "agent_journal": [note]}

    return _node
