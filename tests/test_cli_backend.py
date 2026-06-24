"""CLI ``--backend`` flag (model-backend selection) — no network.

Proves the flag selects the proxy backend: the default is the deterministic
keyless mock; the four real names map to the right transport (constructed, never
called); an unknown name fails closed; and the default run is unchanged.
"""

from __future__ import annotations

import pytest

from foundry.cli import BACKENDS, _build_proxy, main, run
from foundry.proxy import ModelProviderProxy
from foundry.proxy.cli_transports import (
    ClaudeCliTransport,
    CodexCliTransport,
    OllamaTransport,
)
from foundry.proxy.model_provider_proxy import AnthropicTransport
from foundry.proxy.test_doubles import MockTransport


def test_default_backend_is_mock_and_run_passes() -> None:
    result = run("build a thing")  # no backend arg
    assert result.backend == "mock"
    assert result.passed is True


def test_build_proxy_mock_uses_mocktransport() -> None:
    proxy = _build_proxy("mock")
    assert isinstance(proxy, ModelProviderProxy)
    assert isinstance(proxy._transport, MockTransport)


@pytest.mark.parametrize(
    ("backend", "transport_cls"),
    [
        ("anthropic", AnthropicTransport),
        ("claude-cli", ClaudeCliTransport),
        ("codex", CodexCliTransport),
        ("ollama-gemma", OllamaTransport),
    ],
)
def test_build_proxy_maps_real_backends_to_transports(backend: str, transport_cls: type) -> None:
    # Construction only — no model is called, so no key/binary/server is needed.
    proxy = _build_proxy(backend)
    assert isinstance(proxy, ModelProviderProxy)
    assert isinstance(proxy._transport, transport_cls)


def test_real_backend_carries_the_dev_signer() -> None:
    # The zone-B signer must be injected for every backend so the judgment path
    # can stamp verdicts regardless of which model egress is configured.
    assert _build_proxy("ollama-gemma")._signer is not None
    assert _build_proxy("mock")._signer is not None


def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        _build_proxy("bogus")
    with pytest.raises(ValueError, match="unknown backend"):
        run("x", backend="bogus")


def test_cli_unknown_backend_exits_2() -> None:
    assert main(["--backend", "bogus", "build a thing"]) == 2


def test_cli_missing_backend_value_exits_2() -> None:
    assert main(["--backend"]) == 2


def test_explicit_mock_backend_is_listed_and_default() -> None:
    assert "mock" in BACKENDS and BACKENDS[0] == "mock"
    assert run("x", backend="mock").backend == "mock"


def test_summary_reports_backend() -> None:
    summary = run("build a thing").summary()
    assert "backend:   mock" in summary
