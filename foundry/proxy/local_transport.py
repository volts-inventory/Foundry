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
    _message_pairs,
    _schema_of,
    _system_text,
    _usage,
)
from foundry.proxy.model_provider_proxy import ProviderResponse, _ClientCache

__all__ = [
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_LOCAL_TIMEOUT_S",
    "DEFAULT_LOCAL_URL",
    "LocalOpenAITransport",
    "LocalServerStatus",
    "check_local_server",
    "validate_local_url",
]

DEFAULT_LOCAL_URL = "http://localhost:1234/v1"  # LM Studio's default server
# Qwen 3.6-27B (dense) at 6-bit, MLX — the plan's default local model. The exact id
# depends on how the model was downloaded; `--local-check` lists what the server
# actually serves, and --local-model / FOUNDRY_LOCAL_MODEL override this.
DEFAULT_LOCAL_MODEL = "mlx-community/Qwen3.6-27B-6bit"
# A dense 27B on a laptop can take many minutes for one long completion.
DEFAULT_LOCAL_TIMEOUT_S = 1800.0

# The SDK insists on a key; a local server ignores it. Never the real OpenAI key.
_PLACEHOLDER_KEY = "local-no-key"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
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

        def _build() -> Any:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - import guard
                raise TransportError("the local backend requires the openai SDK") from exc
            return openai.OpenAI(base_url=self.base_url, api_key=_PLACEHOLDER_KEY,
                                 timeout=timeout_s, max_retries=0)
        self._cache = _ClientCache(_build, injected=client)

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise TransportError("local transport requires a pinned model id on the request")
        messages: list[dict[str, str]] = []
        sys_text = _system_text(request)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for role, text in _message_pairs(request):
            messages.append({"role": role, "content": text})

        params: dict[str, Any] = {"model": model, "messages": messages}
        max_tokens = request.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            params["max_tokens"] = max_tokens
        if "temperature" in request:
            params["temperature"] = request["temperature"]
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
        response = self._cache.run(lambda c: c.chat.completions.create(**params))
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
