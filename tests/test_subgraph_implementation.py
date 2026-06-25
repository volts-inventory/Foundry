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

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.implementation.senior_engineer import (
    DESIGN_KIND,
    OWNERSHIP_KIND,
    task_id_for_unit,
)
from foundry.durability.artifact_store import ArtifactStore
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
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


# ---- Wave C: parallel IC pool (max_slots > 1) --------------------------------


class _ConcurrencyProbe:
    """Adapter that proves real parallelism DETERMINISTICALLY: a Barrier(parties)
    only releases once that many tasks are concurrently in-flight, so serial
    execution would time out (BrokenBarrierError) instead of silently passing."""

    supports_turn_snapshots = True
    name = "concurrency-probe"

    def __init__(self, parties: int = 2) -> None:
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(parties, timeout=5.0)
        self.active = 0
        self.max_active = 0

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self._barrier.wait()  # blocks until `parties` runs overlap (or times out)
            tid = str(packet.task["id"])
            (Path(sandbox.worktree) / "out.txt").write_text(tid)
            diff_uri = sandbox.put_blob(f"diff:{tid}".encode())
            return AdapterResult(diff_uri=diff_uri, snapshot_digest=sandbox.snapshot())
        finally:
            with self._lock:
                self.active -= 1


class _ConstantDiffAdapter:
    """Every task writes IDENTICAL worktree content + diff bytes -> identical
    snapshot AND blob digests, so concurrent workers collide on the shared
    content-addressed store. Exercises the Wave C parallel-write race directly."""

    supports_turn_snapshots = True
    name = "constant-diff"

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        (Path(sandbox.worktree) / "same.txt").write_text("constant")
        diff_uri = sandbox.put_blob(b"identical-diff")
        return AdapterResult(diff_uri=diff_uri, snapshot_digest=sandbox.snapshot())


def test_pool_parallel_output_matches_serial(tmp_path: Any) -> None:
    """max_slots>1 must produce the SAME feature diffs (task_id + idempotency uri)
    as the serial path — the IC pool changes execution order, never the result."""
    (tmp_path / "README.md").write_text("seed\n")

    def run(max_slots: int, run_id: str) -> set[tuple[str | None, str]]:
        app = build_implementation_subgraph(
            _runtime(), source_dir=str(tmp_path), resolve=_resolve, max_slots=max_slots
        ).compile()
        final: FactoryState = app.invoke(_seed_state(), _config(run_id))
        return {(a.task_id, a.uri) for a in _feature_diffs(final)}

    serial = run(1, "run-serial")
    parallel = run(4, "run-parallel")
    assert parallel == serial
    assert len(parallel) == 2  # both ownership units built


def test_pool_runs_tasks_concurrently(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    probe = _ConcurrencyProbe()
    body = _decomposition_payload()
    proxy = ModelProviderProxy(
        MockTransport(completions=[body], default_text=body), signer=ZoneBSigner(KEY, KEY_ID)
    )
    rt = AgentRuntime(proxy=proxy, adapter=probe)
    app = build_implementation_subgraph(
        rt, source_dir=str(tmp_path), resolve=_resolve, max_slots=2
    ).compile()

    final: FactoryState = app.invoke(_seed_state(), _config("run-conc"))

    assert len(_feature_diffs(final)) == 2
    assert probe.max_active == 2  # the Barrier only releases if both ran at once


def test_pool_concurrent_cas_writes_are_safe(tmp_path: Any) -> None:
    """The shared content-addressed store (durable snapshot_store + rt.cas) must
    survive concurrent identical-digest writes from pool workers. Pre-fix this
    raced (FileExistsError / 'Directory not empty') because the sandbox staged on a
    PID-only temp name shared across threads; the diffs must still resolve."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    store = str(tmp_path / "store")  # shared DURABLE store -> snapshots/blobs shared
    body = _decomposition_payload()
    proxy = ModelProviderProxy(
        MockTransport(completions=[body], default_text=body), signer=ZoneBSigner(KEY, KEY_ID)
    )
    rt = AgentRuntime(proxy=proxy, adapter=_ConstantDiffAdapter(), cas=ArtifactStore())
    app = build_implementation_subgraph(
        rt, source_dir=str(src), resolve=_resolve, snapshot_store=store, max_slots=4
    ).compile()

    final: FactoryState = app.invoke(_seed_state(), _config("run-cas"))

    diffs = _feature_diffs(final)
    assert len(diffs) == 2  # two tasks, distinct idempotency keys, no race crash
    # the identical diff blob mirrored into rt.cas resolves end to end.
    assert rt.cas is not None
    assert rt.cas.resolve_blob("cas://sha256/" + hashlib.sha256(b"identical-diff").hexdigest()) == (
        b"identical-diff"
    )


def test_pool_partial_budget_rejection(tmp_path: Any) -> None:
    """Within ONE domain, a cap between 1x and 2x the per-task estimate admits the
    first task and rejects the second (fail-closed) — the mixed admitted/rejected
    path: exactly one diff is produced. (Both units share one owner, so they share
    one domain quota = the area cap = the global cap here.)"""
    (tmp_path / "README.md").write_text("seed\n")
    ownership = OwnershipMap(
        owners={"services/a.py": "svc", "services/b.py": "svc"},  # one domain, 2 units
        seq=_seq(),
    )
    app = build_implementation_subgraph(
        _runtime(),
        source_dir=str(tmp_path),
        resolve=_resolve,
        max_slots=2,
        cost_cap_microusd=1_500_000,  # one task (est 1_000_000) admits, the next exceeds
    ).compile()

    final: FactoryState = app.invoke(_seed_with_ownership(ownership), _config("run-partial"))

    assert len(_feature_diffs(final)) == 1  # one admitted, one rejected at admit


def _seed_with_ownership(ownership: OwnershipMap) -> FactoryState:
    """Seed state with a custom OwnershipMap (for area/budget-tree tests)."""
    design = _design()
    handles = [
        emit_artifact(design, DESIGN_KIND, "principal_architect", _seq()),
        emit_artifact(ownership, OWNERSHIP_KIND, "principal_architect", _seq()),
    ]
    _BODIES[handles[0].uri] = design
    _BODIES[handles[1].uri] = ownership
    return {"request": "x", "artifacts": handles, "pinned_constraints": _constraints()}


def test_pool_per_area_budget_isolation(tmp_path: Any) -> None:
    """The budget tree (Wave D): the global envelope is sub-allocated per AREA, so
    an area is starved by ITS OWN share — not by global contention. One data domain
    + three backend domains; a global cap of 3M splits to data=750k (< the 1M task,
    so data is rejected) and backend=2.25M (admits 2 of its 3 tasks). The data task
    is rejected even though global had room — proving per-area isolation, not a
    single global bucket."""
    (tmp_path / "README.md").write_text("seed\n")
    ownership = OwnershipMap(
        owners={
            "warehouse/etl.py": "data_team",  # -> data
            "services/a.py": "svc",  # -> backend
            "services/b.py": "svc",
            "services/c.py": "svc",
        },
        seq=_seq(),
    )
    app = build_implementation_subgraph(
        _runtime(),
        source_dir=str(tmp_path),
        resolve=_resolve,
        max_slots=4,
        cost_cap_microusd=3_000_000,  # data share 750k < 1M est; backend share 2.25M
    ).compile()

    final: FactoryState = app.invoke(_seed_with_ownership(ownership), _config("run-iso"))

    diffs = _feature_diffs(final)
    assert len(diffs) == 2  # data starved by its sub-cap; backend admits 2 of 3
    built = {a.task_id for a in diffs}
    assert task_id_for_unit("warehouse/etl.py") not in built  # data area rejected
    assert task_id_for_unit("services/a.py") in built  # backend admitted within its cap


def test_pool_per_domain_isolation_within_one_area(tmp_path: Any) -> None:
    """Two domains in the SAME area each get their own sub-budget, so one domain
    cannot starve its sibling (per-DOMAIN isolation, not just per-area). Both are
    backend; the area cap is split between them, and the heavier domain exhausts
    ITS share while the lighter one still builds."""
    (tmp_path / "README.md").write_text("seed\n")
    ownership = OwnershipMap(
        owners={
            "services/light.py": "svc_light",  # backend domain, 1 unit
            "services/heavy_a.py": "svc_heavy",  # backend domain, 3 units
            "services/heavy_b.py": "svc_heavy",
            "services/heavy_c.py": "svc_heavy",
        },
        seq=_seq(),
    )
    # global 3.6M -> backend area 3.6M -> split by domain weight (1:3):
    # svc_light 900k, svc_heavy 2.7M (est 1M/task). svc_light's task is rejected by
    # its OWN 900k share; svc_heavy admits 2 (1M,2M<=2.7M), rejects the 3rd (3M).
    app = build_implementation_subgraph(
        _runtime(),
        source_dir=str(tmp_path),
        resolve=_resolve,
        max_slots=4,
        cost_cap_microusd=3_600_000,  # backend 3.6M -> light 900k (<1M reject), heavy 2.7M (2 of 3)
    ).compile()

    final: FactoryState = app.invoke(_seed_with_ownership(ownership), _config("run-domain-iso"))

    diffs = _feature_diffs(final)
    built = {a.task_id for a in diffs}
    # svc_light starved by its own 900k share even though svc_heavy (same area) ran;
    # svc_heavy admits 2 of its 3 (1M,2M <= 2.7M; 3M exceeds).
    assert task_id_for_unit("services/light.py") not in built
    assert len(built) == 2


def test_pool_budget_admission_is_resume_deterministic(tmp_path: Any) -> None:
    """Under a binding cap the admitted subset is a deterministic function of the
    ready (merge) order, so two independent runs of the same build admit the
    identical task ids — a resume re-derives the same outcome, never a different
    subset."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    store = str(tmp_path / "store")
    ownership = OwnershipMap(
        owners={
            "services/light.py": "svc_light",
            "services/heavy_a.py": "svc_heavy",
            "services/heavy_b.py": "svc_heavy",
            "services/heavy_c.py": "svc_heavy",
        },
        seq=_seq(),
    )

    def run(run_id: str) -> set[str | None]:
        app = build_implementation_subgraph(
            _runtime(),
            source_dir=str(src),
            resolve=_resolve,
            snapshot_store=store,
            max_slots=4,
            cost_cap_microusd=3_600_000,
        ).compile(checkpointer=MemorySaver())
        final: FactoryState = app.invoke(_seed_with_ownership(ownership), _config(run_id))
        return {a.task_id for a in _feature_diffs(final)}

    assert run("run-det-1") == run("run-det-2")


def test_pool_fail_closed_when_over_budget(tmp_path: Any) -> None:
    """A cost cap below the per-task estimate rejects every task at ADMIT
    (fail-closed): nothing runs, no diff is produced, and the downstream gate has
    nothing to pass — an approve can never ship what the pool refused to build."""
    (tmp_path / "README.md").write_text("seed\n")
    app = build_implementation_subgraph(
        _runtime(),
        source_dir=str(tmp_path),
        resolve=_resolve,
        max_slots=2,
        cost_cap_microusd=10,  # << per-task est (cost_budget default 1_000_000)
    ).compile()

    final: FactoryState = app.invoke(_seed_state(), _config("run-budget"))

    assert _feature_diffs(final) == []  # rejected at admit -> nothing built
