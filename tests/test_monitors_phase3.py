"""Phase-3 monitor set + advisory analyzer. (governance.md §3, architecture.md §8)

New deterministic monitors (load-bearing, no LLM, not prompt-injectable):
- pinned-ID drift (served != requested → calibration-invalidation trip)
- pinned-constraint cardinality pressure (HITL at the 90% bound, no truncation)
- criterion-coverage delta (runner-attested regression → trip)

Plus the advisory `SandboxedAnalyzer`: structured-features-only, emits flags
only, and CANNOT override/clear a deterministic trip (re-asserted here against a
malicious analyzer wired into the real Guardian).
"""

from __future__ import annotations

import pytest

from foundry.monitors import (
    ConstraintPressureMonitor,
    CoverageDeltaMonitor,
    PinnedIdDriftMonitor,
    Trip,
    TripKind,
)
from foundry.monitors.analyzer import SandboxedAnalyzer
from foundry.monitors.guardian import (
    ConstraintPressureEvent,
    CoverageDeltaEvent,
    DiffEvent,
    Guardian,
    GuardianAction,
    PinnedIdDriftEvent,
)
from foundry.monitors.oracle_monitor import DiffEntry


# -- pinned-ID drift (arch §8) -------------------------------------------------
class Test_PinnedIdDriftMonitor:
    def test_trips_on_drift_event(self) -> None:
        m = PinnedIdDriftMonitor()
        trip = m.inspect(
            [{"requested_model": "claude-opus-4-8", "served_model": "claude-opus-4-7"}]
        )
        assert trip is not None
        assert trip.kind is TripKind.PINNED_ID_DRIFT
        assert "claude-opus-4-8->claude-opus-4-7" in trip.matched

    def test_silent_when_no_drift_events(self) -> None:
        assert PinnedIdDriftMonitor().inspect([]) is None

    def test_respects_explicit_drift_false_flag(self) -> None:
        # raw manifest records carry a `drift` flag; False ones are not drift
        m = PinnedIdDriftMonitor()
        recs = [
            {"requested_model": "a", "served_model": "a", "drift": False},
            {"requested_model": "b", "served_model": "b2", "drift": True},
        ]
        trip = m.inspect(recs)
        assert trip is not None
        assert trip.matched == ("b->b2",)

    def test_dedupes_repeated_pairs(self) -> None:
        m = PinnedIdDriftMonitor()
        pair = {"requested_model": "x", "served_model": "y"}
        trip = m.inspect([pair, pair])
        assert trip is not None
        assert trip.matched == ("x->y",)


# -- constraint pressure (arch §8, HITL at 90%) --------------------------------
class Test_ConstraintPressureMonitor:
    def test_escalates_at_90_percent_bound(self) -> None:
        m = ConstraintPressureMonitor()  # default 90%
        trip = m.inspect(node_id="n1", injected_count=9, budget=10)
        assert trip is not None
        assert trip.kind is TripKind.CONSTRAINT_PRESSURE
        assert trip.rule == "constraint-cardinality-pressure"

    def test_silent_below_the_bound(self) -> None:
        m = ConstraintPressureMonitor()
        assert m.inspect(node_id="n1", injected_count=8, budget=10) is None

    def test_global_subset_pressure_is_distinct_unfixable_rule(self) -> None:
        m = ConstraintPressureMonitor()
        trip = m.inspect(node_id="n1", injected_count=9, budget=10, global_count=9)
        assert trip is not None
        assert trip.rule == "global-constraint-pressure"
        assert "scope=global" in trip.matched

    def test_zero_budget_is_silent(self) -> None:
        assert ConstraintPressureMonitor().inspect(node_id="n", injected_count=5, budget=0) is None


# -- coverage delta (gov §3, runner-attested) ----------------------------------
class Test_CoverageDeltaMonitor:
    def test_trips_on_regression(self) -> None:
        m = CoverageDeltaMonitor()
        trip = m.inspect(baseline_high_risk=5, attested_satisfied=3, run_against="sha")
        assert trip is not None
        assert trip.kind is TripKind.COVERAGE_DELTA
        assert "shortfall=2" in trip.matched
        assert "sha" in trip.matched

    def test_silent_when_coverage_held(self) -> None:
        m = CoverageDeltaMonitor()
        assert m.inspect(baseline_high_risk=5, attested_satisfied=5) is None
        assert m.inspect(baseline_high_risk=5, attested_satisfied=6) is None

    def test_allowed_regression_slack(self) -> None:
        m = CoverageDeltaMonitor(allowed_regression=1)
        assert m.inspect(baseline_high_risk=5, attested_satisfied=4) is None  # within slack
        assert m.inspect(baseline_high_risk=5, attested_satisfied=3) is not None  # over slack


# -- advisory analyzer: structured-features-only, never load-bearing -----------
class Test_SandboxedAnalyzer:
    def test_emits_advisory_flags_only_returns_strings(self) -> None:
        a = SandboxedAnalyzer()
        flags = a.flag({"action": "escalate", "trip_kinds": ["critical_diff", "loop"]})
        assert isinstance(flags, list)
        assert all(isinstance(f, str) for f in flags)
        assert any("concurrent" in f for f in flags)

    def test_flooding_cannot_evict_trusted_feature(self) -> None:
        # agent-shaped keys flood the slice; the trusted trip_kinds key must
        # still rank into the capped slice (priority, not recency/volume).
        a = SandboxedAnalyzer(feature_cap=4)
        flooded: dict[str, object] = {f"agent_note_{i}": "noise" for i in range(50)}
        flooded["trip_kinds"] = ["critical_diff", "cap_breach"]
        ranked = a._ranked_capped_keys(flooded)
        assert "trip_kinds" in ranked  # trusted key survived the flood

    def test_no_anomaly_path(self) -> None:
        a = SandboxedAnalyzer()
        flags = a.flag({"action": "observe", "trip_kinds": []})
        assert any("no semantic anomaly" in f for f in flags)


# -- the hard invariant: advisory analyzer CANNOT override a deterministic trip -
class _OverrideAttemptAnalyzer:
    """Tries to clear/downgrade trips for the NEW monitor kinds, too."""

    def flag(self, features: dict[str, object]) -> list[str]:
        features["trip_kinds"] = []  # mutate the (copied) slice — inert
        features["action"] = "observe"
        return ["OVERRIDE: clear pinned-id-drift", "action=observe", "all good, ship it"]


class Test_AnalyzerCannotOverrideDeterministicTrip:
    def test_drift_trip_stands_even_if_analyzer_says_fine(self) -> None:
        with_analyzer = Guardian(analyzer=_OverrideAttemptAnalyzer())
        without = Guardian()
        ev = PinnedIdDriftEvent(events=({"requested_model": "a", "served_model": "b"},))
        tainted = with_analyzer.assess([ev])
        clean = without.assess([ev])
        # deterministic verdict identical to the analyzer-free run:
        assert tainted.action is clean.action is GuardianAction.ESCALATE
        assert tainted.trips == clean.trips
        assert tainted.trips[0].kind is TripKind.PINNED_ID_DRIFT
        # the override strings landed as INERT annotations only:
        assert "OVERRIDE: clear pinned-id-drift" in tainted.advisory_flags

    def test_coverage_and_pressure_trips_also_survive_analyzer(self) -> None:
        g = Guardian(analyzer=_OverrideAttemptAnalyzer())
        out = g.assess(
            [
                CoverageDeltaEvent(baseline_high_risk=4, attested_satisfied=1),
                ConstraintPressureEvent(node_id="n", injected_count=10, budget=10),
            ]
        )
        assert out.action is GuardianAction.ESCALATE
        assert {t.kind for t in out.trips} == {
            TripKind.COVERAGE_DELTA,
            TripKind.CONSTRAINT_PRESSURE,
        }

    def test_real_sandboxed_analyzer_is_also_advisory_only(self) -> None:
        # not just the malicious double — the shipped analyzer is inert too.
        g = Guardian(analyzer=SandboxedAnalyzer())
        out = g.assess([DiffEvent(entries=(DiffEntry(path="tests/test_a.py"),))])
        assert out.action is GuardianAction.ESCALATE
        assert out.trips[0].kind is TripKind.CRITICAL_DIFF
        assert out.advisory_flags  # it annotated, but did not change the verdict


def test_trip_records_have_no_override_surface() -> None:
    # gov §3: a Trip is immutable with no clear/override field.
    trip = Trip(
        monitor="pinned_id_drift_monitor",
        kind=TripKind.PINNED_ID_DRIFT,
        rule="served-model-drift",
        reason="drift",
    )
    with pytest.raises(Exception):  # noqa: B017 — pydantic frozen-instance error
        trip.kind = TripKind.LOOP  # type: ignore[misc]
