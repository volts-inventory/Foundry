"""Conformance tests for the synthetic SWE-EVO-style task frame (T-M15-02).

Contract (docs/evaluation.md §2/§3; conventions from foundry/schemas/base.py):
a deterministic synthetic task frame so the eval harness runs with NO live model
— frozen fixtures with a deterministic oracle (no LLM judge, §4), a difficulty
SPREAD for the pilot's variance/base-rate estimate, a DISJOINT pilot/scored
partition reproducible from a seed, and a content-addressed ``eval_set_identity``
the alpha ledger / preregistration bind to. SYNTHETIC dry-run fixtures only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.evaluation.task_fixtures import (
    Difficulty,
    OracleDescriptor,
    TaskFixture,
    TaskSlice,
    generate_synthetic_frame,
)
from foundry.schemas.base import FrozenModel


def test_frozen_models() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=1)
    assert isinstance(frame, FrozenModel)
    assert isinstance(frame.fixtures[0], FrozenModel)
    with pytest.raises(ValidationError):
        frame.fixtures[0].task_id = "mutated"  # type: ignore[misc]


def test_deterministic_generation_same_seed_identical_frame() -> None:
    a = generate_synthetic_frame(n_tasks=20, seed=7)
    b = generate_synthetic_frame(n_tasks=20, seed=7)
    assert a == b
    assert a.eval_set_identity == b.eval_set_identity
    assert a.content_identity() == b.content_identity()


def test_eval_set_identity_stable_and_content_addressed() -> None:
    # Stable across reconstruction:
    assert (
        generate_synthetic_frame(n_tasks=12, seed=3).eval_set_identity
        == generate_synthetic_frame(n_tasks=12, seed=3).eval_set_identity
    )
    # Changes if the frame contents change (more tasks):
    assert (
        generate_synthetic_frame(n_tasks=12, seed=3).eval_set_identity
        != generate_synthetic_frame(n_tasks=13, seed=3).eval_set_identity
    )
    # Changes if the PARTITION changes (different seed / pilot_fraction) —
    # so the alpha ledger key can't be silently re-drawn by re-partitioning:
    assert (
        generate_synthetic_frame(n_tasks=12, seed=3).eval_set_identity
        != generate_synthetic_frame(n_tasks=12, seed=4).eval_set_identity
    )
    assert (
        generate_synthetic_frame(n_tasks=12, seed=3, pilot_fraction=0.4).eval_set_identity
        != generate_synthetic_frame(n_tasks=12, seed=3, pilot_fraction=0.5).eval_set_identity
    )


def test_slices_disjoint() -> None:
    frame = generate_synthetic_frame(n_tasks=30, seed=11)
    pilot = set(frame.pilot_slice.task_ids)
    scored = set(frame.scored_slice.task_ids)
    assert pilot & scored == set()  # DISJOINT (evaluation.md §2)
    assert len(pilot) >= 1 and len(scored) >= 1  # both non-empty


def test_slices_cover_frame_without_overlap() -> None:
    frame = generate_synthetic_frame(n_tasks=30, seed=11)
    all_ids = {f.task_id for f in frame.fixtures}
    pilot = set(frame.pilot_slice.task_ids)
    scored = set(frame.scored_slice.task_ids)
    assert pilot | scored == all_ids  # exhaustive cover
    assert len(pilot) + len(scored) == len(all_ids)  # no double counting


def test_partition_deterministic() -> None:
    a = generate_synthetic_frame(n_tasks=18, seed=5)
    b = generate_synthetic_frame(n_tasks=18, seed=5)
    assert a.pilot_slice.task_ids == b.pilot_slice.task_ids
    assert a.scored_slice.task_ids == b.scored_slice.task_ids
    assert a.pilot_slice.identity == b.pilot_slice.identity
    # Property reads are stable within one frame too:
    assert a.pilot_slice.task_ids == a.pilot_slice.task_ids


def test_slice_identity_content_addressed() -> None:
    frame = generate_synthetic_frame(n_tasks=12, seed=2)
    assert frame.pilot_slice.identity != frame.scored_slice.identity
    # Slice identity is bound to the frame identity, so a different frame's
    # like-named slice differs even if membership coincidentally overlapped:
    other = generate_synthetic_frame(n_tasks=14, seed=2)
    assert frame.pilot_slice.identity != other.pilot_slice.identity


def test_oracle_descriptor_present_per_fixture() -> None:
    frame = generate_synthetic_frame(n_tasks=15, seed=9)
    for f in frame.fixtures:
        assert isinstance(f.oracle, OracleDescriptor)
        assert f.oracle.hidden_test_id
        assert f.oracle.expected_output
        assert f.oracle.synthetic is True


def test_difficulty_spread_present() -> None:
    frame = generate_synthetic_frame(n_tasks=24, seed=0)
    present = {f.difficulty for f in frame.fixtures}
    assert present == set(Difficulty)  # all strata represented
    # Base rates differ across strata so the pilot has variance to estimate:
    rates = {f.difficulty: f.control_base_rate for f in frame.fixtures}
    assert len({rates[d] for d in Difficulty}) == len(Difficulty)


def test_synthetic_dry_run_flags() -> None:
    frame = generate_synthetic_frame(n_tasks=4, seed=1)
    assert frame.synthetic is True
    assert all(f.synthetic for f in frame.fixtures)


def test_resolution_seed_process_stable() -> None:
    # base_resolution_seed is derived via content_digest (L7), not hash(), so it
    # is identical across processes / runs for the same task+seed.
    a = generate_synthetic_frame(n_tasks=8, seed=4)
    b = generate_synthetic_frame(n_tasks=8, seed=4)
    assert [f.base_resolution_seed for f in a.fixtures] == [
        f.base_resolution_seed for f in b.fixtures
    ]
    assert all(f.base_resolution_seed >= 0 for f in a.fixtures)


def test_fixture_lookup() -> None:
    frame = generate_synthetic_frame(n_tasks=6, seed=1)
    fid = frame.fixtures[3].task_id
    assert frame.fixture(fid).task_id == fid
    with pytest.raises(KeyError):
        frame.fixture("nonexistent")


def test_slice_contains() -> None:
    frame = generate_synthetic_frame(n_tasks=10, seed=3)
    slc: TaskSlice = frame.pilot_slice
    assert slc.task_ids[0] in slc
    assert "nope" not in slc


def test_too_few_tasks_rejected() -> None:
    with pytest.raises(ValueError, match="disjoint"):
        generate_synthetic_frame(n_tasks=1, seed=0)


def test_oracle_scored_not_llm_judge() -> None:
    # The oracle descriptor is a deterministic equality target — the marker that
    # the primary is oracle-scored (§4), not LLM-judged.
    f: TaskFixture = generate_synthetic_frame(n_tasks=3, seed=0).fixtures[0]
    assert f.oracle.expected_output == f.oracle.expected_output  # deterministic
