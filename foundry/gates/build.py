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

A JS install (a tree with a ``package.json``) is reused within the run: its result
(every ``node_modules`` and any lockfile it wrote) is stored, from a successful run
only, under a digest of the install line and the tree's install inputs, in the run's
``ToolchainCache.installs`` — a host-side directory no step is bound to. A later build
with the same digest gets a copy instead of re-running the install.

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

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tokenize
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.canonical import content_digest
from foundry.fs import force_rmtree
from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.telemetry import counted

from .runner import ZoneAObservation, observe
from .sandbox import ground_untrusted, toolchain_scope

if TYPE_CHECKING:
    from .pydeps import PythonBuildEnv
    from .sandbox import ToolchainCache

__all__ = [
    "MISSING_BINARY_EXIT_CODES",
    "TYPECHECK_ONLY",
    "UNDECLARED_IMPORTS",
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
# The head of a build that passed but imports what its manifest does not declare.
UNDECLARED_IMPORTS = "the build passed, but the project imports modules it does not declare"
# mypy's closing line when it checked the tree and found type errors. A stack's build
# line runs `build && typecheck`, so mypy reporting at all means the steps before it
# passed; "errors prevented further checking" (a syntax error) never matches.
_MYPY_FOUND = re.compile(
    r"^Found \d+ errors? in \d+ files? \(checked \d+ source files?\)$", re.MULTILINE
)
# mypy's advisory note on each annotated line of an untyped function: never an
# error, and enough of them filled the bounded tail and hid the errors it closes on.
_MYPY_UNCHECKED_NOTE = re.compile(r": note: .*\[annotation-unchecked\]\s*$")
_MYPY_NAME_DEFINED = re.compile(
    r"^(?P<path>[^\s:]+\.pyi?):(?P<line>\d+)(?::\d+)?: error: "
    r'Name "(?P<name>\w+)" is not defined\s+\[name-defined\]',
    re.MULTILINE,
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
            head += (
                " (the project's declared dependencies cannot be installed — FAIL: "
                "fix requirements.txt / pyproject.toml)"
            )
            hints = unavailable_requirement_hints(stdout + stderr, workdir)
        else:
            head += (
                " (dependency install failed on the network, the index or the host "
                "— an ERROR, never latched)"
            )
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
        hints = (
            *undeclared_import_hints(stdout + stderr, workdir),
            *forward_annotation_hints(stdout + stderr, workdir),
        )
    tail = output_tail(stdout, stderr, workdir)
    return (f"{head}; last {len(tail)} output line(s):", *tail, *hints)


@counted("build.forward_annotation")
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
        if _quoted_only(
            src.read_text(encoding="utf-8", errors="replace"), name, int(m.group("line"))
        ):
            hint = (
                f"{rel}:{m.group('line')}: `{name}` is used only in a quoted annotation: "
                "import it under `if TYPE_CHECKING:` (from typing import TYPE_CHECKING) "
                "in that module"
            )
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
            if (
                tok.type == tokenize.STRING
                and tok.start[0] <= line <= tok.end[0]
                and word.search(tok.string)
            ):
                quoted_here = True
    except (tokenize.TokenError, SyntaxError):
        return False
    return quoted_here


def output_tail(stdout: str, stderr: str, workdir: Path) -> tuple[str, ...]:
    """The last lines of a command's output (stdout then stderr), sanitized, bounded,
    with the workdir path normalized to ``.``. mypy's diagnostics, when present, are
    regrouped (``_mypy_excerpt``) and close the excerpt, under the same bounds."""
    text = "\n".join(t for t in (stdout, stderr) if t.strip())
    root = str(Path(workdir).resolve())
    text = text.replace(root + "/", "./").replace(root, ".")
    text = _CONTROL_RE.sub("", _ANSI_RE.sub("", text))
    lines = [
        ln.rstrip()[:_LINE_CHARS]
        for ln in text.splitlines()
        if ln.strip() and not _MYPY_UNCHECKED_NOTE.search(ln)
    ]
    mypy = _mypy_excerpt(lines)
    if mypy:
        lines = [ln for ln in lines if not _MYPY_LINE.match(ln) and not _MYPY_FOUND.match(ln)]
    lines_left = _EXCERPT_LINES - len(mypy)
    used = sum(len(ln) for ln in mypy)
    tail: list[str] = []
    for ln in reversed(lines[-lines_left:] if lines_left > 0 else []):
        if used + len(ln) > _EXCERPT_CHARS:
            break
        tail.append(ln)
        used += len(ln)
    return (*reversed(tail), *mypy)


_MYPY_LINE = re.compile(
    r"^(?P<path>[^\s:][^:]*?\.pyi?):(?P<line>\d+)(?::\d+){0,3}: "
    r"(?P<kind>error|note): (?P<msg>.*)$"
)
_MYPY_PER_FILE = 5
_MYPY_ERRORS = 15
_MYPY_NOTES_PER_ERROR = 2


def _mypy_excerpt(lines: list[str]) -> list[str]:
    """mypy's diagnostics for a fixer: the errors grouped by file (files in first-seen
    order), an error repeated with the same message in one file shown once with its
    other lines, at most ``_MYPY_PER_FILE`` per file and ``_MYPY_ERRORS`` in all, each
    followed only by the notes mypy attached to it (same file:line, right after it);
    then how many errors were left out, then mypy's own ``Found N errors`` line.
    Empty when the output holds no mypy error."""
    files: dict[str, dict[str, list[str]]] = {}  # path -> message -> [line, *notes]
    extra: dict[tuple[str, str], list[str]] = {}  # (path, message) -> other line numbers
    owner: tuple[str, str, str] | None = None  # (path, line, message) notes attach to
    found = ""
    for ln in lines:
        if _MYPY_FOUND.match(ln):
            found = ln
            continue
        m = _MYPY_LINE.match(ln)
        if m is None:
            owner = None
            continue
        path, num, msg = m.group("path"), m.group("line"), m.group("msg")
        if m.group("kind") == "error":
            by_msg = files.setdefault(path, {})
            if msg in by_msg:
                extra.setdefault((path, msg), []).append(num)
                owner = None
            else:
                by_msg[msg] = [ln]
                owner = (path, num, msg)
        elif owner is not None and owner[:2] == (path, num):
            entry = files[path][owner[2]]
            if len(entry) <= _MYPY_NOTES_PER_ERROR and ln not in entry:
                entry.append(ln)
    if not files:
        return []
    out: list[str] = []
    shown = 0
    hidden: dict[str, int] = {}
    used = 0
    for path, by_msg in files.items():
        for i, (msg, entry) in enumerate(by_msg.items()):
            again = extra.get((path, msg))
            head = entry[0] + (f" (also at line {', '.join(again)})" if again else "")
            block = [head[:_LINE_CHARS], *entry[1:]]
            size = sum(len(b) for b in block)
            if (
                i >= _MYPY_PER_FILE
                or shown >= _MYPY_ERRORS
                or used + size > _EXCERPT_CHARS - 2 * _LINE_CHARS
                or len(out) + len(block) > _EXCERPT_LINES - 2
            ):
                hidden[path] = hidden.get(path, 0) + 1
                continue
            out.extend(block)
            shown += 1
            used += size
    if hidden:
        n = sum(hidden.values())
        out.append(
            f"... {n} more error{'s' if n != 1 else ''} in {len(hidden)} "
            f"file{'s' if len(hidden) != 1 else ''} not shown"
        )
    if found:
        out.append(found)
    return out


# ---- the run's JS installs, reused by builds with the same install inputs ---------

_LOCKFILES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"})
_INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepare")
# Plain sources an install reads only through the project's own lifecycle scripts
# (when one is declared, the whole tree is keyed). Every other file is keyed: the
# manifests, lockfiles and configs, and inputs a dependency's postinstall may read
# (Prisma's generate reads schema.prisma).
_SOURCE_SUFFIXES = frozenset(
    {
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte",
        ".css", ".scss", ".sass", ".less", ".html", ".md",
    }
)  # fmt: skip


def _scan(root: Path) -> tuple[dict[str, os.stat_result], list[str]]:
    """Every file and symlink outside ``node_modules`` and ``.git`` (lstat), and the
    tree's outermost ``node_modules`` entries."""
    files: dict[str, os.stat_result] = {}
    modules: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        descend = []
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            relpath = os.path.normpath(os.path.join(rel, name))
            if name == "node_modules":
                modules.append(relpath)
            elif name in dirnames and not os.path.islink(path):
                if name != ".git":
                    descend.append(name)
            else:
                files[relpath] = os.lstat(path)
        dirnames[:] = descend
    return files, modules


def _read_regular(path: Path) -> bytes | None:
    """``path``'s bytes when it is a regular file (never through a symlink, never
    blocking on a FIFO the tree planted), else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read() if stat.S_ISREG(os.fstat(fd).st_mode) else None


def _runs_install_script(manifest: Path) -> bool:
    try:
        scripts = json.loads(_read_regular(manifest) or b"").get("scripts") or {}
        return any(k in scripts for k in _INSTALL_SCRIPTS)
    except (ValueError, AttributeError, TypeError):
        return True  # unparseable: assume the install runs the project's own code


def install_key(install_cmd: str, workdir: Path, files: Mapping[str, os.stat_result]) -> str:
    """What a JS install's result is a function of: the install line and every file
    it may read (see ``_SOURCE_SUFFIXES``)."""
    full = any(
        _runs_install_script(workdir / rel)
        for rel in files
        if os.path.basename(rel) == "package.json"
    )
    keyed: dict[str, str] = {}
    for rel, st in sorted(files.items()):
        if not full and os.path.splitext(rel)[1] in _SOURCE_SUFFIXES:
            continue
        if stat.S_ISLNK(st.st_mode):
            keyed[rel] = "link:" + os.readlink(workdir / rel)
        else:
            data = _read_regular(workdir / rel)
            keyed[rel] = "special" if data is None else hashlib.sha256(data).hexdigest()
    return content_digest({"install_cmd": install_cmd, "files": keyed})


def _sig(st: os.stat_result | None) -> tuple[int, int, int] | None:
    return None if st is None else (st.st_mode, st.st_size, st.st_mtime_ns)


def _links_into(tree: Path, workdir: Path) -> bool:
    roots = {str(workdir), os.path.realpath(workdir)}
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                target = os.readlink(path)
                if any(target == r or target.startswith(r + os.sep) for r in roots):
                    return True
    return False


def _store_install(
    installs: Path, key: str, workdir: Path, before: Mapping[str, os.stat_result]
) -> None:
    """Store what a successful install wrote — unless it wrote anything a copy of its
    ``node_modules`` and lockfiles would not restore (generated sources, a symlinked
    ``node_modules``, a link to this workdir's own path)."""
    after, modules = _scan(workdir)
    changed = sorted(
        r for r in before.keys() | after.keys() if _sig(before.get(r)) != _sig(after.get(r))
    )
    if any(
        os.path.basename(r) not in _LOCKFILES
        or r not in after
        or not stat.S_ISREG(after[r].st_mode)
        for r in changed
    ) or any(not stat.S_ISDIR(os.lstat(workdir / m).st_mode) for m in modules):
        return
    tmp = Path(tempfile.mkdtemp(dir=installs, prefix=".store-"))
    # The install itself passed: a store that cannot be written (disk full, or a
    # concurrent build's identical store renamed in first) only costs later reuse.
    with contextlib.suppress(OSError):
        try:
            for rel in modules:
                shutil.copytree(workdir / rel, tmp / "tree" / rel, symlinks=True)
            for rel in changed:
                (tmp / "tree" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workdir / rel, tmp / "tree" / rel, follow_symlinks=False)
            if not _links_into(tmp, workdir):
                (tmp / "manifest.json").write_text(json.dumps([*modules, *changed]))
                os.rename(tmp, installs / key)
        finally:
            if tmp.exists():
                force_rmtree(tmp)


def _restore_install(stored: Path, workdir: Path) -> bool:
    """Copy a stored install into ``workdir``; False, writing nothing, when an entry
    would land through a symlink (the copy runs host-side, outside the sandbox)."""
    entries: list[str] = json.loads((stored / "manifest.json").read_text(encoding="utf-8"))
    root = workdir.resolve()
    for rel in entries:
        dst = workdir / rel
        if dst.is_symlink() or not dst.parent.resolve().is_relative_to(root):
            return False
    for rel in entries:
        src, dst = stored / "tree" / rel, workdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    return True


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
        install_timeout_s: float | None = None,
        seal_key: bytes | None = None,
        profile_name: str = "",
        pyenv: PythonBuildEnv | None = None,
        python: bool = False,
        first_party: frozenset[str] = frozenset(),
    ) -> None:
        self.cmd = cmd or (sys.executable, "-m", "compileall", "-q", ".")
        self.install_cmd = install_cmd or None  # shell line, e.g. the stack's `npm ci`
        self._pyenv = pyenv  # a Python build's declared deps: pip into its own venv
        # A Python build also checks that every unconditional import is declared;
        # ``first_party`` names the modules later tickets will add to the tree.
        self._python = python
        self._first_party = first_party
        self._timeout_s = timeout_s
        # The networked install keeps the gate's bound when the steps after it are
        # bounded tighter (an agent's in-session check).
        self._install_timeout_s = install_timeout_s or timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # Class-2 reuse-key bindings (§1f). Build has no held-out mutant
        # machinery; the ids are still bound so the key shape is uniform.
        self.mutant_rotation_id = 0
        # v2 = commands run in the confined sandbox: never reuse a pre-sandbox verdict.
        # The command digest: a cached verdict never outlives a change of commands.
        self.scanner_rule_version = "build-cmd-v4:" + commands_digest(
            cmd=list(self.cmd),
            install_cmd=self.install_cmd,
            profile=profile_name,
            pip_install=list(pyenv.requirements) if pyenv is not None else None,
            import_scan=sorted(first_party) if python else None,
        )

    # ---- zone A: [install, then] build; observe the exit code(s) ------------

    def execute(self, workdir: Path) -> ZoneAObservation:
        with toolchain_scope() as cache:  # the install's downloads reach the build step
            return self._execute(workdir, cache)

    def _install(
        self, install_cmd: str, workdir: Path, cache: ToolchainCache, structured: dict[str, object]
    ) -> tuple[int, str, str]:
        before, modules = _scan(workdir)
        manifest = before.get("package.json")
        key = None
        if manifest is not None and stat.S_ISREG(manifest.st_mode) and not modules:
            key = install_key(install_cmd, workdir, before)
            stored = cache.installs / key
            if stored.is_dir() and _restore_install(stored, workdir):
                structured["install_cache"] = "hit"
                return 0, f"reused this run's install {key[:16]} (skipped `{install_cmd}`)\n", ""
        structured["install_cache"] = "miss"
        rc, out, err = ground_untrusted(
            ("/bin/sh", "-c", install_cmd),
            workdir,
            network=True,
            timeout_s=self._install_timeout_s,
        )
        if rc == 0 and key is not None:
            _store_install(cache.installs, key, workdir, before)
        return rc, out, err

    def _execute(self, workdir: Path, cache: ToolchainCache) -> ZoneAObservation:
        structured: dict[str, object] = {"cmd": list(self.cmd)}
        codes: tuple[int, ...] = ()
        out = err = ""
        if self._pyenv is not None:
            structured["pip_install"] = list(self._pyenv.requirements)
            rc, out, err = self._pyenv.install(workdir, timeout_s=self._install_timeout_s)
            codes = (rc,)
            if rc != 0:
                return observe(
                    self.name,
                    exit_codes=codes,
                    stdout=out,
                    stderr=err,
                    findings=build_findings(rc, out, err, self._pyenv.scratch, step="pip install"),
                    structured=structured,
                    seal_key=self._seal_key,
                )
        elif self.install_cmd is not None:
            structured["install_cmd"] = self.install_cmd
            rc, out, err = self._install(self.install_cmd, workdir, cache, structured)
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
            self.cmd,
            workdir,
            timeout_s=self._timeout_s,
            extra_env=self._pyenv.run_env() if self._pyenv is not None else None,
            binds=self._pyenv.binds if self._pyenv is not None else (),
        )
        findings = build_findings(rc, b_out, b_err, workdir) if rc != 0 else ()
        if self._python:
            from .pydeps import _NO_MODULE, undeclared_import_findings, undeclared_imports

            missing = undeclared_imports(
                workdir,
                first_party=self._first_party,
                site=Path(self._pyenv.site) if self._pyenv is not None else None,
            )
            if missing:
                structured["undeclared_imports"] = sorted(missing)
                # The build's own ModuleNotFoundError already got its hint.
                named = {m.split(".", 1)[0] for m in _NO_MODULE.findall(b_out + b_err)}
                scan = undeclared_import_findings(
                    {k: v for k, v in missing.items() if rc == 0 or k not in named}, workdir
                )
                if rc == 0:
                    findings = (
                        f"{UNDECLARED_IMPORTS} — FAIL: the gates install only the "
                        "declared dependencies, so these imports fail there:",
                        *scan,
                    )
                else:
                    findings = (*findings, *scan)
        return observe(
            self.name,
            exit_codes=(*codes, rc),
            stdout=out + b_out,
            stderr=err + b_err,
            findings=findings,
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
        if any(c != 0 for c in codes) or obs.structured.get("undeclared_imports"):
            return GateStatus.FAIL
        return GateStatus.PASS if len(codes) == expected else GateStatus.ERROR
