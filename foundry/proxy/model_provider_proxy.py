"""The mediating model-provider proxy. (architecture.md §2b, plan §3.3)

Single egress + single credential holder. Every model call in the factory
goes through :class:`ModelProviderProxy`, which:

- observes the ACTUALLY-served model from the provider response
  (``response.model`` + ``request_id`` — there is no system_fingerprint
  field on this API; those two are the recorded observation) and resolves
  it through the curated :class:`~foundry.proxy.lineage.LineageMap` —
  adapter self-report is never consulted;
- owns trusted verdict-extraction for judgment calls: the verdict ``status``
  is parsed from the grammar-constrained structured-output channel
  (``output_config.format`` json_schema), never from adapter free-form text;
- stamps the judgment-verdict binding OUTSIDE the implementing adapter via an
  injected :class:`~foundry.gates.attestation.ZoneBSigner`, including the
  ``judgment_provenance`` stamp (SELF in the self-judge profile, plan §0.2);
- appends a per-call manifest record (requested vs served model, lineage,
  request id, usage, sampling config, cache reads) and raises a
  **pinned-ID-drift** alert when the served model id departs from the
  pinned request (arch §8);
- meters tokens on every call (usage is returned to the caller for the
  budget ledger).

Determinism policy (arch §8) vs current API surface: "temperature 0 for
gates/judges" pins a determinism control, but WHICH control is backend/model-
aware (:class:`SamplingPolicy`). Reasoning / no-sampling models (the Anthropic
T1 reasoning tier; any OpenAI/Codex id) REMOVE sampling params — sending a
temperature is a 400 (and a CLI transport that silently drops it would leave
the call with no determinism control at all) — so the proxy pins
``output_config={"effort": "high"}`` instead and records the substitution in
the manifest. Models that honour sampling (Anthropic Sonnet/Haiku, Ollama/
Gemma) get ``temperature 0``. Thinking is enabled (adaptive) only for
ideation-class reasoning-model calls and OMITTED for deterministic gate/judge
calls.

Degraded-mode rule (fail-closed): on transport failure the proxy retries
with bounded backoff and then raises :class:`ProxyUnavailable`. It NEVER
silently falls back to a different model family — judgment nodes catching
``ProxyUnavailable`` pause-and-escalate (same discipline as the
no-auto-downgrade rule, arch §2b/§8).

Pinned-ID-drift policy (arch §8, ops §5): a served model id that departs
from the pinned request invalidates calibration baselines. The proxy records
EVERY drift on the manifest + ``drift_events`` regardless of policy. What
DIFFERS by policy is whether a drift on a high-risk call ALSO fails closed:

- :attr:`DriftMode.ADVISORY` (DEFAULT — preserves prior behaviour): record +
  alert, never raise. Ideation stays advisory; a recalibration flag rides the
  scorecard and the run continues (operator chooses continue/fork/abort
  out-of-band, ops §5).
- :attr:`DriftMode.FAIL_CLOSED`: on a gate/judgment drift the proxy raises
  :class:`PinnedIdDriftError` (mirroring :class:`ProxyUnavailable`) so a
  judgment node pause-and-escalates instead of trusting a drifted model — the
  same no-silent-downgrade discipline applied to weights, not just transport.

Tier-mismatch is drift even WITHIN a lineage family: when the served model
resolves to the same family as the pin but is not the pinned tier id (e.g. a
smaller Ollama tag ``gemma3:12b`` served against a ``gemma3:27b`` pin) the
served capability silently degraded. That is flagged as drift (kind
``tier-mismatch``) even though family resolution would otherwise pass it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any, Literal, Protocol

from foundry.gates.attestation import ZoneBSigner
from foundry.proxy.lineage import LineageMap
from foundry.schemas.gates import GateResult, GateStatus, JudgmentProvenance, VerdictClass
from foundry.state.seq import Seq

# Tier map (plan §4): capability levels WITHIN the single Anthropic lineage —
# a cost choice, not a decorrelation choice (plan §0.2). Pinned ids, exact.
TIER_MODELS: dict[str, str] = {
    "T1": "claude-opus-4-8",
    "T2": "claude-sonnet-4-6",
    "T3": "claude-haiku-4-5",
}

# ---- Determinism policy: effort vs temperature, per backend/model (arch §8) ----
#
# arch §8 pins determinism for gates/judges (temperature 0). But the *control*
# that pins it is backend/model-specific: some models REMOVE sampling params
# (400 if a temperature is sent) and instead take a reasoning-`effort` knob.
# Pinning the WRONG control means either a rejected request or — worse, on a CLI
# transport that silently drops an unsupported field — NO determinism control at
# all. So the choice is a property of the model id, not a hardcoded Anthropic id.
#
# A model id matching any of these globs takes a pinned `effort` (and NEVER a
# temperature) on deterministic gate/judge calls:
#   - reasoning / OpenAI lineage (gpt-*, o1/o3/o4*, codex-*, *-codex) — reject
#     temperature; the Codex CLI forwards `model_reasoning_effort` instead;
#   - the Anthropic T1 reasoning model (claude-opus-*) — sampling params are
#     removed (existing behaviour, kept).
# Everything else (Anthropic Sonnet/Haiku, Ollama/Gemma) honours `temperature`,
# so a deterministic call pins temperature 0 there.
_DEFAULT_EFFORT_MODEL_GLOBS: tuple[str, ...] = (
    "claude-opus-*",
    "gpt-*",
    "o1*",
    "o3*",
    "o4*",
    "codex-*",
    "*-codex",
    "*-codex-*",
)

_DEFAULT_EFFORT = "high"

_DEFAULT_MAX_TOKENS = 16000

Purpose = Literal["ideation", "gate", "judgment"]
_PURPOSES = ("ideation", "gate", "judgment")

# The grammar-constrained verdict channel (arch §2b): the proxy extracts
# `status` from THIS schema's output, independent of adapter post-processing.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["approve", "request-changes"]},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "findings"],
    "additionalProperties": False,
}

_STATUS_BY_VERDICT = {
    "approve": GateStatus.PASS,
    "request-changes": GateStatus.FAIL,
}

# Optional observer for rate-limit waits (advisory, fail-soft): a host app (the
# Workshop) sets this to surface "usage limit hit — resuming ~HH:MM" in its UI.
# Called as on_rate_limit_wait(wait_seconds, resume_epoch, explicit) from the
# retrying thread; exceptions are swallowed — telemetry never breaks a build.
on_rate_limit_wait: Callable[[float, float, bool], None] | None = None


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """Backend/model-aware determinism control selector (arch §8).

    For a deterministic gate/judge call the proxy must pin *some* determinism
    control. Which one is a property of the served model id: a reasoning /
    no-sampling model takes a pinned ``effort`` (and rejects ``temperature``);
    every other model honours ``temperature`` and gets temperature 0.

    Injectable so an operator can pin the exact policy for the backend they run;
    the default (:func:`default_sampling_policy`) covers the curated backends
    (Anthropic / OpenAI-Codex / Ollama-Gemma).
    """

    effort_model_globs: tuple[str, ...] = _DEFAULT_EFFORT_MODEL_GLOBS
    effort: str = _DEFAULT_EFFORT

    def takes_effort(self, model_id: str) -> bool:
        """True if this model takes a pinned ``effort`` instead of a temperature
        on deterministic calls (reasoning / no-sampling models)."""
        return any(fnmatchcase(model_id, glob) for glob in self.effort_model_globs)


def default_sampling_policy() -> SamplingPolicy:
    """The curated default policy (Anthropic opus + OpenAI/Codex => effort;
    Anthropic sonnet/haiku + Ollama/Gemma => temperature 0)."""
    return SamplingPolicy()


# ---- Pinned-ID-drift policy (arch §8, ops §5) ---------------------------------
#
# Drift is ALWAYS recorded (manifest + drift_events) — the policy only decides
# whether a drift on a HIGH-RISK call additionally fails closed. Default is
# ADVISORY (the prior behaviour: record + alert, never crash); FAIL_CLOSED makes
# a gate/judgment drift raise so a judgment node pause-and-escalates rather than
# trusting a drifted (recalibration-invalidating) model.


class DriftMode(StrEnum):
    """How the proxy reacts to a recorded pinned-ID drift."""

    ADVISORY = "advisory"  # record + alert, never raise (DEFAULT)
    FAIL_CLOSED = "fail-closed"  # raise PinnedIdDriftError on a high-risk drift


# The high-risk purposes a fail-closed policy guards. Ideation is exploratory and
# may stay advisory even under FAIL_CLOSED; gate/judgment feed trusted verdicts.
_DEFAULT_FAIL_CLOSED_PURPOSES: frozenset[Purpose] = frozenset({"gate", "judgment"})


@dataclass(frozen=True, slots=True)
class DriftPolicy:
    """Injectable pinned-ID-drift reaction policy (arch §8, ops §5).

    ``mode`` defaults to :attr:`DriftMode.ADVISORY` — drift is recorded and
    surfaced as an alert but never raises, preserving prior behaviour. Under
    :attr:`DriftMode.FAIL_CLOSED` a drift whose ``purpose`` is in
    ``fail_closed_purposes`` (gate/judgment by default) raises
    :class:`PinnedIdDriftError`, so the no-silent-downgrade discipline that
    already covers transport unavailability also covers a drifted model.
    """

    mode: DriftMode = DriftMode.ADVISORY
    fail_closed_purposes: frozenset[Purpose] = _DEFAULT_FAIL_CLOSED_PURPOSES

    def should_raise(self, purpose: Purpose) -> bool:
        """True iff a recorded drift on this purpose must fail closed."""
        return self.mode is DriftMode.FAIL_CLOSED and purpose in self.fail_closed_purposes


def default_drift_policy() -> DriftPolicy:
    """The default ADVISORY policy (record + alert, never raise)."""
    return DriftPolicy()


class ProxyUnavailable(RuntimeError):
    """Transport exhausted retries. Judgment nodes pause-and-escalate —
    there is no silent fallback to another model or family (fail-closed)."""


class PinnedIdDriftError(RuntimeError):
    """A served model drifted from the pinned id on a high-risk (gate/judgment)
    call under a FAIL_CLOSED :class:`DriftPolicy`.

    Carries the recorded drift event so nothing is swallowed; judgment nodes
    catch it and pause-and-escalate (same discipline as :class:`ProxyUnavailable`,
    arch §2b/§8) rather than trusting weights whose calibration baseline is now
    invalid (arch §8 / ops §5)."""

    def __init__(self, event: Mapping[str, Any]) -> None:
        self.event: dict[str, Any] = dict(event)
        super().__init__(
            f"pinned-ID drift on {event.get('purpose')!r} call node "
            f"{event.get('node_id')!r}: pinned {event.get('requested_model')!r} but "
            f"served {event.get('served_model')!r} (kind={event.get('kind')!r}); "
            "calibration baselines are invalidated (arch §8) — pause-and-escalate, "
            "do not trust the drifted model (fail-closed drift policy)"
        )


class FamilyOverlapError(RuntimeError):
    """reviewer ∩ implementer served_family is non-empty (decorrelated profile)."""


class VerdictChannelError(RuntimeError):
    """The structured verdict channel returned something outside the grammar."""


class StructuredOutputError(RuntimeError):
    """A schema (structured-output) completion didn't yield a parseable JSON
    object. SDK structured-outputs are grammar-constrained, but the CLI backends
    (claude -p / codex) only carry a json-only PROMPT instruction, so a model may
    return prose, a ```json fence, or empty text. Raised inside the send-retry
    loop so a transient miss is RE-ROLLED, then escalates to ProxyUnavailable —
    never an opaque JSONDecodeError mid-graph (fail-closed)."""


def _extract_json_object(text: str) -> dict[str, Any]:
    """Recover the JSON object from a completion REQUESTED to be schema-conforming.

    Strict parse first (the SDK path and a well-behaved CLI response land here),
    else the outermost ``{...}`` span (strips a prose preamble or a ```json fence
    the CLI model may add). Raises :class:`StructuredOutputError` when nothing
    parseable is present (empty / non-object), so the caller fails closed rather
    than crashing — and, inside the retry loop, re-rolls the call first."""
    s = (text or "").strip()
    if s:
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(s[start : end + 1])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    raise StructuredOutputError(
        f"schema completion returned no parseable JSON object ({len(s)} chars)"
    )


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The proxy-observed provider response (the trusted observation)."""

    served_model: str
    request_id: str
    text: str
    usage: dict[str, int]
    stop_reason: str


class ProviderTransport(Protocol):
    """Minimal egress seam: one credentialed request out, one observation in."""

    def send(self, request: dict[str, Any]) -> ProviderResponse: ...


class AnthropicTransport:
    """Default transport over the real Anthropic SDK.

    The client (and the credential it resolves) is constructed lazily on
    first use — importing/instantiating this module never requires network
    or an API key, so tests run entirely on a fake transport.
    """

    def __init__(self, client: Any | None = None) -> None:
        def _build() -> Any:
            import anthropic
            return anthropic.Anthropic()
        self._cache = _ClientCache(_build, injected=client)

    def _ensure_client(self) -> Any:
        return self._cache.get()

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        response = self._cache.run(lambda c: c.messages.create(**request))
        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        usage = response.usage
        return ProviderResponse(
            served_model=response.model,
            request_id=response._request_id or "",
            text=text,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            },
            stop_reason=response.stop_reason or "",
        )


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """What a proxy call hands back: text/JSON + the trusted observations."""

    text: str
    parsed: dict[str, Any] | None
    served_model: str
    served_family: frozenset[str]  # resolved by the proxy, never self-reported
    request_id: str
    usage: dict[str, int]  # surfaced on EVERY call for the budget ledger
    stop_reason: str
    manifest_record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """A proxy-extracted, proxy-stamped judgment verdict."""

    gate_result: GateResult  # carries the judgment_binding + provenance stamp
    completion: CompletionResult


@dataclass
class _RetryPolicy:
    max_retries: int = 2
    backoff_base_s: float = 0.5
    sleeper: Callable[[float], None] = time.sleep
    delays: list[float] = field(default_factory=list)

    def sleep_before_retry(self, attempt: int) -> None:
        delay = self.backoff_base_s * (2**attempt)
        self.delays.append(delay)
        self.sleeper(delay)


# Words/codes in a transport error that mean "the PROVIDER threw us back" — a
# throttle (HTTP 429, Anthropic 529 overloaded, quota), a subscription-window
# limit, OR a transient stall (a timeout / wall-clock hard-kill). None of these
# is a failure of the pinned model: they're TRANSIENT and must be waited out on a
# SEPARATE, more generous (but still bounded) budget than a real transport failure
# — so one hiccup can't kill a multi-hour overnight run.
_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "429", "529", "overloaded", "quota exceeded", "throttl",
    # Subscription-window exhaustion (Claude Max via the claude CLI): the CLI
    # reports "Claude AI usage limit reached|<reset-epoch>", but ALSO — and this
    # is the case that silently killed overnight runs — often just STALLS when the
    # window is spent, so the subprocess is hard-killed at the wall-clock cap. A
    # bare timeout/hard-kill is transient by nature (a wedged prompt still fails
    # closed after the bounded budget), so it rides the generous budget too.
    "usage limit", "limit reached", "limit will reset",
    # Claude Max via the claude CLI also reports a spent 5-hour window as
    # "You've hit your session limit · resets 8am (UTC)" — a DIFFERENT phrase
    # from "usage limit", and the one that hard-crashed the overnight PIR run
    # (it wasn't classified as a throttle, so it burned the 3-attempt transport
    # budget and raised). It is the same transient subscription-window
    # exhaustion, so it rides the generous rate-limit budget too.
    "session limit",
    "hard-killed at wall-clock cap", "timed out", "timeout",
)


def _int_env(name: str, default: int) -> int:
    """A non-negative int from the environment (blank/garbage -> default)."""
    try:
        return max(0, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# Markers that mean "the credential was rejected" (vs a transient network blip). On
# one of these the SDK client is dropped so the NEXT call rebuilds it — re-resolving
# the credential, so a rotated key / refreshed token is picked up without a restart.
_AUTH_ERROR_MARKERS = (
    "unauthorized", "authentication", "invalid api key", "invalid x-api-key",
    "expired", "credential", "401",
)


def _is_auth_error(exc: BaseException) -> bool:
    """True when a transport error looks like a rejected/expired credential."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 401:
        return True
    if "authentication" in type(exc).__name__.lower():
        return True
    return any(m in str(exc).lower() for m in _AUTH_ERROR_MARKERS)


class _ClientCache:
    """Lazily build an SDK client and rebuild it after a TTL or on an auth error.

    A multi-day run can outlive a credential (token expiry) or have its key rotated
    underneath it. This cache rebuilds the client — re-resolving the credential —
    when it goes stale (``FOUNDRY_CLIENT_TTL_S`` seconds, off by default) or right
    after a call fails with an auth error. An injected client (tests) is used as-is
    and never proactively refreshed."""

    def __init__(self, factory: Callable[[], Any], *, ttl_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 injected: Any | None = None) -> None:
        self._factory = factory
        self._client = injected
        self._injected = injected is not None
        self._clock = clock
        self._built_at = clock() if injected is not None else 0.0
        self._ttl = float(ttl_s if ttl_s is not None
                          else _int_env("FOUNDRY_CLIENT_TTL_S", 0))

    def get(self) -> Any:
        now = self._clock()
        stale = self._ttl > 0 and (now - self._built_at) >= self._ttl
        if self._client is None or (stale and not self._injected):
            self._client = self._factory()
            self._built_at = now
            self._injected = False
        return self._client

    def invalidate(self) -> None:
        self._client = None
        self._injected = False

    def run(self, call: Callable[[Any], Any]) -> Any:
        try:
            return call(self.get())
        except Exception as exc:
            if _is_auth_error(exc):
                self.invalidate()   # next call rebuilds → re-resolves the credential
            raise


def _seconds_until_clock_reset(text: str) -> float | None:
    """Seconds until a wall-clock reset named in a limit message, or None.

    The claude CLI's session-limit message states the reset as a clock time in
    UTC ("resets 8am (UTC)", "resets at 14:30") rather than a unix epoch. Parse
    the hour (12h am/pm or 24h) and return the seconds until the NEXT occurrence
    of that UTC time (today if still ahead, else tomorrow), plus a little slack
    for clock skew. Anything unparseable yields None so the caller falls back to
    plain backoff."""
    m = re.search(
        r"reset(?:s|ting)?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() + 30.0


def _classify_rate_limit(exc: BaseException) -> tuple[bool, float | None]:
    """(is_rate_limited, retry_after_seconds_or_None) for a transport exception.

    Detects across SDK error shapes (a ``status_code``/``status`` of 429/529, a
    ``RateLimit``/``Overloaded`` class name) and as a last resort the message text,
    and pulls a ``Retry-After`` header value when the provider supplied one."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    is_rl = status in (429, 529)
    if not is_rl:
        name = type(exc).__name__.lower()
        if "ratelimit" in name or "overloaded" in name:
            is_rl = True
    if not is_rl and any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS):
        is_rl = True
    if not is_rl:
        return False, None
    # Honor Retry-After (seconds) when the provider gave one.
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            getter = headers.get if hasattr(headers, "get") else (lambda *_: None)
            ra = getter("retry-after") or getter("Retry-After")
            if ra is not None:
                return True, max(0.0, float(ra))
        except (TypeError, ValueError):
            pass
    # The claude CLI's usage-limit message carries the reset time as a unix epoch
    # ("Claude AI usage limit reached|1751826000") — turn it into an explicit
    # wait-until-reset (plus slack for clock skew) so a Max-window exhaustion is
    # ridden out in ONE long wait instead of six useless 60-second ones.
    text = str(exc)
    if "usage limit" in text.lower():
        m = re.search(r"\b(1[6-9]\d{8}|2\d{9})\b", text)   # a plausible unix epoch
        if m:
            until = float(m.group(1)) - time.time()
            if 0 < until < 48 * 3600:
                return True, until + 30.0
    # The session-limit message carries a wall-CLOCK reset, not an epoch
    # ("session limit · resets 8am (UTC)"). Turn that into an explicit
    # wait-until-that-hour so the window is ridden out in ONE long sleep (bounded
    # by the max-wait ceiling upstream) instead of a burst of short backoffs.
    if "session limit" in text.lower() or "usage limit" in text.lower():
        secs = _seconds_until_clock_reset(text)
        if secs is not None and 0 < secs < 48 * 3600:
            return True, secs
    return True, None


class _RateLimiter:
    """Optional client-side throttle in front of every model egress.

    Two independent knobs, both OFF (no-op) by default so behaviour is unchanged
    unless an operator opts in:

    * ``max_rpm`` — paces requests to at most N per minute (a reserved-slot pacer,
      so concurrent callers space out instead of bursting past the provider's RPM);
    * ``max_concurrent`` — caps simultaneous in-flight calls (a semaphore).

    Thread-safe: model calls fan out across worker threads, so the pacer reserves
    its next slot under a lock, then sleeps OUTSIDE the lock to keep concurrency."""

    def __init__(self, max_rpm: int = 0, max_concurrent: int = 0, *,
                 sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = 60.0 / max_rpm if max_rpm > 0 else 0.0
        self._sem = threading.Semaphore(max_concurrent) if max_concurrent > 0 else None
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._sleeper = sleeper
        self._clock = clock

    @contextmanager
    def slot(self) -> Iterator[None]:
        if self._sem is not None:
            self._sem.acquire()
        try:
            if self._min_interval > 0:
                with self._lock:
                    now = self._clock()
                    start = max(now, self._next_allowed)
                    self._next_allowed = start + self._min_interval
                    wait = start - now
                if wait > 0:
                    self._sleeper(wait)
            yield
        finally:
            if self._sem is not None:
                self._sem.release()


class ModelProviderProxy:
    """Single egress, lineage observer, manifest writer, judgment stamper."""

    def __init__(
        self,
        transport: ProviderTransport | None = None,
        lineage_map: LineageMap | None = None,
        signer: ZoneBSigner | None = None,
        *,
        profile: Literal["self_judge", "decorrelated"] = "self_judge",
        tier_models: Mapping[str, str] | None = None,
        sampling_policy: SamplingPolicy | None = None,
        drift_policy: DriftPolicy | None = None,
        max_retries: int = 2,
        backoff_base_s: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        max_rpm: int | None = None,
        max_concurrent_calls: int | None = None,
        rate_limit_max_retries: int | None = None,
        rate_limit_backoff_base_s: float = 2.0,
        rate_limit_backoff_cap_s: float = 60.0,
        manifest_max: int | None = None,
    ) -> None:
        if profile not in ("self_judge", "decorrelated"):
            raise ValueError(f"unknown profile: {profile!r}")
        self._transport = transport or AnthropicTransport()
        self._lineage = lineage_map or LineageMap()
        self._signer = signer
        self._profile = profile
        # Backend/model-aware determinism control (arch §8): which knob a
        # deterministic gate/judge call pins (effort vs temperature 0) is a
        # property of the configured backend's model ids, not a hardcoded id.
        self._sampling_policy = sampling_policy or default_sampling_policy()
        # Pinned-ID-drift reaction policy (arch §8, ops §5). Default ADVISORY:
        # drift is recorded + alerted but never raises. A FAIL_CLOSED policy
        # makes gate/judgment drift raise PinnedIdDriftError (pause-and-escalate).
        self._drift_policy = drift_policy or default_drift_policy()
        # Tier -> pinned-model-id map. Defaults to the Anthropic lineage
        # (TIER_MODELS) but is injectable so an alternative backend (Codex/
        # Ollama-Gemma) can pin ITS real model ids — the flow is identical, the
        # ids the proxy pins/observes/drift-checks against just change with the
        # configured backend. Empty/unknown tiers still fail closed in complete().
        self._tier_models: dict[str, str] = (
            dict(tier_models) if tier_models is not None else dict(TIER_MODELS)
        )
        if not self._tier_models:
            raise ValueError("tier_models must define at least one tier")
        self._retry = _RetryPolicy(max_retries, backoff_base_s, sleeper)
        # Rate-limit (429/529) handling: a separate, more generous retry budget that
        # honors Retry-After, so a long run rides out provider throttling instead of
        # failing closed on it. Defaults come from the environment (off-by-default for
        # the throttle, 6 waits for the budget) but stay injectable for tests.
        self._rl_sleeper = sleeper
        self._rl_max_retries = (
            rate_limit_max_retries if rate_limit_max_retries is not None
            else _int_env("FOUNDRY_RATE_LIMIT_RETRIES", 6)
        )
        self._rl_backoff_base_s = rate_limit_backoff_base_s
        self._rl_backoff_cap_s = rate_limit_backoff_cap_s
        # Ceiling for ONE explicit wait-until-reset (Retry-After / a usage-window
        # epoch). Distinct from the backoff cap: implicit throttling backs off in
        # ≤60s steps; an explicit reset hours away is honored up to this bound.
        self._rl_max_wait_s = float(
            _int_env("FOUNDRY_RATE_LIMIT_MAX_WAIT_S", 6 * 3600)
        )
        self._limiter = _RateLimiter(
            max_rpm=(max_rpm if max_rpm is not None
                     else _int_env("FOUNDRY_MAX_RPM", 0)),
            max_concurrent=(max_concurrent_calls if max_concurrent_calls is not None
                            else _int_env("FOUNDRY_MAX_CONCURRENT_CALLS", 0)),
            sleeper=sleeper,
        )
        self._max_tokens = max_tokens
        # Per-call records. Bounded so a days-long run with tens of thousands of calls
        # can't grow the manifest without limit; the oldest records roll off (and are
        # counted) past the cap. FOUNDRY_MANIFEST_MAX=0 keeps the full unbounded log.
        _mm = (manifest_max if manifest_max is not None
               else _int_env("FOUNDRY_MANIFEST_MAX", 100_000))
        self._manifest: deque[dict[str, Any]] = deque(maxlen=_mm if _mm > 0 else None)
        self._manifest_dropped = 0
        self._drift_events: list[dict[str, Any]] = []

    # ---- read-only views ----------------------------------------------------

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def lineage_map_revision(self) -> str:
        return self._lineage.revision

    @property
    def tier_models(self) -> dict[str, str]:
        """The configured tier -> pinned-model-id map (read-only copy)."""
        return dict(self._tier_models)

    @property
    def sampling_policy(self) -> SamplingPolicy:
        """The configured determinism-control policy (effort vs temperature)."""
        return self._sampling_policy

    @property
    def drift_policy(self) -> DriftPolicy:
        """The configured pinned-ID-drift reaction policy (advisory/fail-closed)."""
        return self._drift_policy

    @property
    def manifest(self) -> tuple[dict[str, Any], ...]:
        """Per-call record set (read-only view); bounded — see ``manifest_dropped``."""
        return tuple(self._manifest)

    @property
    def manifest_dropped(self) -> int:
        """How many oldest records have rolled off the bounded manifest (0 = none)."""
        return self._manifest_dropped

    @property
    def drift_events(self) -> tuple[dict[str, Any], ...]:
        """Pinned-ID-drift alerts (arch §8): served model != pinned request."""
        return tuple(self._drift_events)

    # ---- decorrelation seam ---------------------------------------------------

    def assert_disjoint(
        self,
        reviewer_family: frozenset[str] | set[str],
        implementer_family: frozenset[str] | set[str],
    ) -> None:
        """Decorrelated profile: reviewer/implementer family sets must not
        intersect (arch §2/§8). In the self-judge profile this check is
        REPLACED by the recorded ``judgment_provenance="self"`` stamp
        (plan §0.2) — same lineage is the declared operating condition, so
        the call is a no-op there rather than a false guarantee.
        """
        if self._profile == "self_judge":
            return
        overlap = frozenset(reviewer_family) & frozenset(implementer_family)
        if overlap:
            raise FamilyOverlapError(
                f"reviewer/implementer served_family overlap {sorted(overlap)} — "
                "judgment node must not proceed in a correlated configuration"
            )

    # ---- the single egress ------------------------------------------------------

    def complete(
        self,
        node_id: str,
        tier: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        *,
        purpose: Purpose,
        schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """One credentialed model call: build → send (bounded retry) →
        observe lineage → record manifest → return text/JSON + usage."""
        if purpose not in _PURPOSES:
            raise ValueError(f"unknown purpose: {purpose!r}")
        if tier not in self._tier_models:
            raise ValueError(
                f"unknown tier: {tier!r} (expected one of {sorted(self._tier_models)})"
            )
        requested_model = self._tier_models[tier]
        request, sampling, sampling_note = self._build_request(
            requested_model, system_sop, messages, purpose, schema
        )

        response, parsed = self._send_with_retry(request, schema=schema)

        # Lineage is resolved from the proxy-OBSERVED served model id; an
        # unknown id fails closed inside the LineageMap (never guessed).
        served_family = self._lineage.resolve(response.served_model)

        drift, drift_kind = self._classify_drift(requested_model, response.served_model, tier)
        record: dict[str, Any] = {
            "node_id": node_id,
            "purpose": purpose,
            "tier": tier,
            "requested_model": requested_model,
            "served_model": response.served_model,
            "served_family": sorted(served_family),
            "lineage_map_revision": self._lineage.revision,
            "request_id": response.request_id,
            "usage": dict(response.usage),
            "cache_read_tokens": response.usage.get("cache_read_input_tokens", 0),
            "sampling": sampling,
            "drift": drift,
        }
        if drift_kind is not None:
            record["drift_kind"] = drift_kind
        if sampling_note:
            record["sampling_note"] = sampling_note
        if self._manifest.maxlen is not None and len(self._manifest) == self._manifest.maxlen:
            self._manifest_dropped += 1     # this append rolls the oldest record off
        self._manifest.append(record)
        if drift:
            # Pinned-ID drift (arch §8): invalidates calibration baselines. The
            # event is ALWAYS recorded (advisory or fail-closed); the policy only
            # decides whether a high-risk (gate/judgment) drift ALSO raises.
            event = {
                "node_id": node_id,
                "purpose": purpose,
                "tier": tier,
                "kind": drift_kind,
                "requested_model": requested_model,
                "served_model": response.served_model,
                "request_id": response.request_id,
            }
            self._drift_events.append(event)
            if self._drift_policy.should_raise(purpose):
                # Fail-closed: a drifted high-risk call must not be trusted — the
                # judgment node pause-and-escalates (mirrors ProxyUnavailable).
                raise PinnedIdDriftError(event)

        # `parsed` was extracted inside the retry unit (lenient for CLI backends,
        # re-rolled on a miss, fail-closed after the budget) — see _send_with_retry.
        return CompletionResult(
            text=response.text,
            parsed=parsed,
            served_model=response.served_model,
            served_family=served_family,
            request_id=response.request_id,
            usage=dict(response.usage),
            stop_reason=response.stop_reason,
            manifest_record=record,
        )

    # ---- trusted judgment path ---------------------------------------------------

    def judge(
        self,
        node_id: str,
        subject_artifact_uri: str,
        state_sha: str,
        verdict_nonce: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        *,
        seq: Seq,
        tier: str = "T1",
        gate_name: str = "review",
    ) -> JudgmentResult:
        """Run a judgment call and return a proxy-stamped ``GateResult``.

        The verdict ``status`` is extracted by the PROXY from the
        grammar-constrained structured-output channel — a compromised adapter
        cannot have a free-form "approve" authentically signed (arch §2b).
        The binding is stamped here, outside the adapter, by the injected
        ZoneBSigner; the adapter never holds the key.
        """
        if self._signer is None:
            raise RuntimeError(
                "judge() requires an injected ZoneBSigner — the proxy is the "
                "only component (outside zone B) that stamps judgment bindings"
            )
        if seq.node_id != node_id:
            raise ValueError(
                f"seq.node_id {seq.node_id!r} != node_id {node_id!r}: the binding "
                "signs seq.node_id; a mismatched record would never verify coherently"
            )
        completion = self.complete(
            node_id, tier, system_sop, messages, purpose="judgment", schema=VERDICT_SCHEMA
        )
        verdict = completion.parsed
        if not isinstance(verdict, dict) or verdict.get("status") not in _STATUS_BY_VERDICT:
            raise VerdictChannelError(
                f"constrained verdict channel returned out-of-grammar payload: {verdict!r}"
            )
        provenance = (
            JudgmentProvenance.SELF
            if self._profile == "self_judge"
            else JudgmentProvenance.INDEPENDENT
        )
        unstamped = GateResult(
            name=gate_name,
            status=_STATUS_BY_VERDICT[verdict["status"]],
            decision_deterministic=False,
            execution_deterministic=False,
            verdict_class=VerdictClass.PERCEPTUAL,
            verdict_nonce=verdict_nonce,
            served_family=completion.served_family,  # proxy-observed set
            subject_artifact_uri=subject_artifact_uri,
            lineage_map_revision=self._lineage.revision,
            run_against=state_sha,
            findings=tuple(str(f) for f in verdict.get("findings", ())),
            judgment_provenance=provenance,
            seq=seq,
        )
        stamped = self._signer.with_judgment_binding(unstamped)
        return JudgmentResult(gate_result=stamped, completion=completion)

    # ---- internals ------------------------------------------------------------

    def _classify_drift(
        self, requested_model: str, served_model: str, tier: str
    ) -> tuple[bool, str | None]:
        """Decide whether the observed served id is pinned-ID drift, and its kind.

        Returns ``(drift, kind)``:

        - ``(False, None)`` — the served id IS the pinned id, or a version-suffixed
          form of it (``claude-sonnet-4-6-20250101`` against ``claude-sonnet-4-6``):
          same pinned tier, no drift.
        - ``(True, "tier-mismatch")`` — the served id is a DIFFERENT configured
          tier's id (e.g. a smaller Ollama tag served against a larger pin). It may
          resolve to the same lineage family, so the family check would pass it; we
          surface it as drift anyway because the served CAPABILITY silently changed.
        - ``(True, "pinned-id")`` — any other departure from the pinned id.
        """
        # Exact pin, or a version-suffixed form of it, is the trusted case. The
        # suffix must start at a version-style boundary so e.g. a `gemma3:27b` pin
        # is NOT matched by a `gemma3:27b-it` distinct tag silently — but a bare
        # date/version suffix on the same id is (the prior startswith contract).
        if served_model == requested_model or served_model.startswith(requested_model + "-"):
            return (False, None)
        # Same-family but wrong-tier: the served id is some OTHER pinned tier id.
        for other_tier, other_model in self._tier_models.items():
            if other_tier == tier:
                continue
            if served_model == other_model or served_model.startswith(other_model + "-"):
                return (True, "tier-mismatch")
        return (True, "pinned-id")

    def _build_request(
        self,
        requested_model: str,
        system_sop: str,
        messages: Sequence[dict[str, Any]],
        purpose: Purpose,
        schema: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        """Returns (request, sampling-record, substitution-note).

        The stable SOP prefix is cached (`cache_control` on the system block,
        arch §8: prompt caching is the primary token-cost lever); volatile
        per-call content rides in `messages`, after the breakpoint.
        """
        request: dict[str, Any] = {
            "model": requested_model,
            "max_tokens": self._max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system_sop,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": list(messages),
        }
        sampling: dict[str, Any] = {}
        note: str | None = None
        output_config: dict[str, Any] = {}
        takes_effort = self._sampling_policy.takes_effort(requested_model)
        effort = self._sampling_policy.effort

        if purpose in ("gate", "judgment"):
            # Determinism policy (arch §8). Thinking is OMITTED on this path.
            # The control is backend/model-aware: reasoning / no-sampling models
            # take a pinned `effort` (a temperature would be rejected and — on a
            # CLI transport that silently drops it — leave the call with NO
            # determinism control); every other model honours temperature 0.
            if takes_effort:
                output_config["effort"] = effort
                sampling = {"effort": effort}
                note = (
                    f"sampling params removed on {requested_model}; "
                    f'pinned output_config={{"effort": "{effort}"}} in lieu of '
                    "temperature 0 (reasoning/no-sampling model)"
                )
            else:
                request["temperature"] = 0
                sampling = {"temperature": 0}
        else:  # ideation — exploration helps (arch §8)
            if takes_effort:
                request["thinking"] = {"type": "adaptive"}
                sampling = {"thinking": "adaptive"}

        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            request["output_config"] = output_config
        return request, sampling, note

    def _send_with_retry(
        self, request: dict[str, Any], *, schema: dict[str, Any] | None = None
    ) -> tuple[ProviderResponse, dict[str, Any] | None]:
        """Bounded retry/backoff, then fail closed; returns (response, parsed).

        When a ``schema`` is set the structured-output PARSE is part of the
        retried unit: a CLI backend that returns prose / a fence / empty for a
        structured call is RE-ROLLED (lenient extraction first), and only
        escalates to ProxyUnavailable after the retry budget — never an opaque
        JSONDecodeError mid-graph. No model/family fallback ever happens here —
        unavailability of the pinned model is surfaced as ProxyUnavailable so
        judgment nodes pause-and-escalate instead of degrading silently
        (arch §2b/§8).
        """
        attempt = 0          # real transport failures (bounded, fail-closed)
        rl_attempt = 0       # provider throttling (separate, more generous budget)
        while True:
            try:
                with self._limiter.slot():
                    response = self._transport.send(request)
                parsed = (
                    _extract_json_object(response.text) if schema is not None else None
                )
                return response, parsed
            except Exception as exc:
                is_rl, retry_after = _classify_rate_limit(exc)
                if is_rl:
                    # Provider throttled us — wait it out on the rate-limit budget,
                    # honoring Retry-After; only fail closed after that budget too.
                    if rl_attempt >= self._rl_max_retries:
                        raise ProxyUnavailable(
                            f"provider rate-limited after {rl_attempt + 1} wait(s); "
                            "judgment nodes must pause-and-escalate"
                        ) from exc
                    backoff = min(
                        self._rl_backoff_cap_s,
                        self._rl_backoff_base_s * (2**rl_attempt),
                    )
                    if retry_after is not None:
                        # An EXPLICIT reset/Retry-After (e.g. a Max-plan usage
                        # window) may be hours away — honor it past the backoff
                        # cap, bounded by the max-wait ceiling, so an overnight
                        # run sleeps through the window instead of failing.
                        wait = min(retry_after, self._rl_max_wait_s)
                    else:
                        wait = backoff
                    if wait >= 300:
                        resume_at = time.strftime(
                            "%H:%M", time.localtime(time.time() + wait))
                        print(f"[proxy] provider limit hit — waiting "
                              f"{wait / 60:.0f} min (resuming ~{resume_at}) "
                              f"[wait {rl_attempt + 1}/{self._rl_max_retries}]")
                    if on_rate_limit_wait is not None:
                        try:
                            on_rate_limit_wait(
                                wait, time.time() + wait, retry_after is not None
                            )
                        except Exception:  # noqa: S110 — advisory telemetry only
                            pass
                    self._rl_sleeper(wait)
                    rl_attempt += 1
                    continue
                if attempt >= self._retry.max_retries:
                    raise ProxyUnavailable(
                        f"provider transport failed after {attempt + 1} attempt(s); "
                        "judgment nodes must pause-and-escalate (no family fallback)"
                    ) from exc
                self._retry.sleep_before_retry(attempt)
                attempt += 1
