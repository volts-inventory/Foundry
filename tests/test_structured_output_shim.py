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
    text = "Here is the result:\n```json\n{\"a\": 1, \"b\": [2, 3]}\n```\nDone."
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
        "n", "T1", "sop", [{"role": "user", "content": "go"}],
        purpose="ideation", schema={"type": "object"},
    )
    assert out.parsed == {"intent": "ok"}
    assert tx.calls == 2  # re-rolled exactly once


def test_persistent_miss_fails_closed_not_crash() -> None:
    tx = _FlakyTransport(["nope"])  # always unparseable
    proxy = _proxy(tx)
    with pytest.raises(ProxyUnavailable):  # NOT a raw JSONDecodeError
        proxy.complete(
            "n", "T1", "sop", [{"role": "user", "content": "go"}],
            purpose="ideation", schema={"type": "object"},
        )


def test_no_schema_does_not_parse() -> None:
    tx = _FlakyTransport(["free text, no json"])
    proxy = _proxy(tx)
    out = proxy.complete(
        "n", "T1", "sop", [{"role": "user", "content": "go"}], purpose="ideation"
    )
    assert out.parsed is None
    assert out.text == "free text, no json"
