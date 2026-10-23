"""Build gate — class 2 (execution-deterministic). (governance §1, plan §5.1)

Grounding: zone A runs a pinned build command in the workdir and observes the
**exit code** — the verdict is computed by zone B from that observation, never
self-reported by anything the build executes. Default command is
``<this interpreter> -m compileall -q .`` (real compilation of every tracked
module; dev-grade stand-in for the full compile+lint+typecheck pipeline,
plan §0.3 — production swaps the command set, not the seam).

Isolation: every command runs in the confined namespace sandbox
(``sandbox.ground_untrusted`` — no network, writes fenced to the workdir). An
optional ``install_cmd`` (the stack's dependency install, e.g. ``npm ci``) runs
FIRST in its own confined invocation, the only one that keeps network access; the
build then runs offline over what the install wrote into the workdir. An install
failure is a build FAIL carrying the install output as findings.

Fail-closed: a missing build tool — or an unavailable sandbox — raises
``GroundingUnavailable`` in zone A, which the runner signs as ERROR — never PASS.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted

__all__ = ["BuildGate", "build_findings"]

_EXCERPT_LINES = 40
_LINE_CHARS = 200
_EXCERPT_CHARS = 4000
# ANSI escape sequences, then any remaining control char except tab.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def build_findings(
    rc: int, stdout: str, stderr: str, workdir: Path, *, step: str = "build"
) -> tuple[str, ...]:
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
    return (f"{step} command exited {rc}; last {len(tail)} output line(s):", *reversed(tail))


class BuildGate:
    """PASS iff the install step (when configured) and the pinned build command exit 0."""

    name = "build"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...] | None = None,
        *,
        install_cmd: str | None = None,
        timeout_s: float = 600.0,
        seal_key: bytes | None = None,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "compileall", "-q", ".")
        self.install_cmd = install_cmd or None  # shell line, e.g. the stack's `npm ci`
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f). Build has no held-out mutant
        # machinery; the ids are still bound so the key shape is uniform.
        self.mutant_rotation_id = 0
        # v2 = commands run in the confined sandbox: never reuse a pre-sandbox verdict.
        self.scanner_rule_version = "build-cmd-v2"

    # ---- zone A: [install, then] build; observe the exit code(s) ------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        structured: dict[str, object] = {"cmd": list(self.cmd)}
        codes: tuple[int, ...] = ()
        out = err = ""
        if self.install_cmd is not None:
            structured["install_cmd"] = self.install_cmd
            rc, out, err = ground_untrusted(
                ("/bin/sh", "-c", self.install_cmd),
                workdir,
                network=True,
                timeout_s=self._timeout_s,
            )
            codes = (rc,)
            if rc != 0:
                return observe(
                    self.name,
                    exit_codes=codes,
                    stdout=out,
                    stderr=err,
                    findings=build_findings(rc, out, err, workdir, step="install"),
                    structured=structured,
                    seal_key=self._seal_key,
                )
        rc, b_out, b_err = ground_untrusted(self.cmd, workdir, timeout_s=self._timeout_s)
        return observe(
            self.name,
            exit_codes=(*codes, rc),
            stdout=out + b_out,
            stderr=err + b_err,
            findings=build_findings(rc, b_out, b_err, workdir) if rc != 0 else (),
            structured=structured,
            seal_key=self._seal_key,
        )

    # ---- zone B: pure decision over the observed exit code(s) ---------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        expected = 1 if self.install_cmd is None else 2
        codes = obs.exit_codes
        if not codes or len(codes) > expected:
            return GateStatus.ERROR  # no / malformed observation -> not a verdict
        if any(c != 0 for c in codes):
            return GateStatus.FAIL  # a failed install short-circuits the build
        return GateStatus.PASS if len(codes) == expected else GateStatus.ERROR
