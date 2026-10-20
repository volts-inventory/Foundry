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
    require_verdict_nonce,
    server_issued_nonce,
    state_sha,
    subject_artifact_uri,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime, NodeFn
    from foundry.proxy.model_provider_proxy import JudgmentResult
    from foundry.schemas.base import Artifact
    from foundry.schemas.journal import JournalEntry
    from foundry.schemas.product import AcceptanceCriterion
    from foundry.state.factory_state import FactoryState

_NODE_ID = "reviewer_critic"
_GATE_NAME = "review"  # a JUDGMENT_VERDICTS gate (reducers.py)

# The code-review JUDGE runs every release AND every fix-loop iteration, so its
# tier is a recurring cost. Sonnet (T2) is a strong reviewer; that's the default
# cost/value pick (tiers are a cost choice — arch §8). Restore Opus review with
# FOUNDRY_REVIEW_TIER=T1 when you want the heaviest reasoning on the review gate.
import os as _os  # noqa: E402

_REVIEW_TIER = _os.environ.get("FOUNDRY_REVIEW_TIER", "T2").strip() or "T2"

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


def _attested_tree_sha(state: FactoryState) -> str | None:
    """The REAL integrated ``tree_sha`` the deterministic gates ran against, read
    off the newest-by-``Seq`` attested verdict on the ``gates`` channel.

    Every ``GateResult`` carries the TRUE ``run_against`` of the integrated tree it
    was evaluated on (``materialize_workdir`` → ``tree_sha``; GW2 binds the release
    gates to ONE tree). Used as the reviewer's ``state_sha`` fallback so that when
    the runner does NOT pin a synthetic ``state_sha`` label (real backends), the
    verdict's ``run_against`` honestly names the tree that was actually built —
    rather than a bare run id. ``None`` when no verdict carries one yet (e.g. the
    reviewer runs before the build gates), in which case the caller falls back to
    a stable per-run label. Config-pin precedence means the ``mock`` smoke (which
    pins ``state_sha``) is unaffected."""
    best: Any = None
    for g in state.get("gates") or ():
        if getattr(g, "run_against", "") and (best is None or g.seq > best.seq):
            best = g
    return str(best.run_against) if best is not None else None

_SOP = (
    "You are the Reviewer/Critic, the evaluator side of the evaluator-optimizer "
    "loop. Review the diff for correctness, design adherence, readability, and "
    "scrutinize the TEST diffs for weakened assertions. Return a verdict: "
    "approve only if the change is correct and the tests still bind the "
    "criteria; otherwise request-changes with specific, actionable findings."
)


def review(
    rt: AgentRuntime,
    state: FactoryState,
    config: RunnableConfig,
    *,
    node_id: str,
    role: str,
    criteria: Sequence[AcceptanceCriterion] = (),
) -> tuple[JudgmentResult, JournalEntry]:
    """Issue ONE proxy-stamped review verdict for ``node_id`` and its journal note.

    Shared by the assurance ``reviewer_critic`` node and the quality ``reviewer``
    node. ``node_id`` is signed into the binding (via ``seq.node_id``) AND into the
    per-request nonce, so two reviewers in one run must use distinct ids (H3).
    ``criteria`` (when given) are put in front of the judge so it reviews against
    them. Returns the :class:`JudgmentResult` and the advisory journal entry.
    """
    # Seq.node_id MUST equal the node_id passed to judge() — the binding
    # signs seq.node_id, so judge() rejects a mismatched record (proxy L5).
    seq = rt.seq(config, node_id, "verdict:review")

    # Fail closed on a missing server-issued nonce BEFORE anything else (the most
    # fundamental trust input of a verdict); the per-request nonce is derived
    # below, once the subject + state it binds to are known.
    require_verdict_nonce(config)

    # Primary source is the config-pinned subject (the runner sets it); the
    # state fallback selects the feature_diff by KIND, newest-by-seq (C-R1-03)
    # — never insertion-order ``artifacts[-1]``.
    state_subject = _newest_feature_diff_uri(state.get("artifacts") or [])
    subject_uri = subject_artifact_uri(config, state_subject)
    # DT2 / fail-closed: a verdict needs a concrete subject to bind to; an empty
    # subject yields a coherent-but-empty binding the gate runner can't latch.
    if not subject_uri:
        raise ValueError(
            f"{node_id} has no subject to judge: neither "
            "config['configurable']['subject_artifact_uri'] nor a state "
            "artifact is present. The supervisor/gate-wiring wave (T-M1-13) "
            "must pin subject_artifact_uri on the run config for judgment nodes."
        )
    # Config-pinned `state_sha` wins (the `mock` smoke pins it); otherwise
    # (real backends) prefer the REAL integrated tree_sha the gates ran
    # against, falling back to a stable per-run label only before any gate
    # verdict carries one.
    run_against = state_sha(config, fallback=_attested_tree_sha(state) or seq.run_id)

    # Server-issued, single-use; the agent consumes but never mints it. Derived
    # per-request (bound to node/subject/state) so each verdict in a run has its
    # OWN nonce — fail-closed when the run config has no base nonce.
    nonce = server_issued_nonce(
        config, node_id=node_id, subject_uri=subject_uri, state_sha=run_against
    )

    content = (
        f"Review the artifact under {subject_uri} against the interfaces and "
        "acceptance criteria. Emit your verdict."
    )
    if criteria:
        content += "\n\nAcceptance criteria:\n" + "\n".join(
            f"- {c.id} [{c.criticality}]: {c.text}" for c in criteria
        )
    result = rt.proxy.judge(
        node_id,
        subject_uri,
        run_against,
        nonce,
        _SOP,
        [{"role": "user", "content": content}],
        seq=seq,
        tier=_REVIEW_TIER,
        gate_name=_GATE_NAME,
    )

    # ADVISORY journal entry (plan [11]): it only ECHOES the proxy-stamped verdict
    # onto the SEPARATE `agent_journal` channel, which no gate/router reads. The
    # TRUST path is the signed `gates` write; `_merge_gates` reads ONLY that.
    note = journal_entry(
        node_id=node_id,
        role=role,
        seq=rt.seq(config, node_id, "journal"),
        summary=f"Reviewed {subject_uri} -> verdict {result.gate_result.status.value}.",
        assumptions=(f"Judged against state {run_against}.",),
        concerns=tuple(result.gate_result.findings),
    )
    return result, note


def make_reviewer_critic_node(rt: AgentRuntime) -> NodeFn:
    """Factory for the assurance reviewer judgment node (LEARNINGS AR3).

    Issues a verdict via ``rt.proxy.judge`` (see :func:`review`) and writes the
    proxy-stamped ``GateResult`` to ``gates``. ``_merge_gates`` admits it on a
    valid binding + fresh nonce + no latch; it is dropped fail-closed otherwise
    (L5) — the agent never signed it.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        result, note = review(rt, state, config, node_id=_NODE_ID, role="Reviewer / Critic")
        return {"gates": [result.gate_result], "agent_journal": [note]}

    return _node
