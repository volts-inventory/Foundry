"""Ideation front-end (task [13]) — the bounded, advisory spec-authoring loop.

Drives :func:`foundry.ideation.ideate` over a REAL
:class:`~foundry.proxy.model_provider_proxy.ModelProviderProxy` backed by a
:class:`~foundry.proxy.test_doubles.MockTransport` (no live API key, no network).
Asserts:

* the loop runs exactly N BOUNDED rounds (one proxy ``complete`` per round, all
  ``purpose="ideation"``), and the turn cap clamps an over-large ``rounds``;
* it produces a non-empty markdown SPEC DOCUMENT with the sections [8]'s ``--spec``
  ingest grounds intent in (capabilities + Given/When/Then acceptance criteria);
* it is PURELY ADVISORY — it writes no ``GateResult`` / ``GateTriage`` /
  ``JournalEntry`` / ``Seq``, has no channel-write surface (never touches
  ``FactoryState`` or a reducer), and its proxy calls never hit the verdict path;
* the rendered spec round-trips through task [8]'s ``_load_spec`` ingest seam.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from foundry.agents._runtime import AgentRuntime
from foundry.cli import _load_spec
from foundry.ideation import IDEATION_SCHEMA, IdeationResult, ideate, render_spec_markdown
from foundry.ideation.loop import _MAX_ROUNDS, IdeationRound
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport

# A representative ideation round payload (matches IDEATION_SCHEMA).
IDEATION_PAYLOAD: dict[str, Any] = {
    "title": "Personal Todo Service",
    "summary": "Let a single user add, list, and complete todos via a small API.",
    "clarifying_questions": ["Which auth provider?", "Web or CLI client?"],
    "assumptions": ["single user", "no multi-tenant"],
    "capabilities": ["rest-api", "persistence"],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears in the list.",
            "oracle_hint": "POST /todos then GET /todos returns the new item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "oracle_hint": "request without token returns 401; negative oracle",
            "security_abuse": True,
        },
    ],
    "architecture_sketch": ["HTTP API layer", "SQLite store", "auth middleware"],
}


def _proxy(default_text: str | None = None) -> tuple[ModelProviderProxy, MockTransport]:
    """A real proxy over a MockTransport serving the ideation payload by default."""
    transport = MockTransport(default_text=default_text or json.dumps(IDEATION_PAYLOAD))
    return ModelProviderProxy(transport=transport), transport


def test_loop_runs_n_bounded_rounds() -> None:
    proxy, transport = _proxy()
    rt = AgentRuntime(proxy=proxy)

    result = ideate(rt, "a personal todo service", rounds=3)

    assert isinstance(result, IdeationResult)
    assert len(result.rounds) == 3
    # One proxy complete() per round, all ideation-purpose, never the verdict path.
    assert len(transport.requests) == 3
    assert [r["purpose"] for r in proxy.manifest] == ["ideation", "ideation", "ideation"]


def test_turn_cap_clamps_and_floors() -> None:
    proxy, _ = _proxy()
    rt = AgentRuntime(proxy=proxy)

    # Over-large rounds is clamped to the hard cap; zero/negative floored to 1.
    capped = ideate(rt, "an idea", rounds=_MAX_ROUNDS + 50)
    assert len(capped.rounds) == _MAX_ROUNDS

    proxy2, _ = _proxy()
    floored = ideate(AgentRuntime(proxy=proxy2), "an idea", rounds=0)
    assert len(floored.rounds) == 1


def test_produces_non_empty_spec_document() -> None:
    proxy, _ = _proxy()
    result = ideate(AgentRuntime(proxy=proxy), "a personal todo service", rounds=2)

    md = result.spec_markdown
    assert md.strip()
    # The sections [8]'s --spec ingest grounds intent in.
    assert "# Personal Todo Service" in md
    assert "## Capabilities" in md
    assert "## Acceptance Criteria" in md
    assert "AC-001" in md and "AC-002" in md
    assert "*(security/abuse)*" in md  # the flagged criterion is surfaced
    assert "## Architecture Sketch" in md
    # The advisory disclaimer is present (front-of-funnel authoring aid).
    assert "Advisory" in md


def test_spec_markdown_feeds_task8_ingest() -> None:
    proxy, _ = _proxy()
    result = ideate(AgentRuntime(proxy=proxy), "a personal todo service", rounds=1)

    # The deliverable round-trips through task [8]'s spec-ingest seam as LITERAL
    # spec text (a non-path string) — exactly how a human would feed it.
    ingested = _load_spec(result.spec_markdown)
    assert ingested == result.spec_markdown
    assert "Acceptance Criteria" in ingested


def test_empty_idea_rejected() -> None:
    proxy, _ = _proxy()
    rt = AgentRuntime(proxy=proxy)
    with pytest.raises(ValueError):
        ideate(rt, "   ", rounds=2)


def test_robust_to_terse_payload() -> None:
    # A minimal payload (only required fields) still renders a usable spec.
    proxy, _ = _proxy(default_text=json.dumps({"title": "Bare", "summary": "Just enough."}))
    result = ideate(AgentRuntime(proxy=proxy), "a bare idea", rounds=1)
    assert "# Bare" in result.spec_markdown
    assert "_(none proposed)_" in result.spec_markdown  # empty sections degrade cleanly


def test_round_from_partial_payload_defaults() -> None:
    rnd = IdeationRound.from_payload(None)
    assert rnd.title == "" and rnd.acceptance_criteria == ()
    # Non-dict criteria entries are skipped, not crashed on.
    rnd2 = IdeationRound.from_payload({"acceptance_criteria": ["bogus", {"id": "X", "text": "t"}]})
    assert len(rnd2.acceptance_criteria) == 1
    assert rnd2.acceptance_criteria[0]["id"] == "X"


def test_ideation_schema_is_not_the_verdict_schema() -> None:
    # Structural guard: the ideation schema must NOT be detected as the judgment
    # verdict channel, or its completions would be shadowed by the verdict FIFO.
    from foundry.proxy.test_doubles import _is_verdict_schema

    assert not _is_verdict_schema(IDEATION_SCHEMA)
    status = IDEATION_SCHEMA["properties"].get("status")
    assert status is None  # no approve/request-changes verdict enum


def test_advisory_no_channel_write_surface() -> None:
    """The whole module is ADVISORY: it imports/writes NO gate/verdict/journal
    machinery and never touches FactoryState or a reducer (no channel write)."""
    import foundry.ideation.loop as loop_mod

    src = inspect.getsource(loop_mod)
    # No trust/verdict/journal/state types are referenced anywhere in the loop.
    for forbidden in (
        "GateResult",
        "GateTriage",
        "GateStatus",
        "JournalEntry",
        "FactoryState",
        "judge(",
        "agent_journal",
        "with_judgment_binding",
    ):
        assert forbidden not in src, f"ideation must not reference {forbidden!r}"

    # The result carries no Seq and no channel keys — it is plain data.
    proxy, _ = _proxy()
    result = ideate(AgentRuntime(proxy=proxy), "an idea", rounds=1)
    assert not hasattr(result, "seq")
    assert isinstance(result.spec_markdown, str)


def test_render_is_pure_given_a_round() -> None:
    # The renderer is a pure function of (idea, round) — no proxy, no I/O.
    rnd = IdeationRound.from_payload(IDEATION_PAYLOAD)
    md = render_spec_markdown("an idea", rnd)
    assert md == render_spec_markdown("an idea", rnd)  # deterministic
    assert "Personal Todo Service" in md


def test_main_degrades_gracefully_on_proxy_failure(monkeypatch, capsys) -> None:
    """Standalone entry: a failure inside ideate() prints a clean error + exits 1,
    never an uncaught traceback (advisory tool, no trust path)."""
    import foundry.ideation.__main__ as ideation_cli

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("backend down")

    monkeypatch.setattr(ideation_cli, "ideate", _boom)
    rc = ideation_cli.main(["--mock", "build a thing"])
    assert rc == 1
    assert "ideation failed" in capsys.readouterr().err
