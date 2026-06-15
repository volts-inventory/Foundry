"""Deterministic test doubles for the model-provider proxy. (plan §0.3, §3.3)

These are the single seam every later wave injects to run the whole graph +
CLI end-to-end with NO live API key and NO ``claude -p`` binary. The proxy is
the only credential holder (LEARNINGS L6: all model egress goes through it), so
making the factory testable means making *its transport* a deterministic fake —
not stubbing model calls scattered through agents/gates.

:class:`MockTransport` implements :class:`~foundry.proxy.model_provider_proxy.ProviderTransport`
and returns a deterministic :class:`ProviderResponse`:

* ``served_model`` defaults to the request's pinned model (a real
  ``TIER_MODELS`` value the :class:`LineageMap` resolves to ``anthropic-claude``);
* a synthetic-but-STABLE ``request_id`` derived from the request via
  ``content_digest`` (LEARNINGS L7: never builtin ``hash()`` — it is
  seed-randomized per process and would make request ids non-reproducible);
* a valid ``usage`` dict (input/output/cache tokens);
* ``stop_reason="end_turn"``.

Two scripting layers, in priority order:

1. **Keyed scripts** (``responses=``): a caller pre-loads
   ``ResponseScript`` entries each carrying an optional ``match`` predicate
   over the outgoing request; the FIRST matching script wins. Convenience
   constructors build the common shapes:

   * :meth:`MockTransport.scripted` — keyed by a substring that must appear in
     the serialized system SOP or messages;
   * the ``verdicts=``/``completions=`` ctor kwargs — ordered queues consumed
     FIFO on the verdict / free-text channels respectively.

2. **Channel default**: when no script matches, a constrained verdict request
   (``output_config.format.type == "json_schema"``) yields a valid verdict
   JSON (default ``approve``, overridable via ``default_verdict``); any other
   request yields ``default_text``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from foundry.canonical import content_digest
from foundry.proxy.model_provider_proxy import TIER_MODELS, ProviderResponse

__all__ = [
    "DEFAULT_USAGE",
    "MockTransport",
    "ResponseScript",
    "Verdict",
    "verdict_json",
]

# A valid, lineage-resolvable served model id (LineageMap globs ``claude-*``).
_DEFAULT_SERVED_MODEL = TIER_MODELS["T1"]

# A plausible, fixed usage dict so the budget-ledger feed is always populated.
DEFAULT_USAGE: dict[str, int] = {
    "input_tokens": 1024,
    "output_tokens": 96,
    "cache_read_input_tokens": 768,
    "cache_creation_input_tokens": 128,
}

Verdict = Literal["approve", "request-changes"]


def verdict_json(status: Verdict = "approve", findings: Sequence[str] = ()) -> str:
    """Serialize a grammar-conforming verdict payload (the constrained channel).

    The shape matches ``VERDICT_SCHEMA``: a ``status`` in the verdict grammar
    plus a ``findings`` array. The proxy ``json.loads`` this off the verdict
    channel, so it MUST be valid JSON.
    """
    return json.dumps({"status": status, "findings": list(findings)})


def _request_is_verdict_channel(request: dict[str, Any]) -> bool:
    """A constrained json_schema/verdict request carries an output_config
    format of type ``json_schema`` (set by the proxy when a schema is passed)."""
    fmt = request.get("output_config", {}).get("format", {})
    return isinstance(fmt, dict) and fmt.get("type") == "json_schema"


def _serialized_prompt(request: dict[str, Any]) -> str:
    """Flatten the request's system SOP + messages into one string for
    substring matching (deterministic, order-preserving)."""
    return json.dumps(
        {"system": request.get("system", []), "messages": request.get("messages", [])},
        sort_keys=True,
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class ResponseScript:
    """One pre-loaded response with an optional request predicate.

    ``match`` returns True if this script should serve a given request; when
    None the script matches any request (a catch-all). ``served_model`` and
    ``stop_reason`` default to the deterministic values; ``usage`` defaults to
    :data:`DEFAULT_USAGE`.
    """

    text: str
    match: Callable[[dict[str, Any]], bool] | None = None
    served_model: str = _DEFAULT_SERVED_MODEL
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_USAGE))


def _substring_match(needle: str) -> Callable[[dict[str, Any]], bool]:
    def _match(request: dict[str, Any]) -> bool:
        return needle in _serialized_prompt(request)

    return _match


class MockTransport:
    """Deterministic ``ProviderTransport`` with scripted/keyed responses.

    Parameters
    ----------
    responses:
        Ordered :class:`ResponseScript` entries; the first whose ``match``
        accepts the request serves it.
    verdicts / completions:
        FIFO queues for the two channels — verdict statuses (or pre-serialized
        verdict JSON) consumed on the constrained json_schema channel, and
        free-text bodies consumed on every other channel. Consumed before the
        per-channel default. Useful for "approve then request-changes" sequences.
    default_verdict:
        Status returned on the verdict channel when nothing is scripted.
    default_text:
        Body returned on the free-text channel when nothing is scripted.
    served_model:
        The model id reported as served (must resolve in the LineageMap).
    """

    def __init__(
        self,
        responses: Sequence[ResponseScript] = (),
        *,
        verdicts: Sequence[Verdict | str] = (),
        completions: Sequence[str] = (),
        default_verdict: Verdict = "approve",
        default_text: str = "",
        served_model: str = _DEFAULT_SERVED_MODEL,
        usage: dict[str, int] | None = None,
    ) -> None:
        self._responses: list[ResponseScript] = list(responses)
        self._verdict_queue: list[str] = [
            v if v.startswith("{") else verdict_json(v)  # type: ignore[arg-type]
            for v in verdicts
        ]
        self._completion_queue: list[str] = list(completions)
        self._default_verdict: Verdict = default_verdict
        self._default_text = default_text
        self._served_model = served_model
        self._usage = dict(usage) if usage is not None else dict(DEFAULT_USAGE)
        self.requests: list[dict[str, Any]] = []  # observed, in send order

    # ---- convenience constructors ------------------------------------------

    @classmethod
    def scripted(cls, mapping: dict[str, str], **kwargs: Any) -> MockTransport:
        """Build a transport keyed by a substring of the system SOP / messages.

        Each ``{needle: response_text}`` becomes a substring-matched script, in
        insertion order (first match wins). Remaining kwargs configure the
        channel defaults (e.g. ``default_verdict="request-changes"``).
        """
        scripts = [
            ResponseScript(text=text, match=_substring_match(needle))
            for needle, text in mapping.items()
        ]
        return cls(scripts, **kwargs)

    # ---- the egress seam ----------------------------------------------------

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.requests.append(request)
        script = next((s for s in self._responses if s.match is None or s.match(request)), None)
        if script is not None:
            return self._respond(request, script.text, script.served_model, script.usage)

        if _request_is_verdict_channel(request):
            text = self._verdict_queue.pop(0) if self._verdict_queue else verdict_json(
                self._default_verdict
            )
        else:
            text = self._completion_queue.pop(0) if self._completion_queue else self._default_text
        return self._respond(request, text, self._served_model, self._usage)

    # ---- internals ----------------------------------------------------------

    def _respond(
        self,
        request: dict[str, Any],
        text: str,
        served_model: str,
        usage: dict[str, int],
    ) -> ProviderResponse:
        return ProviderResponse(
            served_model=served_model,
            # Stable, process-reproducible id (L7: content_digest, not hash()).
            request_id=f"req_mock_{content_digest(request)[:16]}",
            text=text,
            usage=dict(usage),
            stop_reason="end_turn",
        )
