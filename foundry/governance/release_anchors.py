"""Release/Design sign-off anchor derivation — the single shared source of truth.

(governance.md §1/§6, plan §0.2)

The Design / Release sign-off boundaries bind to four what-you-sign anchors
(governance.md §6): the decision-point subject artifact, the integrated
``state_sha``, a per-approval server nonce, and the presented-evidence hash. Both
the supervisor's sign-off boundary nodes (:mod:`foundry.graph.supervisor`) AND the
gated emit's release re-check (:mod:`foundry.graph.factory_graph`) must derive the
SAME anchors — the emit guard re-derives them to confirm the recorded sign-off
binds THIS exact release (``release_allowed``).

These were previously private helpers on ``supervisor.py`` that ``factory_graph``
reached across a module boundary to import (``_evidence_hash`` &c.). Promoting them
to PUBLIC functions in one shared module removes the cross-module private import and
the attendant drift risk: there is now exactly one definition of each anchor, and
both call sites import it from here. Behavior is byte-identical to the former
supervisor privates (pure functions of the bound inputs; L7 ``content_digest``,
never ``hash()`` — so they are stable across a kill-then-resume).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.canonical import content_digest

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.schemas.base import Artifact
    from foundry.state.factory_state import FactoryState

__all__ = [
    "evidence_hash",
    "newest_artifact_uri",
    "newest_release_sign_off",
    "sign_off_nonce",
    "state_sha",
]


def newest_artifact_uri(state: FactoryState, kind: str) -> str:
    """The newest-by-``Seq`` artifact uri of ``kind`` on the ``artifacts`` channel.

    The Design / Release sign-off binds to the attested decision-point artifact
    (gov §6). ``artifacts`` folds by ``merge_by_uri`` (insertion-order, not seq),
    so we pick the max-``Seq`` handle of the requested kind (mirrors
    ``senior_engineer._first_of_kind`` / QS8) rather than ``artifacts[-1]``.
    Empty when no such artifact exists yet (the sign-off still records, bound to
    an empty subject — the always-recorded invariant; admission is fail-closed).
    """
    best: Artifact | None = None
    for a in state.get("artifacts") or ():
        if a.kind != kind:
            continue
        if best is None or a.seq > best.seq:
            best = a
    return best.uri if best is not None else ""


def state_sha(config: RunnableConfig) -> str:
    """The integrated ``state_sha`` the sign-off binds to (a RUN INPUT; Q1).

    Threaded onto ``config['configurable']`` by the runtime exactly like
    ``thread_id`` / ``verdict_nonce``; empty when absent (the record is still
    made — admission via ``release_allowed`` then fail-closes the mismatch)."""
    configurable = config.get("configurable") or {}
    return str(configurable.get("state_sha") or "")


def sign_off_nonce(config: RunnableConfig, point: str, subject_uri: str, state_sha: str) -> str:
    """A process-stable per-approval server nonce (L7: ``content_digest``, never
    ``hash()`` — seed-randomized per process defeats resume idempotency).

    The nonce binds the sign-off to the exact decision point (point + subject +
    state) so it cannot be replayed across boundaries; it is stable across a
    kill-then-resume because it is a pure function of those bound anchors + the
    run's thread_id."""
    configurable = config.get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "")
    return "nonce-" + content_digest(
        {"point": point, "subject": subject_uri, "state_sha": state_sha, "thread_id": thread_id}
    )


def evidence_hash(subject_uri: str, state_sha: str, gates: list[Any]) -> str:
    """Content hash of the evidence presented to the signer (the what-you-see
    anchor, gov §6): the subject + state + the ordered attested-gate identities.

    Pure function of the bound inputs (L7 ``content_digest``), so the same
    decision point yields the same hash on resume — the sign-off's
    ``presented_evidence_hash`` and the ``release_allowed`` re-derivation agree."""
    gate_ids = sorted(f"{g.name}:{g.status}:{g.run_against}" for g in gates)
    return content_digest({"subject": subject_uri, "state_sha": state_sha, "gates": gate_ids})


def newest_release_sign_off(state: FactoryState) -> Any:
    """The newest-by-``Seq`` Release ``SignOffRecord`` on the ``sign_offs`` channel.

    ``sign_offs`` folds by ``union_sign_offs`` (dedup by content identity); a
    Release record is identified by its ``role`` (``release-approver``). Newest
    by ``Seq`` so a re-presented record on resume coalesces to one. Returns
    ``None`` when none has been recorded — ``release_allowed`` then fail-closes."""
    best: Any = None
    for s in state.get("sign_offs") or ():
        if s.role != "release-approver":
            continue
        if best is None or s.seq > best.seq:
            best = s
    return best
