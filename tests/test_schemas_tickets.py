"""Ticket-driven schema: the shared contract the planner, scheduler, BDD gate and
Workshop all build against."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.durability.serde import foundry_serde
from foundry.schemas.implementation import (
    DEFAULT_TICKET_TIERS,
    Difficulty,
    Specialty,
    Task,
    Ticket,
    feature_path,
    scenario_ref,
)
from foundry.schemas.product import AcceptanceCriterion, render_scenario
from foundry.state.seq import Seq

SEQ = Seq("r", 1, 1, "planner", 0)


def test_ticket_defaults_and_frozen() -> None:
    t = Ticket(id="T-001", title="Add item", specialty=Specialty.BACKEND, seq=SEQ)
    assert t.difficulty is Difficulty.STANDARD and t.depends_on == ()
    with pytest.raises(ValidationError):
        t.title = "x"  # type: ignore[misc]


def test_ticket_rejects_unknown_specialty() -> None:
    with pytest.raises(ValidationError):
        Ticket(id="T-1", title="x", specialty="devops", seq=SEQ)  # type: ignore[arg-type]


def test_task_ticket_fields_default_for_ownership_tasks() -> None:
    t = Task(id="t-1", spec_uri="cas://x", criterion_ids=(), branch="task/t-1", seq=SEQ)
    assert t.ticket_id is None and t.tier is None and t.depends_on == ()


def test_scenario_refs_and_tier_table() -> None:
    assert feature_path("T-007") == "features/T-007.feature"
    assert scenario_ref("T-007", "AC-3") == "features/T-007.feature::AC-3"
    assert DEFAULT_TICKET_TIERS[Difficulty.TRIVIAL] == "T3"
    assert DEFAULT_TICKET_TIERS[Difficulty.COMPLEX] == "T1"


def test_render_scenario() -> None:
    assert render_scenario(("a list",), ("I add x",), ("x is listed", "count is 1")) == (
        "Given a list When I add x Then x is listed and count is 1"
    )


def test_new_records_round_trip_the_checkpoint_serde() -> None:
    serde = foundry_serde()
    ticket = Ticket(id="T-1", title="x", specialty=Specialty.TEST,
                    difficulty=Difficulty.TRIVIAL, depends_on=("T-0",), seq=SEQ)
    ac = AcceptanceCriterion(id="AC-1", text="Given a When b Then c", given=("a",),
                             when=("b",), then=("c",), ticket_id="T-1", seq=SEQ)
    task = Task(id="t", spec_uri="u", criterion_ids=("AC-1",), branch="task/t",
                ticket_id="T-1", specialty=Specialty.TEST, tier="T3", seq=SEQ)
    for obj in (ticket, ac, task):
        assert serde.loads_typed(serde.dumps_typed(obj)) == obj
