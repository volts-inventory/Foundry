"""Interview an agent about how its turn went. (plan [11] — ADVISORY ONLY)

The companion to the per-agent work-journal (``foundry/schemas/journal.py``):
given an agent role + the journal entries it recorded + a little run context,
re-instantiate the role's "voice" through the proxy and ask it a free-text
question — "how is it going?", "what worried you about this design?".

This is an OBSERVABILITY affordance, nothing more. It calls
``rt.proxy.complete(..., purpose="ideation")`` and returns the answer TEXT. It
writes NO channel — no ``GateResult``, no ``GateTriage``, no ``JournalEntry`` —
so it can never enter a verdict/trust path (LEARNINGS L5: agents propose, only
signed gates decide). It is purely a read of the advisory journal turned into
prose (the Workshop's "interview a lead" dialog). Best-effort: a proxy failure
yields a short fallback string, not a raised exception, because an interview
must never be able to wedge a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.agents._common import as_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from foundry.proxy.model_provider_proxy import ModelProviderProxy
    from foundry.schemas.journal import JournalEntry

__all__ = ["interview"]

_INTERVIEW_SOP = (
    "You are being interviewed about work you just did on a software build. You "
    "are given your ROLE, the advisory journal entries you recorded during your "
    "turn(s), and the operator's question. Answer in the first person, candidly "
    "and concisely, grounded ONLY in your journal entries and the run context. "
    "This is an advisory reflection for a human operator — you are NOT issuing a "
    "verdict, approving anything, or changing the build's state."
)


def _entries_payload(entries: Sequence[JournalEntry]) -> list[dict[str, object]]:
    """The journal as plain, prompt-stable data (no model call to build it)."""
    return [
        {
            "node_id": e.node_id,
            "role": e.role,
            "summary": e.summary,
            "assumptions": list(e.assumptions),
            "concerns": list(e.concerns),
            "confidence": e.confidence,
        }
        for e in entries
    ]


def interview(
    proxy: ModelProviderProxy,
    *,
    role: str,
    question: str,
    entries: Sequence[JournalEntry],
    run_context: dict[str, object] | None = None,
    tier: str = "T3",
) -> str:
    """Ask ``role`` ``question`` given its journal ``entries``; return the answer.

    ADVISORY ONLY — returns the proxy-generated answer TEXT and writes nothing to
    any channel (it cannot mint a gate/verdict; see module docstring). Best-effort:
    any proxy error degrades to a short fallback string rather than raising, so an
    interview can never wedge or fail a run. ``tier`` defaults to the cheapest
    (``T3``) — interviewing is observability, not a load-bearing model call.
    """
    user = {
        "role": role,
        "question": question,
        "journal_entries": _entries_payload(entries),
        "run_context": run_context or {},
    }
    try:
        result = proxy.complete(
            f"interview:{role}",
            tier,
            _INTERVIEW_SOP,
            [{"role": "user", "content": as_text(user)}],
            purpose="ideation",
        )
    except Exception as exc:  # best-effort: never let an interview wedge a run
        return f"(interview unavailable: {type(exc).__name__})"
    return result.text
