"""Per-tier model pinning: the curated defaults + FOUNDRY_MODEL_T{1,2,3} overrides.

The proxy pins a model id per reasoning tier and the fail-closed drift policy
rejects a gate/judge call whose served model doesn't resolve to the pinned one.
An operator must be able to name the EXACT ids their egress serves WITHOUT
editing code — these pin that env contract.
"""

from __future__ import annotations

import pytest

from foundry.cli import _default_tier_models, _tier_models


def test_claude_cli_uses_the_anthropic_map() -> None:
    c = _default_tier_models("claude-cli")
    assert set(c) == {"T1", "T2", "T3"}
    assert c["T1"].startswith("claude-")


def test_env_override_overlays_a_single_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_MODEL_T1", "claude-opus-4-8-20990101")
    monkeypatch.delenv("FOUNDRY_MODEL_T2", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL_T3", raising=False)
    m = _tier_models("claude-cli")
    assert m["T1"] == "claude-opus-4-8-20990101"        # overridden
    assert m["T2"] == _default_tier_models("claude-cli")["T2"]  # default kept


def test_env_override_can_pin_all_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_MODEL_T1", "m1")
    monkeypatch.setenv("FOUNDRY_MODEL_T2", "m2")
    monkeypatch.setenv("FOUNDRY_MODEL_T3", "m3")
    assert _tier_models("claude-cli") == {"T1": "m1", "T2": "m2", "T3": "m3"}


def test_no_env_is_byte_identical_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("FOUNDRY_MODEL_T1", "FOUNDRY_MODEL_T2", "FOUNDRY_MODEL_T3"):
        monkeypatch.delenv(var, raising=False)
    assert _tier_models("claude-cli") == _default_tier_models("claude-cli")


def test_build_proxy_threads_pinned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real backend's proxy must carry the overridden tier map (no live egress
    # needed — the proxy is constructed offline; we only inspect its tier_models).
    monkeypatch.setenv("FOUNDRY_MODEL_T1", "claude-opus-pinned")
    from foundry.cli import _build_proxy

    proxy = _build_proxy("claude-cli")
    assert proxy.tier_models["T1"] == "claude-opus-pinned"
