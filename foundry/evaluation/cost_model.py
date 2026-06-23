"""Per-feature + A/B-scale cost & wall-clock model (docs/evaluation.md §8).

This module makes the §8 "viability" / "<=5% overhead" claims **falsifiable** by
turning them into a concrete, deterministic accounting model rather than an
assertion. It computes (a) the **per-feature** cost from structured run signals
and (b) the **A/B-scale** roll-up cost of running the pre-registered design at a
given ``(n, k)`` rung (``3 arms x n tasks x k repeats x per-task cost``).

THE LOAD-BEARING POINT (docs/evaluation.md §8, COST-F5): the token-only
enumeration **understates** what the design itself mandates. The cost is
dominated by **non-token** terms:

* **wall-clock** — lead time on the scorecard (§7), the queue/replica sizing
  input (operations.md §6);
* **trusted-runner compute** — the behavioral-invariance discriminator runs the
  mutant + perturbation families on CPU/GPU, NOT model tokens, and scales with
  the §4 adequacy/diversity floor, not feature size;
* **human-review (HITL) time** — gate/release decisions per feature
  (operations.md §7), a bounded budget, NOT an unbounded externality;
* **infra** — replicas, queue depth, reserved burst capacity (operations.md §6).

So an arm that is **cheaper on model tokens** can be **more expensive
end-to-end**: :meth:`CostBreakdown.non_token_dominates` surfaces exactly that, and
the §7 scorecard / §9 optimization consume the full breakdown, never the token
line alone.

HONESTY NOTE (read before reuse): this is a **methodology dry-run**. The
**FORMULA / accounting is real** (integer micro-USD for money, integer units for
compute, integer ms for time — no floats for money, the pinned global constraint;
budget_controller.py / state_sha.py style), but the **input numbers are
SYNTHETIC** — real funded per-node token counts, $/hr runner rates, and measured
HITL minutes are not available at Phase 1.5. A :class:`CostBreakdown` carries
``provenance == Provenance.DRY_RUN`` and asserts NOTHING about real cost; it only
fixes the accounting shape so the scorecard / optimization build on a stable
contract. Flip to ``Provenance.SCORED`` once funded inputs exist.

Everything here is PURE / DETERMINISTIC: the same signals yield an identical
:class:`CostBreakdown` (no RNG, no I/O, no wall-clock read — wall-clock is an
*input signal*, never ``time.time()``). Money is summed in integer micro-USD so
there is no float drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from foundry.evaluation.hypothesis import Arm, Provenance
from foundry.schemas.base import FrozenModel

__all__ = [
    "CostBreakdown",
    "CostModel",
    "RunSignals",
]


class RunSignals(FrozenModel):
    """The structured signals one feature (or one per-arm task run) emits.

    These are the *inputs* to the accounting — already-metered counts, NOT a
    live measurement. Money is integer micro-USD, compute is integer units, time
    is integer milliseconds (no floats for money — the pinned global constraint,
    base.py / budget_controller.py). Wall-clock is an INPUT signal here, never a
    ``time.time()`` read, so the model stays pure/deterministic.

    The fields map 1:1 onto the §8 cost terms:

    * ``model_token_microusd`` — the token-only enumeration (nodes x loop
      iterations x tokens/node x tier price + analyzer invocations + cache
      assumptions). This is the term §8 warns is the CHEAP part.
    * ``compute_units`` — trusted-runner CPU/GPU for the behavioral-invariance
      discriminator (mutant + perturbation families) and ML eval-set re-runs
      (§8a). An integer compute dimension DISTINCT from USD (state_sha.py
      ComputeBudget). Priced into USD via ``compute_unit_microusd``.
    * ``compute_unit_microusd`` — $/unit (e.g. a GPU-minute), integer micro-USD.
    * ``wall_clock_ms`` — lead time per feature (§7 scorecard), the
      queue/replica sizing input (operations.md §6). Priced via
      ``wall_clock_unit_microusd`` (e.g. an opportunity-cost / runner-rental
      $/ms) so wall-clock contributes to the USD total too.
    * ``wall_clock_unit_microusd`` — micro-USD charged per wall-clock ms.
    * ``human_review_ms`` — HITL gate/release decision time (operations.md §7),
      a BOUNDED per-tier budget, not an unbounded externality. Priced via
      ``human_review_unit_microusd`` (a loaded reviewer $/ms).
    * ``human_review_unit_microusd`` — micro-USD per HITL ms.
    * ``infra_microusd`` — replicas, queue depth, reserved burst capacity
      (operations.md §6) attributed to this feature, already in micro-USD.
    """

    # Token line — the CHEAP part §8 warns about (already in micro-USD).
    model_token_microusd: int = Field(default=0, ge=0)

    # Trusted-runner compute (§8a): integer units priced into USD.
    compute_units: int = Field(default=0, ge=0)
    compute_unit_microusd: int = Field(default=0, ge=0)

    # Wall-clock (§7/§8): integer ms priced into USD (opportunity / rental cost).
    wall_clock_ms: int = Field(default=0, ge=0)
    wall_clock_unit_microusd: int = Field(default=0, ge=0)

    # Human-review / HITL (§7/operations §7): integer ms priced into USD.
    human_review_ms: int = Field(default=0, ge=0)
    human_review_unit_microusd: int = Field(default=0, ge=0)

    # Infra (operations §6): replicas / queue / reserved burst, already micro-USD.
    infra_microusd: int = Field(default=0, ge=0)

    version: int = 1


class CostBreakdown(FrozenModel):
    """A frozen, term-by-term cost record (docs/evaluation.md §8) + the total.

    Enumerates EACH term so the §7 scorecard / §9 optimization read the *whole*
    cost, never the token line alone. All money is integer micro-USD (no floats —
    no drift); wall-clock / human-review time are ALSO carried as raw integer ms
    (the §7 scorecard reports wall-clock per feature in time, not only its priced
    USD), and compute as raw integer units.

    THE LOAD-BEARING METHODS:

    * :meth:`non_token_microusd` — the sum of all NON-token USD terms.
    * :meth:`non_token_dominates` — True iff the non-token terms exceed the token
      term, i.e. a "cheaper-on-tokens" arm may be more expensive end-to-end
      (docs/evaluation.md §8, COST-F5). This is the bit the scorecard surfaces.

    HONESTY: ``provenance`` defaults to DRY_RUN — the accounting is real but the
    inputs are synthetic; this asserts NOTHING about real cost.
    """

    # USD terms (integer micro-USD — never float).
    model_token_microusd: int = Field(ge=0)
    compute_microusd: int = Field(ge=0)  # compute_units x compute_unit_microusd
    wall_clock_microusd: int = Field(ge=0)  # wall_clock_ms x wall_clock_unit_microusd
    human_review_microusd: int = Field(ge=0)  # human_review_ms x human_review_unit_microusd
    infra_microusd: int = Field(ge=0)
    total_microusd: int = Field(ge=0)  # sum of the five USD terms above

    # Raw non-USD dimensions carried alongside (the scorecard reports these too —
    # §7 wall-clock-per-feature is a TIME budget, compute is a UNIT budget).
    compute_units: int = Field(ge=0)
    wall_clock_ms: int = Field(ge=0)
    human_review_ms: int = Field(ge=0)

    provenance: Provenance = Provenance.DRY_RUN
    dry_run: bool = True
    version: int = 1

    def non_token_microusd(self) -> int:
        """Sum of all NON-token USD terms (compute + wall-clock + human + infra).

        These are the §8 DOMINANT terms — the ones the token-only enumeration
        understates. Integer micro-USD throughout (no float drift).
        """
        return (
            self.compute_microusd
            + self.wall_clock_microusd
            + self.human_review_microusd
            + self.infra_microusd
        )

    def non_token_dominates(self) -> bool:
        """True iff non-token terms EXCEED the model-token term (§8, COST-F5).

        When this is True, a "cheaper on tokens" arm can be MORE expensive
        end-to-end — the whole reason §8 carries the dominant non-token terms and
        the reason the scorecard / optimization must read ``total_microusd``, not
        ``model_token_microusd``.
        """
        return self.non_token_microusd() > self.model_token_microusd

    def token_fraction(self) -> float:
        """The model-token share of the total (a rate, not money — float OK).

        0.0 when the total is 0 (degenerate empty feature). A SMALL fraction is
        the §8 warning made concrete: most of the cost is non-token.
        """
        if self.total_microusd == 0:
            return 0.0
        return self.model_token_microusd / self.total_microusd


class CostModel:
    """The deterministic §8 cost accountant (per-feature + A/B-scale roll-up).

    Pure: every method is a function of its inputs only (no RNG, no I/O, no
    ``time.time()`` — wall-clock is an input SIGNAL). Money is accumulated in
    integer micro-USD (the pinned global constraint — base.py /
    budget_controller.py), so summing across terms / arms / tasks never drifts.

    HONESTY: ``provenance`` defaults to DRY_RUN; every :class:`CostBreakdown` this
    model mints carries it. The accounting is real; the inputs are synthetic.
    """

    def __init__(self, *, provenance: Provenance = Provenance.DRY_RUN) -> None:
        self.provenance = provenance

    # -- per-feature cost --------------------------------------------------
    def feature_cost(self, run_signals: RunSignals) -> CostBreakdown:
        """The per-feature cost breakdown (docs/evaluation.md §8).

        Prices each §8 term into integer micro-USD and sums them:

        * model tokens (already micro-USD — the CHEAP part §8 warns about);
        * compute = ``compute_units x compute_unit_microusd`` (§8a trusted-runner);
        * wall-clock = ``wall_clock_ms x wall_clock_unit_microusd`` (§7 lead time);
        * human-review = ``human_review_ms x human_review_unit_microusd`` (HITL);
        * infra (already micro-USD — replicas / queue / burst, operations §6).

        The total is the integer sum of the five — so the breakdown surfaces that
        the non-token terms can DOMINATE (:meth:`CostBreakdown.non_token_dominates`).
        Deterministic: same ``run_signals`` -> identical breakdown.
        """
        compute_microusd = run_signals.compute_units * run_signals.compute_unit_microusd
        wall_clock_microusd = (
            run_signals.wall_clock_ms * run_signals.wall_clock_unit_microusd
        )
        human_review_microusd = (
            run_signals.human_review_ms * run_signals.human_review_unit_microusd
        )
        total_microusd = (
            run_signals.model_token_microusd
            + compute_microusd
            + wall_clock_microusd
            + human_review_microusd
            + run_signals.infra_microusd
        )
        return CostBreakdown(
            model_token_microusd=run_signals.model_token_microusd,
            compute_microusd=compute_microusd,
            wall_clock_microusd=wall_clock_microusd,
            human_review_microusd=human_review_microusd,
            infra_microusd=run_signals.infra_microusd,
            total_microusd=total_microusd,
            compute_units=run_signals.compute_units,
            wall_clock_ms=run_signals.wall_clock_ms,
            human_review_ms=run_signals.human_review_ms,
            provenance=self.provenance,
            dry_run=self.provenance is Provenance.DRY_RUN,
        )

    # -- A/B-scale roll-up -------------------------------------------------
    def ab_scale_cost(
        self,
        arms: Mapping[Arm, RunSignals],
        n: int,
        k: int,
    ) -> CostBreakdown:
        """The A/B-scale roll-up at the ``(n, k)`` rung (docs/evaluation.md §8).

        The decisive run must be shown affordable, not just one feature: the
        per-task cost is multiplied through the pre-registered design
        ``Σ_arm (per-task cost of `arm`) x n tasks x k repeats``. ``arms`` maps
        EACH arm to its per-task :class:`RunSignals` (Foundry's per-task cost is
        HIGHER — it pays coordination + monitor overhead the baseline pours into
        the task, §8 roll-up). Every term scales with ``n x k`` per arm, then sums
        across arms.

        The result is a :class:`CostBreakdown` whose terms are the design-wide
        totals — so ``non_token_dominates`` answers the A/B-scale question too
        (is the powered run dominated by wall-clock / runner-compute / HITL, not
        tokens?), feeding the operations.md §6 replica/queue/burst sizing.

        Deterministic and integer-money throughout: each arm's per-task breakdown
        is scaled by the integer ``n x k`` and the scaled integers are summed, so
        there is no float drift. Raises ``ValueError`` for non-positive ``n``/``k``
        or an empty ``arms`` map.
        """
        if n < 1 or k < 1:
            raise ValueError(f"n and k must be >= 1 (got n={n}, k={k})")
        if not arms:
            raise ValueError("ab_scale_cost requires at least one arm")

        rung = n * k  # integer attempts per arm (§2 power rung)

        # Accumulate every term in integer micro-USD / units / ms across arms.
        model_token = 0
        compute_usd = 0
        wall_clock_usd = 0
        human_usd = 0
        infra = 0
        total = 0
        compute_units = 0
        wall_clock_ms = 0
        human_review_ms = 0

        for signals in arms.values():
            per_task = self.feature_cost(signals)
            model_token += per_task.model_token_microusd * rung
            compute_usd += per_task.compute_microusd * rung
            wall_clock_usd += per_task.wall_clock_microusd * rung
            human_usd += per_task.human_review_microusd * rung
            infra += per_task.infra_microusd * rung
            total += per_task.total_microusd * rung
            compute_units += per_task.compute_units * rung
            wall_clock_ms += per_task.wall_clock_ms * rung
            human_review_ms += per_task.human_review_ms * rung

        return CostBreakdown(
            model_token_microusd=model_token,
            compute_microusd=compute_usd,
            wall_clock_microusd=wall_clock_usd,
            human_review_microusd=human_usd,
            infra_microusd=infra,
            total_microusd=total,
            compute_units=compute_units,
            wall_clock_ms=wall_clock_ms,
            human_review_ms=human_review_ms,
            provenance=self.provenance,
            dry_run=self.provenance is Provenance.DRY_RUN,
        )
