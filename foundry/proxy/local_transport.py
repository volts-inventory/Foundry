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
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
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
# Qwen 3.6-27B (dense), Ollama's 8-bit MLX coding tag — the plan's default local model. The exact
# id depends on how the model was pulled; `--local-check` lists what the server
# actually serves, and --local-model / FOUNDRY_LOCAL_MODEL override this.
DEFAULT_LOCAL_MODEL = "qwen3.6:27b-coding-mxfp8"
# A dense 27B on a laptop can take many minutes for one long completion.
DEFAULT_LOCAL_TIMEOUT_S = 1800.0

# The SDK insists on a key; a local server ignores it. Never the real OpenAI key.
_PLACEHOLDER_KEY = "local-no-key"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
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


def _strip_thinking(text: str) -> str:
    """Drop reasoning blocks some local models (Qwen) emit inline: their braces
    would otherwise derail the JSON extraction."""
    return _THINK_BLOCK.sub("", text).strip() if "<think>" in text else text


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

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise TransportError("local transport requires a pinned model id on the request")
        messages = _chat_messages(request)

        # Thinking models (Qwen3.x) reason by default, multiplying output tokens
        # ~5x on short agent turns; Ollama's /v1 ignores `think` and honours this.
        params: dict[str, Any] = {"model": model, "messages": messages,
                                  "reasoning_effort": "none"}
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
        text = (getattr(choice.message, "content", "") or "") if choice is not None else ""
        usage = getattr(response, "usage", None)
        return ProviderResponse(
            served_model=str(getattr(response, "model", "") or ""),
            request_id=str(getattr(response, "id", "") or ""),
            text=_strip_thinking(text if isinstance(text, str) else str(text)),
            usage=_usage(
                input_tokens=_as_int(getattr(usage, "prompt_tokens", 0)) if usage else 0,
                output_tokens=_as_int(getattr(usage, "completion_tokens", 0)) if usage else 0,
            ),
            stop_reason=str(getattr(choice, "finish_reason", "") or "stop") if choice else "stop",
            tool_calls=_chat_tool_calls(choice.message) if choice is not None else (),
        )


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
