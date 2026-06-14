"""Budget Controller — enforcement at superstep boundaries. (arch §6, gov §7)

LangGraph supersteps are atomic (no mid-node checkpoint), so:

- **Caps are checked BEFORE dispatching the next superstep**
  (`authorize_superstep`), never inside a running node.
- **Long node bodies** poll a cooperative `CancellationToken` (thread-safe)
  and persist partial progress before yielding — the controller signals, it
  never kills mid-write.
- **Per-phase and per-loop caps** in addition to the absolute cap; per-loop
  caps draw from the rework reserve, modeled here as their own bucket.
- **Global convergence bound** (arch §7): `note_fix_attempt(tree_sha)` counts
  DISTINCT tree_shas produced since the last all-green; crossing the bound
  emits a NON-CONVERGENCE escalation — a *different event type* from a
  budget breach, so operators get the right diagnosis before maximum spend.

All monetary values are integer micro-USD (pinned global constraint).
"""

from __future__ import annotations

import threading
from enum import StrEnum

from pydantic import Field

from foundry.schemas.base import BudgetLedger, FrozenModel

DEFAULT_NON_CONVERGENCE_BOUND = 5


class CancellationToken:
    """Thread-safe cooperative-cancellation flag (arch §6).

    The controller `cancel()`s it at a boundary decision; long node bodies
    poll `cancelled` between units of work and commit partial progress
    before yielding. There is deliberately no `reset()` — a cancelled
    superstep yields; resume gets a fresh token.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class GovernanceEventKind(StrEnum):
    """Budget breach and non-convergence are DIFFERENT diagnoses (arch §7)."""

    BUDGET_BREACH = "budget_breach"
    NON_CONVERGENCE = "non_convergence"


class GovernanceEvent(FrozenModel):
    kind: GovernanceEventKind
    reason: str
    details: tuple[str, ...] = ()


class BudgetCaps(FrozenModel):
    """Absolute + per-phase + per-loop caps, integer micro-USD."""

    total_microusd: int
    per_phase_microusd: dict[str, int] = Field(default_factory=dict)
    per_loop_microusd: int | None = None  # draws from the rework reserve (gov §7)


class SuperstepRequest(FrozenModel):
    """What the next superstep intends to spend (estimate, micro-USD)."""

    phase: str
    est_spend_microusd: int
    loop_key: str | None = None  # set when the superstep is a rework-loop attempt


class BudgetDecision(FrozenModel):
    allowed: bool
    reason: str
    cap_kind: str | None = None  # "total" | "phase" | "loop" when denied
    remaining_microusd: int = 0
    event: GovernanceEvent | None = None  # BUDGET_BREACH when denied


class BudgetController:
    """Single-writer budget authority; checks happen at superstep boundaries."""

    def __init__(
        self,
        caps: BudgetCaps,
        *,
        non_convergence_bound: int = DEFAULT_NON_CONVERGENCE_BOUND,
    ) -> None:
        if non_convergence_bound < 1:
            raise ValueError("non_convergence_bound must be >= 1")
        self.caps = caps
        self.non_convergence_bound = non_convergence_bound
        self._phase_spend: dict[str, int] = {}
        self._loop_spend: dict[str, int] = {}
        # distinct tree_shas produced since last all-green (arch §7):
        self._fix_tree_shas: set[str] = set()
        self._non_convergence_escalated = False

    # -- superstep-boundary authorization ---------------------------------
    def authorize_superstep(self, ledger: BudgetLedger, requested: SuperstepRequest) -> (
        BudgetDecision
    ):
        """Check caps BEFORE dispatch (arch §6) — never called inside a node."""
        projected_total = ledger.spend_microusd + requested.est_spend_microusd
        if projected_total > self.caps.total_microusd:
            return self._deny(
                "total",
                f"projected spend {projected_total}µ$ exceeds absolute cap "
                f"{self.caps.total_microusd}µ$",
                remaining=self.caps.total_microusd - ledger.spend_microusd,
            )

        phase_cap = self.caps.per_phase_microusd.get(requested.phase)
        phase_spent = self._phase_spend.get(requested.phase, 0)
        if phase_cap is not None and phase_spent + requested.est_spend_microusd > phase_cap:
            return self._deny(
                "phase",
                f"phase {requested.phase!r} projected spend "
                f"{phase_spent + requested.est_spend_microusd}µ$ exceeds phase cap "
                f"{phase_cap}µ$",
                remaining=phase_cap - phase_spent,
            )

        if requested.loop_key is not None and self.caps.per_loop_microusd is not None:
            loop_spent = self._loop_spend.get(requested.loop_key, 0)
            if loop_spent + requested.est_spend_microusd > self.caps.per_loop_microusd:
                return self._deny(
                    "loop",
                    f"loop {requested.loop_key!r} projected spend "
                    f"{loop_spent + requested.est_spend_microusd}µ$ exceeds per-loop cap "
                    f"{self.caps.per_loop_microusd}µ$ (rework reserve)",
                    remaining=self.caps.per_loop_microusd - loop_spent,
                )

        return BudgetDecision(
            allowed=True,
            reason="within caps",
            remaining_microusd=self.caps.total_microusd - projected_total,
        )

    @staticmethod
    def _deny(cap_kind: str, reason: str, *, remaining: int) -> BudgetDecision:
        return BudgetDecision(
            allowed=False,
            reason=reason,
            cap_kind=cap_kind,
            remaining_microusd=max(remaining, 0),
            event=GovernanceEvent(
                kind=GovernanceEventKind.BUDGET_BREACH,
                reason=reason,
                details=(cap_kind,),
            ),
        )

    def record_superstep(self, requested: SuperstepRequest, actual_spend_microusd: int) -> None:
        """Book actual spend after a superstep completes (boundary accounting)."""
        self._phase_spend[requested.phase] = (
            self._phase_spend.get(requested.phase, 0) + actual_spend_microusd
        )
        if requested.loop_key is not None:
            self._loop_spend[requested.loop_key] = (
                self._loop_spend.get(requested.loop_key, 0) + actual_spend_microusd
            )

    # -- global convergence bound (arch §7) ---------------------------------
    def note_fix_attempt(self, tree_sha: str) -> GovernanceEvent | None:
        """Count DISTINCT tree_shas since all-green; crossing the bound emits
        a NON_CONVERGENCE escalation (NOT a budget breach — arch §7)."""
        self._fix_tree_shas.add(tree_sha)
        if (
            not self._non_convergence_escalated
            and len(self._fix_tree_shas) > self.non_convergence_bound
        ):
            self._non_convergence_escalated = True
            return GovernanceEvent(
                kind=GovernanceEventKind.NON_CONVERGENCE,
                reason=(
                    f"{len(self._fix_tree_shas)} distinct tree_shas produced since "
                    f"all-green exceeds the convergence bound "
                    f"{self.non_convergence_bound} — non-converging cross-gate cascade"
                ),
                details=tuple(sorted(self._fix_tree_shas)),
            )
        return None

    def note_all_green(self) -> None:
        """Reset the convergence counter on all-integrated-green."""
        self._fix_tree_shas.clear()
        self._non_convergence_escalated = False

    @property
    def fix_attempts_since_all_green(self) -> int:
        return len(self._fix_tree_shas)
