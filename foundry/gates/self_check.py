"""An agent's in-session gate check: the gates themselves, run on its sandbox.

A local model's session used to check its work with its own ``run`` commands, in a
sandbox that differs from where the work is judged (network on, its own pip
installs, step modules the gate never binds, a tree without the trusted features
or contracts). This module runs what the per-wave check runs — the Build gate and
the Test gate scoped to the buildable scenarios (``run_build_gate``) — on a
gate-shaped copy of the session's tree: the integrated tree the wave builds on
(``materialize_workdir``), the session's in-scope changes on top, the manifest
scaffolded as integration does. The gates' own ``execute`` + ``decide`` run it,
unattested: no verdict is signed and no FAIL latches, so a session may be red
mid-work.

A result is split for the session: failures that are its own (its scenarios, its
files, anything it broke) and pre-existing ones (red on the tree it started from).
The engineering node and the fixer pass a :class:`CheckSpec` per session through
:func:`check_scope`; the proxy-agent loop reads it with :func:`current_check`. Every
proxy-agent session gets it — the local tiers, and the cloud API backends
(``--adapter proxy-agent``) too — unless ``FOUNDRY_SELF_CHECK`` says otherwise:
``0``/``off`` turns it off, ``local`` keeps it to the local tiers' sessions (the
tiered adapter marks those, :func:`local_session`); unset or anything else is on.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from foundry import progress
from foundry.canonical import content_digest
from foundry.schemas.gates import GateStatus

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import GateImpl
    from foundry.graph.gate_wiring import ArtifactResolver, BlobResolver
    from foundry.state.factory_state import FactoryState

__all__ = [
    "CHECK_BUDGET",
    "CheckBase",
    "CheckSpec",
    "CheckVerdict",
    "GateOutcome",
    "SelfCheckSettings",
    "SessionCheck",
    "check_base",
    "check_scope",
    "current_check",
    "judge",
    "render",
    "run_check",
]

CHECK_BUDGET = 8
# Per gate step (the install, the build, the scoped scenarios): a local check is
# seconds to a minute; a hung one must not eat the session.
CHECK_TIMEOUT_S = 180.0
# "0"/"off": no check anywhere; "local": local-tier sessions only (an A/B on the bench).
DISABLE_ENV = "FOUNDRY_SELF_CHECK"
_OFF = frozenset({"0", "off", "false", "no"})
_SECTION_CAP = 3_000


@dataclass(frozen=True)
class SelfCheckSettings:
    """How the sessions of one graph check: the profile the wave check falls back
    to when the state has no stack, the per-session budget and the step timeout."""

    profile: VerificationProfile | None = None
    budget: int = CHECK_BUDGET
    timeout_s: float = CHECK_TIMEOUT_S

    @staticmethod
    def enabled() -> bool:
        return _mode() not in _OFF


def _mode() -> str:
    return os.environ.get(DISABLE_ENV, "").strip().lower()


_LOCAL: ContextVar[bool] = ContextVar("foundry_local_session", default=False)


@contextlib.contextmanager
def local_session() -> Iterator[None]:
    """Mark the session run inside as a local-tier one (the tiered adapter)."""
    token = _LOCAL.set(True)
    try:
        yield
    finally:
        _LOCAL.reset(token)


def wanted() -> bool:
    """Does the session running now get the check (``FOUNDRY_SELF_CHECK``)?"""
    mode = _mode()
    if mode in _OFF:
        return False
    return _LOCAL.get() if mode == "local" else True


class CheckBase:
    """The integrated tree one wave's (or one fix pass's) sessions check against:
    the materialized tree before any session's changes (gate inputs included, the
    manifest not scaffolded — each check scaffolds its own copy). Built lazily by
    the first session that checks (its opening check), so a wave whose adapter never
    checks (claude-code, stub) never builds it; the opening check's gate run is
    shared by every session with the same scenario scope."""

    def __init__(
        self,
        state: FactoryState,
        tmp: Path,
        *,
        resolve: ArtifactResolver,
        resolve_blob: BlobResolver,
        rt: AgentRuntime,
        settings: SelfCheckSettings,
    ) -> None:
        self.state = state
        self.rt = rt
        self.settings = settings
        self._tmp = tmp
        self._resolve = resolve
        self._resolve_blob = resolve_blob
        self._lock = threading.Lock()
        self._built: tuple[Path, tuple[str, ...]] | None = None
        self._openings: dict[frozenset[str], tuple[GateOutcome, ...]] = {}
        self._opening_locks: dict[frozenset[str], threading.Lock] = {}

    def tree(self) -> tuple[Path, tuple[str, ...]]:
        """(the materialized tree, every ticket's planned files). Raises
        :class:`~foundry.graph.gate_wiring.MaterializationError` when the tree
        cannot be built."""
        from foundry.graph.gate_wiring import materialize_workdir, planned_files

        with self._lock:
            if self._built is None:
                root = self._tmp / "tree"
                shutil.rmtree(root, ignore_errors=True)
                materialize_workdir(self.state, root, resolve=self._resolve,
                                    resolve_blob=self._resolve_blob, scaffold_manifest=False)
                self._built = (root, tuple(planned_files(self.state, self._resolve)))
            return self._built

    def opening(self, spec: CheckSpec) -> tuple[GateOutcome, ...]:
        """The gates on the unchanged tree for ``spec``'s scope, run once per scope.
        The unbuilt files are the planned files still absent, the session's own
        included: each session judges which of them are its own."""
        with self._lock:
            lock = self._opening_locks.setdefault(spec.scope, threading.Lock())
        with lock:
            if spec.scope not in self._openings:
                self._openings[spec.scope] = run_check(spec, {}, ())
            return self._openings[spec.scope]


@contextlib.contextmanager
def check_base(
    state: FactoryState,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    rt: AgentRuntime,
    settings: SelfCheckSettings,
) -> Iterator[CheckBase]:
    """The :class:`CheckBase` for ``state``, removed on exit."""
    with tempfile.TemporaryDirectory(prefix="foundry-check-base-") as tmp:
        yield CheckBase(state, Path(tmp), resolve=resolve, resolve_blob=resolve_blob, rt=rt,
                        settings=settings)


@dataclass(frozen=True)
class CheckSpec:
    """One session's check: the scenario refs the scoped Test gate runs, which of
    them are the session's own (``None``: every item is), its write scope (also the
    files whose failures are its own; empty = anything but the gate inputs), whether
    a red build is always its own (a fixer aimed at the build), and the packet-rule
    rewrites for a session that can check."""

    base: CheckBase
    scope: frozenset[str]
    own_refs: frozenset[str] | None
    own_files: tuple[str, ...] = ()
    own_build: bool = False
    rules: Mapping[str, str] = field(default_factory=dict)


# A ContextVar so concurrent pool workers each see their own session's spec.
_SPEC: ContextVar[CheckSpec | None] = ContextVar("foundry_self_check", default=None)


@contextlib.contextmanager
def check_scope(spec: CheckSpec | None) -> Iterator[None]:
    """Expose ``spec`` to :func:`current_check` for the duration."""
    token = _SPEC.set(spec)
    try:
        yield
    finally:
        _SPEC.reset(token)


def current_check() -> CheckSpec | None:
    return _SPEC.get()


@dataclass(frozen=True)
class GateOutcome:
    name: str
    status: GateStatus
    findings: tuple[str, ...]
    scenarios: Mapping[str, str] = field(default_factory=dict)  # ref -> outcome
    # The planned files the checked tree does not hold yet (later or sibling tickets').
    unbuilt: tuple[str, ...] = ()


def _unattested(
    gate: GateImpl, workdir: Path, unbuilt: tuple[str, ...] = (), base: Path | None = None
) -> GateOutcome:
    """The gate's zone-A grounding and zone-B decision, as the runner calls them,
    minus the seal, the signature and the latch. An exception is an ERROR. The
    findings name the copy (and the base it was copied from) as ``.``."""
    out = _outcome(gate, workdir)
    dirs = [d for d in (workdir, base) if d is not None]
    roots = sorted({str(r) for d in dirs for r in (d, d.resolve())}, key=len, reverse=True)

    def rel(line: str) -> str:
        for root in roots:
            line = line.replace(root + "/", "./").replace(root, ".")
        return line

    return GateOutcome(out.name, out.status, tuple(rel(f) for f in out.findings),
                       out.scenarios, unbuilt)


def _outcome(gate: GateImpl, workdir: Path) -> GateOutcome:
    from foundry.gates.runner import timeout_findings
    from foundry.gates.test import BDD_KEY

    try:
        obs = gate.execute(workdir)
    except subprocess.TimeoutExpired as exc:
        return GateOutcome(gate.name, GateStatus.ERROR, timeout_findings(exc, workdir))
    except Exception as exc:
        return GateOutcome(gate.name, GateStatus.ERROR, (f"{type(exc).__name__}: {exc}",))
    try:
        status = gate.decide(obs)
    except Exception as exc:
        return GateOutcome(gate.name, GateStatus.ERROR,
                           (*obs.findings, f"{type(exc).__name__}: {exc}"))
    scenarios = obs.structured.get(BDD_KEY)
    return GateOutcome(gate.name, status, obs.findings,
                       dict(scenarios) if isinstance(scenarios, dict) else {})


def run_check(
    spec: CheckSpec, files: Mapping[str, str], deleted: Iterable[str] = ()
) -> tuple[GateOutcome, ...]:
    """Build + scoped Test on the base tree with ``files`` written and ``deleted``
    removed (the session's in-scope changes), exactly as the wave check would
    judge it once integrated."""
    from foundry.graph.gate_wiring import (
        _build_env,
        _build_gate,
        _contained_target,
        _scaffold_manifest,
        _wave_test_gate,
        gate_profile,
        planned_paths,
        wave_stack,
    )

    base = spec.base
    root, planned = base.tree()
    with tempfile.TemporaryDirectory(prefix="foundry-check-") as tmp:
        work = Path(tmp) / "tree"
        shutil.copytree(root, work, symlinks=True)
        for rel, text in sorted(files.items()):
            target = _contained_target(work, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for rel in deleted:
            _contained_target(work, rel).unlink(missing_ok=True)
        stack = base.state.get("stack")
        if stack is None or stack.language == "python":
            _scaffold_manifest(work)
        stack, _ = wave_stack(base.state, planned, work)
        profile = gate_profile({**base.state, "stack": stack}, base.settings.profile)
        timeout = base.settings.timeout_s
        unbuilt = tuple(sorted(p for p in planned_paths(planned)
                               if not _contained_target(work, p).exists()))
        with _build_env(profile, work, base.rt) as pyenv:
            gates: list[GateImpl] = [
                _build_gate(profile, None, pyenv, planned=planned, timeout_s=timeout)]
            if spec.scope:
                test = _wave_test_gate(profile, None, spec.scope, pyenv, timeout_s=timeout)
                if test is not None:
                    gates.append(test)
            return tuple(_unattested(g, work, unbuilt, root) for g in gates)


@dataclass(frozen=True)
class CheckVerdict:
    """One check, split for the session: ``yours`` (fix before done),
    ``pre_existing`` (red on the starting tree, not caused by the session's files),
    ``errors`` (a gate that could not judge: infra, not a verdict)."""

    yours: tuple[str, ...] = ()
    pre_existing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    checked: tuple[str, ...] = ()  # what ran: "build", "N scenario(s)"
    seconds: float = 0.0

    @property
    def red(self) -> bool:
        return bool(self.yours)

    @property
    def green(self) -> bool:
        return not self.yours and not self.errors


def _red_refs(gate: GateOutcome | None) -> set[str]:
    if gate is None or gate.name != "test" or gate.status is not GateStatus.FAIL:
        return set()
    return {r for r, o in gate.scenarios.items() if o != "passed"}


def _path_rx(paths: Iterable[str]) -> re.Pattern[str] | None:
    alts = [re.escape(f.rstrip("/")) for f in paths if f]
    if not alts:
        return None
    return re.compile(r"(?<![\w.-])(?:\./)?(?:" + "|".join(sorted(alts, key=len, reverse=True))
                      + r")(?![\w-])")


def _mentions(lines: Iterable[str], files: Iterable[str]) -> bool:
    rx = _path_rx(files)
    return rx is not None and any(rx.search(line) for line in lines)


def _unbuilt_rx(unbuilt: Iterable[str]) -> re.Pattern[str] | None:
    """A line naming, or importing, a planned file no ticket has built yet: its
    path, its module (``app.models``, ``./models``) or its package directory
    (``example.com/notes/store``)."""
    alts: set[str] = set()
    for p in unbuilt:
        pp = PurePosixPath(p)
        stem = pp.with_suffix("").as_posix()
        alts.add(r"(?<![\w.-])(?:\./)?" + re.escape(p) + r"(?![\w-])")
        alts.add(r"(?<![\w.-])(?:\./)?" + re.escape(stem) + r"(?![\w./-])")
        alts.add(r"/" + re.escape(pp.stem) + r"['\"]")
        if pp.suffix == ".py":
            alts.add(r"(?<![\w.])" + re.escape(stem.replace("/", ".")) + r"(?![\w.])")
        if pp.parent.as_posix() not in (".", ""):
            alts.add(r"/" + re.escape(pp.parent.as_posix()) + r"(?![\w./-])")
    return re.compile("|".join(sorted(alts))) if alts else None


def _sourced(line: str) -> bool:
    from foundry.graph.gate_wiring import _PY_FRAME, _SOURCE_LINE

    return bool(_SOURCE_LINE.search(line) or _PY_FRAME.search(line))


def _norm(line: str) -> str:
    from foundry.graph.gate_wiring import _VOLATILE

    return _VOLATILE.sub("#", line).strip()


def _split_build(
    spec: CheckSpec, g: GateOutcome, start: GateOutcome
) -> tuple[list[str], list[str]]:
    """(own, other) lines of a FAILed build. A line is the session's when it names
    one of its files, or is new since ``start`` (per normalized line: a broken
    caller in another file is new too). A line naming or importing a planned file
    not built yet waits on that ticket and is never the session's to fix; neither
    is a line already failing at ``start`` that names other files."""
    everything = spec.own_refs is None or spec.own_build
    own_files = spec.own_files
    unbuilt = [p for p in g.unbuilt
               if not any(p == f or (f.endswith("/") and p.startswith(f)) for f in own_files)]
    waits = _unbuilt_rx(unbuilt)
    was = {_norm(x) for x in start.findings} if start.status is GateStatus.FAIL else set()
    head, body = (g.findings[0], g.findings[1:]) if g.findings else ("", ())
    own: list[str] = []
    other: list[str] = []
    context: list[str] = []
    for line in body:
        if waits is not None and waits.search(line):
            other.append(f"(waits on a planned file not built yet) {line}")
        elif everything or _mentions([line], own_files) or (
                _norm(line) not in was and (not was or _sourced(line))):
            own.append(line)
        elif _sourced(line):
            other.append(line)
        else:
            context.append(line)
    (own if own else other).extend(context)
    if not body and head:
        own_head = everything or not was or _mentions([head], own_files)
        return ([head], []) if own_head else ([], [head])
    return ([head, *own] if own and head else own), ([head, *other] if other and head else other)


def judge(
    spec: CheckSpec,
    gates: tuple[GateOutcome, ...],
    start: Mapping[str, GateOutcome] | None = None,
    seconds: float = 0.0,
) -> CheckVerdict:
    """Split ``gates`` against ``start`` (each gate's baseline: its first result that
    judged the tree; a gate with none yet is its own baseline, as in the opening
    check). A failure is the session's own when it names one of its scenarios or
    files, or was not failing at its baseline. A gate ERROR blocks nothing."""
    from foundry.graph.gate_wiring import failing_ids

    base = {g.name: g for g in gates}
    base.update(start or {})
    everything = spec.own_refs is None
    yours: list[str] = []
    pre: list[str] = []
    errors: list[str] = []
    checked: list[str] = []
    for g in gates:
        if g.name == "test":
            checked.append(f"{len(spec.scope)} scenario(s)")
        else:
            checked.append(g.name)
        if g.status is GateStatus.ERROR:
            errors += [f"[{g.name} gate could not run — infra, not a verdict]", *g.findings]
            continue
        if g.status is not GateStatus.FAIL:
            continue
        before = base[g.name]
        head = f"[{g.name} gate FAILED]"
        if g.name != "test":
            own, other = _split_build(spec, g, before)
            if own:
                yours += [head, *own]
            if other:
                pre += [head, *other]
            continue
        start_red = _red_refs(before)
        red = {r for r, o in g.scenarios.items() if o != "passed"}
        mine = {r for r in red
                if everything or r in (spec.own_refs or ()) or r not in start_red}
        theirs = red - mine
        if not red and before.status is GateStatus.FAIL and not everything \
                and not _mentions(g.findings, spec.own_files):
            # Red with no scenario to blame (a module failing to collect) and red at
            # the baseline too: not the session's unless it names the session's files.
            pre += [head, *g.findings]
            continue
        if not theirs:
            yours += [head, *g.findings]
            continue
        own_lines: list[str] = []
        pre_lines: list[str] = []
        for line in g.findings:
            ids = set(failing_ids([line]))
            if ids & mine or _mentions([line], spec.own_files):
                own_lines.append(line)
            elif ids:  # a pre-existing scenario, or a test of a module not the session's
                pre_lines.append(line)
            else:  # context: with the session's own failures if it has any
                (own_lines if mine else pre_lines).append(line)
        if own_lines:
            yours += [head, *own_lines]
        if pre_lines:
            pre += [head, *pre_lines]
    return CheckVerdict(_stable(yours), _stable(pre), _stable(errors), tuple(checked), seconds)


# A Foundry temp dir (a gate venv cache, a check base or copy): random per process.
_TMP_PATH = re.compile(r"(?:/[^\s'\"():]*)?/(foundry-[a-z-]+?-)[a-z0-9_]{8}\b")


def _stable(lines: list[str]) -> tuple[str, ...]:
    """Run-varying bits (timings, addresses, temp dirs) out: the report is part of the
    next model request, which the run's call cache keys on byte for byte."""
    from foundry.graph.gate_wiring import _VOLATILE

    return tuple(_TMP_PATH.sub(r"$TMP/\1", _VOLATILE.sub("#", line)) for line in lines)


def _section(title: str, lines: tuple[str, ...], cap: int) -> str:
    out: list[str] = [title]
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) > cap:
            out.append(f"  ... {len(lines) - i} more line(s)")
            break
        out.append(f"  {line}")
        used += len(line)
    return "\n".join(out)


def render(v: CheckVerdict, *, left: int, cached: bool = False, opening: bool = False) -> str:
    """The observation a session reads for one check."""
    ran = " + ".join(v.checked) or "nothing"
    if opening:
        head = (f"GATE CHECK ON THE STARTING TREE ({ran}, run as the gates run them on a "
                f"gate-shaped copy of this tree). You have {left} `check` "
                "call(s) this session; call `check` after your edits, and finish when it "
                "reports nothing as yours.")
    else:
        status = ("FAILED" if v.red else "PASSED" if v.green
                  else "COULD NOT JUDGE (infra, not a verdict about your code)")
        how = " (unchanged tree, cached result)" if cached else ""
        head = f"CHECK {status}: {ran}, run as the gates run them{how}; {left} check(s) left."
    parts = [head]
    if v.yours:
        parts.append(_section("YOURS (fix these):" if not opening else
                              "YOURS (failing now; your work must make these pass):",
                              v.yours, _SECTION_CAP))
    if v.pre_existing:
        parts.append(_section(
            "PRE-EXISTING (failing before this session; not yours unless your files cause "
            "them — leave them and name them in a TRIAGE line):",
            v.pre_existing, _SECTION_CAP // 2))
    if v.errors:
        parts.append(_section("COULD NOT RUN:", v.errors, _SECTION_CAP // 2))
    return "\n".join(parts)


class SessionCheck:
    """One session's checks: the opening run on its starting tree (free), then at
    most ``budget`` runs, each cached by the in-scope changes it judged (an
    unchanged tree answers from the cache, free)."""

    def __init__(self, spec: CheckSpec, budget: int | None = None, label: str = "") -> None:
        self.spec = spec
        self.label = label
        self.left = spec.base.settings.budget if budget is None else budget
        self.last: CheckVerdict | None = None
        # Gate name -> its first result that judged the tree (PASS or FAIL).
        self._start: dict[str, GateOutcome] = {}
        self._cache: dict[str, CheckVerdict] = {}

    def _scoped(
        self, files: Mapping[str, str], deleted: Iterable[str]
    ) -> tuple[dict[str, str], list[str], str]:
        from foundry.agents.implementation.tickets import in_write_scope

        own = self.spec.own_files
        kept = {p: t for p, t in files.items() if in_write_scope(p, own)}
        gone = sorted(p for p in deleted if in_write_scope(p, own))
        return kept, gone, content_digest({"files": kept, "deleted": gone})

    def _run(
        self, files: dict[str, str], deleted: list[str], key: str, *, opening: bool = False
    ) -> CheckVerdict:
        t0 = time.monotonic()
        gates = (self.spec.base.opening(self.spec) if opening
                 else run_check(self.spec, files, deleted))
        verdict = judge(self.spec, gates, self._start, time.monotonic() - t0)
        for g in gates:
            if g.status in (GateStatus.PASS, GateStatus.FAIL):
                self._start.setdefault(g.name, g)
        self._cache[key] = verdict
        self.last = verdict
        status = "red" if verdict.red else "green" if verdict.green else "error"
        progress.log(f"self-check {self.label or '-'}: {status} ({len(verdict.yours)} own, "
                     f"{len(verdict.pre_existing)} pre-existing line(s); "
                     f"{verdict.seconds:.1f}s; {self.left} left)")
        return verdict

    def opening(self) -> CheckVerdict:
        """The check of the starting tree (no changes); not counted."""
        files, deleted, key = self._scoped({}, ())
        return self._run(files, deleted, key, opening=True)

    def cached(self, files: Mapping[str, str], deleted: Iterable[str]) -> CheckVerdict | None:
        verdict = self._cache.get(self._scoped(files, deleted)[2])
        if verdict is not None:
            self.last = verdict
        return verdict

    def check(self, files: Mapping[str, str], deleted: Iterable[str]) -> CheckVerdict:
        """A counted run; the caller checks :meth:`cached` and ``left`` first."""
        kept, gone, key = self._scoped(files, deleted)
        verdict = self._run(kept, gone, key)
        self.left -= 1  # only a check that judged the tree spends the budget
        return verdict
