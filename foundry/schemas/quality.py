"""Quality & verification artifacts (agents.md Layer 4 — Quality & Verification).

QA Engineer → `TestPlan` + `OracleBinding[]` + `TestReport`; SDET →
`E2EReport`. Test/E2E VERDICTS are runner-signed `GateResult`s (gates.py);
these records are the agent-authored plans and result handles the runner
consumes/produces around them. Result bodies are content-addressed (DS9).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from foundry.schemas.base import FrozenModel
from foundry.state.seq import Seq


class OracleKind(StrEnum):
    """Oracle taxonomy from the QA SOP (agents.md Layer 4)."""

    EXAMPLE = "example"
    NEGATIVE = "negative"  # must FAIL against a known-bad reference (criterion fidelity)
    PROPERTY = "property"
    GOLDEN = "golden"


class TestPlan(FrozenModel):
    """QA Engineer output (agents.md Layer 4): tests derived from acceptance
    criteria with `criterion_id` traceability — every test maps to the
    criteria it exercises, so the Test gate can check criterion coverage and
    fidelity, not just line coverage (governance.md §1)."""

    __test__ = False  # model named Test*: tell pytest this is not a test class

    plan_uri: str  # CAS handle to the full plan / test source body (DS9)
    test_criteria: dict[str, tuple[str, ...]]  # test_id -> criterion_ids exercised
    version: int = 1
    supersedes: str | None = None
    seq: Seq

    def covered_criteria(self) -> frozenset[str]:
        """All criterion_ids exercised by at least one planned test."""
        return frozenset(c for ids in self.test_criteria.values() for c in ids)


class OracleBinding(FrozenModel):
    """A FROZEN oracle ref per criterion (agents.md Layer 4/5): once bound,
    any diff touching a test oracle is a deterministic Guardian trip → HITL
    (agents.md Layer 7), so a rebind only arrives as a supersession."""

    criterion_id: str
    oracle_uri: str  # content-addressed oracle ref — frozen, never edited in place
    oracle_kind: OracleKind
    version: int = 1
    supersedes: str | None = None
    seq: Seq


class TestReport(FrozenModel):
    """QA run output (agents.md Layer 4): a handle to the raw results plus the
    counts and the flake-quarantine list (governance.md §1a quarantine rule).
    A new run is a new report keyed by `run_against` + `seq`, not a
    supersession."""

    __test__ = False  # model named Test*: tell pytest this is not a test class

    results_uri: str  # CAS handle to raw results (DS9)
    run_against: str  # task branch, then integrated tree_sha
    passed: int
    failed: int
    quarantined: tuple[str, ...] = ()  # flaky: fail-then-pass within the retry budget
    seq: Seq


class E2EReport(FrozenModel):
    """SDET output on the integrated tree (agents.md Layer 4 — E2E gate;
    flake policy of governance.md §1a applies: pinned seeds, frozen clock,
    mocked network, N=2 fail-then-pass → quarantine)."""

    results_uri: str  # CAS handle to raw scenario results (DS9)
    run_against: str  # integrated tree_sha
    scenarios_passed: int
    scenarios_failed: int
    pinned_seeds_note: str  # records the pinned seeds / frozen clock / mocked network
    # scenario -> criterion_ids exercised: criterion traceability on the integrated
    # tree, so the E2E gate checks criterion coverage end-to-end, not just green
    # scenarios (mirrors TestPlan.test_criteria; agents.md Layer 4 — SDET SOP):
    scenario_criteria: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    flake_retry_budget: int = 2  # N=2 (governance.md §1a)
    quarantined: tuple[str, ...] = ()
    seq: Seq
