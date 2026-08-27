"""Claude Code CLI backend for the model-provider proxy. (arch §2b)

The factory runs ONE flow. The only thing a backend swaps is the proxy's
egress seam (:class:`~foundry.proxy.model_provider_proxy.ProviderTransport`)
together with the tier→model map and the lineage map it resolves against —
nothing in the graph, agents, gates, or admission changes. This module adds
the single shipped transport:

* :class:`ClaudeCliTransport`  — shells ``claude -p "<prompt>" --output-format json``;

and its preset (:func:`claude_cli_backend`) plus :func:`build_backend` that
returns a fully-wired :class:`ModelProviderProxy` — same proxy class, same
contract.

TRUST NOTES (honest, per arch §2b / plan §0.3) — what stays and what changes
when the backend is the ``claude`` CLI rather than a direct SDK:

* *Observed-not-trusted still holds for lineage.* The transport reports the
  model the backend SAYS it served (CLI JSON ``modelUsage`` key), which the
  proxy resolves through the curated :class:`LineageMap` and drift-checks
  against the pinned tier id. It is the best wire-level observation a CLI
  exposes; it is never the adapter self-report, and an unknown id still fails
  closed.
* *The grammar-constrained verdict channel weakens to fail-closed.* A direct
  SDK could enforce the verdict JSON schema as a decoding grammar. The CLI does
  not, so for schema calls this transport INSTRUCTS json-only output and the
  proxy still parses + ``ZoneBSigner``-stamps the result; a non-conforming reply
  raises :class:`VerdictChannelError` (no PR, no signed verdict) rather than
  being mis-admitted. The safety property (no forged authentic verdict) is
  preserved; only availability degrades.
* *The credential locus moves.* The secret lives in the ``claude`` CLI's own
  auth (env/keychain). The proxy remains the sole *egress path* (all calls
  route through it), but it is no longer the sole secret holder — record this.
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
    "TransportError",
    "build_backend",
    "claude_cli_backend",
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
    proxy attached a schema (the CLI has no native grammar constraint).

    ``include_system=False`` omits the system SOP — used when the caller routes it
    through the CLI's ``--append-system-prompt`` (so it can be prompt-cached)."""
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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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
# `claude` invocation blocks its worker thread forever (the transport accepts
# timeout_s=None, but the shipped backend never sets it). Override via
# claude_cli_backend.
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
# Backend — Claude Code CLI (`claude -p`)                                       #
# --------------------------------------------------------------------------- #

# Prompt caching (routing the stable SOP through `--append-system-prompt`) is the
# primary token-cost lever and is ALWAYS ON — it is not a setting. The only thing
# that could make it fail is a `claude` too old to know the flag, so we make a new
# enough binary a REQUIREMENT instead of degrading silently: probe the version once
# per process and fail closed with an upgrade instruction if it's below the floor.
# `--append-system-prompt` has shipped since long before this floor; the floor's job
# is only to reject a genuinely ancient pre-1.0 binary. Raise it if you pin higher.
_MIN_CLAUDE_VERSION = (1, 0, 0)
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

    def _cache_supported(self) -> bool:
        """Probe (once per process, per binary) that `claude` is new enough to
        accept `--append-system-prompt`. A parseable version BELOW the floor is a
        hard error that tells the operator exactly how to fix it; an UNPARSEABLE
        `--version` is treated as modern so a probe quirk never bricks a run."""
        cached = _CLAUDE_CAP_CACHE.get(self._bin)
        if cached is not None:
            return cached
        result = self._run([self._bin, "--version"], min(self._timeout_s or 30.0, 30.0))
        version = _parse_claude_version(result.stdout)
        if version is not None and version < _MIN_CLAUDE_VERSION:
            floor = ".".join(map(str, _MIN_CLAUDE_VERSION))
            got = ".".join(map(str, version))
            raise TransportError(
                f"claude {got} is too old: prompt caching needs --append-system-prompt "
                f"(>= {floor}). Upgrade with `npm i -g @anthropic-ai/claude-code`."
            )
        _CLAUDE_CAP_CACHE[self._bin] = True
        return True

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        # Token-cost lever (ALWAYS ON, not a setting): the stable per-role SOP is the
        # big prefix that repeats across many calls (every reviewer call shares the
        # reviewer SOP; every fix-loop retry re-sends it). Flattened into the `-p`
        # user string it is a fresh, uncacheable message each call. Routed through the
        # CLI's `--append-system-prompt` instead, Claude Code marks it cacheable, so
        # those byte-identical prefixes hit Anthropic's prompt cache across invocations
        # (5-min TTL) — verify with the manifest's cache-read tokens. A `claude` too
        # old for the flag is rejected up front by `_cache_supported` (probe once), so
        # this never degrades silently. The probe only runs when there's a SOP to cache.
        sys_text = _system_text(request)
        cache_system = bool(sys_text) and self._cache_supported()
        if cache_system:
            prompt = _flatten_to_prompt(request, include_system=False)
        else:
            prompt = _flatten_to_prompt(request)
        argv = [self._bin, "-p", prompt, "--output-format", "json"]
        if cache_system:
            argv += ["--append-system-prompt", sys_text]
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
# Preset — wire (transport + tier_models + lineage) into the SAME proxy         #
# --------------------------------------------------------------------------- #

BackendName = Literal["claude-cli"]


def claude_cli_backend(
    *,
    tier_models: dict[str, str] | None = None,
    runner: CliRunner | None = None,
    timeout_s: float | None = _DEFAULT_CLI_TIMEOUT_S,
    **proxy_kwargs: Any,
) -> ModelProviderProxy:
    """Proxy backed by ``claude -p``. Same Anthropic tier ids + lineage as a
    direct SDK transport, so calibration baselines carry over. ``timeout_s``
    hard-caps each subprocess (default 300s) so a hung CLI can't wedge the
    worker forever."""
    return ModelProviderProxy(
        transport=ClaudeCliTransport(runner=runner, timeout_s=timeout_s),
        lineage_map=LineageMap(DEFAULT_LINEAGE_ENTRIES),
        tier_models=tier_models,  # None ⇒ proxy default TIER_MODELS
        **proxy_kwargs,
    )


def build_backend(name: BackendName, **kwargs: Any) -> ModelProviderProxy:
    """Config entry point: name → fully-wired proxy (the only thing that
    changes between backends; the rest of the factory flow is identical).

    ``"claude-cli"`` is the sole real backend — ``claude -p``.
    """
    if name == "claude-cli":
        return claude_cli_backend(**kwargs)
    raise ValueError(f"unknown backend {name!r} (expected claude-cli)")
