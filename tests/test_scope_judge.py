"""Scope tiering — the Scope Judge node and the caps it drives.

Asserts the properties the feature's safety rests on:

* the judge is STANDARD-biased in both stages (no smallness word, an oversized
  request, or a supplied design spec => STANDARD with NO model call);
* a smallness word only makes DEMO *reachable* — the model still decides, and
  anything that is not exactly "demo" degrades to STANDARD;
* an already-decided ``scope_tier`` (an operator ``--scope`` pin, or a resume
  re-entry) short-circuits the node with no write and no model call;
* ``scope_directive`` is EMPTY at standard tier, so a non-demo run's prompts are
  byte-identical to a pre-tiering run — the feature's only behavioural delta is
  on a DEMO run;
* the demo directives preserve the floor the deterministic gates enforce (the
  per-criterion oracle hint, one security/abuse criterion, the manifest
  criterion, and a non-empty ownership map);
* scope tiering never relaxes integrity, and the judge writes no gate channel.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import scope_directive
from foundry.agents.product import make_scope_judge_node
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.governance import ScopeDecision, ScopeTier
from foundry.state.seq import Seq

KEY = b"dev-zone-b-scope-key"
KEY_ID = "dev-key-scope"

DEMO_PAYLOAD: dict[str, Any] = {
    "tier": "demo",
    "rationale": "asks for a demo of one feature",
    "signals": ["demo"],
}
STANDARD_PAYLOAD: dict[str, Any] = {
    "tier": "standard",
    "rationale": "a real system despite the word 'simple'",
    "signals": [],
}


def runtime_for(*payloads: dict[str, Any]) -> AgentRuntime:
    """A runtime whose free-text completion queue yields ``payloads`` in order."""
    return AgentRuntime(
        proxy=ModelProviderProxy(
            MockTransport(completions=[json.dumps(p) for p in payloads]),
            signer=ZoneBSigner(KEY, KEY_ID),
        )
    )


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": step}}


def _seq() -> Seq:
    return Seq(run_id="test", emission_epoch=0, superstep_index=0, node_id="test", local=0)


def _pinned(tier: ScopeTier) -> ScopeDecision:
    return ScopeDecision(tier=tier, rationale="pinned", source="flag", seq=_seq())


# ---- stage 1: the deterministic pre-filter (no model call) -------------------


def test_request_without_smallness_word_is_standard_without_a_model_call() -> None:
    # An EMPTY completion queue: if the node called the model this would raise.
    rt = runtime_for()
    writes = make_scope_judge_node(rt)({"request": "build a URL shortener"}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.STANDARD
    assert "gates" not in writes  # a product node never mints a verdict (L5)


def test_supplied_design_spec_forces_standard_without_a_model_call() -> None:
    rt = runtime_for()
    writes = make_scope_judge_node(rt)(
        {"request": "a simple demo shortener", "spec": "# Design\nDetailed doc."},
        config_for("r"),
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD
    assert "design spec" in writes["scope_tier"].rationale


def test_oversized_request_forces_standard_without_a_model_call() -> None:
    rt = runtime_for()
    long_request = "build a simple demo service. " + ("more detail. " * 100)
    writes = make_scope_judge_node(rt)({"request": long_request}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_smallness_word_matches_on_word_boundaries_only() -> None:
    # "sampler" / "justify" contain "sample" / "just" as substrings but are not
    # smallness words — an empty queue proves no model call was made.
    rt = runtime_for()
    writes = make_scope_judge_node(rt)(
        {"request": "build an audio sampler that can justify its latency"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


# ---- stage 2: the model adjudicates a keyword hit ----------------------------


def test_smallness_word_reaches_the_model_and_demo_is_honored() -> None:
    rt = runtime_for(DEMO_PAYLOAD)
    writes = make_scope_judge_node(rt)({"request": "a demo URL shortener"}, config_for("r"))

    decision = writes["scope_tier"]
    assert decision.tier is ScopeTier.DEMO
    assert decision.signals == ("demo",)
    assert decision.source == "judge"


def test_model_may_overrule_the_keyword_and_return_standard() -> None:
    rt = runtime_for(STANDARD_PAYLOAD)
    writes = make_scope_judge_node(rt)(
        {"request": "a simple ledger for production payroll"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_unrecognized_tier_degrades_to_standard() -> None:
    # Fail-to-full-scope: only the exact string "demo" caps the build.
    rt = runtime_for({"tier": "DEMO-ish", "rationale": "?"})
    writes = make_scope_judge_node(rt)({"request": "a demo shortener"}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


# ---- idempotence: an existing decision short-circuits the node ---------------


def test_existing_decision_short_circuits_with_no_write_and_no_model_call() -> None:
    rt = runtime_for()  # empty queue: a model call would raise
    writes = make_scope_judge_node(rt)(
        {"request": "a demo shortener", "scope_tier": _pinned(ScopeTier.DEMO)},
        config_for("r"),
    )

    assert writes == {}


# ---- the caps the decision drives -------------------------------------------


def test_standard_tier_directive_is_empty_for_both_read_sites() -> None:
    # The feature's ONLY behavioural delta is on a demo run: at standard tier
    # every prompt must be byte-identical to a pre-tiering run.
    for state in ({}, {"scope_tier": None}, {"scope_tier": _pinned(ScopeTier.STANDARD)}):
        assert scope_directive(state) == ""
        assert scope_directive(state, architect=True) == ""


def test_demo_pm_directive_preserves_the_gate_enforced_floor() -> None:
    directive = scope_directive({"scope_tier": _pinned(ScopeTier.DEMO)})

    assert "AT MOST 5 acceptance criteria" in directive
    # gates/spec.py FAILs on any criterion with an empty oracle, and separately
    # requires a security/abuse criterion — a cap must not drop either.
    assert "oracle_hint" in directive
    assert "EXACTLY ONE security/abuse criterion" in directive
    assert "manifest" in directive


def test_demo_architect_directive_caps_fan_out_but_never_to_zero() -> None:
    directive = scope_directive({"scope_tier": _pinned(ScopeTier.DEMO)}, architect=True)

    assert "AT MOST 1 component" in directive
    assert "EMPTY interfaces array" in directive
    # Ownership UNITS drive task fan-out (one task per unit), so they are the cap
    # that matters — but an EMPTY map yields zero tasks and an empty repo that
    # fails the build gate, so the floor of one is load-bearing.
    assert "AT LEAST ONE owner" in directive
    assert "EMPTY ownership map is invalid" in directive


# ---- trust posture -----------------------------------------------------------


def test_scope_tiering_never_relaxes_integrity() -> None:
    assert _pinned(ScopeTier.DEMO).relaxes_integrity() is False
    assert _pinned(ScopeTier.STANDARD).relaxes_integrity() is False


def test_scope_decision_round_trips_the_checkpoint_serde() -> None:
    # A type missing from the allowlist degrades to a dict on resume, which would
    # silently revert a resumed run to STANDARD prompts mid-build.
    from foundry.durability.serde import FOUNDRY_MSGPACK_MODULES

    assert ("foundry.schemas.governance", "ScopeDecision") in FOUNDRY_MSGPACK_MODULES
    assert ("foundry.schemas.governance", "ScopeTier") in FOUNDRY_MSGPACK_MODULES
