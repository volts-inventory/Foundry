"""Scope tiering — the planner's scope decision and the caps it drives.

Asserts the properties the feature's safety rests on:

* a supplied design spec forces STANDARD whatever the model answers;
* the model decides everything else, and anything that is not exactly "demo"
  degrades to STANDARD;
* an already-decided ``scope_tier`` (an operator ``--scope`` pin, or a spec_fix
  re-entry) is never rewritten, and its caps reach the prompt — except on a
  revision pass, where closing the gate gap wins over the cap;
* ``scope_directive`` is EMPTY at standard tier, so a non-demo run's prompts
  carry no caps — the feature's only behavioural delta is on a DEMO run;
* the demo directives preserve the floor the deterministic gates enforce (the
  per-criterion oracle hint, one security/abuse criterion, the manifest
  criterion, and a non-empty ownership map);
* scope tiering never relaxes integrity, and the planner writes no gate channel.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.agents._common import scope_directive
from foundry.agents.planner import make_planner_node
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.governance import ScopeDecision, ScopeTier
from foundry.state.seq import Seq
from tests.test_planner import PLAN

KEY = b"dev-zone-b-scope-key"
KEY_ID = "dev-key-scope"

DEMO_SCOPE: dict[str, Any] = {
    "tier": "demo",
    "rationale": "asks for a demo of one feature",
    "signals": ["demo"],
}
STANDARD_SCOPE: dict[str, Any] = {
    "tier": "standard",
    "rationale": "a real system despite the word 'simple'",
    "signals": [],
}


def runtime_for(*scopes: dict[str, Any]) -> tuple[AgentRuntime, MockTransport]:
    """A runtime whose planner replies carry ``scopes`` in order."""
    tx = MockTransport(completions=[json.dumps({**PLAN, "scope": s}) for s in scopes])
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy), tx


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}, "metadata": {"langgraph_step": step}}


def _seq() -> Seq:
    return Seq(run_id="test", emission_epoch=0, superstep_index=0, node_id="test", local=0)


def _pinned(tier: ScopeTier) -> ScopeDecision:
    return ScopeDecision(tier=tier, rationale="pinned", seq=_seq())


def _prompt(tx: MockTransport) -> str:
    return json.dumps(tx.requests[-1], ensure_ascii=False)


# ---- the one deterministic override ------------------------------------------


def test_supplied_design_spec_forces_standard_whatever_the_model_says() -> None:
    rt, tx = runtime_for(DEMO_SCOPE)
    writes = make_planner_node(rt)(
        {"request": "a simple demo shortener", "spec": "# Design\nDetailed doc."},
        config_for("r"),
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD
    assert "design spec" in writes["scope_tier"].rationale
    assert "SCOPE IS FIXED: standard" in _prompt(tx)
    assert "gates" not in writes  # the planner never mints a verdict (L5)


# ---- the model decides everything else --------------------------------------


def test_demo_is_honored_and_the_conditional_caps_are_offered() -> None:
    rt, tx = runtime_for(DEMO_SCOPE)
    writes = make_planner_node(rt)({"request": "a demo URL shortener"}, config_for("r"))

    decision = writes["scope_tier"]
    assert decision.tier is ScopeTier.DEMO
    assert decision.signals == ("demo",)
    prompt = _prompt(tx)
    assert "IF you judge scope.tier = 'demo'" in prompt
    assert "AT MOST 5 acceptance criteria" in prompt
    assert "AT MOST 1 component" in prompt


def test_model_may_overrule_the_wording_and_return_standard() -> None:
    rt, _ = runtime_for(STANDARD_SCOPE)
    writes = make_planner_node(rt)(
        {"request": "a simple ledger for production payroll"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_unrecognized_tier_degrades_to_standard() -> None:
    # Fail-to-full-scope: only the exact string "demo" caps the build.
    rt, _ = runtime_for({"tier": "DEMO-ish", "rationale": "?"})
    writes = make_planner_node(rt)({"request": "a demo shortener"}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


# ---- an existing decision is kept --------------------------------------------


def test_existing_decision_is_never_rewritten_and_its_caps_reach_the_prompt() -> None:
    rt, tx = runtime_for(STANDARD_SCOPE)  # the model's answer must not win
    writes = make_planner_node(rt)(
        {"request": "a demo shortener", "scope_tier": _pinned(ScopeTier.DEMO)},
        config_for("r"),
    )

    assert "scope_tier" not in writes
    prompt = _prompt(tx)
    assert "SCOPE IS FIXED: demo" in prompt
    assert "AT MOST 5 acceptance criteria" in prompt


def test_a_revision_pass_drops_the_scope_cap() -> None:
    """A revision pass exists only to close a gate-BLOCKING gap, so its "reproduce
    every criterion AND add" instruction must not be handed to the model alongside
    an "at most 5 criteria" cap. Closing the gap wins."""
    rt, tx = runtime_for(DEMO_SCOPE)
    failed = GateResult(
        name="spec",
        status=GateStatus.FAIL,
        decision_deterministic=True,
        execution_deterministic=False,
        verdict_class=VerdictClass.CONFIG_EVALUABLE,
        findings=("criteria set is empty",),
        run_against="sha",
        seq=_seq(),
    )
    make_planner_node(rt)(
        {
            "request": "a demo shortener",
            "scope_tier": _pinned(ScopeTier.DEMO),
            "gates": [failed],
        },
        config_for("r"),
    )

    prompt = _prompt(tx)
    assert "REVISION PASS" in prompt
    assert "AT MOST 5 acceptance criteria" not in prompt


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
    # requires a security/abuse criterion — a cap must not drop either. The
    # security criterion is a FLOOR, not a quota: capping it AT one would pin
    # assurance content to the gate minimum, which is a weakening, not a cap.
    assert "oracle_hint" in directive
    assert "AT LEAST ONE security/abuse criterion" in directive
    assert "EXACTLY ONE" not in directive
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
