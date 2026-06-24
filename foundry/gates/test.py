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
from typing import TYPE_CHECKING

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, run_grounding_command

if TYPE_CHECKING:
    from .discriminator import TestSuiteDiscriminator

__all__ = ["TestGate", "parse_pytest_summary"]

# Structured-observation key carrying the discriminator's report (zone A → B).
DISCRIMINATOR_KEY = "discriminator"

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
        discriminator: TestSuiteDiscriminator | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "pytest", "-q", "--tb=no")
        self.min_passed = min_passed
        # The held-out-mutant + behavioral-invariance discriminator: a green
        # pytest run is NECESSARY but NOT SUFFICIENT — a vacuous/gamed-green
        # suite that survives a held-out mutant or violates an invariant is
        # FAILed here even though pytest is green (gov §1c, the malicious-test
        # defense). None → no discriminator wired (legacy: green suffices).
        self._discriminator = discriminator
        # ``mutant_rotation_id`` rotates the held-out family the discriminator
        # draws from; mirror it from the discriminator when one is wired so the
        # reuse key and the substitution family agree.
        self.mutant_rotation_id = (
            discriminator.mutant_rotation_id if discriminator is not None else mutant_rotation_id
        )
        self.scanner_rule_version = "pytest-harness-v1"
        self._timeout_s = timeout_s

    def _env(self) -> dict[str, str]:
        return pinned_test_env()

    # ---- zone A: pinned harness execution, raw rc + summary ---------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        rc, out, err = run_grounding_command(
            self.cmd, workdir, env=self._env(), timeout_s=self._timeout_s
        )
        structured: dict[str, object] = {
            "cmd": list(self.cmd),
            "summary": parse_pytest_summary(out),
        }
        findings: tuple[str, ...] = ()
        # The discriminator runs in zone A (no signing key in scope) ONLY when
        # the agent suite is actually green — a red/no-tests run is already a
        # FAIL/ERROR by exit code, so there is nothing to re-verify. The report
        # rides the sealed observation; zone B decides over it.
        if self._discriminator is not None and rc == 0:
            report = self._discriminator.evaluate(workdir)
            structured[DISCRIMINATOR_KEY] = report.model_dump(mode="json")
            findings = report.findings
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            findings=findings,
            structured=structured,
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
        if passed < self.min_passed:
            return GateStatus.FAIL  # adequacy floor: a vacuous green run cannot pass
        # Green + adequate is NECESSARY but NOT SUFFICIENT: a gamed-green suite
        # that survives a held-out mutant or violates a behavioral invariant is
        # FAILed here (gov §1c). A surviving mutant means the suite tested
        # nothing real; the discriminator report rode the sealed observation.
        report = obs.structured.get(DISCRIMINATOR_KEY)
        if report is not None and not report.get("passed", False):
            return GateStatus.FAIL
        return GateStatus.PASS
