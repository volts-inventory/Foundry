"""Test gate — class 2 (execution-deterministic). (governance §1/§1c, plan §5.1)

Grounding: zone A invokes the **pinned trusted harness** (this interpreter's
pytest — agents supply test *sources*, never the scoring harness; plugin
autoload is disabled so an agent-dropped plugin cannot become the reporter)
and observes exit code + parsed summary counts. Zone B computes the verdict
from those observations alone.

Adequacy-floor hook: ``min_passed`` — a green run that exercised fewer than
``min_passed`` tests is FAIL, not PASS, so an empty/vacuous suite can never
clear the gate (the dev-grade floor of governance §1c's adequacy/diversity
requirement). The full held-out-mutant + behavioral-invariance + criterion-
fidelity machinery is Phase 2; its reuse-key binding is wired NOW —
``mutant_rotation_id`` (default 0) is bound into the class-2 reuse key via
``TwoZoneGateRunner.reuse_key`` so cached verdicts already invalidate across
a rotation boundary when rotation arrives.

Exit-code semantics (pytest): 0 green, 1 test failures, 2/3/4 harness/usage
errors (→ ERROR: infra, not a verdict), 5 no tests collected (→ adequacy FAIL
when a floor is set).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, run_grounding_command

__all__ = ["TestGate", "parse_pytest_summary"]

_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")

# pytest exit codes that are harness/usage failures, not verdicts.
_INFRA_EXIT_CODES = frozenset({2, 3, 4})
_NO_TESTS_COLLECTED = 5


def parse_pytest_summary(stdout: str) -> dict[str, int]:
    """Counts from the pytest tail line (raw observation, zone A side)."""
    counts: dict[str, int] = {}
    for n, kind in _SUMMARY_RE.findall(stdout):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    return counts


def pinned_test_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Pinned-harness child env: no agent plugin autoload, no opt injection."""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)  # invocation is pinned, not ambient
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # no agent-supplied plugins (gov §1c)
    if extra:
        env.update(extra)
    return env


class TestGate:
    """PASS iff the pinned harness is green and the adequacy floor is met."""

    name = "test"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...] | None = None,
        *,
        min_passed: int = 1,
        mutant_rotation_id: int = 0,
        timeout_s: float = 600.0,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "pytest", "-q", "--tb=no")
        self.min_passed = min_passed
        # Bound into the class-2 reuse key now; rotated by Phase-2 machinery.
        self.mutant_rotation_id = mutant_rotation_id
        self.scanner_rule_version = "pytest-harness-v1"
        self._timeout_s = timeout_s

    def _env(self) -> dict[str, str]:
        return pinned_test_env()

    # ---- zone A: pinned harness execution, raw rc + summary ---------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        rc, out, err = run_grounding_command(
            self.cmd, workdir, env=self._env(), timeout_s=self._timeout_s
        )
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            structured={"cmd": list(self.cmd), "summary": parse_pytest_summary(out)},
        )

    # ---- zone B: pure decision over rc + counts ----------------------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != 1:
            return GateStatus.ERROR
        rc = obs.exit_codes[0]
        if rc in _INFRA_EXIT_CODES:
            return GateStatus.ERROR  # harness failure is not a verdict (DS7)
        if rc == _NO_TESTS_COLLECTED:
            # Adequacy floor: an empty suite cannot clear the gate.
            return GateStatus.FAIL if self.min_passed > 0 else GateStatus.PASS
        if rc != 0:
            return GateStatus.FAIL
        passed = obs.structured.get("summary", {}).get("passed", 0)
        return GateStatus.PASS if passed >= self.min_passed else GateStatus.FAIL
