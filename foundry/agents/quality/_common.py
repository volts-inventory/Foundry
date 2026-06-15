"""Shared helpers for the Quality-layer node factories (agents.md Layer 4).

Three concerns recur across reviewer_critic / qa_engineer / sdet and live here
so each factory stays a faithful SOP + a thin model-call body:

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

from typing import TYPE_CHECKING, Any

# CR5: ``emit_artifact`` is an agents-level shared primitive (one definition in
# ``foundry.agents._common``); re-exported here so the quality factories' import
# path stays stable and there is no byte-duplicate to drift.
from foundry.agents._common import emit_artifact
from foundry.schemas.product import AcceptanceCriterion

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.state.factory_state import FactoryState


__all__ = [
    "MissingVerdictNonceError",
    "criteria_from_state",
    "emit_artifact",
    "server_issued_nonce",
    "state_sha",
    "subject_artifact_uri",
]


def criteria_from_state(state: FactoryState) -> tuple[AcceptanceCriterion, ...]:
    """The acceptance criteria the QA / SDET nodes plan against — read from state.

    The CRITERIA SET (not the model) is the source of truth for coverage (Q4), and
    it is a **DATA input** — so per the node convention (AR1-AR4) it is read from
    ``state`` inside the closure, NOT frozen in as a build-time factory kwarg. The
    product layer authors :class:`AcceptanceCriterion` records and the orchestrator
    resolves their CAS bodies and seeds the typed records onto the ``artifacts``
    channel between the product and quality phases; this helper recovers them by
    TYPE (``isinstance``), ordered by ``criterion_id`` so the planned coverage is
    fold-order-independent (byte-identical across a resume re-fold). The small
    ``Artifact`` HANDLES that also ride the channel (kind ``acceptance_criterion``)
    are ignored — they carry no body to plan against. Absent criteria -> ``()``;
    the QA node then has nothing to fail-close coverage to (an empty plan), which
    surfaces the wiring gap at the Test gate rather than silently mis-planning.
    """
    found = [a for a in (state.get("artifacts") or []) if isinstance(a, AcceptanceCriterion)]
    return tuple(sorted(found, key=lambda c: c.id))


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


def server_issued_nonce(config: RunnableConfig) -> str:
    """The single-use ``verdict_nonce`` the runtime threaded onto the run config.

    Server-issued by the nonce ledger (``global_records.py``); the reviewer
    consumes it and never mints one (LEARNINGS L5: agents do not author the
    trust inputs of a verdict). Fail-closed when absent.
    """
    nonce = _configurable(config).get("verdict_nonce")
    if not isinstance(nonce, str) or not nonce:
        raise MissingVerdictNonceError(
            "reviewer_critic requires a server-issued verdict_nonce on "
            "config['configurable']['verdict_nonce']; agents never mint one"
        )
    return nonce


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
