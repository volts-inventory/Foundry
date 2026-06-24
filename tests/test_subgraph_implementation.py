"""Implementation (Layer 3) role subgraph (T-M1-10).

Compiles the REAL ``build_implementation_subgraph`` standalone and runs it over a
REAL :class:`ModelProviderProxy` backed by :class:`MockTransport` (no live API
key, LEARNINGS D1) and a real :class:`StubAdapter` as ``rt.adapter`` (no
``claude -p`` binary, D3). The Senior's structured decomposition payload rides
the natural free-text completion path (``completions=``) — a schema-constrained
IDEATION call is NOT the verdict channel (AC3 resolved, DT1).

State is seeded as the architecture layer would leave it: a SystemDesign +
OwnershipMap as content-addressed Artifact HANDLES on ``artifacts`` (bodies
out-of-band, reconstituted by the injected ``resolve``) and PinnedConstraint
records on the SEPARATE ``pinned_constraints`` channel. Asserts:

* >=1 FeatureDiff (``cas://`` handle) reaches ``artifacts`` (senior -> junior);
* branch-per-task is preserved (one diff per ownership unit / task);
* idempotent re-run yields the SAME feature_diff key (L4/L7, IM1).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.implementation.senior_engineer import DESIGN_KIND, OWNERSHIP_KIND
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates.attestation import ZoneBSigner
from foundry.graph.subgraphs.implementation import build_implementation_subgraph
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import (
    ConstraintScope,
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.base import Artifact, FrozenModel
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq

KEY = b"dev-zone-b-impl-subgraph-key"
KEY_ID = "dev-key-impl-subgraph"


def _seq(node: str = "principal_architect", epoch: int = 1) -> Seq:
    return Seq("run", epoch, 1, node, 0)


def _design() -> SystemDesign:
    return SystemDesign(
        title="Todo service",
        design_uri="cas://design/abc",
        components=("store", "api"),
        interface_ids=("TodoStore.add",),
        seq=_seq(),
    )


def _ownership() -> OwnershipMap:
    return OwnershipMap(
        owners={"src/api.py": "owner-b", "src/store.py": "owner-a"},
        seq=_seq(),
    )


def _constraints() -> list[PinnedConstraint]:
    return [
        PinnedConstraint(
            id="c1",
            text="MUST store money as integer cents, never floats.",
            scope=ConstraintScope.GLOBAL,
            source="ADR-1",
            seq=_seq(),
        ),
    ]


# uri -> body map the injected resolve reconstitutes from (CAS in prod).
_BODIES: dict[str, FrozenModel] = {}


def _seed_state() -> FactoryState:
    """State as the architecture layer would leave it: handles + constraints."""
    design, ownership = _design(), _ownership()
    handles = [
        emit_artifact(design, DESIGN_KIND, "principal_architect", _seq()),
        emit_artifact(ownership, OWNERSHIP_KIND, "principal_architect", _seq()),
    ]
    _BODIES[handles[0].uri] = design
    _BODIES[handles[1].uri] = ownership
    return {
        "request": "build a todo service",
        "artifacts": handles,
        "pinned_constraints": _constraints(),
    }


def _resolve(handle: Artifact) -> FrozenModel:
    return _BODIES[handle.uri]


def _decomposition_payload() -> str:
    # One task per ownership unit, with criteria traceability.
    return json.dumps(
        {
            "tasks": [
                {"unit": "src/store.py", "title": "build store", "criterion_ids": ["AC-1"]},
                {"unit": "src/api.py", "title": "build api", "criterion_ids": ["AC-2"]},
            ]
        }
    )


def _runtime() -> AgentRuntime:
    # One decomposition call PER Tech-Lead now (Wave B fan-out): serve the full
    # scripted payload on every call (default_text covers the 2nd..Nth lead) —
    # each lead extracts the criteria for its own ownership units.
    body = _decomposition_payload()
    transport = MockTransport(completions=[body], default_text=body)
    proxy = ModelProviderProxy(transport, signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=StubAdapter())


def _config(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def _feature_diffs(state: FactoryState) -> list[Artifact]:
    return [a for a in state.get("artifacts", []) if a.kind == "feature_diff"]


# ---- tests -------------------------------------------------------------------


def test_subgraph_produces_feature_diffs(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    app = build_implementation_subgraph(
        _runtime(), source_dir=str(tmp_path), resolve=_resolve
    ).compile()

    final: FactoryState = app.invoke(_seed_state(), _config("run-a"))

    diffs = _feature_diffs(final)
    # >=1 FeatureDiff reaches artifacts, each a cas:// handle (DS9).
    assert len(diffs) >= 1
    for art in diffs:
        assert art.uri.startswith("cas://feature_diff/")
        assert art.produced_by == "junior_engineer"


def test_subgraph_preserves_branch_per_task(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    app = build_implementation_subgraph(
        _runtime(), source_dir=str(tmp_path), resolve=_resolve
    ).compile()

    final: FactoryState = app.invoke(_seed_state(), _config("run-b"))

    diffs = _feature_diffs(final)
    # One diff per ownership unit (2 units -> 2 tasks -> 2 branches).
    task_ids = {a.task_id for a in diffs}
    assert len(task_ids) == 2  # branch-per-task preserved
    # Each FeatureDiff is a distinct cas:// handle (distinct branch identity).
    assert len({a.uri for a in diffs}) == 2
    # The Senior decomposed the same set of task ids onto the tasks channel
    # (within one run the Senior's plain Task dict wins the keyed union, IM3,
    # so the done-marker's diff_uri does not survive here — the diff lives on
    # the artifacts channel instead).
    senior_task_ids = {t["id"] for t in final["tasks"]}
    assert task_ids == senior_task_ids
    for t in final["tasks"]:
        assert t["branch"] == f"task/{t['id']}"


def test_subgraph_idempotent_rerun_same_key(tmp_path: Any) -> None:
    # Source tree and the durable store live in SEPARATE dirs so the store is
    # never cloned into the worktree (which would change the base snapshot).
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    store = str(tmp_path / "store")  # DURABLE store survives destroy/rebuild (IM1)

    def _build() -> Any:
        return build_implementation_subgraph(
            _runtime(),
            source_dir=str(src),
            resolve=_resolve,
            snapshot_store=store,
        ).compile(checkpointer=MemorySaver())

    cfg = _config("run-c")
    first: FactoryState = _build().invoke(_seed_state(), cfg)
    second: FactoryState = _build().invoke(_seed_state(), cfg)

    keys1 = sorted(a.uri for a in _feature_diffs(first))
    keys2 = sorted(a.uri for a in _feature_diffs(second))
    # Process-stable idempotency keys (content_digest, L7): re-run -> same uris.
    assert keys1 == keys2
    assert keys1  # and there was at least one
