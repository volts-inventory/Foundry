"""Deterministic test doubles for the model-provider proxy. (plan §0.3, §3.3)

These are the single seam every later wave injects to run the whole graph +
CLI end-to-end with NO live API key and NO ``claude -p`` binary. The proxy is
the only credential holder (LEARNINGS L6: all model egress goes through it), so
making the factory testable means making *its transport* a deterministic fake —
not stubbing model calls scattered through agents/gates.

:class:`MockTransport` implements :class:`~foundry.proxy.model_provider_proxy.ProviderTransport`
and returns a deterministic :class:`ProviderResponse`:

* ``served_model`` defaults to **the model id present in the request** (the
  requested tier's pinned model, a real ``TIER_MODELS`` value the
  :class:`LineageMap` resolves to ``anthropic-claude``), so a T2/T3 call is not
  spuriously flagged for pinned-ID drift. Pass an explicit ``served_model=`` to
  the ctor (or set it on a :class:`ResponseScript`) to FORCE a drift scenario;
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

2. **Channel default**: when no script matches, a constrained VERDICT request
   yields a valid verdict JSON (default ``approve``, overridable via
   ``default_verdict``); any other request yields ``default_text``.

Verdict-channel detection (AC3, RESOLVED — DT1): a request is the verdict
channel only when it carries ``output_config.format.type == "json_schema"`` AND
the schema IS the judgment verdict schema (``VERDICT_SCHEMA``), detected by
STRUCTURE — a ``status`` enum of ``["approve","request-changes"]`` or a
``required`` set covering both ``status`` and ``findings`` (see
:func:`_is_verdict_schema`). The mere presence of a json_schema format is NOT
enough: a schema-constrained *ideation* completion (PRD / SystemDesign /
TestPlan / decomposition / ...) is NOT shadowed by the verdict FIFO/default — it
falls through to the keyed-script / ``completions=`` / ``default_text`` path and
a scripted JSON payload is returned verbatim as ``text``. ``judge()`` passes
``VERDICT_SCHEMA``, so it still hits the verdict path.

Channel/script priority (R1-01/R1-03): on the **constrained verdict channel**
the verdict queue/default take priority over a keyed script, so a verdict is
ALWAYS emitted there and ``proxy.judge()`` can never crash in ``json.loads`` on
free-text that happened to match a script. On every other channel (including
schema-constrained ideation), keyed scripts / ``completions=`` win as before.
Verdict queue items are detected as literal-vs-JSON robustly (``json.loads``
probe, whitespace-tolerant), so leading whitespace no longer mis-classifies a
status literal as JSON.
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
# Only used as a last-resort fallback when the request carries no model id;
# normally ``served_model`` mirrors the request's pinned model (no spurious drift).
_DEFAULT_SERVED_MODEL = TIER_MODELS["T1"]


class _UseRequestModel:
    """Sentinel: derive ``served_model`` from the request (no forced drift)."""


# Default served_model = the request's own model id (R1-02). A test forces a
# drift scenario by passing an explicit served_model= override instead.
_FROM_REQUEST: Any = _UseRequestModel()

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


def _normalize_verdict_item(item: str) -> str:
    """Coerce a verdict-queue item to a JSON verdict payload.

    Items may be a status literal (``"approve"`` / ``"request-changes"``,
    possibly with surrounding whitespace) or pre-serialized verdict JSON. We
    probe with ``json.loads`` (whitespace-tolerant) rather than ``startswith("{")``
    (R1-01): if the item parses to a JSON object it is already a payload and is
    passed through verbatim; otherwise it is treated as a status literal.
    """
    try:
        parsed = json.loads(item)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return item
    return verdict_json(item.strip())  # type: ignore[arg-type]


def _request_model(request: dict[str, Any]) -> str | None:
    """The pinned model id the proxy put on the request, if any."""
    model = request.get("model")
    return model if isinstance(model, str) else None


def _is_verdict_schema(schema: Any) -> bool:
    """True iff ``schema`` IS the judgment verdict schema (``VERDICT_SCHEMA``).

    Detected by STRUCTURE, not by the mere presence of a json_schema format
    (AC3, resolved): a ``status`` enum of exactly ``["approve","request-changes"]``,
    OR a ``required`` set covering both ``status`` and ``findings``. Any OTHER
    json_schema (a PRD, SystemDesign, TestPlan, ... ideation payload) is NOT the
    verdict channel and falls through to the keyed-script / ``completions=`` /
    ``default_text`` path, so a scripted structured payload is served verbatim.
    """
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if not isinstance(props, dict):
        return False
    status = props.get("status")
    if isinstance(status, dict) and status.get("enum") == ["approve", "request-changes"]:
        return True
    required = schema.get("required")
    return (
        "findings" in props
        and isinstance(required, (list, tuple))
        and {"status", "findings"} <= set(required)
    )


def _request_is_verdict_channel(request: dict[str, Any]) -> bool:
    """True iff this request is the judgment VERDICT channel.

    The proxy attaches ``output_config.format.type == "json_schema"`` to EVERY
    schema-constrained ``complete()`` — not just verdicts (AC3). So the json_schema
    format alone is necessary but NOT sufficient: we additionally require the
    schema to BE the verdict schema (``_is_verdict_schema``). ``judge()`` passes
    ``VERDICT_SCHEMA``, so it still routes here; a scripted structured ideation
    payload (PRD/SystemDesign/TestPlan/...) does NOT, and is served via the
    keyed-script / ``completions=`` / ``default_text`` path instead.
    """
    fmt = request.get("output_config", {}).get("format", {})
    if not (isinstance(fmt, dict) and fmt.get("type") == "json_schema"):
        return False
    return _is_verdict_schema(fmt.get("schema"))


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
    None the script matches any request (a catch-all). ``served_model`` defaults
    to the request's own pinned model (no spurious pinned-ID drift, R1-02) — set
    it to a concrete id to FORCE a drift scenario. ``stop_reason`` defaults to
    the deterministic value; ``usage`` defaults to :data:`DEFAULT_USAGE`.
    """

    text: str
    match: Callable[[dict[str, Any]], bool] | None = None
    served_model: str = _FROM_REQUEST
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
        OPTIONAL override for the model id reported as served (must resolve in
        the LineageMap). When omitted, ``served_model`` mirrors the request's own
        pinned model so a T2/T3 call is not spuriously flagged for pinned-ID
        drift (R1-02); pass an explicit id to FORCE a drift scenario.
    """

    def __init__(
        self,
        responses: Sequence[ResponseScript] = (),
        *,
        verdicts: Sequence[Verdict | str] = (),
        completions: Sequence[str] = (),
        default_verdict: Verdict = "approve",
        default_text: str = "",
        served_model: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self._responses: list[ResponseScript] = list(responses)
        self._verdict_queue: list[str] = [_normalize_verdict_item(v) for v in verdicts]
        self._completion_queue: list[str] = list(completions)
        self._default_verdict: Verdict = default_verdict
        self._default_text = default_text
        # None => mirror the request's pinned model (R1-02); a concrete id forces drift.
        self._served_model: str | Any = served_model if served_model is not None else _FROM_REQUEST
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
        # On the constrained verdict channel the verdict queue/default take
        # PRIORITY over keyed scripts (R1-03): a verdict is always emitted there
        # so proxy.judge() can never crash in json.loads on free-text that
        # happened to match a script. Scripts still win on every other channel.
        if _request_is_verdict_channel(request):
            text = (
                self._verdict_queue.pop(0)
                if self._verdict_queue
                else verdict_json(self._default_verdict)
            )
            return self._respond(request, text, self._served_model, self._usage)

        script = next((s for s in self._responses if s.match is None or s.match(request)), None)
        if script is not None:
            return self._respond(request, script.text, script.served_model, script.usage)

        text = self._completion_queue.pop(0) if self._completion_queue else self._default_text
        return self._respond(request, text, self._served_model, self._usage)

    # ---- internals ----------------------------------------------------------

    def _respond(
        self,
        request: dict[str, Any],
        text: str,
        served_model: str | Any,
        usage: dict[str, int],
    ) -> ProviderResponse:
        # served_model defaults to the request's own pinned model (no spurious
        # pinned-ID drift, R1-02); a concrete override forces a drift scenario.
        resolved = served_model
        if isinstance(resolved, _UseRequestModel):
            resolved = _request_model(request) or _DEFAULT_SERVED_MODEL
        return ProviderResponse(
            served_model=resolved,
            # Stable, process-reproducible id (L7: content_digest, not hash()).
            request_id=f"req_mock_{content_digest(request)[:16]}",
            text=text,
            usage=dict(usage),
            stop_reason="end_turn",
        )
