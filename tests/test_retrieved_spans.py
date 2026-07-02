"""Phase 4 (ROADMAP): the codegen packet is grounded in the spec it traces to.

``retrieved_spans`` was always empty and ``spec_uri`` is a synthetic handle, so
the coding substrate used to see only the pinned constraints + a task id. Now the
Junior resolves — through the SAME typed ``resolve`` seam as the ownership read —
the task's ownership unit, its acceptance-criteria bodies, and the system-design
summary into the packet. Resolver unwired ⇒ empty spans, packet byte-identical.
"""

from __future__ import annotations

from typing import Any

from foundry.agents.implementation.junior_engineer import _retrieved_spans
from foundry.agents.implementation.senior_engineer import (
    DESIGN_KIND,
    OWNERSHIP_KIND,
    task_id_for_unit,
)
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import Task
from foundry.schemas.product import AcceptanceCriterion
from foundry.state.seq import Seq


def a_seq(node: str = "n", local: int = 0) -> Seq:
    return Seq("run", 1, 0, node, local)


def handle(kind: str, uri: str, local: int = 0) -> Artifact:
    return Artifact(kind=kind, uri=uri, produced_by="t", seq=a_seq("pa", local))


def make_state_and_resolver() -> tuple[dict[str, Any], Any, Task]:
    design = SystemDesign(
        title="Moon Colony Sim",
        design_uri="cas://design/1",
        components=("engine", "farming"),
        interface_ids=("IFACE-tick",),
        seq=a_seq(),
    )
    ownership = OwnershipMap(
        owners={"src/engine.py": "lead-a", "src/farm.py": "lead-b"}, seq=a_seq()
    )
    c1 = AcceptanceCriterion(
        id="AC-1", text="Given boot, the loop ticks.", oracle_hint="pytest", seq=a_seq()
    )
    c2 = AcceptanceCriterion(id="AC-2", text="Crops grow daily.", seq=a_seq())
    c_other = AcceptanceCriterion(id="AC-9", text="Unrelated.", seq=a_seq())
    handles = [
        handle(DESIGN_KIND, "cas://d", 0),
        handle(OWNERSHIP_KIND, "cas://o", 1),
        handle("acceptance_criterion", "cas://c1", 2),
        handle("acceptance_criterion", "cas://c2", 3),
        handle("acceptance_criterion", "cas://c9", 4),
    ]
    bodies = {
        "cas://d": design, "cas://o": ownership,
        "cas://c1": c1, "cas://c2": c2, "cas://c9": c_other,
    }
    task = Task(
        id=task_id_for_unit("src/engine.py"),
        spec_uri="cas://task/x",
        criterion_ids=("AC-1", "AC-2"),
        branch="task/t",
        seq=a_seq("senior"),
    )
    return {"artifacts": handles}, (lambda h: bodies[h.uri]), task


def test_spans_carry_unit_criteria_and_design() -> None:
    state, resolve, task = make_state_and_resolver()
    spans = _retrieved_spans(state, resolve, task)
    text = "\n".join(spans)
    assert "OWNERSHIP UNIT: this task implements 'src/engine.py'" in text
    assert "lead-a" in text
    assert "ACCEPTANCE CRITERION AC-1: Given boot, the loop ticks. [oracle: pytest]" in text
    assert "ACCEPTANCE CRITERION AC-2: Crops grow daily." in text
    assert "AC-9" not in text  # only the criteria THIS task traces to
    assert '"title": "Moon Colony Sim"' in text
    # Deterministic order: unit, criteria sorted by id, design last.
    assert spans[0].startswith("OWNERSHIP UNIT")
    assert spans[-1].startswith("SYSTEM DESIGN")


def test_no_resolver_means_no_spans() -> None:
    state, _resolve, task = make_state_and_resolver()
    assert _retrieved_spans(state, None, task) == ()


def test_spans_reach_the_adapter_prompt(tmp_path: Any) -> None:
    # End-to-end: the node passes the spans into the packet, and assemble_prompt
    # renders them — the coding substrate finally SEES the spec.
    from foundry.execution.adapter_contract import AdapterResult, TaskPacket
    from foundry.execution.adapters.stub import StubAdapter

    from tests.test_agents_implementation import (  # reuse the suite's fixtures
        config_for,
        make_runtime,
        tasks_state,
        two_constraints,
    )
    from foundry.agents.implementation.junior_engineer import make_junior_engineer_node

    state, resolve, task = make_state_and_resolver()
    seen: dict[str, TaskPacket] = {}

    class SpyAdapter(StubAdapter):
        def run(self, packet: TaskPacket, sandbox: Any) -> AdapterResult:
            seen["packet"] = packet
            return super().run(packet, sandbox)

    (tmp_path / "README.md").write_text("seed\n")
    rt = make_runtime(adapter=SpyAdapter())
    node = make_junior_engineer_node(rt, source_dir=str(tmp_path), resolve=resolve)
    node(
        {
            "tasks": tasks_state(task),
            "pinned_constraints": two_constraints(),
            "artifacts": state["artifacts"],
        },
        config_for("run-A"),
    )
    packet = seen["packet"]
    assert any("ACCEPTANCE CRITERION AC-1" in s for s in packet.retrieved_spans)
    from foundry.execution.adapter_contract import assemble_prompt

    prompt = assemble_prompt(packet)
    assert "RETRIEVED CONTEXT SPANS:" in prompt
    assert "Moon Colony Sim" in prompt


def test_span_truncation_is_explicit() -> None:
    state, resolve, task = make_state_and_resolver()
    big = AcceptanceCriterion(id="AC-1", text="x" * 10_000, seq=a_seq())
    bodies = {"cas://c1": big}
    spans = _retrieved_spans(
        {"artifacts": [handle("acceptance_criterion", "cas://c1")]},
        lambda h: bodies[h.uri],
        task,
    )
    (span,) = spans
    assert span.endswith("…[truncated]")
    assert len(span) < 10_000
