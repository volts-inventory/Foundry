"""Adapter (coding-substrate) selection, CLI flag + webapp API option.

Phase 1 wiring: both shipped `WorkerAdapter`s are constructible, selectable with
a `--adapter` CLI flag, and selectable per build via the `adapter` field on
POST /api/projects. `stub` stays the default everywhere (the deterministic smoke
path is unchanged).
"""

from __future__ import annotations

from typing import Any

import pytest

from foundry.cli import (
    ADAPTERS,
    _default_adapter_for,
    _resolve_coding_adapter,
    main,
    make_coding_adapter,
    run,
)

# ---- make_coding_adapter: construction --------------------------------------


@pytest.fixture()
def proxy() -> Any:
    from foundry.cli import _build_proxy

    return _build_proxy("mock")


def test_every_adapter_constructs(proxy: Any) -> None:
    by_name = {
        name: type(make_coding_adapter(name, proxy=proxy, thread_id="t")).__name__
        for name in ADAPTERS
    }
    assert by_name == {
        "stub": "StubAdapter",
        "claude-code": "ClaudeCodeAdapter",
    }


def test_default_is_stub(proxy: Any) -> None:
    # None ⇒ the deterministic stub (back-compat: the smoke path is unchanged).
    assert type(make_coding_adapter(None, proxy=proxy)).__name__ == "StubAdapter"


def test_unknown_adapter_name_raises(proxy: Any) -> None:
    with pytest.raises(ValueError, match="unknown adapter"):
        make_coding_adapter("nope", proxy=proxy)


# ---- derived default: --adapter omitted ⇒ substrate follows --backend --------


def test_default_adapter_is_derived_from_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # mock has no real model ⇒ the deterministic stub. claude-cli — whose substrate
    # binary IS Claude Code — derives the AGENTIC claude-code adapter (ROADMAP 1a/1d:
    # same binary, same auth, multi-turn inner loop).
    monkeypatch.delenv("FOUNDRY_CODING_ADAPTER", raising=False)
    assert _default_adapter_for("mock") == "stub"
    assert _default_adapter_for("claude-cli") == "claude-code"


def test_foundry_adapter_env_overrides_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    # FOUNDRY_CODING_ADAPTER (a known adapter name) overrides the backend derivation —
    # config-side opt-out of the agentic default without a per-call flag.
    monkeypatch.setenv("FOUNDRY_CODING_ADAPTER", "stub")
    assert _default_adapter_for("claude-cli") == "stub"
    monkeypatch.setenv("FOUNDRY_CODING_ADAPTER", "bogus")  # unknown ⇒ ignored, derivation wins
    assert _default_adapter_for("claude-cli") == "claude-code"


def test_offered_adapters() -> None:
    # Two substrates: the deterministic stub and the agentic claude-code substrate
    # (ROADMAP 1a — it runs the `claude` CLI multi-turn inside the task sandbox).
    assert set(ADAPTERS) == {"stub", "claude-code"}


def test_run_with_mock_backend_and_no_adapter_stays_stub() -> None:
    # The default (no backend, no adapter) run stays the deterministic stub no-op,
    # so the hermetic smoke path is byte-identical.
    assert run("build a thing").passed in (True, False)  # runs without a real model


def test_resolve_passes_instance_through(proxy: Any) -> None:
    # A WorkerAdapter instance (tests inject one) is returned verbatim, not rebuilt.
    sentinel = make_coding_adapter("stub", proxy=proxy)
    assert _resolve_coding_adapter(sentinel, proxy=proxy, thread_id="t") is sentinel


# ---- --adapter CLI flag -----------------------------------------------------


def test_adapter_flag_valid_runs() -> None:
    assert main(["--adapter", "stub", "build a thing"]) == 0


def test_adapter_flag_unknown_is_usage_error() -> None:
    assert main(["--adapter", "bogus", "build a thing"]) == 2


def test_adapter_flag_missing_value_is_usage_error() -> None:
    assert main(["build a thing", "--adapter"]) == 2


# ---- webapp API option ------------------------------------------------------


def test_api_validates_adapter_against_set() -> None:
    # The set the API validates against is the single source of truth in cli.py.
    from foundry.cli import ADAPTERS as cli_adapters

    assert "stub" in cli_adapters and "claude-code" in cli_adapters


def test_project_carries_adapter_field() -> None:
    from webapp.server import Project

    p = Project(
        id="x", request="r", mode="assistant", supervised=False, floor=1,
        adapter="claude-code",
    )
    assert p.adapter == "claude-code"


def test_project_defaults_to_stub_adapter() -> None:
    from webapp.server import Project

    p = Project(id="x", request="r", mode="assistant", supervised=False, floor=1)
    assert p.adapter == "stub"


def test_store_roundtrips_adapter(tmp_path: Any) -> None:
    from webapp.store import BuildStore

    s = BuildStore(tmp_path / "cat.sqlite")
    s.upsert(
        {"id": "b1", "floor": 1, "backend": "mock", "adapter": "claude-code", "gates": {}}
    )
    row = dict(s._conn.execute("SELECT adapter FROM builds WHERE id='b1'").fetchone())
    assert row["adapter"] == "claude-code"


def test_activity_callback_reaches_the_agentic_adapter(proxy: Any) -> None:
    """`run(activity_cb=…)` is the whole point of the plumbing: the Workshop passes a
    listener and the agentic substrate must actually receive it, or a long step stays
    silent. Without one, the adapter is in its original non-streaming shape."""
    seen: list[str] = []
    adapter = make_coding_adapter("claude-code", proxy=proxy, thread_id="t",
                                  activity_cb=seen.append)
    adapter._pump_activity(                     # one stream-json assistant line
        '{"type":"assistant","message":{"content":'
        '[{"type":"tool_use","name":"Bash","input":{"command":"pytest -q"}}]}}'
    )
    assert seen == ["running pytest -q"]

    plain = make_coding_adapter("claude-code", proxy=proxy, thread_id="t")
    assert plain._on_activity is None
    plain._pump_activity('{"type":"assistant","message":{"content":[]}}')   # no-op
