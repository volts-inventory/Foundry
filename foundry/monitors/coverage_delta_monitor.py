"""Criterion-coverage delta monitor — coverage-regression trip. (gov §3)

Deterministic detection of an adequacy regression measured against signals the
implementing agent does **not** solely control (gov §3, AI-R8-F3/AI-R9-F2):

- The **baseline** is the count of high-risk `criterion_id`s in the
  **Spec-gate-frozen, immutable** criterion set. The high-risk flag is an
  immutable attribute of each frozen criterion, so the denominator cannot be
  shrunk by re-labeling (that would be an acceptance-criteria edit → a separate
  deterministic critical-diff trip).
- The **satisfied** side is the count of high-risk criteria whose
  **runner-attested** negative-oracle/mutant family passes (computed by the
  trusted runner, §1c) — NOT the agent-authored test-tag count (which a
  coordinated scope-reduction could co-shrink).

A drop in the runner-attested satisfied count below the baseline (minus an
allowed slack, default 0) trips deterministically. Pure integer comparison over
trusted-infra counts — no LLM (gov §3). The Guardian maps COVERAGE_DELTA to
ESCALATE.
"""

from __future__ import annotations

from foundry.monitors import Trip, TripKind


class CoverageDeltaMonitor:
    def __init__(self, *, allowed_regression: int = 0) -> None:
        if allowed_regression < 0:
            raise ValueError("allowed_regression must be >= 0")
        self.allowed_regression = allowed_regression

    def inspect(
        self,
        *,
        baseline_high_risk: int,
        attested_satisfied: int,
        run_against: str = "",
    ) -> Trip | None:
        """Assess coverage adequacy for one integrated tree.

        `baseline_high_risk` = high-risk criteria in the frozen Spec set;
        `attested_satisfied` = high-risk criteria whose runner-attested
        negative-oracle/mutant family passes. Trips when satisfied falls more
        than `allowed_regression` below the baseline.
        """
        shortfall = baseline_high_risk - attested_satisfied
        if shortfall <= self.allowed_regression:
            return None
        matched = [
            f"baseline={baseline_high_risk}",
            f"attested={attested_satisfied}",
            f"shortfall={shortfall}",
        ]
        if run_against:
            matched.append(run_against)
        return Trip(
            monitor="coverage_delta_monitor",
            kind=TripKind.COVERAGE_DELTA,
            rule="high-risk-coverage-regression",
            reason=(
                f"runner-attested high-risk coverage {attested_satisfied} dropped "
                f"{shortfall} below frozen baseline {baseline_high_risk} "
                f"(allowed slack {self.allowed_regression}) — deterministic trip (gov §3)"
            ),
            matched=tuple(matched),
        )
