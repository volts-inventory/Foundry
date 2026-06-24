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
from foundry.agents._common import emit_artifact, journal_entry

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


__all__ = [
    "MissingVerdictNonceError",
    "emit_artifact",
    "journal_entry",
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
