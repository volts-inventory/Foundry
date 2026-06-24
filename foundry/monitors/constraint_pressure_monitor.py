"""Pinned-constraint cardinality-pressure monitor. (architecture.md §8, gov §3)

The injected `pinned_constraints` set (global + scoped-local) is bounded by a
per-node injection budget; arch §8 mandates it is **never silently truncated** —
when the set approaches the budget the condition escalates to HITL.

Two deterministic detections, both at the 90% bound (arch §8):

- **Total pressure** — the WHOLE injected set (global + local) approaching the
  per-node budget escalates so a human can scope/retire before truncation.
- **Global-subset pressure** — when the `global` (non-scopable, always-injected)
  subset ALONE approaches budget, the condition is unfixable-by-scoping and
  must go to governance/HITL to retire or consolidate globals (a strictly more
  severe variant; it is reported as a distinct matched marker on the trip).

Pure integer arithmetic over trusted-infra-supplied counts — no LLM (gov §3).
The Guardian maps CONSTRAINT_PRESSURE to ESCALATE.
"""

from __future__ import annotations

from foundry.monitors import Trip, TripKind

DEFAULT_PRESSURE_PCT = 90  # arch §8: HITL at the 90% bound, never silent truncation


class ConstraintPressureMonitor:
    def __init__(self, *, pressure_pct: int = DEFAULT_PRESSURE_PCT) -> None:
        if not 1 <= pressure_pct <= 100:
            raise ValueError("pressure_pct must be in 1..100")
        self.pressure_pct = pressure_pct

    def inspect(
        self,
        *,
        node_id: str,
        injected_count: int,
        budget: int,
        global_count: int = 0,
    ) -> Trip | None:
        """Assess the injected pinned-constraint set for one node.

        `injected_count` is the total set size (global + local), `budget` the
        per-node injection budget, `global_count` the always-injected global
        subset size. Trips when EITHER the total or the global subset reaches
        the `pressure_pct` fraction of the budget (integer: count*100 >=
        budget*pct).
        """
        if budget <= 0:
            return None
        threshold = self.pressure_pct  # count*100 >= budget*pct
        total_pressure = injected_count * 100 >= budget * threshold
        global_pressure = global_count * 100 >= budget * threshold
        if not (total_pressure or global_pressure):
            return None

        matched = [node_id, f"injected={injected_count}", f"budget={budget}"]
        if global_pressure:
            rule = "global-constraint-pressure"
            scope_note = (
                "GLOBAL subset alone at budget — unfixable by scoping; "
                "retire/consolidate globals (governance/HITL)"
            )
            matched.append(f"global={global_count}")
            matched.append("scope=global")
        else:
            rule = "constraint-cardinality-pressure"
            scope_note = "total injected set at budget — scope/retire before truncation"
        return Trip(
            monitor="constraint_pressure_monitor",
            kind=TripKind.CONSTRAINT_PRESSURE,
            rule=rule,
            reason=(
                f"node {node_id!r} pinned-constraint injection at/over "
                f"{self.pressure_pct}% of budget {budget} "
                f"(injected={injected_count}, global={global_count}): {scope_note} "
                "— never silent truncation (arch §8)"
            ),
            matched=tuple(matched),
        )
