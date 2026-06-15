"""Implementation-layer agent nodes (T-M1-06) — senior_engineer + junior_engineer.

Drives the REAL node factories over a REAL :class:`ModelProviderProxy` backed
by :class:`MockTransport` (no live API key, LEARNINGS D1) and a real
:class:`StubAdapter` as ``rt.adapter`` (no ``claude -p`` binary, D3). Asserts:

* the Senior decomposes ``SystemDesign`` + ``OwnershipMap`` into a
  deterministic, ownership-ordered ``Task[]`` (``OwnershipMap.merge_order``),
  each on its own ``task/<id>`` branch with ``criterion_ids`` traceability;
* the Junior assembles a ``TaskPacket`` with the pinned constraints injected
  FIRST and VERBATIM (``assemble_prompt`` + the packet field), runs the
  adapter inside a ``Sandbox``, and emits a ``FeatureDiff`` carrying a
  ``cas://`` diff handle (DS9) on the ``task/<id>`` branch;
* idempotent retry (L4) re-runs against the same base tree and re-derives the
  SAME idempotency key / content digest (process-stable, L7), forwarding the
  adapter's advisory ``GateTriage`` (L5) and NEVER a ``GateResult``;
* both nodes follow the ``make_<agent>_node`` convention and drop straight into
  a ``StateGraph(FactoryState)``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime
from foundry.agents.implementation import (
    idempotency_key,
    make_junior_engineer_node,
    make_senior_engineer_node,
    pinned_texts,
    stub_conflict_resolution,
)
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
    return MockTransport(completions=[json.dumps(payload)])


# ---- senior_engineer ---------------------------------------------------------


def test_senior_emits_one_task_per_unit_in_merge_order() -> None:
    ownership = an_ownership_map()
    rt = make_runtime(scripted_decomposition({}))
    node = make_senior_engineer_node(rt, design=a_design(), ownership=ownership)

    writes = node({"request": "x"}, config_for("run-A"))
    tasks = writes["tasks"]

    # One task per ownership unit, sequenced by merge_order (owner, unit).
    assert len(tasks) == len(ownership.owners)
    # local index carries the deterministic merge-order position.
    order = ownership.merge_order()
    assert order == ("src/store.py", "src/util.py", "src/api.py")
    locals_in_order = [t["seq"][4] for t in tasks]  # Seq.local is index 4
    assert locals_in_order == [0, 1, 2]


def test_senior_tasks_are_branch_per_task_with_criteria() -> None:
    rt = make_runtime(
        scripted_decomposition(
            {
                "src/store.py": ["AC-1", "AC-2"],
                "src/util.py": ["AC-3"],
                "src/api.py": [],
            }
        )
    )
    node = make_senior_engineer_node(rt, design=a_design(), ownership=an_ownership_map())

    tasks = node({"request": "x"}, config_for("run-A"))["tasks"]
    by_branch = {t["branch"]: t for t in tasks}

    # Every task is on its own task/<id> branch.
    for t in tasks:
        assert t["branch"] == f"task/{t['id']}"
        assert t["id"].startswith("t-")
    # criterion_ids traceability flows through from the model decomposition.
    store_task = next(t for t in tasks if t["criterion_ids"] == ["AC-1", "AC-2"])
    assert store_task["branch"].startswith("task/")
    assert any(t["criterion_ids"] == ["AC-3"] for t in tasks)
    assert len(by_branch) == 3  # branches are distinct


def test_senior_decomposition_is_deterministic_across_reruns() -> None:
    design, ownership = a_design(), an_ownership_map()
    rt1 = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))
    rt2 = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))

    node1 = make_senior_engineer_node(rt1, design=design, ownership=ownership)
    node2 = make_senior_engineer_node(rt2, design=design, ownership=ownership)
    ids1 = [t["id"] for t in node1({}, config_for("r1"))["tasks"]]
    ids2 = [t["id"] for t in node2({}, config_for("r2"))["tasks"]]
    # Ids derive from content_digest(unit) (L7), so they are process-stable.
    assert ids1 == ids2


def test_senior_never_mints_a_gate_result() -> None:
    rt = make_runtime(scripted_decomposition({}))
    writes = make_senior_engineer_node(rt, design=a_design(), ownership=an_ownership_map())(
        {}, config_for("run-A")
    )
    assert "gates" not in writes  # verdicts are computed + signed by the runner


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


def two_constraints() -> list[PinnedConstraint]:
    # ids chosen so sorted-by-id order is c1 then c2 (deterministic injection).
    return [
        a_constraint("c1", "MUST store money as integer cents, never floats."),
        a_constraint("c2", "MUST log no PII."),
    ]


def test_pinned_constraints_reach_packet_first_and_verbatim(tmp_path: Any) -> None:
    constraints = two_constraints()
    texts = pinned_texts(constraints)
    # The packet field holds the texts byte-for-byte, in deterministic order.
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
    # assemble_prompt places them FIRST and VERBATIM, ahead of the TASK body.
    prompt = assemble_prompt(packet)
    pinned_idx = prompt.index("PINNED CONSTRAINTS")
    task_idx = prompt.index("TASK:")
    assert pinned_idx < task_idx
    for text in texts:
        assert text in prompt  # byte-for-byte, no paraphrase


def test_pinned_texts_uses_live_view_and_stable_order() -> None:
    base = two_constraints()
    # A supersession of c2: live view drops the retired record.
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
    # Stable, fold-order-independent: reversed input -> same texts.
    assert pinned_texts([superseding, *reversed(base)]) == texts


def test_junior_emits_feature_diff_with_cas_handle(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    task = a_task()
    node = make_junior_engineer_node(rt, task=task, source_dir=str(tmp_path))

    writes = node({"pinned_constraints": two_constraints()}, config_for("run-A"))

    # The FeatureDiff travels on `artifacts` as a content-addressed handle.
    artifacts = writes["artifacts"]
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art.kind == "feature_diff"
    assert art.uri.startswith("cas://feature_diff/")
    assert art.produced_by == "junior_engineer"
    assert art.task_id == task.id

    # The task-done marker carries the cas:// diff handle from the adapter.
    marker = next(t for t in writes["tasks"] if t["id"] == task.id)
    assert marker["diff_uri"].startswith("cas://sha256/")
    assert marker["branch"] == f"task/{task.id}"


def test_junior_forwards_triage_never_a_gate_result(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter(triage_note="needs a test"))
    node = make_junior_engineer_node(rt, task=a_task(), source_dir=str(tmp_path))

    writes = node({"pinned_constraints": ()}, config_for("run-A"))

    assert "gates" not in writes  # L5: agents never mint a GateResult
    triage = writes["gate_triage"]
    assert triage and all(isinstance(t, GateTriage) for t in triage)
    assert not any(isinstance(t, GateResult) for t in triage)


def test_junior_self_reported_lineage_is_ignored(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    # The adapter lies about its model; nothing downstream reads it as lineage.
    rt = make_runtime(adapter=StubAdapter(self_reported_model="totally-not-claude"))
    node = make_junior_engineer_node(rt, task=a_task(), source_dir=str(tmp_path))
    writes = node({"pinned_constraints": ()}, config_for("run-A"))
    # No channel carries the self-reported model as provenance.
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
    # Sensitive to the inputs that define the diff identity.
    assert idempotency_key(task, constraints, "other-base") != k1
    assert idempotency_key(task, [], "base-sha-xyz") != k1


def test_idempotent_retry_resets_base_and_repeats_key(tmp_path: Any) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("seed\n")
    # A DURABLE store shared by both runs: a retry builds a fresh sandbox (no
    # reuse, arch §2(d)), so the base snapshot only survives the destroy/rebuild
    # when written somewhere that outlives destroy() (Sandbox docstring).
    store = str(tmp_path / "store")
    constraints = two_constraints()
    task = a_task()

    rt = make_runtime(adapter=StubAdapter())
    node = make_junior_engineer_node(
        rt, task=task, source_dir=str(src), snapshot_store=store
    )

    first = node({"pinned_constraints": constraints}, config_for("run-A", step=1))
    base_sha = first["tasks"][0]["base_tree_sha"]

    # A re-run resets the worktree to the SAME base tree (supersession, not
    # edit): the recorded base digest is passed as the reset point. The diff
    # idempotency key (carried in the artifact uri) re-derives identically.
    rt2 = make_runtime(adapter=StubAdapter())
    retry_node = make_junior_engineer_node(
        rt2,
        task=task,
        source_dir=str(src),
        base_snapshot=base_sha,
        snapshot_store=store,
    )
    second = retry_node({"pinned_constraints": constraints}, config_for("run-A", step=2))

    assert second["tasks"][0]["base_tree_sha"] == base_sha
    assert first["artifacts"][0].uri == second["artifacts"][0].uri  # same key
    # The expected key matches idempotency_key over the same inputs.
    expected = idempotency_key(task, constraints, base_sha)
    assert first["artifacts"][0].uri == f"cas://feature_diff/{expected}"
    # The adapter's content-addressed diff is identical too (determinism, D3).
    assert first["tasks"][0]["diff_uri"] == second["tasks"][0]["diff_uri"]


def test_junior_requires_an_adapter(tmp_path: Any) -> None:
    rt = make_runtime(adapter=None)
    try:
        make_junior_engineer_node(rt, task=a_task(), source_dir=str(tmp_path))
    except ValueError as exc:
        assert "adapter" in str(exc)
    else:  # pragma: no cover - the factory must reject a missing substrate
        raise AssertionError("expected ValueError when rt.adapter is None")


# ---- graph compatibility (StateGraph(FactoryState)) --------------------------


def test_senior_node_runs_inside_a_state_graph() -> None:
    rt = make_runtime(scripted_decomposition({"src/store.py": ["AC-1"]}))
    g = StateGraph(FactoryState)
    senior = make_senior_engineer_node(rt, design=a_design(), ownership=an_ownership_map())
    g.add_node("senior", senior)
    g.add_edge(START, "senior")
    g.add_edge("senior", END)
    app = g.compile()

    final = app.invoke({"request": "build it"}, config_for("run-A"))
    assert len(final["tasks"]) == 3
    assert all(t["branch"].startswith("task/") for t in final["tasks"])


def test_junior_node_runs_inside_a_state_graph(tmp_path: Any) -> None:
    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=StubAdapter())
    g = StateGraph(FactoryState)
    g.add_node("junior", make_junior_engineer_node(rt, task=a_task(), source_dir=str(tmp_path)))
    g.add_edge(START, "junior")
    g.add_edge("junior", END)
    app = g.compile()

    final = app.invoke({"pinned_constraints": two_constraints()}, config_for("run-A"))
    artifacts = final["artifacts"]
    assert any(isinstance(a, FeatureDiff) is False for a in artifacts)  # they are Artifacts
    assert artifacts[0].uri.startswith("cas://feature_diff/")
