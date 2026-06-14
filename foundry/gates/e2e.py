"""E2E gate — class 2, deterministic decision over possibly-flaky execution.

Governance §1a/§1c, plan §5.1: end-to-end scenarios run under a **pinned
environment** (fixed hash seed, frozen timezone, fixed ``SOURCE_DATE_EPOCH``;
production adds frozen clock + mocked network) on the pinned harness, and the
verdict is deterministic over the possibly-flaky execution via the **N=2
flake rule**:

- both runs pass  → PASS
- both runs fail  → FAIL
- split (fail-then-pass or pass-then-fail) → **ERROR** — flake-quarantine
  routing, NOT a verdict: a flake is a property of the execution, not of the
  tree, so it must neither green-light nor stably disqualify the digest
  (ERROR never latches; quarantine policy in governance §1a handles it).

Zone A observes both raw exit codes; zone B's decision is a pure function of
the pair.
"""

from __future__ import annotations

import sys
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, run_grounding_command
from .test import _INFRA_EXIT_CODES, parse_pytest_summary, pinned_test_env

__all__ = ["E2EGate"]

#: Pinned execution environment (governance §1a: pinned seeds, frozen clock).
PINNED_ENV = {
    "PYTHONHASHSEED": "0",
    "TZ": "UTC",
    "SOURCE_DATE_EPOCH": "315532800",  # 1980-01-01T00:00:00Z, fixed forever
}

_N_RUNS = 2  # the N=2 flake budget of governance §1a


class E2EGate:
    """N=2 pinned-env scenario runs; split outcomes quarantine as ERROR."""

    name = "e2e"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...] | None = None,
        *,
        mutant_rotation_id: int = 0,
        timeout_s: float = 600.0,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "pytest", "-q", "--tb=no")
        self.mutant_rotation_id = mutant_rotation_id
        self.scanner_rule_version = "e2e-harness-v1"
        self._timeout_s = timeout_s

    # ---- zone A: N pinned-env runs, raw exit-code pair ----------------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        env = pinned_test_env(PINNED_ENV)
        codes: list[int] = []
        outs: list[str] = []
        for _ in range(_N_RUNS):
            rc, out, err = run_grounding_command(
                self.cmd, workdir, env=env, timeout_s=self._timeout_s
            )
            codes.append(rc)
            outs.append(out if not err else f"{out}\n{err}")
        return observe(
            self.name,
            exit_codes=tuple(codes),
            stdout="\n--- run boundary ---\n".join(outs),
            structured={
                "cmd": list(self.cmd),
                "pinned_env": PINNED_ENV,
                "summaries": [parse_pytest_summary(o) for o in outs],
            },
        )

    # ---- zone B: deterministic N=2 flake rule -------------------------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != _N_RUNS:
            return GateStatus.ERROR
        if any(rc in _INFRA_EXIT_CODES for rc in obs.exit_codes):
            return GateStatus.ERROR  # harness failure, not a verdict
        greens = [rc == 0 for rc in obs.exit_codes]
        if all(greens):
            return GateStatus.PASS
        if not any(greens):
            return GateStatus.FAIL
        return GateStatus.ERROR  # split -> flake-quarantine, never a verdict
