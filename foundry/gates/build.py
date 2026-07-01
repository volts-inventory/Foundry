"""Build gate — class 2 (execution-deterministic). (governance §1, plan §5.1)

Grounding: zone A runs a pinned build command in the workdir and observes the
**exit code** — the verdict is computed by zone B from that observation, never
self-reported by anything the build executes. Default command is
``<this interpreter> -m compileall -q .`` (real compilation of every tracked
module; dev-grade stand-in for the full compile+lint+typecheck pipeline,
plan §0.3 — production swaps the command set, not the seam).

Fail-closed: a missing build tool raises ``GroundingUnavailable`` in zone A,
which the runner signs as ERROR — never PASS.
"""

from __future__ import annotations

import sys
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, run_grounding_command

__all__ = ["BuildGate"]


class BuildGate:
    """PASS iff the pinned build command exits 0."""

    name = "build"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...] | None = None,
        *,
        timeout_s: float = 600.0,
        seal_key: bytes | None = None,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "compileall", "-q", ".")
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f). Build has no held-out mutant
        # machinery; the ids are still bound so the key shape is uniform.
        self.mutant_rotation_id = 0
        self.scanner_rule_version = "build-cmd-v1"

    # ---- zone A: run the build, observe the exit code ----------------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        rc, out, err = run_grounding_command(self.cmd, workdir, timeout_s=self._timeout_s)
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            structured={"cmd": list(self.cmd)},
            seal_key=self._seal_key,
        )

    # ---- zone B: pure decision over the observed exit code -----------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != 1:
            return GateStatus.ERROR  # no observation -> not a verdict
        return GateStatus.PASS if obs.exit_codes[0] == 0 else GateStatus.FAIL
