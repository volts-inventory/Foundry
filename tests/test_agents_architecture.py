"""Architecture-layer agent nodes (T-M1-05) — principal_architect + conditionals.

Drives the REAL Layer-2 node factories over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` (no live API key, LEARNINGS D1). Asserts:

* ``make_principal_architect_node`` writes a SystemDesign / ADR[] /
  InterfaceContract[] / OwnershipMap onto the ``artifacts`` channel and the
  PinnedConstraint records onto the ``pinned_constraints`` channel (the verbatim
  downstream path, architecture.md §8);
* ADRs carry ``rejected_alternatives`` and an OwnershipMap artifact is present;
* the architect retrieves through the context retriever (SymbolIndex) rather
  than reading files ad hoc;
* the conditional ``domain_specialist`` / ``ux_design`` nodes enter only when
  ``required_capabilities`` match (predicate + node guard agree) and are a
  no-op otherwise (``{}``);
* a factory-built node drops straight into a ``StateGraph(FactoryState)``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents.architecture import (
    make_domain_specialist_node,
    make_principal_architect_node,
    make_ux_design_node,
    should_enter_domain_specialist,
    should_enter_ux_design,
)
from foundry.agents.architecture.conditional import (
    DOMAIN_SPECIALIST_NODE_ID,
    UX_DESIGN_NODE_ID,
)
from foundry.agents.architecture.principal_architect import NODE_ID as ARCHITECT_NODE_ID
from foundry.context import SymbolIndex
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import ConstraintScope, PinnedConstraint
from foundry.schemas.base import Artifact
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-architecture-key"
KEY_ID = "dev-key-architecture"

# A representative architect payload on the constrained design channel: a
# SystemDesign, ADRs (with rejected alternatives), interface contracts, an
# ownership map, and pinned constraints (one global cross-cutting invariant).
DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Todo service",
    "design_body": "layered: api -> store; sqlite persistence",
    "components": ["api", "store"],
    "adrs": [
        {
            "id": "ADR-001",
            "title": "Use SQLite",
            "decision": "Persist todos in SQLite",
            "body": "single-node, embedded; no ops overhead",
            "rejected_alternatives": ["Postgres", "in-memory dict"],
        },
        {
            "id": "ADR-002",
            "title": "Synchronous API",
            "decision": "Expose a synchronous REST API",
            "body": "simplicity over throughput at this scale",
            "rejected_alternatives": ["async/event-driven"],
        },
    ],
    "interfaces": [
        {"name": "TodoStore.add", "spec": "add(todo) -> id", "consumers": ["api"]},
        {"name": "TodoStore.list", "spec": "list() -> [todo]", "consumers": ["api"]},
    ],
    "ownership": {"todo/api.py": "task-api", "todo/store.py": "task-store"},
    "pinned_constraints": [
        {
            "id": "PC-001",
            "text": "All access tokens must be redacted from logs.",
            "scope": "global",
            "source": "ADR-001",
        },
        {
            "id": "PC-002",
            "text": "Todo titles are limited to 200 characters.",
            "scope": "local",
            "source": "ADR-002",
        },
    ],
}


def design_transport(n: int = 8) -> MockTransport:
    """A transport that serves the architect's design payload.

    The architect calls ``complete(..., schema=_DESIGN_SCHEMA, purpose="ideation")``.
    That is a schema-constrained IDEATION call, NOT a verdict (AC3 is resolved —
    MockTransport now routes to the verdict queue only when the schema IS the
    verdict schema, DT1). So the structured design payload rides the natural
    free-text completion path: the ``completions=`` FIFO queue returns each item
    verbatim as ``text``, which the proxy ``json.loads`` under the design schema.
    The queue is sized for the number of architect calls a test makes.
    """
    return MockTransport(completions=[json.dumps(DESIGN_PAYLOAD)] * n)


def make_runtime(transport: MockTransport | None = None) -> AgentRuntime:
    proxy = ModelProviderProxy(
        transport or design_transport(),
        signer=ZoneBSigner(KEY, KEY_ID),
    )
    return AgentRuntime(proxy=proxy)


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _artifacts_of(writes: dict[str, Any]) -> list[Artifact]:
    return list(writes.get("artifacts", []))


def _kinds(artifacts: list[Artifact]) -> set[str]:
    return {a.kind for a in artifacts}


# ---- 1. principal_architect produces the full artifact set -------------------


def test_architect_writes_design_adrs_interfaces_ownership_and_constraints() -> None:
    rt = make_runtime()
    node = make_principal_architect_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    artifacts = _artifacts_of(writes)
    kinds = _kinds(artifacts)
    assert "system_design" in kinds
    assert "ownership_map" in kinds  # OwnershipMap present
    assert sum(1 for a in artifacts if a.kind == "adr") == 2
    assert sum(1 for a in artifacts if a.kind == "interface_contract") == 2
    # Every artifact is a content-addressed handle (bodies out-of-band, L4).
    assert all(a.uri.startswith("cas://") for a in artifacts)
    assert all(a.produced_by == ARCHITECT_NODE_ID for a in artifacts)
    # CR4: the two ADRs have different id/title, so their handles — minted off
    # the record's content_identity(), not the body digest — are DISTINCT and
    # neither is dropped under merge_by_uri.
    adr_uris = [a.uri for a in artifacts if a.kind == "adr"]
    assert len(set(adr_uris)) == 2


def test_pinned_constraints_flow_to_their_own_channel() -> None:
    rt = make_runtime()
    node = make_principal_architect_node(rt)
    writes = node({"request": "build a todo app"}, config_for("run-A"))

    constraints = writes["pinned_constraints"]
    assert all(isinstance(c, PinnedConstraint) for c in constraints)
    ids = {c.id for c in constraints}
    assert ids == {"PC-001", "PC-002"}
    by_id = {c.id: c for c in constraints}
    # Verbatim text + scope are preserved for the downstream injector.
    assert by_id["PC-001"].scope is ConstraintScope.GLOBAL
    assert "redacted from logs" in by_id["PC-001"].text
    assert by_id["PC-002"].scope is ConstraintScope.LOCAL
    # Constraints ride pinned_constraints, NOT artifacts.
    assert "pinned_constraints" not in _kinds(_artifacts_of(writes))


def test_adrs_carry_rejected_alternatives() -> None:
    rt = make_runtime()
    # ADRs are emitted as artifact handles; assert the modeled ADR list by
    # exercising the same payload through the node and checking the design's
    # adr coverage, then verify rejected_alternatives via the builder result.
    node = make_principal_architect_node(rt)
    writes = node({"request": "x"}, config_for("run-A"))
    adr_artifacts = [a for a in _artifacts_of(writes) if a.kind == "adr"]
    assert len(adr_artifacts) == 2
    # rejected_alternatives are part of the ADR record (kept in state, not the
    # body handle): rebuild from the payload to confirm they are not dropped.
    from foundry.agents.architecture.principal_architect import _build_adrs

    seq = rt.seq(config_for("run-A"), ARCHITECT_NODE_ID, "artifact:adrs")
    adrs = _build_adrs(DESIGN_PAYLOAD["adrs"], seq)
    assert adrs[0].rejected_alternatives == ("Postgres", "in-memory dict")
    assert adrs[1].rejected_alternatives == ("async/event-driven",)
    assert all(a.status == "accepted" for a in adrs)


def test_constraint_seqs_are_distinct_per_record() -> None:
    rt = make_runtime()
    node = make_principal_architect_node(rt)
    writes = node({"request": "x"}, config_for("run-A"))
    seqs = [c.seq for c in writes["pinned_constraints"]]
    assert len(set(seqs)) == len(seqs)  # local index disambiguates within key


# ---- 2. retrieval goes through the context retriever -------------------------


def test_architect_retrieves_through_symbol_index(tmp_path: Any) -> None:
    """When state carries capability symbols and an index is provided, the
    architect retrieves spans through the SymbolIndex (impact analysis), not by
    reading files ad hoc — the retrieved spans ride into the model prompt."""
    src = tmp_path / "mod.py"
    src.write_text(
        "def TodoStore():\n    return persist()\n\n\ndef persist():\n    return 1\n",
        encoding="utf-8",
    )
    index = SymbolIndex.build(tmp_path)
    transport = design_transport()
    rt = ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))
    node = make_principal_architect_node(AgentRuntime(proxy=rt), index)

    writes = node(
        {"request": "extend todos", "required_capabilities": ["TodoStore"]},
        config_for("run-A"),
    )
    # The node still produced its artifacts...
    assert "system_design" in _kinds(_artifacts_of(writes))
    # ...and the retrieved spans were threaded into the outgoing model prompt.
    sent = json.dumps(transport.requests[-1])
    assert "retrieved_spans" in sent
    assert "TodoStore" in sent


def test_architect_greenfield_no_index_still_produces_design() -> None:
    rt = make_runtime()
    node = make_principal_architect_node(rt)  # index=None (greenfield)
    writes = node({"request": "fresh project"}, config_for("run-A"))
    assert "system_design" in _kinds(_artifacts_of(writes))


# ---- 3. conditional entry predicates ----------------------------------------


def test_domain_specialist_predicate_matches_only_domain_caps() -> None:
    assert should_enter_domain_specialist({"required_capabilities": ["regulated"]})
    assert should_enter_domain_specialist({"required_capabilities": ["ml", "ui"]})
    assert not should_enter_domain_specialist({"required_capabilities": ["ui"]})
    assert not should_enter_domain_specialist({})


def test_ux_predicate_matches_only_ui_caps() -> None:
    assert should_enter_ux_design({"required_capabilities": ["frontend"]})
    assert should_enter_ux_design({"required_capabilities": ["ui"]})
    assert not should_enter_ux_design({"required_capabilities": ["regulated"]})
    assert not should_enter_ux_design({})


def test_domain_specialist_node_is_noop_without_matching_capability() -> None:
    rt = make_runtime()
    node = make_domain_specialist_node(rt)
    assert node({"required_capabilities": ["ui"]}, config_for("run-A")) == {}
    assert node({}, config_for("run-A")) == {}


def test_domain_specialist_node_contributes_on_match() -> None:
    rt = make_runtime()
    node = make_domain_specialist_node(rt)
    writes = node({"required_capabilities": ["regulated"]}, config_for("run-A"))
    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"domain_addendum"}
    assert artifacts[0].produced_by == DOMAIN_SPECIALIST_NODE_ID


def test_ux_node_is_noop_without_ui_capability() -> None:
    rt = make_runtime()
    node = make_ux_design_node(rt)
    assert node({"required_capabilities": ["regulated"]}, config_for("run-A")) == {}


def test_ux_node_contributes_on_match() -> None:
    rt = make_runtime()
    node = make_ux_design_node(rt)
    writes = node({"required_capabilities": ["frontend"]}, config_for("run-A"))
    artifacts = _artifacts_of(writes)
    assert _kinds(artifacts) == {"ux_spec"}
    assert artifacts[0].produced_by == UX_DESIGN_NODE_ID


# ---- 4. graph-compatibility --------------------------------------------------


def test_architect_node_runs_inside_a_state_graph() -> None:
    rt = make_runtime()
    g = StateGraph(FactoryState)
    g.add_node("architect", make_principal_architect_node(rt))
    g.add_edge(START, "architect")
    g.add_edge("architect", END)
    app = g.compile()

    final: FactoryState = app.invoke(
        {"request": "build a todo app"}, config_for("run-A")
    )
    kinds = {a.kind for a in final["artifacts"]}
    assert "system_design" in kinds
    assert "ownership_map" in kinds
    constraint_ids = {c.id for c in final["pinned_constraints"]}
    assert constraint_ids == {"PC-001", "PC-002"}


def test_conditional_nodes_run_inside_a_state_graph() -> None:
    rt = make_runtime()
    g = StateGraph(FactoryState)
    g.add_node("domain", make_domain_specialist_node(rt))
    g.add_node("ux", make_ux_design_node(rt))
    g.add_edge(START, "domain")
    g.add_edge("domain", "ux")
    g.add_edge("ux", END)
    app = g.compile()

    # UI + domain caps: both contribute.
    final: FactoryState = app.invoke(
        {"request": "regulated UI app", "required_capabilities": ["regulated", "ui"]},
        config_for("run-A"),
    )
    assert {a.kind for a in final["artifacts"]} == {"domain_addendum", "ux_spec"}

    # No matching caps: both no-op, no artifacts written.
    final2: FactoryState = app.invoke(
        {"request": "plain backend"}, config_for("run-B")
    )
    assert not final2.get("artifacts")
