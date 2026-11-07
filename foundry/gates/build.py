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

A Python build that declares dependencies installs them with pip into a per-build
venv instead (``pydeps``): same single networked step, and a failed pip install is a
FAIL when pip blames the project's declaration, else ERROR.

Infra vs. verdict: an install that exits non-zero (registry 5xx, network flake) and
any command exiting 126/127 (the shell could not find or run a binary — a missing
toolchain) are ERROR, never FAIL: the runner latches a FAIL on the tree digest
forever, while an ERROR routes to bounded retry and never latches. Only the build
itself exiting non-zero (other than 126/127) is a judgment about the tree.

Fail-closed: a missing build tool — or an unavailable sandbox — raises
``GroundingUnavailable`` in zone A, which the runner signs as ERROR — never PASS.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.canonical import content_digest
from foundry.schemas.gates import GateStatus, VerdictClass

from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted, toolchain_scope

if TYPE_CHECKING:
    from .pydeps import PythonBuildEnv

__all__ = [
    "MISSING_BINARY_EXIT_CODES",
    "TYPECHECK_ONLY",
    "BuildGate",
    "build_findings",
    "commands_digest",
]

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
# The head of a build whose compile steps passed and whose type checker alone
# failed: the fix loop's score still counts the test gate on that tree.
TYPECHECK_ONLY = "only the static typecheck failed"
# mypy's closing line when it checked the tree and found type errors. A stack's build
# line runs `build && typecheck`, so mypy reporting at all means the steps before it
# passed; "errors prevented further checking" (a syntax error) never matches.
_MYPY_FOUND = re.compile(
    r"^Found \d+ errors? in \d+ files? \(checked \d+ source files?\)$", re.MULTILINE
)
_MYPY_NAME_DEFINED = re.compile(
    r"^(?P<path>[^\s:]+\.pyi?):(?P<line>\d+)(?::\d+)?: error: "
    r'Name "(?P<name>\w+)" is not defined\s+\[name-defined\]', re.MULTILINE
)


def build_findings(
    rc: int, stdout: str, stderr: str, workdir: Path, *, step: str = "build"
) -> tuple[str, ...]:
    """The fixer's view of a failed build: the exit code + the tail of the build output
    (stdout then stderr), sanitized and bounded. A pure function of the observed
    output with the workdir path normalized to ``.``, so it adds nothing to
    ``results_digest`` that the raw stdout/stderr didn't already bind."""
    head = f"{step} command exited {rc}"
    hints: tuple[str, ...] = ()
    if rc in MISSING_BINARY_EXIT_CODES:
        head += " (command not found or not executable: missing toolchain — infra ERROR)"
    elif step == "pip install":
        from .pydeps import install_failure_is_project, unavailable_requirement_hints

        if install_failure_is_project(stdout + stderr):
            head += (" (the project's declared dependencies cannot be installed — FAIL: "
                     "fix requirements.txt / pyproject.toml)")
            hints = unavailable_requirement_hints(stdout + stderr, workdir)
        else:
            head += (" (dependency install failed on the network, the index or the host "
                     "— an ERROR, never latched)")
    elif step == "install":
        head += (
            " (dependency install failed — an ERROR, never latched: if the output "
            "names a project problem such as a missing lockfile, a bad manifest or an "
            "unknown package, fix the manifest; otherwise the registry/network failed)"
        )
    elif rc != 0:
        from .pydeps import undeclared_import_hints

        if _MYPY_FOUND.search(stdout + stderr):
            head += f" ({TYPECHECK_ONLY}: the compile steps before mypy passed)"
        hints = (*undeclared_import_hints(stdout + stderr, workdir),
                 *forward_annotation_hints(stdout + stderr, workdir))
    tail = output_tail(stdout, stderr, workdir)
    return (f"{head}; last {len(tail)} output line(s):", *tail, *hints)


def forward_annotation_hints(output: str, workdir: Path) -> tuple[str, ...]:
    """One line per mypy ``name-defined`` error on a name its module uses only inside
    string literals (a quoted / forward annotation such as ``Mapped[list["CartItem"]]``):
    the name needs a type-checking-only import, not a runtime one (which would often
    be circular)."""
    root = Path(workdir).resolve()
    hints: list[str] = []
    for m in _MYPY_NAME_DEFINED.finditer(output):
        name = m.group("name")
        src = (root / m.group("path")).resolve()
        if not src.is_relative_to(root) or not src.is_file():
            continue
        rel = src.relative_to(root).as_posix()
        if _quoted_only(src.read_text(encoding="utf-8", errors="replace"), name,
                        int(m.group("line"))):
            hint = (f"{rel}:{m.group('line')}: `{name}` is used only in a quoted annotation: "
                    "import it under `if TYPE_CHECKING:` (from typing import TYPE_CHECKING) "
                    "in that module")
            if hint not in hints:
                hints.append(hint)
    return tuple(hints)


def _quoted_only(source: str, name: str, line: int) -> bool:
    word = re.compile(rf"\b{re.escape(name)}\b")
    quoted_here = False
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NAME and tok.string == name:
                return False
            if (tok.type == tokenize.STRING and tok.start[0] <= line <= tok.end[0]
                    and word.search(tok.string)):
                quoted_here = True
    except (tokenize.TokenError, SyntaxError):
        return False
    return quoted_here


def output_tail(stdout: str, stderr: str, workdir: Path) -> tuple[str, ...]:
    """The last lines of a command's output (stdout then stderr), sanitized, bounded,
    with the workdir path normalized to ``.``."""
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
    return tuple(reversed(tail))


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
        pyenv: PythonBuildEnv | None = None,
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "compileall", "-q", ".")
        self.install_cmd = install_cmd or None  # shell line, e.g. the stack's `npm ci`
        self._pyenv = pyenv  # a Python build's declared deps: pip into its own venv
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f). Build has no held-out mutant
        # machinery; the ids are still bound so the key shape is uniform.
        self.mutant_rotation_id = 0
        # v2 = commands run in the confined sandbox: never reuse a pre-sandbox verdict.
        # The command digest: a cached verdict never outlives a change of commands.
        self.scanner_rule_version = "build-cmd-v3:" + commands_digest(
            cmd=list(self.cmd), install_cmd=self.install_cmd, profile=profile_name,
            pip_install=list(pyenv.requirements) if pyenv is not None else None,
        )

    # ---- zone A: [install, then] build; observe the exit code(s) ------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        with toolchain_scope():  # the install's downloads reach the build step
            return self._execute(workdir)

    def _execute(self, workdir: Path) -> ZoneAObservation:
        structured: dict[str, object] = {"cmd": list(self.cmd)}
        codes: tuple[int, ...] = ()
        out = err = ""
        if self._pyenv is not None:
            structured["pip_install"] = list(self._pyenv.requirements)
            rc, out, err = self._pyenv.install(workdir, timeout_s=self._timeout_s)
            codes = (rc,)
            if rc != 0:
                return observe(
                    self.name,
                    exit_codes=codes,
                    stdout=out,
                    stderr=err,
                    findings=build_findings(
                        rc, out, err, self._pyenv.scratch, step="pip install"
                    ),
                    structured=structured,
                    seal_key=self._seal_key,
                )
        elif self.install_cmd is not None:
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
        rc, b_out, b_err = ground_untrusted(
            self.cmd, workdir, timeout_s=self._timeout_s,
            extra_env=self._pyenv.run_env() if self._pyenv is not None else None,
            binds=self._pyenv.binds if self._pyenv is not None else (),
        )
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
        installs = self.install_cmd is not None or self._pyenv is not None
        expected = 2 if installs else 1
        codes = obs.exit_codes
        if not codes or len(codes) > expected:
            return GateStatus.ERROR  # no / malformed observation -> not a verdict
        if any(c in MISSING_BINARY_EXIT_CODES for c in codes):
            return GateStatus.ERROR  # missing toolchain: infra, never latched
        if self._pyenv is not None and codes[0] != 0:
            from .pydeps import install_failure_is_project

            # The project's own bad declaration latches; the network's failure never.
            if install_failure_is_project(obs.stdout + obs.stderr):
                return GateStatus.FAIL
            return GateStatus.ERROR
        if self.install_cmd is not None and codes[0] != 0:
            return GateStatus.ERROR  # failed install (network/registry): retriable
        if any(c != 0 for c in codes):
            return GateStatus.FAIL
        return GateStatus.PASS if len(codes) == expected else GateStatus.ERROR
