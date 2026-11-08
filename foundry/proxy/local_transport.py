"""A local OpenAI-compatible model server (LM Studio, Ollama, llama.cpp) as a
proxy egress. (workshop plan, Phase 4b)

:class:`LocalOpenAITransport` is the egress for a mode's local levels. Unlike the
cloud transports it holds NO credential: the server is reached at an operator-
named URL, so the OpenAI SDK is built with a placeholder key — never the
``OPENAI_API_KEY`` the SDK would otherwise read from the environment, which must
not leak to whatever host the URL names.

Structured output: a schema call first asks the server for a grammar-constrained
``response_format`` json_schema. A server that rejects it is remembered and every
later schema call falls back to prompt-and-parse (a json-only instruction, then
the proxy's lenient JSON extraction) — the same fail-closed channel the CLI
backends use.

Sampling: the proxy's per-model local profile arrives as ``local_sampling``;
OpenAI fields go as such, the rest as extra body fields (see ``_OPENAI_SAMPLING``).

Thinking: the proxy's ``local_thinking`` flag picks ``reasoning_effort`` — ``xhigh``
(Qwen3.8's level for agentic loops; ``local_effort`` overrides it, e.g. ``medium``
for agent turns; Ollama maps any effort to plain "on" for a
model with only on/off) or ``none``. The reasoning comes back in the message's
``reasoning`` field (or inline ``<think>``), never in the parsed text, and is
replayed as the ``reasoning`` of that assistant turn on the session's next call —
how Ollama's /v1 carries Qwen's "preserve thinking": it maps an assistant
message's ``reasoning`` onto the turn's thinking, which the Qwen3.8 renderer
always renders (and the Qwen3.5/3.6 one renders for turns after the last user
query).
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from foundry import progress
from foundry.proxy.cli_transports import (
    _JSON_ONLY,
    TransportError,
    _as_dict,
    _as_int,
    _chat_messages,
    _chat_tool_calls,
    _chat_tools,
    _is_tools_rejection,
    _schema_of,
    _usage,
)
from foundry.proxy.model_provider_proxy import ProviderResponse, ToolsUnsupported, _ClientCache

__all__ = [
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_LOCAL_TIMEOUT_S",
    "DEFAULT_LOCAL_URL",
    "LocalOpenAITransport",
    "LocalServerStatus",
    "check_local_server",
    "validate_local_url",
]

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
# Qwen3.8-27B (dense), Ollama's 4-bit NVFP4 tag: ~18 GB of weights leave room for
# a 128K context in 48 GB. The fallback is `qwen3.6:27b-coding-mxfp8`. The exact
# id depends on how the model was pulled; `--local-check` lists what the server
# actually serves, and --local-model / FOUNDRY_LOCAL_MODEL override this.
DEFAULT_LOCAL_MODEL = "qwen3.8:27b-nvfp4"
# One thinking call on a ~128K prompt is minutes of prefill plus up to 40K tokens
# of output at ~10-15 tok/s; this only catches a server that hung.
DEFAULT_LOCAL_TIMEOUT_S = 4 * 3600.0

# The SDK insists on a key; a local server ignores it. Never the real OpenAI key.
_PLACEHOLDER_KEY = "local-no-key"

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# Chat roles some local chat templates reject (Qwen3.8 has no developer turn).
_LOCAL_ROLES = {"developer": "system"}
_REASONING_MEMORY = 1024
# The sampling fields Ollama's /v1/chat/completions maps onto its options. Its Go
# decoder silently drops the rest (top_k, min_p, repeat_penalty), so those ride as
# extra body fields: honoured by llama.cpp / vLLM / LM Studio, while on Ollama the
# model's Modelfile PARAMETERs supply them (Qwen tags ship top_k 20, min_p 0).
_OPENAI_SAMPLING = frozenset({"temperature", "top_p", "presence_penalty",
                              "frequency_penalty", "seed"})
_SCHEMA_REJECTION_MARKERS = ("response_format", "json_schema", "schema", "structured", "grammar")


def validate_local_url(url: str) -> str:
    """The server base URL, or ValueError: only http(s) with a host is accepted
    (urllib would otherwise happily open ``file://`` paths)."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"local server URL must be http(s)://host[:port]/..., got {url!r}")
    return url.strip().rstrip("/")


def _split_thinking(text: str) -> tuple[str, str]:
    """``(reasoning, answer)``: reasoning some local models (Qwen) emit inline is
    split off, or its braces would derail the JSON extraction. A bare leading
    ``...</think>`` is reasoning whose opening tag the chat template wrote."""
    if "</think>" not in text:
        return "", text
    parts = [m.strip() for m in _THINK_BLOCK.findall(text)]
    rest = _THINK_BLOCK.sub("", text)
    if "</think>" in rest:
        head, _, rest = rest.rpartition("</think>")
        parts.insert(0, head.replace("<think>", "").strip())
    return "\n\n".join(p for p in parts if p), rest.strip()


def _turn_key(content: Any, call_ids: tuple[str, ...]) -> tuple[str, ...] | None:
    """What identifies an assistant turn when the session sends it back: its tool
    call ids, else its text."""
    if call_ids:
        return ("calls", *call_ids)
    if isinstance(content, str) and content.strip():
        return ("text", content)
    return None


def _is_schema_rejection(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (401, 403, 429, 529):
        return False
    return any(m in str(exc).lower() for m in _SCHEMA_REJECTION_MARKERS)


class LocalOpenAITransport:
    """``chat.completions`` against a local OpenAI-compatible server."""

    def __init__(
        self,
        base_url: str = DEFAULT_LOCAL_URL,
        *,
        client: Any | None = None,
        timeout_s: float = DEFAULT_LOCAL_TIMEOUT_S,
    ) -> None:
        self.base_url = validate_local_url(base_url)
        # None = untested, True = the server honours response_format, False = it
        # rejected it (prompt-and-parse from then on).
        self.schema_supported: bool | None = None
        # Same for `reasoning_effort` ("none" turns thinking off on Ollama; some
        # servers, e.g. vLLM, accept only low/medium/high and reject the request).
        self.reasoning_supported: bool | None = None
        # Native `tools`, per model: Ollama rejects them for a model whose template
        # has no tool support, and one server serves several tiers' models. False
        # makes the proxy raise ToolsUnsupported without sending.
        self._tools_by_model: dict[str, bool] = {}
        # Each recent assistant turn's reasoning, by _turn_key, replayed when the
        # session sends that turn back (bounded: sessions end, keys go stale).
        self._reasoning: OrderedDict[tuple[str, ...], str] = OrderedDict()
        self._reasoning_lock = threading.Lock()

        def _build() -> Any:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - import guard
                raise TransportError("the local backend requires the openai SDK") from exc
            return openai.OpenAI(base_url=self.base_url, api_key=_PLACEHOLDER_KEY,
                                 timeout=timeout_s, max_retries=0)
        self._cache = _ClientCache(_build, injected=client)

    def tools_support(self, model: str) -> bool | None:
        return self._tools_by_model.get(model)

    def remember(self, response: ProviderResponse) -> None:
        """Keep ``response``'s reasoning for when its turn comes back."""
        key = _turn_key(response.text, tuple(str(c.get("id", "")) for c in response.tool_calls))
        if key is None or not response.reasoning:
            return
        with self._reasoning_lock:
            self._reasoning[key] = response.reasoning
            self._reasoning.move_to_end(key)
            while len(self._reasoning) > _REASONING_MEMORY:
                self._reasoning.popitem(last=False)

    def _messages(self, request: dict[str, Any], thinking: bool) -> list[dict[str, Any]]:
        messages = []
        for msg in _chat_messages(request):
            msg = {**msg, "role": _LOCAL_ROLES.get(msg["role"], msg["role"])}
            if thinking and msg["role"] == "assistant":
                ids = tuple(str(c.get("id", "")) for c in msg.get("tool_calls") or ())
                key = _turn_key(msg.get("content"), ids)
                if key is not None:
                    with self._reasoning_lock:
                        reasoning = self._reasoning.get(key)
                    if reasoning is not None:
                        msg["reasoning"] = reasoning
            messages.append(msg)
        return messages

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise TransportError("local transport requires a pinned model id on the request")
        thinking = request.get("local_thinking") is True
        messages = self._messages(request, thinking)

        # Ollama's /v1 ignores `think` and honours this; "none" turns a Qwen3.x
        # model's default thinking off.
        params: dict[str, Any] = {"model": model, "messages": messages,
                                  "reasoning_effort": (request.get("local_effort") or "xhigh")
                                  if thinking else "none"}
        max_tokens = request.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            params["max_tokens"] = max_tokens
        if "temperature" in request:
            params["temperature"] = request["temperature"]
        sampling = request.get("local_sampling")
        if isinstance(sampling, dict):
            params.update({k: v for k, v in sampling.items() if k in _OPENAI_SAMPLING})
            extra = {k: v for k, v in sampling.items() if k not in _OPENAI_SAMPLING}
            if extra:
                params["extra_body"] = extra
        if "tools" in request:
            if self._tools_by_model.get(model) is False:
                raise ToolsUnsupported(f"local server rejected tools for {model}")
            try:
                response = self._call({**params, "tools": _chat_tools(request)})
            except Exception as exc:
                if model in self._tools_by_model or not _is_tools_rejection(exc):
                    raise
                self._tools_by_model[model] = False
                progress.log(f"local server rejected tools for {model} ({exc}); "
                             "using JSON actions")
                raise ToolsUnsupported(f"local server rejected tools for {model}: {exc}") from exc
            self._tools_by_model[model] = True
            return response
        schema = _schema_of(request)
        if schema is None:
            return self._call(params)
        if self.schema_supported is not False:
            native = {**params, "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }}
            try:
                response = self._call(native)
            except Exception as exc:
                if not _is_schema_rejection(exc):
                    raise
                self.schema_supported = False
                progress.log(f"local server rejected json_schema output ({exc}); "
                             "falling back to prompt-and-parse")
            else:
                self.schema_supported = True
                return response
        instruction = _JSON_ONLY + json.dumps(schema, sort_keys=True)
        fallback = [*messages, {"role": "user", "content": instruction}]
        return self._call({**params, "messages": fallback})

    def _call(self, params: dict[str, Any]) -> ProviderResponse:
        if self.reasoning_supported is False:
            params = {k: v for k, v in params.items() if k != "reasoning_effort"}
        try:
            response = self._cache.run(lambda c: c.chat.completions.create(**params))
        except Exception as exc:
            if "reasoning_effort" not in params or "reasoning" not in str(exc).lower():
                raise
            self.reasoning_supported = False
            progress.log(f"local server rejected reasoning_effort ({exc}); sending without it")
            return self._call(params)
        if "reasoning_effort" in params:
            self.reasoning_supported = True
        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = choice.message if choice is not None else None
        text = (getattr(message, "content", "") or "") if message is not None else ""
        inline, text = _split_thinking(text if isinstance(text, str) else str(text))
        # Ollama names it `reasoning`; llama.cpp / vLLM `reasoning_content`.
        field = getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None)
        reasoning = "\n\n".join(r for r in (field if isinstance(field, str) else "", inline) if r)
        usage = getattr(response, "usage", None)
        out = ProviderResponse(
            served_model=str(getattr(response, "model", "") or ""),
            request_id=str(getattr(response, "id", "") or ""),
            text=text,
            usage=_usage(
                input_tokens=_as_int(getattr(usage, "prompt_tokens", 0)) if usage else 0,
                output_tokens=_as_int(getattr(usage, "completion_tokens", 0)) if usage else 0,
            ),
            stop_reason=str(getattr(choice, "finish_reason", "") or "stop") if choice else "stop",
            tool_calls=_chat_tool_calls(message) if message is not None else (),
            reasoning=reasoning,
        )
        self.remember(out)
        return out


@dataclass(frozen=True)
class LocalServerStatus:
    """What :func:`check_local_server` observed (JSON-ready via ``as_dict``)."""

    url: str
    reachable: bool
    models: tuple[str, ...] = ()
    error: str | None = None
    model: str | None = None
    model_served: bool | None = None  # is ``model`` among ``models`` (None: not asked)

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "reachable": self.reachable, "models": list(self.models),
                "error": self.error, "model": self.model, "model_served": self.model_served}


def check_local_server(
    url: str = DEFAULT_LOCAL_URL, *, model: str | None = None, timeout_s: float = 5.0,
    opener: Any = None,
) -> LocalServerStatus:
    """Is the local server up, and which models does it serve (``GET /models``)?
    Never raises for an unreachable server — that is the answer, not an error.
    Sends no credential."""
    try:
        base = validate_local_url(url)
    except ValueError as exc:
        return LocalServerStatus(url=url, reachable=False, error=str(exc), model=model)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(f"{base}/models", timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return LocalServerStatus(url=base, reachable=False, error=str(exc), model=model)
    data = _as_dict(body).get("data")
    models = tuple(str(m.get("id")) for m in data or () if isinstance(m, dict) and m.get("id"))
    served = (model in models) if model else None
    return LocalServerStatus(url=base, reachable=True, models=models, model=model,
                             model_served=served)
