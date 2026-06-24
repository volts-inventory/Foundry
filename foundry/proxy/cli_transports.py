"""Alternative configured backends for the model-provider proxy. (arch §2b)

The factory runs ONE flow. The only thing a backend swaps is the proxy's
egress seam (:class:`~foundry.proxy.model_provider_proxy.ProviderTransport`)
together with the tier→model map and the lineage map it resolves against —
nothing in the graph, agents, gates, or admission changes. This module adds
three drop-in transports beside the default Anthropic SDK transport:

* :class:`ClaudeCliTransport`  — shells ``claude -p "<prompt>" --output-format json``;
* :class:`CodexCliTransport`   — shells ``codex exec --json -m <model> "<prompt>"``;
* :class:`OllamaTransport`     — POSTs ``/api/chat`` to a local Ollama server
  (e.g. a ``gemma3`` model), the local/offline backend.

and three presets (:func:`claude_cli_backend`, :func:`codex_backend`,
:func:`ollama_gemma_backend`) plus :func:`build_backend` / :func:`backend_from_env`
that return a fully-wired :class:`ModelProviderProxy` — same proxy class, same
contract, different backend.

TRUST NOTES (honest, per arch §2b / plan §0.3) — what stays and what changes
when the backend is not the Anthropic SDK:

* *Observed-not-trusted still holds for lineage.* Each transport reports the
  model the backend SAYS it served (CLI JSON ``modelUsage`` key / Codex session
  model / Ollama ``model``), which the proxy resolves through the curated
  :class:`LineageMap` and drift-checks against the pinned tier id. It is the
  best wire-level observation a CLI/local server exposes; it is never the
  adapter self-report, and an unknown id still fails closed.
* *The grammar-constrained verdict channel weakens to fail-closed.* The
  Anthropic SDK enforces the verdict JSON schema as a decoding grammar. The CLIs
  do not, so for schema calls these transports INSTRUCT json-only output and the
  proxy still parses + ``ZoneBSigner``-stamps the result; a non-conforming reply
  raises :class:`VerdictChannelError` (no PR, no signed verdict) rather than
  being mis-admitted. Ollama honours ``format`` (a real JSON-schema constraint),
  so it keeps the grammar. The safety property (no forged authentic verdict) is
  preserved on all three; only availability degrades on the CLIs.
* *The credential locus moves.* With the SDK the proxy is the sole secret
  holder. With ``claude``/``codex`` the secret lives in the CLI's own auth
  (env/keychain) and with Ollama there is no secret. The proxy remains the sole
  *egress path* (all calls route through it), but it is no longer the sole
  secret holder — record this when you pick a backend.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from foundry.canonical import content_digest
from foundry.proxy.lineage import (
    DEFAULT_LINEAGE_ENTRIES,
    GEMMA_LINEAGE_ENTRIES,
    OPENAI_LINEAGE_ENTRIES,
    LineageMap,
)
from foundry.proxy.model_provider_proxy import (
    ModelProviderProxy,
    ProviderResponse,
)

__all__ = [
    "BackendName",
    "ClaudeCliTransport",
    "CliResult",
    "CliRunner",
    "CodexCliTransport",
    "HttpPost",
    "OllamaTransport",
    "TransportError",
    "backend_from_env",
    "build_backend",
    "claude_cli_backend",
    "codex_backend",
    "ollama_gemma_backend",
]


class TransportError(RuntimeError):
    """A configured backend invocation failed. Raised so the proxy's bounded
    retry / fail-closed ``ProxyUnavailable`` path applies uniformly (no family
    fallback) — never swallowed into a silent or guessed response."""


# --------------------------------------------------------------------------- #
# Shared request flattening                                                    #
# --------------------------------------------------------------------------- #

# A CLI takes one prompt string, so the proxy's (system blocks + messages) get
# flattened. Pinned constraints already ride VERBATIM inside the SOP/messages
# the proxy was handed, so flattening preserves order; we never reorder.

_JSON_ONLY = (
    "You MUST respond with ONLY a single JSON value conforming to this JSON "
    "Schema — no prose, no explanation, no markdown fences:\n"
)


def _system_text(request: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in request.get("system", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "\n\n".join(p for p in parts if p)


def _message_pairs(request: dict[str, Any]) -> list[tuple[str, str]]:
    """(role, text) for each message; content may be a string or text blocks."""
    pairs: list[tuple[str, str]] = []
    for msg in request.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content
            )
        else:
            text = str(content)
        pairs.append((role, text))
    return pairs


def _schema_of(request: dict[str, Any]) -> dict[str, Any] | None:
    fmt = request.get("output_config", {})
    fmt = fmt.get("format", {}) if isinstance(fmt, dict) else {}
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        schema = fmt.get("schema")
        return schema if isinstance(schema, dict) else None
    return None


def _flatten_to_prompt(request: dict[str, Any]) -> str:
    """System + messages → one prompt. Appends a json-only instruction when the
    proxy attached a schema (the CLIs have no native grammar constraint)."""
    parts: list[str] = []
    sys_text = _system_text(request)
    if sys_text:
        parts.append(sys_text)
    for role, text in _message_pairs(request):
        if not text:
            continue
        parts.append(text if role == "user" else f"[{role}] {text}")
    schema = _schema_of(request)
    if schema is not None:
        parts.append(_JSON_ONLY + json.dumps(schema, sort_keys=True))
    return "\n\n".join(parts)


def _usage(
    *, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_creation: int = 0
) -> dict[str, int]:
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_input_tokens": int(cache_read),
        "cache_creation_input_tokens": int(cache_creation),
    }


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------- #
# CLI subprocess seam (injectable for tests — no real binary needed)           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CliResult:
    """A finished CLI invocation, observed."""

    returncode: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False


# A runner takes the argv + a wall-clock cap and returns what it observed. The
# default shells out; tests inject a deterministic fake (same discipline as the
# proxy's MockTransport — the whole stack is exercisable with no binary).
CliRunner = Callable[[Sequence[str], float | None], CliResult]


def _default_cli_runner(argv: Sequence[str], timeout_s: float | None) -> CliResult:
    import subprocess

    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return CliResult(124, _to_text(exc.stdout), _to_text(exc.stderr), timed_out=True)
    except FileNotFoundError as exc:
        raise TransportError(f"backend binary not found: {argv[0]!r}") from exc
    return CliResult(proc.returncode, proc.stdout, proc.stderr)


def _to_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _lenient_json_object(text: str) -> dict[str, Any]:
    """Parse stdout to a dict: strict first, else the outermost ``{...}`` span,
    else ``{}`` (mirrors the claude_code adapter's tolerance)."""
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# --------------------------------------------------------------------------- #
# Backend 1 — Claude Code CLI (`claude -p`)                                     #
# --------------------------------------------------------------------------- #


class ClaudeCliTransport:
    """``claude -p "<prompt>" --output-format json --model <id>``.

    The model is OBSERVED from the JSON envelope's ``modelUsage`` (the key
    whose usage dominates / matches the pinned id), not from the request — an
    empty observation fails closed in the LineageMap, never guessed.
    """

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        runner: CliRunner | None = None,
        timeout_s: float | None = None,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._bin = claude_bin
        self._run: CliRunner = runner or _default_cli_runner
        self._timeout_s = timeout_s
        self._extra_args = tuple(extra_args)

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        prompt = _flatten_to_prompt(request)
        argv = [self._bin, "-p", prompt, "--output-format", "json"]
        model = request.get("model")
        if isinstance(model, str) and model:
            argv += ["--model", model]
        argv += list(self._extra_args)

        result = self._run(argv, self._timeout_s)
        if result.timed_out:
            raise TransportError("claude CLI hard-killed at wall-clock cap")
        parsed = _lenient_json_object(result.stdout)
        if not parsed and result.returncode != 0:
            raise TransportError(
                f"claude CLI exit {result.returncode}: {result.stderr[:400] or '(no stderr)'}"
            )

        text = parsed.get("result")
        if not isinstance(text, str):
            text = result.stdout
        if parsed.get("is_error"):
            raise TransportError(f"claude CLI reported is_error: {str(text)[:400]}")

        served = _observe_claude_model(parsed, requested=model if isinstance(model, str) else None)
        u = _as_dict(parsed.get("usage"))
        session = parsed.get("session_id")
        request_id = (
            f"claude-cli:{session}"
            if isinstance(session, str) and session
            else f"claude-cli:{content_digest(request)[:16]}"
        )
        subtype = parsed.get("subtype")
        return ProviderResponse(
            served_model=served,
            request_id=request_id,
            text=text,
            usage=_usage(
                input_tokens=_as_int(u.get("input_tokens")),
                output_tokens=_as_int(u.get("output_tokens")),
                cache_read=_as_int(u.get("cache_read_input_tokens")),
                cache_creation=_as_int(u.get("cache_creation_input_tokens")),
            ),
            stop_reason="end_turn" if subtype in (None, "success") else str(subtype),
        )


def _observe_claude_model(parsed: dict[str, Any], *, requested: str | None) -> str:
    """Observe the served model from the ``claude -p --output-format json``
    envelope. The current shape is::

        {"type": "result", "subtype": "success", "result": "...",
         "session_id": "...", "total_cost_usd": ..., "usage": {...},
         "modelUsage": {"claude-...": {"inputTokens": ..., "outputTokens": ...}}}

    ``modelUsage`` is keyed by model id and is the primary observation: prefer a
    key matching the pinned request, else the one with the most tokens. Fall back
    to the top-level ``model`` (and, defensively, a ``model`` nested in the
    ``usage`` block) for envelopes that carry only ``total_cost_usd`` + ``usage``.

    An empty result ⇒ fail closed in the LineageMap — we never guess. The
    fallbacks only PROMOTE a model the backend actually reported; they do not
    invent one when the envelope is truly unobservable."""
    usage_by_model = parsed.get("modelUsage")
    if isinstance(usage_by_model, dict) and usage_by_model:
        keys = [k for k in usage_by_model if isinstance(k, str)]
        if requested:
            for k in keys:
                if k.startswith(requested) or requested.startswith(k):
                    return k

        def _tokens(k: str) -> int:
            v = usage_by_model.get(k)
            if not isinstance(v, dict):
                return 0
            return _as_int(v.get("inputTokens")) + _as_int(v.get("outputTokens")) + _as_int(
                v.get("input_tokens")
            ) + _as_int(v.get("output_tokens"))

        if keys:
            return max(keys, key=_tokens)
    model = parsed.get("model")
    if isinstance(model, str) and model:
        return model
    # Defensive fallback: some envelopes omit modelUsage AND top-level model but
    # carry a model id inside the usage block. Still an OBSERVATION, never a guess.
    nested = _as_dict(parsed.get("usage")).get("model")
    return nested if isinstance(nested, str) else ""


# --------------------------------------------------------------------------- #
# Backend 2 — Codex CLI (`codex exec`)                                          #
# --------------------------------------------------------------------------- #


class CodexCliTransport:
    """``codex exec --json [-m <model>] [-c model_reasoning_effort=<e>] "<prompt>"``.

    ALTITUDE CAVEAT: ``codex exec`` is an *agentic loop*, not a single-shot
    completion — it may take tool/command turns before emitting a final
    ``agent_message``. We observe only the LAST agent message as the turn text
    and the model/usage off the stream; the proxy treats this transport as a
    single completion at its own altitude (the intermediate agent steps are
    Codex-internal and not surfaced to the graph).

    Codex emits JSONL events; the model id, the final agent message, and token
    usage are observed from that stream (lenient — the exact event shapes are
    Codex-version-dependent and centralised in :func:`_extract_codex`)."""

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        runner: CliRunner | None = None,
        timeout_s: float | None = None,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._bin = codex_bin
        self._run: CliRunner = runner or _default_cli_runner
        self._timeout_s = timeout_s
        self._extra_args = tuple(extra_args)

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        prompt = _flatten_to_prompt(request)
        argv = [self._bin, "exec", "--json", "--skip-git-repo-check"]
        model = request.get("model")
        if isinstance(model, str) and model:
            argv += ["-m", model]
        effort = request.get("output_config", {})
        effort = effort.get("effort") if isinstance(effort, dict) else None
        if isinstance(effort, str) and effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += list(self._extra_args)
        argv.append(prompt)

        result = self._run(argv, self._timeout_s)
        if result.timed_out:
            raise TransportError("codex CLI hard-killed at wall-clock cap")
        served, text, usage = _extract_codex(result.stdout)
        if not text and result.returncode != 0:
            raise TransportError(
                f"codex CLI exit {result.returncode}: {result.stderr[:400] or '(no stderr)'}"
            )
        if not text:
            # Zero-exit but no agent_message recovered from the JSONL stream:
            # a parser/shape mismatch must NOT masquerade as a silent empty
            # completion (the proxy would admit an empty turn). Fail closed.
            raise TransportError(
                "codex CLI exit 0 but no agent_message text found in JSONL stream "
                f"(stdout {len(result.stdout)} bytes; event shape unrecognised)"
            )
        return ProviderResponse(
            served_model=served,
            request_id=f"codex-cli:{content_digest(request)[:16]}",
            text=text,
            usage=usage,
            stop_reason="end_turn",
        )


def _extract_codex(stdout: str) -> tuple[str, str, dict[str, int]]:
    """Walk Codex JSONL → (served_model, final_text, usage). Tolerant of the
    event-shape variants across Codex versions; the last agent message wins."""
    served = ""
    text = ""
    usage = _usage()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        served = _find_model(event) or served
        msg = _find_agent_message(event)
        if msg:
            text = msg
        u = _find_usage(event)
        if u is not None:
            usage = u
    return served, text, usage


def _find_model(event: dict[str, Any]) -> str:
    for key in ("model", "model_id"):
        v = event.get(key)
        if isinstance(v, str) and v:
            return v
    for sub in ("msg", "session", "config", "item"):
        v = event.get(sub)
        if isinstance(v, dict):
            found = _find_model(v)
            if found:
                return found
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_agent_message_type(etype: str) -> bool:
    return "agent_message" in etype or etype in ("message", "assistant")


def _agent_message_text(carrier: dict[str, Any]) -> str:
    for key in ("message", "text", "content"):
        v = carrier.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _find_agent_message(event: dict[str, Any]) -> str:
    """Final-agent-message text across Codex event shapes:

    * current ``codex exec --json``: the message is nested as
      ``{"type": "item.completed", "item": {"type": "agent_message", "text": ...}}``
      — the OUTER ``type`` is the lifecycle phase, the agent_message tag lives on
      the ``item``;
    * older streams: ``{"msg": {"type": "agent_message", "message": ...}}`` or a
      flat ``{"type": "message"|"assistant"|"agent_message", ...}``.

    We inspect the nested item tag first (so ``item.completed`` no longer misses),
    then fall back to the inner/outer envelope tag — keeping tolerance for the
    older shapes."""
    inner = _as_dict(event.get("msg")) or event
    item = _as_dict(inner.get("item"))
    # 1) current shape: the agent_message tag rides on the nested item.
    if item and _is_agent_message_type(str(item.get("type") or "")):
        text = _agent_message_text(item)
        if text:
            return text
    # 2) older/flat shapes: the tag rides on the inner (msg) or outer envelope.
    etype = str(inner.get("type") or event.get("type") or "")
    if _is_agent_message_type(etype):
        text = _agent_message_text(item) or _agent_message_text(inner)
        if text:
            return text
    return ""


def _find_usage(event: dict[str, Any]) -> dict[str, int] | None:
    # Usage rides at the top level (older shapes), on the ``msg`` envelope, or on
    # the nested ``item`` of a current ``turn.completed`` event.
    src: Any = event.get("usage")
    if not isinstance(src, dict):
        src = _as_dict(event.get("msg")).get("usage")
    if not isinstance(src, dict):
        src = _as_dict(event.get("item")).get("usage")
    if not isinstance(src, dict):
        return None
    return _usage(
        input_tokens=_as_int(src.get("input_tokens") or src.get("prompt_tokens")),
        output_tokens=_as_int(src.get("output_tokens") or src.get("completion_tokens")),
        cache_read=_as_int(src.get("cached_input_tokens") or src.get("cache_read_input_tokens")),
    )


# --------------------------------------------------------------------------- #
# Backend 3 — Ollama (local Gemma)                                              #
# --------------------------------------------------------------------------- #

# Injectable HTTP seam (url, json-payload) -> json-response; tests fake it.
HttpPost = Callable[[str, dict[str, Any]], dict[str, Any]]


def _default_http_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise TransportError(f"ollama request failed: {exc}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise TransportError("ollama returned a non-object response")
    return parsed


class OllamaTransport:
    """POST ``/api/chat`` to a local Ollama server (e.g. ``gemma3``).

    Unlike the CLIs, Ollama supports ``format`` (a JSON-schema constraint), so
    the grammar-constrained verdict channel is PRESERVED here. The system block
    and messages are sent as real chat roles (no flattening). ``temperature``
    pinned by the proxy (0 on deterministic gate/judge calls) rides in
    ``options``."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        http_post: HttpPost | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/api/chat"
        self._post: HttpPost = http_post or _default_http_post

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise TransportError("ollama transport requires a pinned model id on the request")

        messages: list[dict[str, str]] = []
        sys_text = _system_text(request)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for role, text in _message_pairs(request):
            messages.append({"role": role, "content": text})

        options: dict[str, Any] = {}
        if "temperature" in request:
            options["temperature"] = request["temperature"]
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if options:
            payload["options"] = options
        schema = _schema_of(request)
        if schema is not None:
            payload["format"] = schema  # native JSON-schema constraint

        resp = self._post(self._url, payload)
        message = resp.get("message")
        text = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(text, str):
            text = str(text)
        served = resp.get("model")
        done_reason = resp.get("done_reason")
        return ProviderResponse(
            served_model=served if isinstance(served, str) else "",
            request_id=f"ollama:{content_digest(request)[:16]}",
            text=text,
            usage=_usage(
                input_tokens=_as_int(resp.get("prompt_eval_count")),
                output_tokens=_as_int(resp.get("eval_count")),
            ),
            stop_reason=str(done_reason) if done_reason else "end_turn",
        )


# --------------------------------------------------------------------------- #
# Presets — wire (transport + tier_models + lineage) into the SAME proxy        #
# --------------------------------------------------------------------------- #

BackendName = Literal["anthropic", "claude-cli", "codex", "ollama-gemma"]

# Sensible default tier→model maps per backend. Override via build_backend(...,
# tier_models=...) to pin the exact ids you run.
CODEX_TIER_MODELS: dict[str, str] = {"T1": "gpt-5-codex", "T2": "gpt-5", "T3": "gpt-5-mini"}
GEMMA_TIER_MODELS: dict[str, str] = {"T1": "gemma3:27b", "T2": "gemma3:12b", "T3": "gemma3:4b"}


def claude_cli_backend(
    *,
    tier_models: dict[str, str] | None = None,
    runner: CliRunner | None = None,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by ``claude -p``. Same Anthropic tier ids + lineage as the
    SDK transport, so calibration baselines carry over."""
    return ModelProviderProxy(
        transport=ClaudeCliTransport(runner=runner),
        lineage_map=LineageMap(DEFAULT_LINEAGE_ENTRIES),
        tier_models=tier_models,  # None ⇒ proxy default TIER_MODELS
        **proxy_kwargs,
    )


def codex_backend(
    *,
    tier_models: dict[str, str] | None = None,
    runner: CliRunner | None = None,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by ``codex exec``. OpenAI lineage — a genuinely different
    served_family, so the ``decorrelated`` profile becomes meaningful."""
    return ModelProviderProxy(
        transport=CodexCliTransport(runner=runner),
        lineage_map=LineageMap((*OPENAI_LINEAGE_ENTRIES, *DEFAULT_LINEAGE_ENTRIES)),
        tier_models=tier_models or CODEX_TIER_MODELS,
        **proxy_kwargs,
    )


def ollama_gemma_backend(
    *,
    tier_models: dict[str, str] | None = None,
    base_url: str = "http://localhost:11434",
    http_post: HttpPost | None = None,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by a local Ollama Gemma server (offline-capable, keeps the
    JSON-schema verdict grammar via Ollama ``format``)."""
    return ModelProviderProxy(
        transport=OllamaTransport(base_url=base_url, http_post=http_post),
        lineage_map=LineageMap((*GEMMA_LINEAGE_ENTRIES, *DEFAULT_LINEAGE_ENTRIES)),
        tier_models=tier_models or GEMMA_TIER_MODELS,
        **proxy_kwargs,
    )


def build_backend(name: BackendName, **kwargs: Any) -> ModelProviderProxy:
    """Config entry point: name → fully-wired proxy (the only thing that
    changes between backends; the rest of the factory flow is identical).

    ``"anthropic"`` returns the default SDK-backed proxy.
    """
    if name == "anthropic":
        return ModelProviderProxy(**kwargs)
    if name == "claude-cli":
        return claude_cli_backend(**kwargs)
    if name == "codex":
        return codex_backend(**kwargs)
    if name == "ollama-gemma":
        return ollama_gemma_backend(**kwargs)
    raise ValueError(
        f"unknown backend {name!r} (expected anthropic | claude-cli | codex | ollama-gemma)"
    )


def backend_from_env(env: dict[str, str] | None = None, **kwargs: Any) -> ModelProviderProxy:
    """Read ``FOUNDRY_MODEL_BACKEND`` (default ``anthropic``) and build it.

    Lets an operator config-swap the backend without touching call sites:
    ``FOUNDRY_MODEL_BACKEND=ollama-gemma`` runs the exact same flow on Gemma.
    """
    import os

    source = env if env is not None else dict(os.environ)
    name = source.get("FOUNDRY_MODEL_BACKEND", "anthropic").strip() or "anthropic"
    return build_backend(name, **kwargs)  # type: ignore[arg-type]
