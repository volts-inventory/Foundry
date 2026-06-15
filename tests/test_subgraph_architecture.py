"""Architecture (Layer 2) role subgraph (T-M1-09).

Compiles the REAL ``build_architecture_subgraph`` standalone and runs it over a
REAL :class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API
key, LEARNINGS D1). The architect's structured design payload rides the natural
free-text completion path (``completions=``) — a schema-constrained IDEATION
call is NOT the verdict channel (AC3 resolved, DT1), so the payload is returned
verbatim as ``text`` and ``json.loads``'d under the design schema.

Asserts BOTH conditional paths:

* (a) ``required_capabilities`` empty -> the conditional router sends the run
  straight to END; only the Principal Architect's artifacts +
  ``pinned_constraints`` are produced (no domain_addendum / ux_spec);
* (b) ``required_capabilities`` includes a matching cap ("regulated" / "frontend")
  -> the conditional node is entered and contributes its addendum artifact, in
  addition to the architect's full output.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.subgraphs.architecture import build_architecture_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.state.factory_state import FactoryState

KEY = b"dev-zone-b-architecture-subgraph-key"
KEY_ID = "dev-key-architecture-subgraph"

# The architect's structured design payload on the constrained design channel:
# a SystemDesign, ADRs (with rejected alternatives), interface contracts, an
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
    ],
    "interfaces": [
        {"name": "TodoStore.add", "spec": "add(todo) -> id", "consumers": ["api"]},
    ],
    "ownership": {"todo/api.py": "task-api"},
    "pinned_constraints": [
        {
            "id": "PC-001",
            "text": "All access tokens must be redacted from logs.",
            "scope": "global",
            "source": "ADR-001",
        },
    ],
}


def _runtime() -> AgentRuntime:
    # One design payload per architect call (the subgraph invokes it once per run).
    transport = MockTransport(completions=[json.dumps(DESIGN_PAYLOAD)] * 4)
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy)


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _kinds(state: FactoryState) -> set[str]:
    return {a.kind for a in state.get("artifacts", [])}


# ---- (a) no matching capabilities -> conditionals skipped --------------------


def test_no_capabilities_skips_conditionals() -> None:
    app = build_architecture_subgraph(_runtime()).compile()

    final: FactoryState = app.invoke(
        {"request": "build a plain backend todo app"}, _config("run-a")
    )

    kinds = _kinds(final)
    # Architect produced its full artifact set...
    assert "system_design" in kinds
    assert "ownership_map" in kinds
    assert "adr" in kinds
    assert "interface_contract" in kinds
    # ...and pinned constraints landed on their own channel, verbatim.
    constraint_ids = {c.id for c in final["pinned_constraints"]}
    assert constraint_ids == {"PC-001"}
    # The conditional Layer-2 nodes were NOT entered (no addendum / ux_spec).
    assert "domain_addendum" not in kinds
    assert "ux_spec" not in kinds


# ---- (b) a matching capability -> the conditional node IS entered ------------


def test_domain_capability_enters_domain_specialist() -> None:
    app = build_architecture_subgraph(_runtime()).compile()

    final: FactoryState = app.invoke(
        {"request": "regulated todo app", "required_capabilities": ["regulated"]},
        _config("run-b"),
    )

    kinds = _kinds(final)
    assert "system_design" in kinds  # architect still ran
    assert "domain_addendum" in kinds  # domain_specialist was entered
    assert "ux_spec" not in kinds  # ux not triggered by a domain cap


def test_ui_capability_enters_ux_design() -> None:
    app = build_architecture_subgraph(_runtime()).compile()

    final: FactoryState = app.invoke(
        {"request": "todo UI", "required_capabilities": ["frontend"]},
        _config("run-c"),
    )

    kinds = _kinds(final)
    assert "system_design" in kinds
    assert "ux_spec" in kinds  # ux_design was entered
    assert "domain_addendum" not in kinds


def test_both_capabilities_enter_both_conditionals() -> None:
    app = build_architecture_subgraph(_runtime()).compile()

    final: FactoryState = app.invoke(
        {
            "request": "regulated todo UI",
            "required_capabilities": ["regulated", "ui"],
        },
        _config("run-d"),
    )

    kinds = _kinds(final)
    assert {"system_design", "domain_addendum", "ux_spec"} <= kinds
