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

import re
import sys
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe, run_grounding_command

__all__ = ["BuildGate", "build_findings"]

_EXCERPT_LINES = 40
_LINE_CHARS = 200
_EXCERPT_CHARS = 4000
# ANSI escape sequences, then any remaining control char except tab.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def build_findings(rc: int, stdout: str, stderr: str, workdir: Path) -> tuple[str, ...]:
    """The fixer's view of a failed build: the exit code + the tail of the build output
    (stdout then stderr), sanitized and bounded. A pure function of the observed
    output with the workdir path normalized to ``.``, so it adds nothing to
    ``results_digest`` that the raw stdout/stderr didn't already bind."""
    text = "\n".join(t for t in (stdout, stderr) if t.strip())
    root = str(Path(workdir).resolve())
    text = text.replace(root + "/", "./").replace(root, ".")
    text = _CONTROL_RE.sub("", _ANSI_RE.sub("", text))
    lines = [ln.rstrip()[:_LINE_CHARS] for ln in text.splitlines() if ln.strip()]
    tail: list[str] = []
    used = 0
    for ln in reversed(lines[-_EXCERPT_LINES:]):
        if used + len(ln) > _EXCERPT_CHARS:
            break
        tail.append(ln)
        used += len(ln)
    return (f"build command exited {rc}; last {len(tail)} output line(s):", *reversed(tail))


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
            findings=build_findings(rc, out, err, workdir) if rc != 0 else (),
            structured={"cmd": list(self.cmd)},
            seal_key=self._seal_key,
        )

    # ---- zone B: pure decision over the observed exit code -----------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != 1:
            return GateStatus.ERROR  # no observation -> not a verdict
        return GateStatus.PASS if obs.exit_codes[0] == 0 else GateStatus.FAIL
