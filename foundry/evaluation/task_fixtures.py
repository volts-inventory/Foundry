"""Synthetic SWE-EVO-style task fixtures + the shared sampling frame.

This module is the **deterministic synthetic task frame** for the Phase-1.5
decision-gate harness (evaluation.md §2/§3). It lets the whole eval machinery —
the variance/base-rate pilot, the powered A/B, and the pre-registration manifest
— run end-to-end **with no live model and no real benchmark**: resolution is
scored by a deterministic ORACLE descriptor baked into each fixture, never by an
LLM judge (evaluation.md §4 — "the primary is never LLM-judge-scored").

HONESTY NOTE (load-bearing, read before reuse): these are **SYNTHETIC fixtures
for a methodology dry-run**, NOT real SWE-EVO tasks. A frame built here asserts
NOTHING about real Foundry-vs-baseline performance — its oracle outcomes, base
rates, and per-arm resolution parameters are *manufactured* so the harness has a
stable, content-addressable contract to hash into the manifest and to exercise
the pilot/scored split deterministically. Every model defaults `synthetic=True`;
the dry-run flag mirrors :class:`foundry.evaluation.hypothesis.Provenance`.

Design (evaluation.md §2, "Variance/base-rate pilot — runs first, on disjoint
tasks drawn from the SAME frame"):

* One **common sampling frame** (:class:`TaskFrame`) holds the full task set and
  exposes a DETERMINISTIC partition into two DISJOINT slices — a ``pilot_slice``
  and a ``scored_slice`` — reproducible from a seed. The pilot estimates base
  rate + variance components on tasks *disjoint from* the scored A/B slice but
  *sampled from the identical distribution*, so those estimates transfer.
* The frame exposes a stable, content-addressed ``eval_set_identity`` (L7
  ``content_digest`` — never Python's seed-randomized ``hash()``) that the alpha
  look-ledger (``foundry/state/global_records.py``, keyed on
  ``(eval_set_identity, decision_id)``) and the pre-registration bind to. It
  changes iff the frame's tasks or its partition change, and is stable
  otherwise — so forking/retraining cannot silently re-draw the alpha budget.

There is **no statistics logic here** (per EV1): power calcs, bootstrap, and
alpha-spending live in later M1.5 modules. This module only manufactures the
deterministic task frame they consume.
"""

from __future__ import annotations

import random
from enum import StrEnum

from pydantic import Field

from foundry.canonical import content_digest
from foundry.schemas.base import FrozenModel


class Difficulty(StrEnum):
    """Coarse base-rate stratum for a synthetic task (the difficulty SPREAD the
    pilot needs to estimate between-task variance — evaluation.md §2)."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


# Synthetic per-stratum control base rate (probability the *control* arm
# resolves a task of this difficulty). DRY-RUN values — manufactured to give the
# pilot a non-degenerate base rate + between-task variance to estimate, NOT a
# claim about any real benchmark.
_BASE_RATE: dict[Difficulty, float] = {
    Difficulty.EASY: 0.80,
    Difficulty.MEDIUM: 0.45,
    Difficulty.HARD: 0.15,
}


class OracleDescriptor(FrozenModel):
    """The deterministic, hidden-test-style oracle for one fixture.

    Resolution is scored by comparing an arm's produced output against
    ``expected_output`` (evaluation.md §6 — hidden tests live in a harness the
    sandbox cannot read; here they are simply the descriptor the deterministic
    per-arm runner checks). NOT an LLM judge (§4).

    SYNTHETIC: ``expected_output`` / ``hidden_test_id`` are manufactured tokens,
    not real test artifacts.
    """

    hidden_test_id: str  # the held-out test the runner applies AFTER the run (§6)
    expected_output: str  # deterministic oracle target; equality => resolved
    synthetic: bool = True
    version: int = 1


class TaskFixture(FrozenModel):
    """One synthetic SWE-EVO-style task in the shared frame.

    Carries everything the deterministic per-arm runner (the next task) needs to
    simulate a resolution outcome WITHOUT a live model: a stable ``task_id``, a
    synthetic spec/request string, a KNOWN :class:`OracleDescriptor`, a
    difficulty/base-rate hint, and a ``base_resolution_seed`` the runner folds
    with the arm to derive a deterministic per-arm resolved/unresolved outcome.

    SYNTHETIC: this is a methodology-dry-run fixture, not a real SWE-EVO task.
    """

    task_id: str
    spec: str  # synthetic request/specification string
    oracle: OracleDescriptor  # deterministic oracle (no LLM judge — §4)
    difficulty: Difficulty
    control_base_rate: float = Field(gt=0.0, lt=1.0)  # P(control resolves), per stratum
    # Folded with the Arm + repeat index by the runner to make a deterministic,
    # reproducible per-arm resolution draw (process-stable; never `hash()` / RNG state):
    base_resolution_seed: int = Field(ge=0)
    synthetic: bool = True
    version: int = 1


class TaskSlice(FrozenModel):
    """A deterministic, content-addressed subset of a :class:`TaskFrame`.

    ``identity`` is content-addressed over the slice's ordered ``task_ids`` (plus
    the parent frame identity) so two equal slices hash identically and a
    membership change flips it. The pilot and scored slices are DISJOINT
    (evaluation.md §2)."""

    name: str  # "pilot" | "scored"
    task_ids: tuple[str, ...]  # ordered, deterministic
    identity: str  # content_digest over (frame_identity, name, task_ids) — L7

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.task_ids


class TaskFrame(FrozenModel):
    """The shared sampling frame: the full synthetic task set + its deterministic
    disjoint partition into a pilot slice and a scored slice.

    evaluation.md §2 requires base rate + between-task variance to transfer from
    the pilot to the scored run, which only holds if both slices are drawn from
    ONE common frame with the same difficulty distribution. So both slices are
    cut from THIS frame, deterministically (seeded) and DISJOINT.

    ``eval_set_identity`` is content-addressed over the frame's ordered fixtures
    AND its partition (L7 ``content_digest``): the alpha look-ledger and the
    pre-registration bind to it (``(eval_set_identity, decision_id)`` —
    ``foundry/state/global_records.py``). It is stable across reconstruction of
    an equal frame and changes iff the tasks or the partition change.

    SYNTHETIC: a dry-run frame (``synthetic=True``); see module docstring.
    """

    fixtures: tuple[TaskFixture, ...]
    seed: int  # the partition seed (deterministic, reproducible)
    pilot_fraction: float = Field(gt=0.0, lt=1.0)
    eval_set_identity: str  # content_digest over fixtures + partition — L7
    synthetic: bool = True
    version: int = 1

    # ---- deterministic disjoint partition ------------------------------------
    def _partition_ids(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Deterministically split the (sorted) task_ids into (pilot, scored).

        Seeded shuffle of the SORTED task_ids (sorted first so the partition is
        independent of fixture insertion order), then a prefix cut by
        ``pilot_fraction``. Reproducible: same frame + seed => identical split.
        DISJOINT by construction (prefix vs suffix of one permutation).
        """
        ids = sorted(f.task_id for f in self.fixtures)
        rng = random.Random(self.seed)  # partitioning, not crypto
        shuffled = ids[:]
        rng.shuffle(shuffled)
        # At least one task on each side when the frame has >= 2 tasks.
        n_pilot = max(1, min(len(shuffled) - 1, round(len(shuffled) * self.pilot_fraction)))
        pilot = tuple(sorted(shuffled[:n_pilot]))
        scored = tuple(sorted(shuffled[n_pilot:]))
        return pilot, scored

    def _slice(self, name: str, task_ids: tuple[str, ...]) -> TaskSlice:
        identity = content_digest(
            {"frame": self.eval_set_identity, "name": name, "task_ids": list(task_ids)}
        )
        return TaskSlice(name=name, task_ids=task_ids, identity=identity)

    @property
    def pilot_slice(self) -> TaskSlice:
        """The variance/base-rate pilot slice (never scored as the A/B — §2)."""
        pilot, _ = self._partition_ids()
        return self._slice("pilot", pilot)

    @property
    def scored_slice(self) -> TaskSlice:
        """The confirmatory A/B slice (disjoint from the pilot — §2)."""
        _, scored = self._partition_ids()
        return self._slice("scored", scored)

    def fixture(self, task_id: str) -> TaskFixture:
        """Look up a fixture by id (raises ``KeyError`` if absent)."""
        for f in self.fixtures:
            if f.task_id == task_id:
                return f
        raise KeyError(task_id)


def _frame_identity(fixtures: tuple[TaskFixture, ...], seed: int, pilot_fraction: float) -> str:
    """Content-address the frame over its ORDERED-BY-ID fixtures + partition
    params (L7 ``content_digest``, never ``hash()``). Sorting by id makes the
    identity independent of fixture construction order; including seed +
    pilot_fraction makes a re-partition flip the identity (so the alpha ledger
    can't be silently re-drawn by changing the split)."""
    ordered = sorted(fixtures, key=lambda x: x.task_id)
    return content_digest(
        {
            "fixtures": [f.model_dump(mode="json") for f in ordered],
            "seed": seed,
            "pilot_fraction": pilot_fraction,
        }
    )


def generate_synthetic_frame(
    *,
    n_tasks: int = 24,
    seed: int = 0,
    pilot_fraction: float = 0.4,
) -> TaskFrame:
    """Build a deterministic synthetic :class:`TaskFrame` of ``n_tasks`` fixtures.

    Deterministic in ``seed``: same ``(n_tasks, seed, pilot_fraction)`` ⇒ an
    IDENTICAL frame (and a stable ``eval_set_identity``). The fixtures span a
    SPREAD of difficulties (round-robin over EASY/MEDIUM/HARD) so the pilot has a
    non-degenerate base rate + between-task variance to estimate (evaluation.md
    §2). Each fixture's oracle + per-arm resolution seed are derived purely from
    the task index, so resolution is reproducible across processes.

    SYNTHETIC: a methodology-dry-run frame, NOT real SWE-EVO tasks.
    """
    if n_tasks < 2:
        raise ValueError("n_tasks must be >= 2 for a disjoint pilot/scored split")

    strata = list(Difficulty)
    fixtures: list[TaskFixture] = []
    for i in range(n_tasks):
        difficulty = strata[i % len(strata)]  # round-robin => guaranteed spread
        task_id = f"swe-evo-synth-{i:04d}"
        oracle = OracleDescriptor(
            hidden_test_id=f"hidden::{task_id}",
            expected_output=content_digest({"task": task_id, "kind": "oracle"})[:16],
        )
        fixtures.append(
            TaskFixture(
                task_id=task_id,
                spec=(
                    f"[SYNTHETIC dry-run] Evolve module to satisfy spec #{i} "
                    f"({difficulty.value}); resolution scored by hidden oracle "
                    f"{oracle.hidden_test_id}."
                ),
                oracle=oracle,
                difficulty=difficulty,
                control_base_rate=_BASE_RATE[difficulty],
                # Process-stable per-task seed (content_digest, not hash()):
                base_resolution_seed=int(content_digest({"task": task_id, "seed": seed})[:8], 16),
            )
        )

    frame_fixtures = tuple(fixtures)
    identity = _frame_identity(frame_fixtures, seed, pilot_fraction)
    return TaskFrame(
        fixtures=frame_fixtures,
        seed=seed,
        pilot_fraction=pilot_fraction,
        eval_set_identity=identity,
    )
