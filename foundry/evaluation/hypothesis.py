"""Central-hypothesis schemas for the Phase-1.5 decision gate (evaluation.md §1/§2/§2.5).

This module is a **pure schema/enum layer**: frozen pydantic v2 models that
capture the *pre-registered* central hypothesis of the Foundry program — the bet
that org/graph **structure** (planning, decomposition, gates, checkpoints, HITL)
beats a strong single agent on **long-horizon** work at equal cost — so the
Phase-1.5 confirmatory A/B can be hashed into a run manifest and actually fail.

HONESTY NOTE (read before reuse): this harness is a **methodology dry-run on
synthetic fixtures**, NOT funded real evidence. Every model carries a
`provenance` field defaulting to :data:`Provenance.DRY_RUN`. A model with
`provenance == DRY_RUN` makes **no claim about real Foundry vs baseline
performance** — it only fixes the *shape* of the pre-registration so the later
M1.5 tasks (preregistration.py, decision_table.py, the powered A/B) build on a
stable, validated contract. There is **no statistics logic here** — power
calcs, bootstrap, alpha-spending, and scoring live in later modules.

The minimum-detectable-effect (MDE), alpha, and the funded power rung are
fields the *program* commits to **before** any scored run (evaluation.md §2,
"Pre-registered funded power rung (BLOCKER)"); this module only enforces their
basic validity (`0 < alpha < 1`, `mde > 0`, etc.), never their scientific
adequacy.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from foundry.schemas.base import FrozenModel


class Arm(StrEnum):
    """The three pre-registered A/B arms (evaluation.md §2), chosen to isolate
    *what does the work* — roles vs mere verification."""

    FOUNDRY = "foundry"  # the full org/graph structure
    BASELINE_SINGLE_AGENT = "baseline_single_agent"  # strong agentic loop, parity substrate
    SINGLE_AGENT_PLUS_GATES = "single_agent_plus_gates"  # same gates, no role split


class Equalizer(StrEnum):
    """How the arms are held to equal cost (evaluation.md §2, "Equalizer").

    Equal-USD is the primary, economically-honest equalizer + headline viability
    metric; equal effective-task-token is the decision-grade secondary that
    separates {USD-null, token-positive}.
    """

    EQUAL_USD = "equal_usd"  # primary / headline
    EQUAL_EFFECTIVE_TASK_TOKEN = "equal_effective_task_token"  # decision-grade secondary


class DecisionClass(StrEnum):
    """The Phase-1.5 decision-table verdict classes (evaluation.md §2.5).

    The {primary x secondary} cross-product is pre-registered; these are its
    outcomes. A null at the funded MDE is a real KILL trigger, not a cue to slide
    down the de-scoping ladder (§1/§8).
    """

    PROCEED = "proceed"  # primary positive at funded MDE/alpha
    HARD_KILL = "hard_kill"  # USD-null AND token-null at funded MDE
    BOUNDED_OVERHEAD_REDESIGN = "bounded_overhead_redesign"  # USD-null, token-positive


class PrimaryMetric(FrozenModel):
    """The single confirmatory primary endpoint (evaluation.md §2/§7).

    SWE-EVO-style long-horizon resolution rate, scored on **oracle / hidden-test
    signals only** (no LLM judge enters the primary verdict — §4), under an
    **equal-USD** budget constraint, intent-to-treat (denominator = all
    *initiated* tasks per arm; any non-completion scores unresolved — §2).
    """

    name: str = "swe_evo_resolution_rate"
    description: str = (
        "SWE-EVO-style long-horizon multi-commit resolution rate, oracle-scored "
        "(hidden-test signals only; no LLM judge in the primary verdict)."
    )
    higher_is_better: bool = True
    oracle_scored: bool = True  # §4: primary is never LLM-judge-scored
    equalizer: Equalizer = Equalizer.EQUAL_USD  # §2: equal-USD primary
    intent_to_treat: bool = True  # §2 ITT: all initiated tasks; non-completion = unresolved
    version: int = 1


class PowerRung(FrozenModel):
    """The pre-registered **funded** power rung (evaluation.md §2, BLOCKER).

    The funded rung's MDE + power + budget are committed *before* the first
    scored run; walking the de-scoping ladder after cost is observed is
    forbidden. The run can only license claims at or above `mde`. This is a
    placeholder contract — values are dry-run fixtures until the
    variance/base-rate pilot (§2) freezes them.
    """

    name: str = "funded"
    mde: float = Field(gt=0.0)  # minimum detectable effect (resolution-rate margin)
    target_power: float = Field(default=0.8, gt=0.0, lt=1.0)
    n_tasks: int | None = Field(default=None, gt=0)  # frozen off the pilot (§2)
    k_repeats: int | None = Field(default=None, gt=0)  # repeats/task for nondeterminism
    funded_budget_microusd: int | None = Field(default=None, ge=0)  # micro-USD (never float)


class Provenance(StrEnum):
    """Evidence provenance for honesty (this harness vs a funded real run)."""

    DRY_RUN = "dry_run"  # methodology dry-run on synthetic fixtures — NOT real evidence
    PILOT = "pilot"  # the budgeted variance/base-rate pilot (never scored as the A/B)
    SCORED = "scored"  # the funded, pre-registered confirmatory run


class CentralHypothesis(FrozenModel):
    """The pre-registered central hypothesis for the Phase-1.5 decision gate.

    Captures H0/H1, the primary metric + comparison (FOUNDRY vs BASELINE at
    <= cost), the committed-before-scored-runs MDE/alpha, the funded power rung,
    and the decision classes. Frozen + content-addressable via the
    :class:`FrozenModel` base (``content_identity()`` stable across construction
    of equal records) so a pre-registration can be hashed into the run manifest.

    HONESTY: defaults to ``provenance == Provenance.DRY_RUN`` — a methodology
    dry-run on synthetic fixtures. Such an instance asserts NOTHING about real
    Foundry-vs-baseline performance; it only fixes the pre-registration shape.
    Statistics (power, bootstrap, alpha-spending, scoring) are NOT in this
    module — they are later M1.5 tasks.
    """

    h0: str = (
        "Org/graph structure does not improve long-horizon SWE-EVO resolution "
        "over a strong single agent at equal USD cost (effect <= 0 at the funded MDE)."
    )
    h1: str = (
        "Org/graph structure improves long-horizon SWE-EVO resolution over a "
        "strong single agent at equal USD cost (effect >= funded MDE)."
    )
    primary_metric: PrimaryMetric = Field(default_factory=PrimaryMetric)

    # The confirmatory comparison: FOUNDRY beats BASELINE at <= cost (§2/§7).
    treatment_arm: Arm = Arm.FOUNDRY
    control_arm: Arm = Arm.BASELINE_SINGLE_AGENT
    at_most_equal_cost: bool = True  # comparison holds the equalizer fixed

    # Committed BEFORE any scored run (§2, BLOCKER):
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)  # pre-registered significance level
    mde: float = Field(gt=0.0)  # minimum detectable effect, committed pre-run
    funded_power_rung: PowerRung  # the rung the program will actually fund

    decision_classes: tuple[DecisionClass, ...] = (
        DecisionClass.PROCEED,
        DecisionClass.HARD_KILL,
        DecisionClass.BOUNDED_OVERHEAD_REDESIGN,
    )

    provenance: Provenance = Provenance.DRY_RUN  # honesty default (see class docstring)
    dry_run: bool = True  # convenience mirror of `provenance == DRY_RUN`
    version: int = 1
