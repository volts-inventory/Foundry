"""Scope tiering — the Scope Judge node and the caps it drives.

Asserts the properties the feature's safety rests on:

* a supplied design spec short-circuits to STANDARD with no model call;
* the model decides everything else, and anything that is not exactly "demo" /
  "small" degrades to STANDARD (the tie-break stays conservative);
* every node that can make a model call is scripted for `--backend mock`, or the
  run dies as a ProxyUnavailable on the documented smoke path;
* an already-decided ``scope_tier`` (an operator ``--scope`` pin, or a resume
  re-entry) short-circuits the node with no write and no model call;
* ``scope_directive`` is EMPTY at standard tier, so a STANDARD run's prompts are
  byte-identical to a pre-tiering run; a SMALL run's PM prompt is EMPTY too
  (criteria uncapped) and only its architect prompt changes;
* the demo directives preserve the floor the deterministic gates enforce (the
  per-criterion oracle hint, one security/abuse criterion, the manifest
  criterion, and a non-empty ownership map);
* the SMALL directive caps DECOMPOSITION only (fewer units / components) and
  never the criteria, interfaces, or gate coverage;
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
SMALL_PAYLOAD: dict[str, Any] = {
    "tier": "small",
    "rationale": "a real but modest single-purpose app",
    "signals": ["expense tracker"],
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
    return ScopeDecision(tier=tier, rationale="pinned", seq=_seq())


# ---- the one deterministic short-circuit ------------------------------------


def test_supplied_design_spec_forces_standard_without_a_model_call() -> None:
    rt = runtime_for()
    writes = make_scope_judge_node(rt)(
        {"request": "a simple demo shortener", "spec": "# Design\nDetailed doc."},
        config_for("r"),
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD
    assert "design spec" in writes["scope_tier"].rationale
    assert "gates" not in writes  # a product node never mints a verdict (L5)


# ---- the model decides everything else --------------------------------------


def test_every_other_request_reaches_the_model_and_demo_is_honored() -> None:
    rt = runtime_for(DEMO_PAYLOAD)
    writes = make_scope_judge_node(rt)({"request": "a demo URL shortener"}, config_for("r"))

    decision = writes["scope_tier"]
    assert decision.tier is ScopeTier.DEMO
    assert decision.signals == ("demo",)


def test_a_plain_request_with_no_smallness_word_still_reaches_the_model() -> None:
    # No keyword prefilter: the judge sees every request that has no design spec,
    # so wordings a keyword list would miss ("proof of concept") stay reachable.
    rt = runtime_for(DEMO_PAYLOAD)
    writes = make_scope_judge_node(rt)(
        {"request": "a proof of concept for a link redirector"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.DEMO


def test_model_may_overrule_the_wording_and_return_standard() -> None:
    rt = runtime_for(STANDARD_PAYLOAD)
    writes = make_scope_judge_node(rt)(
        {"request": "a simple ledger for production payroll"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_unrecognized_tier_degrades_to_standard() -> None:
    # Fail-to-full-scope: only the exact strings "demo"/"small" cap the build.
    rt = runtime_for({"tier": "DEMO-ish", "rationale": "?"})
    writes = make_scope_judge_node(rt)({"request": "a demo shortener"}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_a_clear_small_app_request_is_classified_small() -> None:
    rt = runtime_for(SMALL_PAYLOAD)
    writes = make_scope_judge_node(rt)(
        {"request": "a personal expense tracker for one user"}, config_for("r")
    )

    decision = writes["scope_tier"]
    assert decision.tier is ScopeTier.SMALL
    assert decision.signals == ("expense tracker",)


def test_a_toy_request_is_classified_demo() -> None:
    rt = runtime_for(DEMO_PAYLOAD)
    writes = make_scope_judge_node(rt)({"request": "a toy URL shortener demo"}, config_for("r"))

    assert writes["scope_tier"].tier is ScopeTier.DEMO


def test_a_large_multi_service_request_is_classified_standard() -> None:
    rt = runtime_for(STANDARD_PAYLOAD)
    writes = make_scope_judge_node(rt)(
        {"request": "a multi-tenant billing platform across several services"}, config_for("r")
    )

    assert writes["scope_tier"].tier is ScopeTier.STANDARD


def test_an_ambiguous_tie_degrades_to_standard() -> None:
    # A blank / undecided verdict is a tie, and every tie resolves conservatively
    # to STANDARD — a wrong down-tier is not self-correcting within the run.
    rt = runtime_for({"tier": "", "rationale": "could go either way"})
    writes = make_scope_judge_node(rt)({"request": "a link app"}, config_for("r"))

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


def test_small_pm_directive_is_byte_identical_to_standard() -> None:
    # SMALL caps decomposition ONLY — criteria are uncapped, so the PM prompt at
    # SMALL must be the empty string, exactly as STANDARD.
    assert scope_directive({"scope_tier": _pinned(ScopeTier.SMALL)}) == ""
    assert scope_directive({"scope_tier": _pinned(ScopeTier.SMALL)}) == scope_directive(
        {"scope_tier": _pinned(ScopeTier.STANDARD)}
    )


def test_small_architect_directive_caps_decomposition_only() -> None:
    directive = scope_directive({"scope_tier": _pinned(ScopeTier.SMALL)}, architect=True)

    # Caps fan-out: fewer components and a modest ownership-unit ceiling, but the
    # ownership floor of one is still load-bearing (an empty map fails the build gate).
    assert "AT MOST 3 components" in directive
    assert "BETWEEN 1 AND 5 ownership units" in directive
    assert "AT LEAST ONE owner" in directive
    assert "EMPTY ownership map is invalid" in directive
    # Decomposition ONLY: it must NOT cap criteria and must NOT forbid interfaces
    # the way DEMO does — the quality bar and gate coverage are preserved. The
    # DEMO criteria cap ("AT MOST N acceptance criteria total") and interface ban
    # ("EMPTY interfaces array") must both be ABSENT.
    assert "acceptance criteria total" not in directive
    assert "EMPTY interfaces array" not in directive
    assert "do not reduce acceptance criteria" in directive.lower()


def test_small_leaves_demo_directives_byte_identical() -> None:
    # Adding SMALL must not perturb DEMO: a DEMO run stays exactly as it was.
    from foundry.agents._common import (
        _DEMO_ARCHITECT_DIRECTIVE,
        _DEMO_PM_DIRECTIVE,
    )

    assert scope_directive({"scope_tier": _pinned(ScopeTier.DEMO)}) == _DEMO_PM_DIRECTIVE
    assert (
        scope_directive({"scope_tier": _pinned(ScopeTier.DEMO)}, architect=True)
        == _DEMO_ARCHITECT_DIRECTIVE
    )


# ---- trust posture -----------------------------------------------------------


def test_scope_tiering_never_relaxes_integrity() -> None:
    assert _pinned(ScopeTier.DEMO).relaxes_integrity() is False
    assert _pinned(ScopeTier.SMALL).relaxes_integrity() is False
    assert _pinned(ScopeTier.STANDARD).relaxes_integrity() is False


def test_every_model_calling_node_is_scripted_for_the_mock_backend() -> None:
    """The mock backend serves `default_text=""` for an unscripted node, which
    fails structured-output parsing and dies as ProxyUnavailable after retries.
    The scope judge shipped unscripted and crashed `--backend mock` on any
    request the (since-removed) keyword prefilter let through — including the
    README's own example. Assert the SOP marker the script is keyed on."""
    from foundry.agents.product.scope_judge import SOP
    from foundry.cli import _SCRIPTS

    matched = [key for key in _SCRIPTS if key in SOP]
    assert matched, f"the Scope Judge SOP matches no _SCRIPTS key; it would serve '' — {SOP[:60]!r}"
    assert json.loads(_SCRIPTS[matched[0]])["tier"] in ("demo", "standard")


def test_a_revision_pass_drops_the_scope_cap() -> None:
    """A revision pass exists only to close a gate-BLOCKING criteria omission, so
    its "reproduce every criterion AND add" instruction must not be handed to the
    model alongside a "at most 5 criteria" cap. Closing the gap wins."""
    from foundry.agents.product.product_manager import _revision_directive

    # No critique on the channel => no revision pass => the cap applies.
    rt = runtime_for()
    state = {"request": "a demo shortener", "scope_tier": _pinned(ScopeTier.DEMO)}
    assert _revision_directive(state, rt) == ""
    assert scope_directive(state) != ""


def test_scope_decision_round_trips_the_checkpoint_serde() -> None:
    # A type missing from the allowlist degrades to a dict on resume, which would
    # silently revert a resumed run to STANDARD prompts mid-build.
    from foundry.durability.serde import FOUNDRY_MSGPACK_MODULES

    assert ("foundry.schemas.governance", "ScopeDecision") in FOUNDRY_MSGPACK_MODULES
    assert ("foundry.schemas.governance", "ScopeTier") in FOUNDRY_MSGPACK_MODULES
