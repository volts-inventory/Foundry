"""Role-ablation harness for the Phase-1.5 decision gate (roadmap §1.5, evaluation.md §2).

This module answers the **"do roles earn their place?"** question (roadmap P-F2):
start from the smallest role set (PM -> Architect -> Senior -> QA core) and KEEP a
role only if an ablation shows it MEASURABLY improves the scorecard. The 16-role
org is a ceiling to grow into, not a given. So for each role this harness runs the
eval over a task slice with that role ABLATED (disabled/bypassed) vs the full core,
and reports the per-role scorecard DELTA (resolution-rate delta + cost delta). A
role whose removal does NOT hurt the scorecard beyond a threshold is FLAGGED as
not-earning-its-place (``earns_place=False``).

SIMULATED ABLATION (load-bearing honesty flag — read before reuse)
------------------------------------------------------------------
This is a **SIMULATED ablation**, NOT a real role-bypass, and every result carries
``simulated_ablation=True`` to say so. Why simulated:

* The shipped Phase-1 graph (:func:`foundry.graph.supervisor.build_supervisor_graph`)
  is a FIXED 4-role pipeline (product -> architecture -> implementation -> quality)
  wired with hard ``add_edge`` calls and NO config seam to bypass a node. Wiring a
  real architecture-less / QA-less variant into the SHIPPED graph is out of scope
  for this task (it would touch the graph package).
* The FOUNDRY arm in :mod:`foundry.evaluation.harness` resolves iff the keyless
  pipeline reaches ``passed AND open_pr`` — a single binary that does NOT vary
  per-role, so it cannot measure a per-role delta either.

So we model each role's contribution as a deterministic resolution-rate EFFECT on
the synthetic task frame (an additive lift over the control base rate, folded into
a process-stable Bernoulli draw via ``content_digest`` — L7, never ``hash()``), and
ABLATE a role by removing its contribution from that composition. This exercises
the ablation METHODOLOGY end-to-end (the per-role delta, the earns-place rule, the
cost accounting, the determinism) on synthetic fixtures. It asserts NOTHING about a
real org design — the conclusion is about the METHOD, not a role-design verdict.
When a real per-role graph bypass exists (a later phase), swap the deterministic
contribution model for a real ablated run behind the same ``run_ablation`` API and
flip ``simulated_ablation`` to ``False``.

There is no inferential statistics here (no bootstrap/power) — just a deterministic
per-role rate + cost delta and the earns-place threshold rule. Provenance defaults
to :data:`foundry.evaluation.hypothesis.Provenance.DRY_RUN`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from foundry.canonical import content_digest
from foundry.evaluation.hypothesis import Provenance
from foundry.schemas.base import FrozenModel

if TYPE_CHECKING:
    from foundry.evaluation.task_fixtures import TaskFixture, TaskFrame

__all__ = [
    "DEFAULT_CORE_ROLES",
    "DEFAULT_EARNS_PLACE_THRESHOLD",
    "DEFAULT_ROLE_CONTRIBUTION",
    "DEFAULT_ROLE_COST_MICROUSD",
    "AblationResult",
    "Role",
    "RoleAblationDelta",
    "run_ablation",
]


class Role(StrEnum):
    """The core role set the ablation starts from (roadmap P-F2, the smallest set).

    PM -> Architect -> Senior -> QA: the ~5-role floor the cited evidence
    (MetaGPT/ChatDev) validates. The ablation measures, per role, the scorecard
    delta from removing it; roles past this core are added only if an ablation
    justifies them (a later growth step, same API).
    """

    PM = "pm"  # Product Manager (Layer 1)
    ARCHITECT = "architect"  # Principal Architect (Layer 2)
    SENIOR = "senior"  # Senior/Implementation (Layer 3)
    QA = "qa"  # Quality / verification (Layer 4)


# The CORE role set the ablation sweeps, in SOP order. Configurable per call.
DEFAULT_CORE_ROLES: tuple[Role, ...] = (Role.PM, Role.ARCHITECT, Role.SENIOR, Role.QA)

# SIMULATED additive resolution-rate contribution per role (the lift each role adds
# over the control base rate in the full-core composition). DRY-RUN knobs — a
# MANUFACTURED stand-in for "what each role buys the scorecard", NOT a claim. QA is
# tuned to ~0 here so the methodology can demonstrate a role that does NOT earn its
# place (its ablation leaves the scorecard unchanged -> earns_place=False); the
# others contribute a measurable lift. Swap for real ablated-run deltas when a real
# per-role graph bypass exists.
DEFAULT_ROLE_CONTRIBUTION: dict[Role, float] = {
    Role.PM: 0.10,
    Role.ARCHITECT: 0.15,
    Role.SENIOR: 0.20,
    Role.QA: 0.0,  # a deliberately non-earning role for the dry-run methodology
}

# SIMULATED per-attempt USD cost each role adds (micro-USD, never float — base.py
# convention). Ablating a role removes BOTH its contribution AND its cost, so the
# cost delta (full - ablated) is positive for every role: removing a role is
# cheaper. DRY-RUN figures, not metered.
DEFAULT_ROLE_COST_MICROUSD: dict[Role, int] = {
    Role.PM: 200_000,
    Role.ARCHITECT: 300_000,
    Role.SENIOR: 400_000,
    Role.QA: 250_000,
}

# A role EARNS ITS PLACE iff removing it drops the resolution rate by at least this
# much (the scorecard is MEASURABLY hurt). A removal that leaves the rate within the
# threshold flags the role as not-earning-its-place. DRY-RUN threshold.
DEFAULT_EARNS_PLACE_THRESHOLD: float = 0.02


class RoleAblationDelta(FrozenModel):
    """The per-role scorecard delta from ablating ONE role vs the full core.

    ``delta = full_rate - ablated_rate`` is the resolution-rate hit from removing
    the role; ``cost_delta = full_cost - ablated_cost`` (>= 0) is the per-attempt
    USD saved by removing it. ``earns_place`` is True iff ``delta`` clears the
    ``earns_place_threshold`` (removing the role MEASURABLY hurts the scorecard).

    SIMULATED: derived from a deterministic contribution model, not a real ablated
    run (see module docstring).
    """

    role: Role
    full_rate: float = Field(ge=0.0, le=1.0)  # resolution rate with the full core
    ablated_rate: float = Field(ge=0.0, le=1.0)  # rate with this role removed
    delta: float  # full_rate - ablated_rate (can be <= 0 for a non-earning role)
    full_cost_microusd: int = Field(ge=0)  # total core cost over the slice
    ablated_cost_microusd: int = Field(ge=0)  # total cost with this role removed
    cost_delta_microusd: int  # full_cost - ablated_cost (>= 0: removal is cheaper)
    earns_place_threshold: float = Field(gt=0.0)
    earns_place: bool  # delta >= threshold
    version: int = 1


class AblationResult(FrozenModel):
    """All per-role deltas for one role-ablation sweep over one task slice.

    Frozen + content-addressable. Deterministic: the same ``(frame, roles, seed,
    k_repeats, threshold, contribution, cost)`` yields an IDENTICAL result.

    HONESTY: ``simulated_ablation`` is True — this is a deterministic contribution
    model, NOT a real per-role graph bypass (module docstring); ``provenance``
    defaults to DRY_RUN. The result asserts NOTHING about a real org design.
    """

    eval_set_identity: str  # the slice the sweep ran over (binds to the frame — EV7)
    slice_name: str
    roles: tuple[Role, ...]  # the core role set swept, in order
    seed: int = Field(ge=0)
    k_repeats: int = Field(gt=0)
    full_rate: float = Field(ge=0.0, le=1.0)  # full-core resolution rate (shared baseline)
    deltas: tuple[RoleAblationDelta, ...]
    simulated_ablation: bool  # True: contribution model, not a real bypass (HONESTY)
    provenance: Provenance = Provenance.DRY_RUN
    version: int = 1

    def delta_for(self, role: Role) -> RoleAblationDelta:
        """Look up the :class:`RoleAblationDelta` for ``role`` (raises if absent)."""
        for d in self.deltas:
            if d.role is role:
                return d
        raise KeyError(role)

    @property
    def non_earning_roles(self) -> tuple[Role, ...]:
        """Roles flagged as NOT earning their place (``earns_place=False``)."""
        return tuple(d.role for d in self.deltas if not d.earns_place)


def _resolves(
    fixture: TaskFixture,
    active_contribution: float,
    *,
    seed: int,
    repeat: int,
) -> bool:
    """Deterministic Bernoulli draw for one (fixture, repeat) under a role set.

    The resolution probability is ``control_base_rate + active_contribution``
    (the sum of the ACTIVE roles' contributions), clamped to ``[0, 0.99]``. The
    uniform draw folds ``(base_resolution_seed, repeat, seed)`` via
    ``content_digest`` (L7 — never ``hash()``, which is process-randomized) — it is
    INDEPENDENT of the role set, so the SAME draw is compared against the full-core
    vs the ablated rate. This is the COMMON-RANDOM-NUMBERS (paired) ablation: a
    role with zero contribution leaves the rate target unchanged, so its full and
    ablated rates are IDENTICAL (delta == 0) — no sampling noise can manufacture a
    spurious earns-place. Resolves iff the draw is below the rate.

    SIMULATED: a manufactured stand-in for an ablated run (module docstring).
    """
    digest = content_digest(
        {
            "base_resolution_seed": fixture.base_resolution_seed,
            "repeat": repeat,
            "seed": seed,
        }
    )
    draw = int(digest[:8], 16) / 0x1_0000_0000  # uniform in [0, 1)
    rate = min(0.99, max(0.0, fixture.control_base_rate + active_contribution))
    return draw < rate


def _rate_and_cost(
    fixtures: tuple[TaskFixture, ...],
    active_roles: tuple[Role, ...],
    *,
    seed: int,
    k_repeats: int,
    contribution: dict[Role, float],
    cost: dict[Role, int],
) -> tuple[float, int]:
    """Resolution rate + total USD cost over ``fixtures`` for one ACTIVE role set.

    The rate is intent-to-treat (resolved / (len(fixtures) * k_repeats) — every
    initiated attempt counts; nothing is dropped, mirroring the harness ITT rule).
    The cost is ``Σ_attempts Σ_active_roles role_cost`` — removing a role removes
    its per-attempt cost (so an ablated set is cheaper).
    """
    active_contribution = sum(contribution[r] for r in active_roles)
    per_attempt_cost = sum(cost[r] for r in active_roles)
    attempted = len(fixtures) * k_repeats
    resolved = 0
    for fixture in fixtures:
        for repeat in range(k_repeats):
            if _resolves(fixture, active_contribution, seed=seed, repeat=repeat):
                resolved += 1
    rate = resolved / attempted if attempted else 0.0
    return rate, per_attempt_cost * attempted


def run_ablation(
    frame: TaskFrame,
    *,
    roles: tuple[Role, ...] = DEFAULT_CORE_ROLES,
    seed: int = 0,
    k_repeats: int = 2,
    threshold: float = DEFAULT_EARNS_PLACE_THRESHOLD,
    contribution: dict[Role, float] | None = None,
    cost: dict[Role, int] | None = None,
    use_pilot_slice: bool = False,
) -> AblationResult:
    """Run the role-ablation sweep over ``frame``'s scored slice (roadmap §1.5).

    For each role in ``roles`` (the core set), run the eval over the slice with the
    full core vs that role ABLATED, and record the per-role scorecard delta — the
    resolution-rate delta + the USD cost delta — plus the earns-place verdict.

    Deterministic: the same ``(frame, roles, seed, k_repeats, threshold,
    contribution, cost, use_pilot_slice)`` yields an IDENTICAL :class:`AblationResult`.

    Parameters
    ----------
    frame:
        The synthetic :class:`~foundry.evaluation.task_fixtures.TaskFrame`. The
        sweep runs over its ``scored_slice`` (or ``pilot_slice`` if
        ``use_pilot_slice``); the result binds to that slice's identity.
    roles:
        The core role set to sweep (defaults to PM -> Architect -> Senior -> QA).
        Every role must have a ``contribution`` and ``cost`` entry.
    seed:
        Folded into every Bernoulli draw (the simulated ablation's determinism).
    k_repeats:
        Repeats per task (ITT denominator is ``len(slice) * k_repeats``).
    threshold:
        A role EARNS ITS PLACE iff removing it drops the rate by >= this much.
    contribution / cost:
        Per-role resolution-rate lift / per-attempt USD cost (default to the
        DRY-RUN tables). Ablating a role removes BOTH.
    use_pilot_slice:
        Run over the pilot slice instead of the scored slice (default scored).

    HONESTY: ``simulated_ablation=True`` — a deterministic contribution model, NOT
    a real per-role graph bypass (module docstring). The conclusion is about the
    METHOD; it asserts NOTHING about a real org design.
    """
    if not roles:
        raise ValueError("roles must be a non-empty core set to ablate")
    contribution = dict(DEFAULT_ROLE_CONTRIBUTION if contribution is None else contribution)
    cost = dict(DEFAULT_ROLE_COST_MICROUSD if cost is None else cost)
    missing = [r for r in roles if r not in contribution or r not in cost]
    if missing:
        raise ValueError(f"missing contribution/cost for roles: {missing}")

    slice_ = frame.pilot_slice if use_pilot_slice else frame.scored_slice
    # Restrict fixtures to the slice's ids (sorted -> order-independent), mirroring
    # the harness/pilot pattern so a stray lookup can't touch an off-slice task.
    fixtures = tuple(frame.fixture(tid) for tid in sorted(slice_.task_ids))

    full_rate, full_cost = _rate_and_cost(
        fixtures,
        roles,
        seed=seed,
        k_repeats=k_repeats,
        contribution=contribution,
        cost=cost,
    )

    deltas: list[RoleAblationDelta] = []
    for role in roles:
        ablated = tuple(r for r in roles if r is not role)
        if ablated:
            ablated_rate, ablated_cost = _rate_and_cost(
                fixtures,
                ablated,
                seed=seed,
                k_repeats=k_repeats,
                contribution=contribution,
                cost=cost,
            )
        else:
            # Single-role core: "ablating" it leaves the empty set; model the
            # empty org as the bare control base rate at zero added cost.
            ablated_rate, ablated_cost = _rate_and_cost(
                fixtures,
                (),
                seed=seed,
                k_repeats=k_repeats,
                contribution=contribution,
                cost=cost,
            )
        delta = full_rate - ablated_rate
        deltas.append(
            RoleAblationDelta(
                role=role,
                full_rate=full_rate,
                ablated_rate=ablated_rate,
                delta=delta,
                full_cost_microusd=full_cost,
                ablated_cost_microusd=ablated_cost,
                cost_delta_microusd=full_cost - ablated_cost,
                earns_place_threshold=threshold,
                earns_place=delta >= threshold,
            )
        )

    return AblationResult(
        eval_set_identity=slice_.identity,
        slice_name=slice_.name,
        roles=tuple(roles),
        seed=seed,
        k_repeats=k_repeats,
        full_rate=full_rate,
        deltas=tuple(deltas),
        simulated_ablation=True,  # HONESTY: contribution model, not a real bypass
    )
