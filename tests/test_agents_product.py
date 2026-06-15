"""Product-layer agent nodes (T-M1-04) — the Layer-1 node factories.

Drives the REAL Layer-1 node factories over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` (no live API key, LEARNINGS D1). Asserts:

* ``make_stakeholder_proxy_node`` -> ``IntentBrief`` handle on ``artifacts``;
* ``make_product_manager_node`` -> a ``PRD`` handle + one
  ``acceptance_criterion`` handle per criterion, with ``required_capabilities``
  flowing to their own channel, and the PRD declaring its ``criterion_ids``;
* acceptance criteria carry their traceability fields (``criterion_id`` /
  ``oracle_hint`` / ``security_abuse``);
* ``make_criteria_completeness_critic_node`` -> a ``criteria_findings`` handle +
  an advisory ``GateTriage`` on the ``spec`` gate, which is marked BLOCKING when
  a security/abuse omission is present and NON-blocking otherwise — and the
  critic NEVER writes a ``GateResult`` to the ``gates`` channel (LEARNINGS L5);
* ``make_eng_program_mgr_node`` -> a ``SprintPlan`` handle with a per-route
  budget cap (integer micro-USD, clamped into the route envelope);
* a factory-built node drops straight into a ``StateGraph(FactoryState)`` and
  the four nodes run in SOP order together.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents.product import (
    make_criteria_completeness_critic_node,
    make_eng_program_mgr_node,
    make_product_manager_node,
    make_stakeholder_proxy_node,
)
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-product-key"
KEY_ID = "dev-key-product"

# ---- representative payloads per node (the constrained content channels) -----

INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Let users manage a personal todo list.",
    "assumptions": ["single user", "web client"],
    "unknowns": ["auth provider"],
    "success_criteria": ["a user can add and complete a todo"],
    "must_haves": ["add todo", "complete todo"],
    "nice_to_haves": ["due dates"],
}

PRD_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "prd_body": "Users add, list, and complete todos via a REST API.",
    "required_capabilities": ["ui"],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given a user, When they add a todo, Then it appears in the list.",
            "criticality": "normal",
            "oracle_hint": "POST /todos then GET /todos returns the new item",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits /todos, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "request without token returns 401; negative oracle",
            "security_abuse": True,
        },
    ],
}

# A critique with NO security/abuse omission (clean).
CRITIQUE_CLEAN: dict[str, Any] = {
    "missing": ["a criterion for deleting a todo"],
    "security_abuse_omissions": [],
}

# A critique that flags an omitted security/abuse case (Spec-gate blocking).
CRITIQUE_SECURITY_OMISSION: dict[str, Any] = {
    "missing": [],
    "security_abuse_omissions": [
        "no criterion covers rate-limiting / abuse of the add endpoint"
    ],
}

SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1 storage", "M2 API", "M3 UI"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "risk: auth provider undecided",
    "done_definition": "all acceptance criteria green on the integrated tree",
}


def product_transport(critique: dict[str, Any] = CRITIQUE_CLEAN) -> MockTransport:
    """Serve each node's payload via the free-text completion queue.

    Every product node passes ``schema=`` to ``proxy.complete`` with
    ``purpose="ideation"`` — exploratory authoring, NOT a verdict (PR1). AC3 is
    resolved (DT1): MockTransport routes to the verdict queue only when the
    schema IS the verdict schema, so these schema-constrained ideation calls take
    the natural ``completions=`` path and each FIFO item is returned verbatim as
    ``text`` (the proxy ``json.loads`` it under the node's content schema). The
    four product nodes run in SOP order, so the queue order matches that order.
    """
    return MockTransport(
        completions=[
            json.dumps(INTENT_PAYLOAD),
            json.dumps(PRD_PAYLOAD),
            json.dumps(critique),
            json.dumps(SPRINT_PAYLOAD),
        ]
    )


def make_runtime(transport: MockTransport | None = None) -> AgentRuntime:
    proxy = ModelProviderProxy(
        transport or product_transport(),
        signer=ZoneBSigner(KEY, KEY_ID),
    )
    return AgentRuntime(proxy=proxy)


def runtime_for(*payloads: dict[str, Any]) -> AgentRuntime:
    """A runtime whose free-text completion queue yields ``payloads`` in order —
    used to drive a single node (or a short sequence) in isolation. These are
    schema-constrained IDEATION calls, not verdicts (AC3 resolved, DT1)."""
    return make_runtime(MockTransport(completions=[json.dumps(p) for p in payloads]))


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _artifacts_of(writes: dict[str, Any]) -> list[Artifact]:
    return list(writes.get("artifacts", []))


def _kinds(artifacts: list[Artifact]) -> set[str]:
    return {a.kind for a in artifacts}


# ---- 1. stakeholder proxy ----------------------------------------------------


def test_stakeholder_proxy_writes_intent_brief_handle() -> None:
    rt = runtime_for(INTENT_PAYLOAD)
    node = make_stakeholder_proxy_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"intent_brief"}
    assert all(a.uri.startswith("cas://") for a in artifacts)  # body out-of-band (L4)
    assert artifacts[0].produced_by == "stakeholder_proxy"
    # No verdict ever written by a product node (LEARNINGS L5).
    assert "gates" not in writes


# ---- 2. product manager: PRD + traceable acceptance criteria -----------------


def test_product_manager_writes_prd_and_criteria_handles() -> None:
    rt = runtime_for(PRD_PAYLOAD)
    node = make_product_manager_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"prd", "acceptance_criterion"}
    assert sum(1 for a in artifacts if a.kind == "acceptance_criterion") == 2
    assert all(a.uri.startswith("cas://") for a in artifacts)
    # required_capabilities flow to their own union channel (drives routing).
    assert writes["required_capabilities"] == ["ui"]
    assert "gates" not in writes


def test_acceptance_criteria_carry_traceability_fields() -> None:
    """criterion_id / oracle_hint / security_abuse are load-bearing downstream:
    rebuild the criteria from the payload and assert they survive intact, and
    that the PRD declares them by criterion_id."""
    from foundry.agents.product.product_manager import _build_criteria
    from foundry.schemas.product import Criticality

    seq = make_runtime().seq(config_for("run-A"), "product_manager", "artifact:acceptance_criteria")
    criteria = _build_criteria(PRD_PAYLOAD["acceptance_criteria"], seq)

    assert [c.id for c in criteria] == ["AC-001", "AC-002"]
    assert all(c.oracle_hint for c in criteria)  # every criterion has an oracle hint
    by_id = {c.id: c for c in criteria}
    assert by_id["AC-002"].security_abuse is True
    assert by_id["AC-002"].criticality is Criticality.HIGH_RISK
    assert by_id["AC-001"].security_abuse is False
    # distinct Seq per record (local index disambiguates within the key).
    assert len({c.seq for c in criteria}) == len(criteria)


def test_prd_declares_its_criterion_ids() -> None:
    """Assert on the PRD the node ACTUALLY emitted (not a hand-rebuilt one).

    The PRD body rides ``artifacts`` as a content-addressed handle whose ``uri``
    is the record's ``content_identity()``; ``criterion_ids`` is part of that
    record. We reconstruct the PRD the node MUST have built from PRD_PAYLOAD and
    confirm its handle matches the emitted ``prd`` handle — so if the node failed
    to derive ``criterion_ids`` from the criteria it declared, the handles would
    diverge and this fails.
    """
    from foundry.agents.product._common import cas_uri, emit_artifact
    from foundry.schemas.product import PRD
    from foundry.state.seq import Seq

    rt = runtime_for(PRD_PAYLOAD)
    node = make_product_manager_node(rt)
    writes = node({"request": "x"}, config_for("run-A", step=4))

    emitted = next(a for a in _artifacts_of(writes) if a.kind == "prd")
    # The node mints the PRD seq via rt.seq(..., "artifact:prd"): first bump for
    # that (run, node/key) is epoch 1; superstep is the config's langgraph_step.
    prd_seq = Seq("run-A", 1, 4, "product_manager", 0)
    expected = PRD(
        title=PRD_PAYLOAD["title"],
        prd_uri=cas_uri(PRD_PAYLOAD["prd_body"]),
        required_capabilities=("ui",),
        criterion_ids=("AC-001", "AC-002"),
        seq=prd_seq,
    )
    expected_handle = emit_artifact(expected, "prd", "product_manager", prd_seq)
    assert emitted.uri == expected_handle.uri
    assert expected.criterion_ids == ("AC-001", "AC-002")


# ---- 3. criteria critic: advisory triage, security-omission blocking ---------


def test_critic_writes_criteria_findings_and_triage_no_gate() -> None:
    rt = runtime_for(CRITIQUE_CLEAN)
    node = make_criteria_completeness_critic_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    assert _kinds(_artifacts_of(writes)) == {"criteria_findings"}
    triage = writes["gate_triage"]
    assert len(triage) == 1
    assert isinstance(triage[0], GateTriage)
    assert triage[0].name == "spec"
    assert triage[0].produced_by == "criteria_completeness_critic"
    # The critic NEVER mints a verdict — the gates channel is untouched (L5).
    assert "gates" not in writes


def test_critic_flags_security_omission_as_blocking() -> None:
    rt = runtime_for(CRITIQUE_SECURITY_OMISSION)
    node = make_criteria_completeness_critic_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    triage = writes["gate_triage"][0]
    assert "BLOCKING" in triage.explanation
    assert "rate-limiting" in triage.explanation
    assert "fails the Spec gate" in triage.explanation
    # Still advisory only — no GateResult.
    assert "gates" not in writes


def test_critic_clean_case_is_non_blocking() -> None:
    rt = runtime_for(CRITIQUE_CLEAN)
    node = make_criteria_completeness_critic_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    triage = writes["gate_triage"][0]
    assert "BLOCKING" not in triage.explanation


def test_critique_stamps_judgment_provenance() -> None:
    """The critique records the proxy profile openly (governance §1e)."""
    from foundry.agents.product.criteria_critic import _provenance
    from foundry.schemas.gates import JudgmentProvenance

    rt = make_runtime()  # default profile is self_judge
    assert _provenance(rt) is JudgmentProvenance.SELF


# ---- 4. eng/program manager: sprint plan + per-route budget cap --------------


def test_eng_program_mgr_writes_sprint_plan_handle() -> None:
    rt = runtime_for(SPRINT_PAYLOAD)
    node = make_eng_program_mgr_node(rt)
    writes = node(
        {"request": "build a todo app", "route": "greenfield"}, config_for("run-A")
    )

    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"sprint_plan"}
    assert artifacts[0].uri.startswith("cas://")
    assert artifacts[0].produced_by == "eng_program_mgr"
    assert "gates" not in writes


def test_sprint_cap_is_clamped_into_route_envelope() -> None:
    """Genuinely exercise the clamp by asserting on the EMITTED SprintPlan.

    SPRINT_PAYLOAD proposes a 30M cap, but the bugfix route envelope is 5M, so
    the node must clamp DOWN to 5M. The plan rides ``artifacts`` as a handle whose
    ``uri`` is the record's ``content_identity()`` (which includes the cap). We
    reconstruct the plan with the CLAMPED cap and confirm its handle matches the
    emitted one; a control reconstruction with the UNCLAMPED 30M cap must NOT —
    proving the clamp is actually running, not asserted on a hand-built object.
    """
    from foundry.agents.product._common import cas_uri, emit_artifact
    from foundry.schemas.product import SprintPlan
    from foundry.state.seq import Seq

    rt = runtime_for(SPRINT_PAYLOAD)
    node = make_eng_program_mgr_node(rt)
    writes = node({"request": "fix it", "route": "bugfix"}, config_for("run-A", step=7))
    emitted = _artifacts_of(writes)[0]
    assert emitted.kind == "sprint_plan"

    seq = Seq("run-A", 1, 7, "eng_program_mgr", 0)

    def _handle(cap: int) -> str:
        plan = SprintPlan(
            milestones=("M1 storage", "M2 API", "M3 UI"),
            usd_cap_microusd=cap,
            risk_register_uri=cas_uri(SPRINT_PAYLOAD["risk_register_body"]),
            done_definition=SPRINT_PAYLOAD["done_definition"],
            seq=seq,
        )
        assert isinstance(plan.usd_cap_microusd, int)
        return emit_artifact(plan, "sprint_plan", "eng_program_mgr", seq).uri

    assert emitted.uri == _handle(5_000_000)  # clamped to the bugfix envelope
    assert emitted.uri != _handle(30_000_000)  # the unclamped proposal would differ


# ---- 5. graph-compatibility: the four nodes run in SOP order -----------------


def test_product_layer_runs_inside_a_state_graph() -> None:
    rt = make_runtime()
    g = StateGraph(FactoryState)
    g.add_node("stakeholder_proxy", make_stakeholder_proxy_node(rt))
    g.add_node("product_manager", make_product_manager_node(rt))
    g.add_node("criteria_critic", make_criteria_completeness_critic_node(rt))
    g.add_node("eng_program_mgr", make_eng_program_mgr_node(rt))
    g.add_edge(START, "stakeholder_proxy")
    g.add_edge("stakeholder_proxy", "product_manager")
    g.add_edge("product_manager", "criteria_critic")
    g.add_edge("criteria_critic", "eng_program_mgr")
    g.add_edge("eng_program_mgr", END)
    app = g.compile()

    final: FactoryState = app.invoke(
        {"request": "build a todo app", "route": "greenfield"}, config_for("run-A")
    )
    kinds = {a.kind for a in final["artifacts"]}
    expected = {"intent_brief", "prd", "acceptance_criterion", "criteria_findings", "sprint_plan"}
    assert expected <= kinds
    # The critic's advisory rode the gate_triage channel; no verdict was minted.
    assert any(t.name == "spec" for t in final["gate_triage"])
    assert not final.get("gates")
    # required_capabilities propagated from the PM.
    assert final["required_capabilities"] == ["ui"]
