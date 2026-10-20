"""Shared helpers for the Quality-layer node factories (agents.md Layer 4).

Concerns shared by the ``reviewer`` and ``reviewer_critic`` factories (plus the
acceptance-criteria read, :func:`read_criteria`):

* **Out-of-band bodies (DS9 / LEARNINGS L4).** A quality node's real output is a
  rich typed model (``TestPlan`` / ``TestReport`` / ``E2EReport`` /
  ``OracleBinding``). The ``artifacts`` channel is ``list[Artifact]`` folded by
  ``merge_by_uri`` (keys on ``.uri``); these typed models carry no ``.uri`` and
  are NOT ``Artifact`` subclasses — so a node never puts a body inline in the
  channel. :func:`emit_artifact` stores the typed body content-addressed and
  writes a small :class:`Artifact` *handle* (``cas://sha256/<hex>``) to the
  channel. The handle's ``uri`` is the body's content identity, so an identical
  re-emission on resume coalesces under ``merge_by_uri`` (idempotent, L7 —
  ``content_digest``, never builtin ``hash()``).

* **The server-issued judgment nonce (the verdict seam).** A judgment verdict's
  ``verdict_nonce`` is single-use and **server-issued** (the cross-fork nonce
  ledger in ``foundry/state/global_records.py``); an agent must NOT mint one.
  The reviewer reads the nonce the runtime threaded onto the run config —
  :func:`server_issued_nonce` — exactly as nodes read ``thread_id``. With no
  nonce present the reviewer cannot produce a verdict that ``_merge_gates`` will
  admit (``nonce_consumed_by_other_verdict`` fail-closes on an empty nonce), so
  the helper raises rather than minting a value that would be silently dropped.

* **Subject identity for the binding.** The judgment binding signs
  ``subject_artifact_uri`` + ``state_sha`` (= ``run_against``); both are run
  inputs read off the config seam (with a deterministic fallback derived from
  state), never invented by the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

# CR5: ``emit_artifact`` is an agents-level shared primitive (one definition in
# ``foundry.agents._common``); re-exported here so the quality factories' import
# path stays stable and there is no byte-duplicate to drift.
from foundry.agents._common import emit_artifact, journal_entry
from foundry.canonical import content_digest
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.product import AcceptanceCriterion

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.state.factory_state import FactoryState


__all__ = [
    "ACCEPTANCE_CRITERION_KIND",
    "ArtifactResolver",
    "MissingCriteriaResolverError",
    "MissingVerdictNonceError",
    "emit_artifact",
    "journal_entry",
    "read_criteria",
    "require_verdict_nonce",
    "server_issued_nonce",
    "state_sha",
    "subject_artifact_uri",
]


class MissingVerdictNonceError(RuntimeError):
    """No server-issued ``verdict_nonce`` on the run config.

    Fail-closed: the reviewer never mints its own nonce (the ledger issues it),
    and a verdict with no nonce can never be proven fresh — ``_merge_gates``
    would drop it. Raising here surfaces the wiring gap instead of silently
    emitting a dead verdict.
    """


def _configurable(config: RunnableConfig) -> dict[str, Any]:
    cfg = config.get("configurable") or {}
    return dict(cfg)


def require_verdict_nonce(config: RunnableConfig) -> str:
    """Return the server-issued BASE nonce, or raise :class:`MissingVerdictNonceError`.

    Called first by a judgment node so a missing nonce fails closed BEFORE anything
    else — the most fundamental trust input of a verdict — even though the
    per-request nonce is only derived later (once the subject/state it binds to are
    known). The reviewer never mints one (L5)."""
    base = _configurable(config).get("verdict_nonce")
    if not isinstance(base, str) or not base:
        raise MissingVerdictNonceError(
            "a judgment node requires a server-issued verdict_nonce on "
            "config['configurable']['verdict_nonce']; agents never mint one"
        )
    return base


def server_issued_nonce(
    config: RunnableConfig, *, node_id: str, subject_uri: str, state_sha: str
) -> str:
    """The single-use ``verdict_nonce`` for THIS review request.

    Server-issued by the nonce ledger (``global_records.py``) as a per-run BASE and
    threaded onto the run config; the reviewer consumes it and never mints one
    (LEARNINGS L5: agents do not author the trust inputs of a verdict). Fail-closed
    when absent.

    The base is BOUND to this request's identity — ``(node_id, subject_uri,
    state_sha)`` — so every judgment verdict in a run gets its OWN single-use nonce
    (H3). Without this, the two reviews a gated/autonomous run emits (quality +
    assurance) would share one nonce, and consuming the first would make
    ``merge_gates`` reject the second. The derivation is still server-grounded (the
    base) and not agent-mintable (node/subject/state are run identity, all signed
    into the judgment binding), and it is deterministic so a resume re-derives the
    SAME nonce (idempotent consumption).
    """
    base = require_verdict_nonce(config)
    return "N-" + content_digest(
        {"base": base, "node": node_id, "subject": subject_uri, "state": state_sha}
    )


def subject_artifact_uri(config: RunnableConfig, state_subject: str | None) -> str:
    """The artifact under review — a run input on the config seam, else state.

    Signed into the judgment binding, so it is taken from the run config the
    runtime set up (the diff/PR being reviewed); falls back to the subject the
    state already names. Fail-closed only at the proxy (an empty subject still
    yields a coherent — if narrow — binding)."""
    subject = _configurable(config).get("subject_artifact_uri")
    if isinstance(subject, str) and subject:
        return subject
    return state_subject or ""


def state_sha(config: RunnableConfig, fallback: str) -> str:
    """The tree/state identity the verdict runs against (``run_against``).

    Read off the config seam (the integrated ``state_sha`` the runner pins for
    this judgment); falls back to a deterministic per-run value so the binding
    is always well-formed."""
    sha = _configurable(config).get("state_sha")
    if isinstance(sha, str) and sha:
        return sha
    return fallback


# The product layer emits each AcceptanceCriterion as a content-addressed
# Artifact HANDLE of this kind (raw typed bodies carry no ``.uri`` and cannot ride
# the ``merge_by_uri``-folded ``artifacts`` channel).
ACCEPTANCE_CRITERION_KIND = "acceptance_criterion"

# INFRA dependency: reconstitutes a typed body from its Artifact handle (CAS in
# prod, an in-memory map in tests) — the Senior Engineer's seam.
ArtifactResolver = Callable[[Artifact], FrozenModel]


class MissingCriteriaResolverError(RuntimeError):
    """``acceptance_criterion`` handles are on state but no ``resolve`` is wired.

    Fail-closed (C-R1-01): without a resolver the reviewer would read ZERO
    criteria and author an empty ``TestPlan`` with no oracles — a VACUOUS Test
    gate pass."""


def read_criteria(
    state: FactoryState, resolve: ArtifactResolver | None
) -> tuple[AcceptanceCriterion, ...]:
    """The acceptance criteria on ``state['artifacts']``, resolved, deduped and
    ordered by ``id`` (fold-order-independent across a resume re-fold).

    Raises :class:`MissingCriteriaResolverError` when handles are present but no
    ``resolve`` is wired (C-R1-01); with no handles at all the result is ``()``."""
    handles = [
        h
        for h in (state.get("artifacts") or ())
        if getattr(h, "kind", None) == ACCEPTANCE_CRITERION_KIND
    ]
    if handles and resolve is None:
        raise MissingCriteriaResolverError(
            f"{len(handles)} {ACCEPTANCE_CRITERION_KIND!r} handle(s) on "
            "state['artifacts'] but no resolve= was wired; without it the reviewer "
            "would read zero criteria and emit an empty TestPlan — a vacuous "
            "Test-gate pass. Thread the CAS resolver into build_quality_subgraph."
        )
    by_id: dict[str, AcceptanceCriterion] = {}
    if resolve is not None:
        for handle in handles:
            body = resolve(handle)
            if isinstance(body, AcceptanceCriterion):
                by_id[body.id] = body
    return tuple(sorted(by_id.values(), key=lambda c: c.id))
