"""Implementation-layer agent nodes (T-M1-06/T-M1-10) — senior + junior.

Drives the REAL node factories over a REAL :class:`ModelProviderProxy` backed
by :class:`MockTransport` (no live API key, LEARNINGS D1) and a real
:class:`StubAdapter` as ``rt.adapter`` (no ``claude -p`` binary, D3). Both nodes
follow the state-reading convention (AR1-AR4 / IM4): their DATA inputs come from
``state`` channels, only INFRA is a factory arg. Asserts:

* the Senior reads the ``SystemDesign`` + ``OwnershipMap`` off ``state["artifacts"]``
  (by ``Artifact.kind``, reconstituted via the injected ``resolve``) and
  decomposes them into a deterministic, ownership-ordered ``Task[]``
  (``OwnershipMap.merge_order``), each on its own ``task/<id>`` branch with
  ``criterion_ids`` traceability;
* the Junior reads the ready ``Task(s)`` off ``state["tasks"]`` + the pinned
  constraints off ``state["pinned_constraints"]``, assembles a ``TaskPacket``
  with the constraints injected FIRST and VERBATIM, runs the adapter inside a
  ``Sandbox``, and emits a ``FeatureDiff`` carrying a ``cas://`` diff handle
  (DS9) on the ``task/<id>`` branch;
* idempotent retry (L4) re-runs against the same base tree and re-derives the
  SAME idempotency key / content digest (process-stable, L7), forwarding the
  adapter's advisory ``GateTriage`` (L5) and NEVER a ``GateResult``;
* both nodes drop straight into a ``StateGraph(FactoryState)``.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents._common import emit_artifact
from foundry.agents.implementation import (
    idempotency_key,
    make_junior_engineer_node,
    make_senior_engineer_node,
    pinned_texts,
    ready_tasks,
    stub_conflict_resolution,
)
from foundry.agents.implementation.senior_engineer import DESIGN_KIND, OWNERSHIP_KIND
from foundry.canonical import content_digest
from foundry.execution.adapter_contract import TaskPacket, assemble_prompt
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import (
    ConstraintScope,
    OwnershipMap,
    PinnedConstraint,
    SystemDesign,
)
from foundry.schemas.base import Artifact, FrozenModel
from foundry.schemas.gates import GateResult, GateTriage
from foundry.schemas.implementation import FeatureDiff, Task
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq

KEY = b"dev-zone-b-impl-key"
KEY_ID = "dev-key-impl"


# ---- fixtures / builders -----------------------------------------------------


def make_runtime(
    transport: MockTransport | None = None,
    *,
    adapter: StubAdapter | None = None,
) -> AgentRuntime:
    proxy = ModelProviderProxy(
        transport or MockTransport(default_text="{}"),
        signer=ZoneBSigner(KEY, KEY_ID),
    )
    return AgentRuntime(proxy=proxy, adapter=adapter)


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


def a_seq(node: str = "test", epoch: int = 1) -> Seq:
    return Seq("run", epoch, 1, node, 0)


def a_design() -> SystemDesign:
    return SystemDesign(
        title="Todo service",
        design_uri="cas://design/abc",
        components=("store", "api"),
        interface_ids=("TodoStore.add",),
        seq=a_seq("principal_architect"),
    )


def an_ownership_map() -> OwnershipMap:
    # Insertion order deliberately NOT the merge order, to prove merge_order()
    # (sorted by (owner, unit)) drives the emitted sequence, not dict order.
    return OwnershipMap(
        owners={
            "src/api.py": "owner-b",
            "src/store.py": "owner-a",
            "src/util.py": "owner-a",
        },
        seq=a_seq("principal_architect"),
    )


def a_constraint(cid: str, text: str) -> PinnedConstraint:
    return PinnedConstraint(
        id=cid,
        text=text,
        scope=ConstraintScope.GLOBAL,
        source="ADR-1",
        seq=a_seq("principal_architect"),
    )


def seed_artifacts(
    design: SystemDesign, ownership: OwnershipMap
) -> tuple[list[Artifact], dict[str, FrozenModel]]:
    """Seed ``state["artifacts"]`` with handles + an in-memory ``resolve`` map.

    The architecture layer emits content-addressed Artifact HANDLES; the bodies
    live out-of-band. Mirror that here: emit handles and a uri->body map the
    Senior's injected ``resolve`` reconstitutes from (CAS in prod)."""
    pa_seq = a_seq("principal_architect")
    handles = [
        emit_artifact(design, DESIGN_KIND, "principal_architect", pa_seq),
        emit_artifact(ownership, OWNERSHIP_KIND, "principal_architect", pa_seq),
    ]
    bodies: dict[str, FrozenModel] = {handles[0].uri: design, handles[1].uri: ownership}
    return handles, bodies


def resolver_for(bodies: dict[str, FrozenModel]) -> Any:
    def _resolve(handle: Artifact) -> FrozenModel:
        return bodies[handle.uri]

    return _resolve


def senior_node(rt: AgentRuntime, design: SystemDesign, ownership: OwnershipMap) -> Any:
    _handles, bodies = seed_artifacts(design, ownership)
    return make_senior_engineer_node(rt, resolve=resolver_for(bodies))


def senior_state(design: SystemDesign, ownership: OwnershipMap) -> FactoryState:
    handles, _bodies = seed_artifacts(design, ownership)
    return {"artifacts": handles}


def scripted_decomposition(criteria_by_unit: dict[str, list[str]]) -> MockTransport:
    """A transport that answers the schema-constrained decomposition call.

    The Senior's ``complete(..., schema=_DECOMPOSITION_SCHEMA, purpose="ideation")``
    is a schema-constrained IDEATION call, NOT a verdict. AC3 is resolved (DT1):
    MockTransport routes to the verdict queue only when the schema IS the verdict
    schema, so the decomposition payload rides the natural ``completions=`` path
    and is returned verbatim as ``text`` (the proxy ``json.loads`` it under the
    decomposition schema).
    """
    payload = {
        "tasks": [
            {"unit": unit, "title": f"build {unit}", "criterion_ids": cids}
            for unit, cids in criteria_by_unit.items()
        ]
    }
    # One decomposition call PER Tech-Lead (per OwnershipMap owner) now, not one
    # global call — so serve the full scripted payload on EVERY call (each lead
    # extracts the criteria for its own units). default_text carries it for the
    # 2nd..Nth lead once the explicit completion is popped.
    body = json.dumps(payload)
    return MockTransport(completions=[body], default_text=body)


# ---- senior_engineer ---------------------------------------------------------


def test_senior_emits_one_task_per_unit_in_merge_order() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt = make_runtime(scripted_decomposition({}))
    node = senior_node(rt, design, ownership)

    writes = node(senior_state(design, ownership), config_for("run-A"))
    tasks = writes["tasks"]

    # One task per ownership unit, sequenced by merge_order (owner, unit).
    assert len(tasks) == len(ownership.owners)
    order = ownership.merge_order()
    assert order == ("src/store.py", "src/util.py", "src/api.py")
    locals_in_order = [t["seq"][4] for t in tasks]  # Seq.local is index 4
    assert locals_in_order == [0, 1, 2]


def test_senior_tasks_are_branch_per_task_with_criteria() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt = make_runtime(
        scripted_decomposition(
            {
                "src/store.py": ["AC-1", "AC-2"],
                "src/util.py": ["AC-3"],
                "src/api.py": [],
            }
        )
    )
    node = senior_node(rt, design, ownership)

    tasks = node(senior_state(design, ownership), config_for("run-A"))["tasks"]
    by_branch = {t["branch"]: t for t in tasks}

    for t in tasks:
        assert t["branch"] == f"task/{t['id']}"
        assert t["id"].startswith("t-")
    store_task = next(t for t in tasks if t["criterion_ids"] == ["AC-1", "AC-2"])
    assert store_task["branch"].startswith("task/")
    assert any(t["criterion_ids"] == ["AC-3"] for t in tasks)
    assert len(by_branch) == 3  # branches are distinct


def test_senior_decomposition_is_deterministic_across_reruns() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt1 = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))
    rt2 = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))

    node1 = senior_node(rt1, design, ownership)
    node2 = senior_node(rt2, design, ownership)
    ids1 = [t["id"] for t in node1(senior_state(design, ownership), config_for("r1"))["tasks"]]
    ids2 = [t["id"] for t in node2(senior_state(design, ownership), config_for("r2"))["tasks"]]
    # Ids derive from content_digest(unit) (L7), so they are process-stable.
    assert ids1 == ids2


def test_senior_skips_redecomposition_when_all_units_already_have_tasks() -> None:
    """Fix-loop re-entry guard: when every owned unit already carries a task, the
    Senior makes ZERO model calls and writes nothing. The `tasks` channel is
    first-keyed-wins, so a re-emission would coalesce to identical state anyway —
    the (T1 Opus) decomposition on every fix attempt is pure waste."""
    design, ownership = a_design(), an_ownership_map()
    scripted = {"src/store.py": ["AC-1"], "src/util.py": ["AC-2"], "src/api.py": ["AC-3"]}

    t1 = scripted_decomposition(scripted)
    node1 = senior_node(make_runtime(t1), design, ownership)
    first = node1(senior_state(design, ownership), config_for("r1"))
    assert len(t1.requests) == len(ownership.leads())  # first run authors the tasks

    t2 = scripted_decomposition(scripted)
    node2 = senior_node(make_runtime(t2), design, ownership)
    reentry = {**senior_state(design, ownership), "tasks": first["tasks"]}
    writes = node2(reentry, config_for("r2"))
    assert t2.requests == []  # ZERO model calls on re-entry
    assert writes == {}  # nothing re-emitted (reducer would coalesce it anyway)


def test_senior_still_runs_when_a_new_unit_is_uncovered() -> None:
    """A design change that introduces a NEW ownership unit leaves it uncovered, so
    the guard does NOT fire — the node runs and emits the missing task."""
    design, ownership = a_design(), an_ownership_map()
    scripted = {"src/store.py": ["AC-1"], "src/util.py": ["AC-2"], "src/api.py": ["AC-3"]}
    node1 = senior_node(make_runtime(scripted_decomposition(scripted)), design, ownership)
    first = node1(senior_state(design, ownership), config_for("r1"))

    t2 = scripted_decomposition(scripted)
    node2 = senior_node(make_runtime(t2), design, ownership)
    # Only ONE of the three units is already on the channel -> not all covered.
    partial = {**senior_state(design, ownership), "tasks": first["tasks"][:1]}
    writes = node2(partial, config_for("r2"))
    assert t2.requests != []  # ran
    assert writes.get("tasks")  # emitted


def test_senior_never_mints_a_gate_result() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt = make_runtime(scripted_decomposition({}))
    node = senior_node(rt, design, ownership)
    writes = node(senior_state(design, ownership), config_for("run-A"))
    assert "gates" not in writes  # verdicts are computed + signed by the runner


# ---- Wave B: per-domain Tech-Lead fan-out ------------------------------------


def _single_owner_map() -> OwnershipMap:
    """All units owned by ONE lead — the byte-identity regression case."""
    return OwnershipMap(
        owners={"src/api.py": "solo", "src/store.py": "solo", "src/util.py": "solo"},
        seq=a_seq("principal_architect"),
    )


def test_single_owner_makes_one_call_byte_identical_to_pre_fanout() -> None:
    """One owner -> exactly ONE decomposition call over all units, and the task
    records are exactly what the pre-fan-out Senior produced (same ids, branches,
    spec_uris, seq order, criteria). The fan-out is a no-op when there's 1 lead."""
    design, ownership = a_design(), _single_owner_map()
    transport = scripted_decomposition({"src/store.py": ["AC-1"], "src/util.py": ["AC-2"]})
    rt = make_runtime(transport)
    node = senior_node(rt, design, ownership)

    tasks = node(senior_state(design, ownership), config_for("run-A"))["tasks"]

    assert len(transport.requests) == 1  # ONE lead -> ONE focused decomposition call
    order = ownership.merge_order()
    assert [t["id"] for t in tasks] == [f"t-{content_digest({'unit': u})[:12]}" for u in order]
    assert [t["seq"][4] for t in tasks] == [0, 1, 2]  # merge-order index preserved
    for unit, t in zip(order, tasks, strict=True):
        assert t["branch"] == f"task/{t['id']}"
        # spec_uri is byte-identical to the pre-fan-out formula (full-record parity)
        spec_digest = content_digest({"unit": unit, "design": design.design_uri})
        assert t["spec_uri"] == f"cas://task/{spec_digest}"
    # criteria flowed from the single call
    assert any(t["criterion_ids"] == ["AC-1"] for t in tasks)
    assert any(t["criterion_ids"] == ["AC-2"] for t in tasks)


def test_multi_owner_fans_out_one_call_per_lead() -> None:
    """N owners -> N decomposition calls (one per Tech-Lead), but the same task
    ids and the same merge order as a single decomposition would yield."""
    design, ownership = a_design(), an_ownership_map()  # owner-a (2 units), owner-b (1)
    transport = scripted_decomposition(
        {"src/store.py": ["AC-1"], "src/util.py": ["AC-2"], "src/api.py": ["AC-3"]}
    )
    rt = make_runtime(transport)
    node = senior_node(rt, design, ownership)

    writes = node(senior_state(design, ownership), config_for("run-A"))
    tasks = writes["tasks"]

    assert len(transport.requests) == len(ownership.leads()) == 2  # one call per lead
    # Same task ids AND same global merge order as a single decomposition would
    # yield — the fan-out changes WHO proposes a task, never its identity/order.
    order = ownership.merge_order()
    assert [t["id"] for t in tasks] == [f"t-{content_digest({'unit': u})[:12]}" for u in order]
    assert [t["seq"][4] for t in tasks] == list(range(len(order)))
    # Per-lead criteria are partitioned to the right unit (owner-a owns store+util,
    # owner-b owns api) — each lead enriched only its own units.
    by_id = {t["id"]: t for t in tasks}

    def crit(unit: str) -> list[str]:
        return by_id[f"t-{content_digest({'unit': unit})[:12]}"]["criterion_ids"]

    assert crit("src/store.py") == ["AC-1"]
    assert crit("src/api.py") == ["AC-3"]


def test_fanout_emits_one_advisory_journal_entry_per_lead() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt = make_runtime(scripted_decomposition({}))
    node = senior_node(rt, design, ownership)

    writes = node(senior_state(design, ownership), config_for("run-A"))
    notes = writes["agent_journal"]

    assert len(notes) == len(ownership.leads()) == 2
    roles = {n.role for n in notes}
    assert all(r.startswith("Tech-Lead") for r in roles)
    # advisory only — never on the gates channel
    assert "gates" not in writes and "gate_triage" not in writes


def test_empty_ownership_map_makes_no_calls() -> None:
    """No owners -> no leads -> no decomposition calls, empty tasks + journal."""
    design = a_design()
    ownership = OwnershipMap(owners={}, seq=a_seq("principal_architect"))
    transport = scripted_decomposition({})
    rt = make_runtime(transport)
    node = senior_node(rt, design, ownership)

    writes = node(senior_state(design, ownership), config_for("run-A"))

    assert writes["tasks"] == []
    assert writes["agent_journal"] == []
    assert len(transport.requests) == 0  # nothing to decompose -> no egress


def test_stub_conflict_resolution_is_typed_and_deterministic() -> None:
    ownership = an_ownership_map()
    seq = a_seq("senior_engineer")
    cr1 = stub_conflict_resolution(
        task_ids=("t-a", "t-b"),
        paths=("src/store.py",),
        base_tree_sha="base123",
        ownership=ownership,
        seq=seq,
    )
    cr2 = stub_conflict_resolution(
        task_ids=("t-a", "t-b"),
        paths=("src/store.py",),
        base_tree_sha="base123",
        ownership=ownership,
        seq=seq,
    )
    assert cr1.resolved_diff_uri.startswith("cas://conflict/")
    assert cr1 == cr2  # deterministic skeleton
    assert "owner-a" in cr1.rationale  # cites the ownership map


# ---- junior_engineer ---------------------------------------------------------


def a_task(tid: str = "t-001") -> Task:
    return Task(
        id=tid,
        spec_uri=f"cas://task/{tid}",
        criterion_ids=("AC-1",),
        branch=f"task/{tid}",
        seq=a_seq("senior_engineer"),
    )


def tasks_state(*tasks: Task) -> list[dict[str, Any]]:
    return [t.model_dump(mode="json") for t in tasks]


def two_constraints() -> list[PinnedConstraint]:
    # ids chosen so sorted-by-id order is c1 then c2 (deterministic injection).
    return [
        a_constraint("c1", "MUST store money as integer cents, never floats."),
        a_constraint("c2", "MUST log no PII."),
    ]


def test_pinned_constraints_reach_packet_first_and_verbatim(tmp_path: Any) -> None:
    constraints = two_constraints()
    texts = pinned_texts(constraints)
    packet = TaskPacket(
        task={"id": "t-001"},
        pinned_constraints=texts,
        worktree_path=str(tmp_path),
        turn_budget=4,
        cost_budget_microusd=1,
    )
    assert packet.pinned_constraints == (
        "MUST store money as integer cents, never floats.",
        "MUST log no PII.",
    )
    prompt = assemble_prompt(packet)
    pinned_idx = prompt.index("PINNED CONSTRAINTS")
    task_idx = prompt.index("TASK:")
    assert pinned_idx < task_idx
    for text in texts:
        assert text in prompt  # byte-for-byte, no paraphrase


def test_pinned_texts_uses_live_view_and_stable_order() -> None:
    base = two_constraints()
    superseding = PinnedConstraint(
        id="c3",
        text="MUST log no secrets and no PII.",
        scope=ConstraintScope.GLOBAL,
        source="ADR-2",
        supersedes="c2",
        seq=a_seq(),
    )
    texts = pinned_texts([*base, superseding])
    assert "MUST log no PII." not in texts  # c2 retired
    assert "MUST log no secrets and no PII." in texts
    assert pinned_texts([superseding, *reversed(base)]) == texts


def test_ready_tasks_reads_state_in_merge_order() -> None:
    # The Junior's DATA input (WHICH tasks) is read off state["tasks"] (AR1).
    t_a = Task(id="t-a", spec_uri="cas://a", criterion_ids=(), branch="task/t-a", seq=a_seq("s", 1))
    t_b = Task(id="t-b", spec_uri="cas://b", criterion_ids=(), branch="task/t-b", seq=a_seq("s", 3))
    # Reversed input order; ready_tasks must order by Seq.
    state: FactoryState = {"tasks": tasks_state(t_b, t_a)}
    ids = [t.id for t in ready_tasks(state)]
    assert ids == ["t-a", "t-b"]


def test_junior_emits_feature_diff_with_cas_handle(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    task = a_task()
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))

    writes = node(
        {"tasks": tasks_state(task), "pinned_constraints": two_constraints()},
        config_for("run-A"),
    )

    artifacts = writes["artifacts"]
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art.kind == "feature_diff"
    assert art.uri.startswith("cas://feature_diff/")
    assert art.produced_by == "junior_engineer"
    assert art.task_id == task.id

    marker = next(t for t in writes["tasks"] if t["id"] == task.id)
    assert marker["diff_uri"].startswith("cas://sha256/")
    assert marker["branch"] == f"task/{task.id}"


def test_junior_feature_diff_body_and_blob_resolve_via_store(tmp_path: Any) -> None:
    """With an AgentRuntime.cas set, the junior stores the TYPED FeatureDiff body
    keyed by its handle uri AND mirrors the diff BLOB into the store — so
    gate_wiring's resolve(feature_diff_handle) + resolve_blob(diff_uri) both
    round-trip end to end (the gap T-M1-14b closes). Driven through a REAL
    StubAdapter (the genuine producer of the unified-diff blob)."""
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.schemas.implementation import FeatureDiff as _FeatureDiff

    (tmp_path / "README.md").write_text("seed\n")
    cas = ArtifactStore()
    proxy = ModelProviderProxy(MockTransport(default_text="{}"), signer=ZoneBSigner(KEY, KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=cas)
    task = a_task()
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))

    writes = node(
        {"tasks": tasks_state(task), "pinned_constraints": two_constraints()},
        config_for("run-cas"),
    )

    handle = writes["artifacts"][0]
    assert handle.kind == "feature_diff"
    # resolve(handle) reconstitutes the TYPED FeatureDiff body keyed on the
    # handle uri (which carries the idempotency key, not content_identity).
    diff = cas.resolve(handle)
    assert isinstance(diff, _FeatureDiff)
    assert diff.task_id == task.id
    assert diff.branch == f"task/{task.id}"
    # resolve_blob(diff.diff_uri) reads the raw unified-diff blob the adapter
    # wrote via sandbox.put_blob — mirrored into the store at the SAME uri.
    blob = cas.resolve_blob(diff.diff_uri)
    assert isinstance(blob, bytes)
    assert b"+++ b/" in blob  # the StubAdapter's unified-diff addition hunk


def test_junior_builds_each_ready_task_on_its_own_branch(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    t1, t2 = a_task("t-001"), a_task("t-002")
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))

    writes = node({"tasks": tasks_state(t1, t2), "pinned_constraints": ()}, config_for("run-A"))

    branches = {a.task_id for a in writes["artifacts"]}
    assert branches == {"t-001", "t-002"}  # branch-per-task preserved
    assert len(writes["artifacts"]) == 2


def test_junior_skips_already_built_tasks_on_a_clean_resume(tmp_path: Any) -> None:
    """Resume-cheap: on a CLEAN continuation (no failed gate), a task that already
    produced a feature_diff on the durable artifacts channel is NOT rebuilt — only the
    un-built tasks run, so a resumed/paused build re-derives nothing it already did."""
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    t1, t2 = a_task("t-001"), a_task("t-002")
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))
    prior = Artifact(kind="feature_diff", uri="cas://feature_diff/prev",
                     produced_by="junior_engineer", task_id="t-001", seq=a_seq("junior_engineer"))
    writes = node({"tasks": tasks_state(t1, t2), "pinned_constraints": (),
                   "artifacts": [prior]}, config_for("resume"))
    assert {a.task_id for a in writes["artifacts"]} == {"t-002"}  # t-001 skipped, t-002 built


def test_junior_rebuilds_all_tasks_on_a_fix_loop(tmp_path: Any) -> None:
    """A fix-loop re-entry (a gate FAILED) rebuilds EVERYTHING — the fix reaches the
    gate by overwriting the diff at its base-stable uri, so a task that already has a
    feature_diff must NOT be skipped or the unfixed code would ship."""
    from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
    from foundry.state.seq import Seq as _Seq

    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    t1, t2 = a_task("t-001"), a_task("t-002")
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))
    prior = Artifact(kind="feature_diff", uri="cas://feature_diff/prev",
                     produced_by="junior_engineer", task_id="t-001", seq=a_seq("junior_engineer"))
    failed = GateResult(name="test", status=GateStatus.FAIL, decision_deterministic=True,
                        execution_deterministic=True,
                        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
                        findings=("FAILED something",), run_against="a" * 64,
                        seq=_Seq("r", 1, 1, "test", 0))
    writes = node({"tasks": tasks_state(t1, t2), "pinned_constraints": (),
                   "artifacts": [prior], "gates": [failed]}, config_for("fix"))
    assert {a.task_id for a in writes["artifacts"]} == {"t-001", "t-002"}  # both rebuilt


def test_worker_cb_fires_start_and_finish_per_task(tmp_path: Any) -> None:
    """Advisory per-worker telemetry (E3): each built task emits exactly one start and
    one finish through rt.worker_cb, carrying its task id — so the floor can seat one
    desk per concurrent worker. Runs pooled (max_slots>1)."""
    (tmp_path / "README.md").write_text("seed\n")
    events: list[dict[str, Any]] = []
    proxy = ModelProviderProxy(MockTransport(default_text="{}"), signer=ZoneBSigner(KEY, KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), worker_cb=events.append)
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path), max_slots=3)

    writes = node({"tasks": tasks_state(a_task("t-001"), a_task("t-002")),
                   "pinned_constraints": ()}, config_for("run-A"))

    assert len(writes["artifacts"]) == 2  # build unaffected by the advisory callback
    by_task: dict[str, set[str]] = {}
    for e in events:
        by_task.setdefault(e["task_id"], set()).add(e["phase"])
    assert by_task == {"t-001": {"start", "finish"}, "t-002": {"start", "finish"}}


def test_worker_cb_failure_never_breaks_the_build(tmp_path: Any) -> None:
    """A telemetry sink that raises must not fail a coding worker (the callback runs on
    worker threads; it is strictly advisory) — the build still produces its diffs."""
    (tmp_path / "README.md").write_text("seed\n")

    def boom(_e: dict[str, Any]) -> None:
        raise RuntimeError("sink is down")

    proxy = ModelProviderProxy(MockTransport(default_text="{}"), signer=ZoneBSigner(KEY, KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), worker_cb=boom)
    # serial path (single task) AND pooled path (two tasks) both stay guarded.
    serial = make_junior_engineer_node(rt, source_dir=str(tmp_path))
    assert len(serial({"tasks": tasks_state(a_task("s-1")), "pinned_constraints": ()},
                      config_for("run-A"))["artifacts"]) == 1
    pooled = make_junior_engineer_node(rt, source_dir=str(tmp_path), max_slots=2)
    assert len(pooled({"tasks": tasks_state(a_task("p-1"), a_task("p-2")),
                       "pinned_constraints": ()}, config_for("run-B"))["artifacts"]) == 2


def test_junior_forwards_triage_never_a_gate_result(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter(triage_note="needs a test"))
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))

    writes = node({"tasks": tasks_state(a_task()), "pinned_constraints": ()}, config_for("run-A"))

    assert "gates" not in writes  # L5: agents never mint a GateResult
    triage = writes["gate_triage"]
    assert triage and all(isinstance(t, GateTriage) for t in triage)
    assert not any(isinstance(t, GateResult) for t in triage)


def test_junior_self_reported_lineage_is_ignored(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter(self_reported_model="totally-not-claude"))
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path))
    writes = node({"tasks": tasks_state(a_task()), "pinned_constraints": ()}, config_for("run-A"))
    blob = json.dumps(
        {"tasks": writes["tasks"], "uri": writes["artifacts"][0].uri},
        default=str,
    )
    assert "totally-not-claude" not in blob


# ---- idempotent retry (L4/L7) ------------------------------------------------


def test_idempotency_key_is_process_stable() -> None:
    task = a_task()
    constraints = two_constraints()
    k1 = idempotency_key(task, constraints, "base-sha-xyz")
    k2 = idempotency_key(task, constraints, "base-sha-xyz")
    assert k1 == k2  # content_digest, not hash() (L7)
    assert idempotency_key(task, constraints, "other-base") != k1
    assert idempotency_key(task, [], "base-sha-xyz") != k1


def test_idempotent_retry_resets_base_and_repeats_key(tmp_path: Any) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    # A DURABLE store shared by both runs (LEARNINGS IM1): a retry builds a fresh
    # sandbox (no reuse, arch §2(d)), so the base snapshot only survives the
    # destroy/rebuild when written somewhere that outlives destroy().
    store = str(tmp_path / "store")
    constraints = two_constraints()
    task = a_task()

    rt = make_runtime(adapter=StubAdapter())
    node = make_junior_engineer_node(rt, source_dir=str(src), snapshot_store=store)

    first = node(
        {"tasks": tasks_state(task), "pinned_constraints": constraints},
        config_for("run-A", step=1),
    )
    base_sha = first["tasks"][0]["base_tree_sha"]

    # The retry re-presents the produced done-marker (carrying base_tree_sha) on
    # state["tasks"]; the Junior resets the worktree to that SAME base (super-
    # session, not edit) and the diff idempotency key re-derives identically.
    # NB (LEARNINGS IM3 / T-M1-10): this drives the node in ISOLATION — `first["tasks"]`
    # (the node's raw return) is fed straight in, BYPASSING the `tasks` reducer. In the
    # wired subgraph the reducer is first-keyed-wins, so the marker would be dropped and
    # base_tree_sha would NOT be readable off `tasks`; this is not proof of wired supersession.
    rt2 = make_runtime(adapter=StubAdapter())
    retry_node = make_junior_engineer_node(rt2, source_dir=str(src), snapshot_store=store)
    second = retry_node(
        {"tasks": first["tasks"], "pinned_constraints": constraints},
        config_for("run-A", step=2),
    )

    assert second["tasks"][0]["base_tree_sha"] == base_sha
    assert first["artifacts"][0].uri == second["artifacts"][0].uri  # same key
    expected = idempotency_key(task, constraints, base_sha)
    assert first["artifacts"][0].uri == f"cas://feature_diff/{expected}"
    assert first["tasks"][0]["diff_uri"] == second["tasks"][0]["diff_uri"]


def test_junior_requires_an_adapter(tmp_path: Any) -> None:
    rt = make_runtime(adapter=None)
    try:
        make_junior_engineer_node(rt, source_dir=str(tmp_path))
    except ValueError as exc:
        assert "adapter" in str(exc)
    else:  # pragma: no cover - the factory must reject a missing substrate
        raise AssertionError("expected ValueError when rt.adapter is None")


# ---- graph compatibility (StateGraph(FactoryState)) --------------------------


def test_senior_node_runs_inside_a_state_graph() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))
    g = StateGraph(FactoryState)
    g.add_node("senior", senior_node(rt, design, ownership))
    g.add_edge(START, "senior")
    g.add_edge("senior", END)
    app = g.compile()

    final = app.invoke(senior_state(design, ownership), config_for("run-A"))
    assert len(final["tasks"]) == 3
    assert all(t["branch"].startswith("task/") for t in final["tasks"])


def test_junior_node_runs_inside_a_state_graph(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    g = StateGraph(FactoryState)
    g.add_node("junior", make_junior_engineer_node(rt, source_dir=str(tmp_path)))
    g.add_edge(START, "junior")
    g.add_edge("junior", END)
    app = g.compile()

    final = app.invoke(
        {"tasks": tasks_state(a_task()), "pinned_constraints": two_constraints()},
        config_for("run-A"),
    )
    artifacts = final["artifacts"]
    assert any(isinstance(a, FeatureDiff) is False for a in artifacts)  # they are Artifacts
    assert artifacts[0].uri.startswith("cas://feature_diff/")


def test_prior_failures_feeds_failed_gate_findings_back_to_the_engineer() -> None:
    """The fix loop's feedback seam: a FAILED gate's findings become the packet's
    prior_failures; a clean / PASSed state contributes nothing (first-pass blind)."""
    from foundry.agents.implementation.junior_engineer import _prior_failures
    from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
    from foundry.state.seq import Seq

    failing = GateResult(
        name="test",
        status=GateStatus.FAIL,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        findings=("FAILED test_rw::test_double_spaces", "E   assert 'a b' == 'a  b'"),
        run_against="a" * 64,
        seq=Seq("r", 1, 1, "test", 0),
    )
    fed = _prior_failures({"gates": [failing]})
    assert "E   assert 'a b' == 'a  b'" in fed
    assert any("test gate FAILED" in f for f in fed)
    assert _prior_failures({}) == ()                                   # first pass: blind
    passing = failing.model_copy(update={"status": GateStatus.PASS})
    assert _prior_failures({"gates": [passing]}) == ()                 # green: no feedback


def test_coding_admission_bounds_global_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1: the cross-lane coding-session gate caps total concurrent adapter runs
    (they bypass the proxy limiter, so nothing else bounds them). With the bound set
    to 2, at most 2 admissions are ever in-flight at once, however many threads
    contend. Unset/0 is unbounded (the serial default) — asserted by the peak == N."""
    from foundry.agents.implementation.junior_engineer import coding_admission, reset_coding_gate

    def peak_concurrency(n_threads: int) -> int:
        reset_coding_gate()
        live = 0
        peak = 0
        lock = threading.Lock()
        start = threading.Barrier(n_threads)

        def worker() -> None:
            nonlocal live, peak
            start.wait()
            with coding_admission():
                with lock:
                    live += 1
                    peak = max(peak, live)
                time.sleep(0.03)  # hold the slot so contenders pile up
                with lock:
                    live -= 1

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return peak

    monkeypatch.setenv("FOUNDRY_MAX_CODING_SESSIONS", "2")
    try:
        assert peak_concurrency(6) <= 2  # bound holds across all contenders
        monkeypatch.setenv("FOUNDRY_MAX_CODING_SESSIONS", "0")
        assert peak_concurrency(6) == 6  # unset/0 is unbounded (serial-default behaviour)
    finally:
        reset_coding_gate()  # don't leak the env-built gate to other tests
