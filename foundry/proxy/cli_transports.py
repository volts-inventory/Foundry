"""Alternative configured backends for the model-provider proxy. (arch §2b)

The factory runs ONE flow. The only thing a backend swaps is the proxy's
egress seam (:class:`~foundry.proxy.model_provider_proxy.ProviderTransport`)
together with the tier→model map and the lineage map it resolves against —
nothing in the graph, agents, gates, or admission changes. This module adds
three drop-in transports beside the default Anthropic SDK transport:

* :class:`OpenAITransport`     — calls the OpenAI SDK ``chat.completions`` directly
  (the first-party OpenAI API, the direct-API twin of the Anthropic SDK transport);
* :class:`ClaudeCliTransport`  — shells ``claude -p "<prompt>" --output-format json``;
* :class:`CodexCliTransport`   — shells ``codex exec --json -m <model> "<prompt>"``.

and three presets (:func:`openai_backend`, :func:`claude_cli_backend`,
:func:`codex_backend`) plus :func:`build_backend` that return a fully-wired
:class:`ModelProviderProxy` —
same proxy class, same contract, different backend.

TRUST NOTES (honest, per arch §2b / plan §0.3) — what stays and what changes
when the backend is not the Anthropic SDK:

* *Observed-not-trusted still holds for lineage.* Each transport reports the
  model the backend SAYS it served (CLI JSON ``modelUsage`` key / Codex session
  model / OpenAI response ``model``), which the proxy resolves through the curated
  :class:`LineageMap` and drift-checks against the pinned tier id. It is the
  best wire-level observation a CLI exposes; it is never the
  adapter self-report, and an unknown id still fails closed.
* *The grammar-constrained verdict channel weakens to fail-closed.* The
  Anthropic SDK enforces the verdict JSON schema as a decoding grammar. The CLIs
  do not, so for schema calls these transports INSTRUCT json-only output and the
  proxy still parses + ``ZoneBSigner``-stamps the result; a non-conforming reply
  raises :class:`VerdictChannelError` (no PR, no signed verdict) rather than
  being mis-admitted. OpenAI honours ``response_format=json_schema``, so it keeps
  the grammar. The safety property (no forged authentic verdict) is preserved on
  all three; only availability degrades on the CLIs.
* *The credential locus moves.* With the SDK the proxy is the sole secret
  holder. With ``claude``/``codex`` the secret lives in the CLI's own auth
  (env/keychain). The proxy remains the sole
  *egress path* (all calls route through it), but it is no longer the sole
  secret holder — record this when you pick a backend.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from foundry.canonical import content_digest
from foundry.proxy.lineage import (
    DEFAULT_LINEAGE_ENTRIES,
    OPENAI_LINEAGE_ENTRIES,
    LineageMap,
)
from foundry.proxy.model_provider_proxy import (
    ModelProviderProxy,
    ProviderResponse,
    _ClientCache,
)

__all__ = [
    "BackendName",
    "ClaudeCliTransport",
    "CliResult",
    "CliRunner",
    "CodexCliTransport",
    "OpenAITransport",
    "TransportError",
    "build_backend",
    "claude_cli_backend",
    "codex_backend",
    "openai_backend",
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


def _flatten_to_prompt(request: dict[str, Any], *, include_system: bool = True) -> str:
    """System + messages → one prompt. Appends a json-only instruction when the
    proxy attached a schema (the CLIs have no native grammar constraint).

    ``include_system=False`` omits the system SOP — used when the caller routes it
    through the CLI's ``--system-prompt`` flag instead."""
    parts: list[str] = []
    sys_text = _system_text(request) if include_system else ""
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


# Default wall-clock cap for a CLI backend subprocess. Without it a hung
# `claude`/`codex` invocation blocks its worker thread forever (the transports
# accept timeout_s=None, but the shipped backends never set it). Override via
# claude_cli_backend/codex_backend.
_DEFAULT_CLI_TIMEOUT_S = 300.0


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
    else ``{}`` (tolerant of CLI chatter around the JSON object)."""
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

# Persona calls are one-shot text completions, so each launch REPLACES Claude
# Code's default system prompt and drops its tools, settings, MCP servers and
# skills. Left at defaults, every call re-sent ~40k tokens of agent scaffolding
# for a ~1k-token answer (measured: 39.8k vs 1.3k input tokens on a trivial call).
# The flags need a modern `claude`, so a binary below the floor fails closed with
# an upgrade instruction instead of silently running the expensive default.
_MIN_CLAUDE_VERSION = (2, 0, 0)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# Process-global capability cache, keyed by binary path — one `--version` probe per
# distinct `claude` binary, not one per checkpoint (a fresh transport is built per
# proxy, and a proxy is built per stage). Reset in tests via `reset_claude_cap_cache`.
_CLAUDE_CAP_CACHE: dict[str, bool] = {}


def reset_claude_cap_cache() -> None:
    """Clear the per-process `claude --version` capability cache (test seam)."""
    _CLAUDE_CAP_CACHE.clear()


def _parse_claude_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


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

    def _check_version(self) -> None:
        """Probe (once per process, per binary) that `claude` is new enough for
        the one-shot flags. A parseable version BELOW the floor is a
        hard error that tells the operator exactly how to fix it; an UNPARSEABLE
        `--version` is treated as modern so a probe quirk never bricks a run."""
        if _CLAUDE_CAP_CACHE.get(self._bin):
            return
        result = self._run([self._bin, "--version"], min(self._timeout_s or 30.0, 30.0))
        version = _parse_claude_version(result.stdout)
        if version is not None and version < _MIN_CLAUDE_VERSION:
            floor = ".".join(map(str, _MIN_CLAUDE_VERSION))
            got = ".".join(map(str, version))
            raise TransportError(
                f"claude {got} is too old: one-shot persona calls need --tools/--system-prompt "
                f"(>= {floor}). Upgrade with `npm i -g @anthropic-ai/claude-code`."
            )
        _CLAUDE_CAP_CACHE[self._bin] = True

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self._check_version()
        prompt = _flatten_to_prompt(request, include_system=False)
        argv = [
            self._bin, "-p", prompt, "--output-format", "json",
            # An empty --system-prompt would fall back to the default agent prompt.
            "--system-prompt", _system_text(request) or "Answer the user's request.",
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
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
# Backend 3 — OpenAI (direct SDK, `chat.completions`)                           #
# --------------------------------------------------------------------------- #


class OpenAITransport:
    """Default transport over the real OpenAI SDK (``chat.completions``).

    The first-party OpenAI egress — the direct-API twin of
    :class:`~foundry.proxy.model_provider_proxy.AnthropicTransport`. (The ``codex``
    backend also reaches OpenAI, but through the agentic ``codex exec`` CLI; this
    one calls the API directly, a single-shot completion.) The client and the
    credential it resolves are built lazily on first use, so importing this module
    never needs a key or network — tests inject a fake ``client``.

    OpenAI honours ``response_format=json_schema`` (a real decoding grammar), so the
    grammar-constrained verdict channel is PRESERVED here (unlike the CLIs). The
    system block + messages are sent as real chat roles; the proxy-pinned
    ``temperature`` (deterministic gate/judge calls) and reasoning ``effort`` ride
    through unchanged.
    """

    def __init__(self, client: Any | None = None) -> None:
        def _build() -> Any:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - import guard
                raise TransportError(
                    "the 'openai' backend requires the openai SDK; install it "
                    "(e.g. `uv add openai` / `pip install openai`)"
                ) from exc
            return openai.OpenAI()
        self._cache = _ClientCache(_build, injected=client)

    def _ensure_client(self) -> Any:
        return self._cache.get()

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise TransportError("openai transport requires a pinned model id on the request")

        messages: list[dict[str, str]] = []
        sys_text = _system_text(request)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for role, text in _message_pairs(request):
            messages.append({"role": role, "content": text})

        params: dict[str, Any] = {"model": model, "messages": messages}
        max_tokens = request.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            params["max_completion_tokens"] = max_tokens  # reasoning-model-safe form
        if "temperature" in request:
            params["temperature"] = request["temperature"]
        effort = _as_dict(request.get("output_config", {})).get("effort")
        if isinstance(effort, str) and effort:
            params["reasoning_effort"] = effort
        schema = _schema_of(request)
        if schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": schema, "strict": True},
            }

        response = self._cache.run(lambda c: c.chat.completions.create(**params))
        choice = response.choices[0] if getattr(response, "choices", None) else None
        text = (getattr(choice.message, "content", "") or "") if choice is not None else ""
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        return ProviderResponse(
            served_model=str(getattr(response, "model", "") or ""),
            request_id=str(getattr(response, "id", "") or ""),
            text=text if isinstance(text, str) else str(text),
            usage=_usage(
                input_tokens=_as_int(getattr(usage, "prompt_tokens", 0)) if usage else 0,
                output_tokens=_as_int(getattr(usage, "completion_tokens", 0)) if usage else 0,
                cache_read=_as_int(getattr(details, "cached_tokens", 0)) if details else 0,
            ),
            stop_reason=str(getattr(choice, "finish_reason", "") or "stop") if choice else "stop",
        )


# --------------------------------------------------------------------------- #
# Presets — wire (transport + tier_models + lineage) into the SAME proxy        #
# --------------------------------------------------------------------------- #

BackendName = Literal["anthropic", "openai", "claude-cli", "codex"]

# Sensible default tier→model maps per backend. Override via build_backend(...,
# tier_models=...) to pin the exact ids you run.
OPENAI_TIER_MODELS: dict[str, str] = {"T1": "gpt-5", "T2": "gpt-5", "T3": "gpt-5-mini"}
CODEX_TIER_MODELS: dict[str, str] = {"T1": "gpt-5-codex", "T2": "gpt-5", "T3": "gpt-5-mini"}


def claude_cli_backend(
    *,
    tier_models: dict[str, str] | None = None,
    runner: CliRunner | None = None,
    timeout_s: float | None = _DEFAULT_CLI_TIMEOUT_S,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by ``claude -p``. Same Anthropic tier ids + lineage as the
    SDK transport, so calibration baselines carry over. ``timeout_s`` hard-caps each
    subprocess (default 300s) so a hung CLI can't wedge the worker forever."""
    return ModelProviderProxy(
        transport=ClaudeCliTransport(runner=runner, timeout_s=timeout_s),
        lineage_map=LineageMap(DEFAULT_LINEAGE_ENTRIES),
        tier_models=tier_models,  # None ⇒ proxy default TIER_MODELS
        **proxy_kwargs,
    )


def openai_backend(
    *,
    tier_models: dict[str, str] | None = None,
    client: Any | None = None,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by the OpenAI SDK (``chat.completions``). OpenAI lineage — a
    genuinely different ``served_family`` from Anthropic. The direct-API twin of
    the default Anthropic SDK backend; ``client`` is injectable for tests (else
    built lazily from the env)."""
    return ModelProviderProxy(
        transport=OpenAITransport(client=client),
        lineage_map=LineageMap((*OPENAI_LINEAGE_ENTRIES, *DEFAULT_LINEAGE_ENTRIES)),
        tier_models=tier_models or OPENAI_TIER_MODELS,
        **proxy_kwargs,
    )


def codex_backend(
    *,
    tier_models: dict[str, str] | None = None,
    runner: CliRunner | None = None,
    timeout_s: float | None = _DEFAULT_CLI_TIMEOUT_S,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by ``codex exec``. OpenAI lineage — a genuinely different
    served_family. ``timeout_s`` hard-caps each subprocess (default 300s) so a
    hung CLI can't wedge the worker."""
    return ModelProviderProxy(
        transport=CodexCliTransport(runner=runner, timeout_s=timeout_s),
        lineage_map=LineageMap((*OPENAI_LINEAGE_ENTRIES, *DEFAULT_LINEAGE_ENTRIES)),
        tier_models=tier_models or CODEX_TIER_MODELS,
        **proxy_kwargs,
    )


def build_backend(name: BackendName, **kwargs: Any) -> ModelProviderProxy:
    """Config entry point: name → fully-wired proxy (the only thing that
    changes between backends; the rest of the factory flow is identical).

    ``"anthropic"`` returns the default SDK-backed proxy.
    """
    if name == "anthropic":
        return ModelProviderProxy(**kwargs)
    if name == "openai":
        return openai_backend(**kwargs)
    if name == "claude-cli":
        return claude_cli_backend(**kwargs)
    if name == "codex":
        return codex_backend(**kwargs)
    raise ValueError(
        f"unknown backend {name!r} "
        "(expected anthropic | openai | claude-cli | codex)"
    )

