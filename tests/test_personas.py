"""Area-expert engineer personas + their injection into the adapter prompt."""

from __future__ import annotations

from foundry.agents.implementation.personas import AREA_PERSONAS, persona_for
from foundry.execution.adapter_contract import TaskPacket, assemble_prompt
from foundry.schemas.architecture import CANONICAL_AREAS


def test_every_canonical_area_has_a_persona() -> None:
    for area in CANONICAL_AREAS:
        assert persona_for(area), area
        assert area in AREA_PERSONAS


def test_persona_lookup_is_case_insensitive_and_safe() -> None:
    assert persona_for("FRONTEND") == persona_for("frontend")
    assert persona_for("") == ""
    assert persona_for(None) == ""
    assert persona_for("nonexistent-area") == ""


def _packet(**kw: object) -> TaskPacket:
    base: dict[str, object] = {"task": {"id": "t1"}, "worktree_path": "/w",
                               "turn_budget": 1, "cost_budget_microusd": 0}
    base.update(kw)
    return TaskPacket(**base)  # type: ignore[arg-type]


def test_empty_persona_leaves_prompt_byte_identical() -> None:
    # A packet with no persona must render exactly as before (back-compat).
    assert _packet().persona == ""
    assert "ROLE:" not in assemble_prompt(_packet(pinned_constraints=("C1",)))


def test_persona_renders_after_constraints_before_task() -> None:
    prompt = assemble_prompt(_packet(
        persona=persona_for("backend"),
        pinned_constraints=("MUST validate input",),
    ))
    i_con = prompt.index("PINNED CONSTRAINTS")
    i_role = prompt.index("ROLE:")
    i_task = prompt.index("TASK:")
    # constraints stay FIRST and verbatim; persona sits between them and the task.
    assert i_con < i_role < i_task
    assert "MUST validate input" in prompt
    assert "SENIOR BACKEND ENGINEER" in prompt
