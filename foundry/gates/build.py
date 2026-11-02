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
build then runs offline over what the install wrote into the workdir.

Infra vs. verdict: an install that exits non-zero (registry 5xx, network flake) and
any command exiting 126/127 (the shell could not find or run a binary — a missing
toolchain) are ERROR, never FAIL: the runner latches a FAIL on the tree digest
forever, while an ERROR routes to bounded retry and never latches. Only the build
itself exiting non-zero (other than 126/127) is a judgment about the tree.

Fail-closed: a missing build tool — or an unavailable sandbox — raises
``GroundingUnavailable`` in zone A, which the runner signs as ERROR — never PASS.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from foundry.canonical import content_digest
from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted

__all__ = ["MISSING_BINARY_EXIT_CODES", "BuildGate", "build_findings", "commands_digest"]

# POSIX shell: 126 = found but not executable, 127 = command not found.
MISSING_BINARY_EXIT_CODES = frozenset({126, 127})


def commands_digest(**commands: object) -> str:
    """Short digest of a gate's effective commands, for its reuse-key version."""
    return content_digest({k: v for k, v in sorted(commands.items())})[:16]

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
    head = f"{step} command exited {rc}"
    if rc in MISSING_BINARY_EXIT_CODES:
        head += " (command not found or not executable: missing toolchain — infra ERROR)"
    elif step == "install":
        head += (
            " (dependency install failed — an ERROR, never latched: if the output "
            "names a project problem such as a missing lockfile, a bad manifest or an "
            "unknown package, fix the manifest; otherwise the registry/network failed)"
        )
    for ln in reversed(lines[-_EXCERPT_LINES:]):
        if used + len(ln) > _EXCERPT_CHARS:
            break
        tail.append(ln)
        used += len(ln)
    return (f"{head}; last {len(tail)} output line(s):", *reversed(tail))


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
        profile_name: str = "",
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "compileall", "-q", ".")
        self.install_cmd = install_cmd or None  # shell line, e.g. the stack's `npm ci`
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f). Build has no held-out mutant
        # machinery; the ids are still bound so the key shape is uniform.
        self.mutant_rotation_id = 0
        # v2 = commands run in the confined sandbox: never reuse a pre-sandbox verdict.
        # The command digest: a cached verdict never outlives a change of commands.
        self.scanner_rule_version = "build-cmd-v3:" + commands_digest(
            cmd=list(self.cmd), install_cmd=self.install_cmd, profile=profile_name
        )

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
        if any(c in MISSING_BINARY_EXIT_CODES for c in codes):
            return GateStatus.ERROR  # missing toolchain: infra, never latched
        if self.install_cmd is not None and codes[0] != 0:
            return GateStatus.ERROR  # failed install (network/registry): retriable
        if any(c != 0 for c in codes):
            return GateStatus.FAIL
        return GateStatus.PASS if len(codes) == expected else GateStatus.ERROR
