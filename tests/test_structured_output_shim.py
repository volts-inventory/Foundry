"""Structured-output robustness for CLI backends.

The SDK enforces schema-conforming JSON; the CLI backends (claude -p / codex)
only carry a json-only PROMPT instruction, so a model may wrap the object in
prose / a ```json fence, or return empty. The proxy must recover the object
where possible, RE-ROLL a transient miss, and fail closed (never crash with a
raw JSONDecodeError mid-graph).
"""

from __future__ import annotations

from typing import Any

import pytest

from foundry.proxy.model_provider_proxy import (
    ProviderResponse,
    ProxyUnavailable,
    StructuredOutputError,
    _extract_json_object,
)

# ---- lenient extraction -----------------------------------------------------


def test_clean_json_parses() -> None:
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_fenced_json_is_recovered() -> None:
    text = 'Here is the result:\n```json\n{"a": 1, "b": [2, 3]}\n```\nDone.'
    assert _extract_json_object(text) == {"a": 1, "b": [2, 3]}


def test_prose_preamble_is_stripped() -> None:
    assert _extract_json_object('Sure! {"ok": true}') == {"ok": True}


def test_empty_raises_structured_output_error() -> None:
    with pytest.raises(StructuredOutputError):
        _extract_json_object("")
    with pytest.raises(StructuredOutputError):
        _extract_json_object("   \n  ")


def test_non_object_json_raises() -> None:
    # A bare array / scalar is not the schema object the caller needs.
    with pytest.raises(StructuredOutputError):
        _extract_json_object("[1, 2, 3]")
    with pytest.raises(StructuredOutputError):
        _extract_json_object("just an apology, no json here")


# ---- re-roll + fail-closed inside the retry unit ----------------------------


class _FlakyTransport:
    """Returns garbage then valid JSON — exercises the re-roll path."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return ProviderResponse(
            text=text,
            served_model=request.get("model", "claude-opus-4-8"),
            request_id=f"r{self.calls}",
            usage={},
            stop_reason="end_turn",
        )


def _proxy(transport: Any) -> Any:
    from foundry.proxy.model_provider_proxy import ModelProviderProxy

    return ModelProviderProxy(transport)


def test_transient_miss_is_re_rolled_then_succeeds() -> None:
    tx = _FlakyTransport(["", '{"intent": "ok"}'])  # 1st empty, 2nd valid
    proxy = _proxy(tx)
    out = proxy.complete(
        "n",
        "T1",
        "sop",
        [{"role": "user", "content": "go"}],
        purpose="ideation",
        schema={"type": "object"},
    )
    assert out.parsed == {"intent": "ok"}
    assert tx.calls == 2  # re-rolled exactly once


def test_persistent_miss_fails_closed_not_crash() -> None:
    tx = _FlakyTransport(["nope"])  # always unparseable
    proxy = _proxy(tx)
    with pytest.raises(ProxyUnavailable):  # NOT a raw JSONDecodeError
        proxy.complete(
            "n",
            "T1",
            "sop",
            [{"role": "user", "content": "go"}],
            purpose="ideation",
            schema={"type": "object"},
        )


def test_no_schema_does_not_parse() -> None:
    tx = _FlakyTransport(["free text, no json"])
    proxy = _proxy(tx)
    out = proxy.complete("n", "T1", "sop", [{"role": "user", "content": "go"}], purpose="ideation")
    assert out.parsed is None
    assert out.text == "free text, no json"


# ---- a re-roll must differ from the miss it retries --------------------------


class _RecordingTransport(_FlakyTransport):
    def __init__(self, texts: list[str], stop_reason: str = "end_turn") -> None:
        super().__init__(texts)
        self.requests: list[dict[str, Any]] = []
        self.stop_reason = stop_reason

    def send(self, request: dict[str, Any]) -> ProviderResponse:
        self.requests.append(request)
        out = super().send(request)
        return ProviderResponse(
            text=out.text,
            served_model=out.served_model,
            request_id=out.request_id,
            usage={},
            stop_reason=self.stop_reason,
        )


def _quick(transport: Any, **kw: Any) -> Any:
    from foundry.proxy.model_provider_proxy import ModelProviderProxy

    return ModelProviderProxy(transport, sleeper=lambda _s: None, **kw)


def _judge_call(proxy: Any, tier: str = "T1", purpose: str = "judgment") -> Any:
    return proxy.complete(
        "n",
        tier,
        "sop",
        [{"role": "user", "content": "go"}],
        purpose=purpose,
        schema={"type": "object"},
    )


def test_a_re_roll_names_the_miss_so_a_deterministic_model_answers_differently() -> None:
    tx = _RecordingTransport(['{"findings": ["x", "x", "x"', '{"ok": true}'], stop_reason="length")
    assert _judge_call(_quick(tx)).parsed == {"ok": True}
    first, second = (r["messages"][-1]["content"] for r in tx.requests)
    assert first == "go"
    assert second.startswith("go\n\n[Retry 1:")
    assert "27 chars" in second and "'length'" in second


def test_an_unparseable_reply_is_not_a_transient_failure() -> None:
    from foundry.proxy.model_provider_proxy import is_transient_failure

    with pytest.raises(ProxyUnavailable) as caught:
        _judge_call(_quick(_FlakyTransport(["nope"])))
    assert isinstance(caught.value.__cause__, StructuredOutputError)
    assert "no parseable structured reply in 3 attempt(s)" in str(caught.value)
    # Autoresume would resend the same prompt and get the same reply back.
    assert not is_transient_failure(caught.value)
    assert is_transient_failure(ProxyUnavailable("transport failed; pause-and-escalate"))


def test_the_run_caps_are_checked_before_every_retry() -> None:
    from foundry.proxy.token_meter import TimeCapExceeded

    class _Meter:
        checks = 0

        def check(self) -> None:
            self.checks += 1
            if self.checks > 1:
                raise TimeCapExceeded("time cap reached")

        def charge(self, usage: Any) -> None:
            pass

    tx = _FlakyTransport(["nope"])
    proxy = _quick(tx)
    proxy.token_meter = _Meter()
    with pytest.raises(TimeCapExceeded):
        _judge_call(proxy)
    assert tx.calls == 1


def test_a_local_judgment_call_gets_a_tighter_output_cap() -> None:
    from foundry.proxy.model_provider_proxy import (
        _DEFAULT_MAX_TOKENS,
        _LOCAL_JUDGMENT_MAX_TOKENS,
    )

    tx = _RecordingTransport(['{"ok": true}'])
    proxy = _quick(tx, local_tiers={"T2"})
    for tier, purpose in (("T2", "judgment"), ("T2", "ideation"), ("T1", "judgment")):
        _judge_call(proxy, tier, purpose)
    assert [r["max_tokens"] for r in tx.requests] == [
        _LOCAL_JUDGMENT_MAX_TOKENS,
        _DEFAULT_MAX_TOKENS,
        _DEFAULT_MAX_TOKENS,
    ]
