"""Smoke gate — class 2 (execution-deterministic). (ROADMAP Phase 3)

The RUNTIME boot gate: zone A runs the profile-pinned smoke command (e.g.
``node smoke.mjs`` booting a game loop for N ticks) in the workdir and observes
the exit code; zone B derives PASS iff 0. Same shape and trust posture as
:class:`~foundry.gates.build.BuildGate` — the verdict is computed from the
observed exit code, never self-reported by anything the smoke run executes. It runs in the confined
namespace sandbox (``sandbox.ground_untrusted``): loopback only, writes fenced to
the workdir.

Fail-closed: a missing runtime (no ``node``, no ``love``) or an unavailable
sandbox raises
``GroundingUnavailable`` in zone A, which the runner signs as ERROR — a missing
toolchain is loudly not-a-verdict, never a silent PASS.
"""

from __future__ import annotations

from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .build import build_findings
from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted

__all__ = ["SmokeGate"]


class SmokeGate:
    """PASS iff the profile-pinned smoke (runtime boot) command exits 0."""

    name = "smoke"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...],
        *,
        timeout_s: float = 600.0,
        seal_key: bytes | None = None,
    ) -> None:
        self.cmd = cmd
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f) — uniform key shape with Build/Test.
        self.mutant_rotation_id = 0
        self.scanner_rule_version = "smoke-cmd-v3"  # v2 = confined sandbox; v3 = findings

    def execute(self, workdir: Path) -> ZoneAObservation:
        rc, out, err = ground_untrusted(self.cmd, workdir, timeout_s=self._timeout_s)
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            structured={"cmd": list(self.cmd)},
            # A red boot carries its WHY (exit code + output tail) for the fixer.
            findings=build_findings(rc, out, err, workdir, step="smoke") if rc != 0 else (),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != 1:
            return GateStatus.ERROR  # no observation -> not a verdict
        return GateStatus.PASS if obs.exit_codes[0] == 0 else GateStatus.FAIL
