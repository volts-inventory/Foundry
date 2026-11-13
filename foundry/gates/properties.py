"""Contract properties: is each planned invariant proven by a property-based test?

The planner gives function/class contracts ``properties`` ("PR1: for any valid
notation the total lies within [min, max]"); the implementing ticket writes one
property-based test per property with its language's library, carrying the id in
the test's name. This check reads the tree (nothing runs): a property is covered
when a source file that uses the library (the profile's ``marker``) names its id,
and does not import it only optionally (the profile's ``guard``: a test that skips
when the library is missing proves nothing).
The tests themselves run in the stack's own test command, so a broken invariant
FAILs the Test gate there; :func:`property_findings` names the property a failing
output line carries so the fix loop sees which invariant broke, and marks one the
run reports skipped as not run (a skip on the first generated input skips it every
run, and a skipped test passes the suite).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from foundry.gates.profiles import SOURCE_SUFFIXES, property_testing
from foundry.schemas.implementation import FEATURE_DIR

_PRUNE = frozenset(
    {"node_modules", "vendor", "target", "venv", "__pycache__", "site-packages", FEATURE_DIR}
)
_MAX_BYTES = 1_000_000
_FAILED = re.compile(r"FAIL|Error|panicked|✗|✕|not ok|Falsifying", re.IGNORECASE)
# A runner's per-test skip line (pytest -v, Go -v, cargo, jest/vitest, TAP).
_SKIP_LINE = re.compile(r"\bSKIP(PED)?\b|\bskipped\b|\bignored\b|\bpending\b|[↓○]|# SKIP")
# pytest -rs: "SKIPPED [1] tests/test_props.py:7: reason" names the skip's location.
_SKIP_AT = re.compile(r"^SKIPPED \[\d+\] (.+?):(\d+): ", re.MULTILINE)
# One failing test's block in a runner's report: pytest's "___ name ___" (and its
# "=== FAILURES ===" rule), jest/vitest "●", Go "--- FAIL:", cargo "---- name ----", TAP.
_BLOCK_HEAD = re.compile(
    r"^(?:_{3,}\s.*\s_{3,}\s*$|={3,}\s.*\s={3,}\s*$|\s*●\s|\s*--- FAIL:|---- .+ ----\s*$|not ok\b)"
)
# Raised while the library built an input, in a fixture's setup, or by the library
# rejecting the test's own strategy: the test's setup broke, not the invariant.
_SETUP_FAILED = re.compile(
    r"while generating|InvalidArgument|FailedHealthCheck|Unsatisfiable|hypothesis\.errors|"
    r"ERROR at setup|fixture '[^']*' not found|error in (?:a |the )?(?:strategy|arbitrary|fixture)",
    re.IGNORECASE,
)
# State an earlier generated example left behind (a shared database, file, registry).
_SHARED_STATE = re.compile(
    r"already exists|duplicate|unique constraint|IntegrityError|FileExistsError|"
    r"already (?:registered|taken|in use)",
    re.IGNORECASE,
)
# The property's own verdict: an assertion that failed, not an error in its setup.
_ASSERTION = re.compile(
    r"AssertionError|^\s*E\s+assert\b|assertion failed|AssertError|\bexpect\(|Expected:",
    re.MULTILINE,
)
_FRAME = re.compile(
    r"(?:File \")?((?:\.{0,2}/)?[\w.@+-]+(?:/[\w.@+-]+)*\.(?:py|[cm]?[jt]sx?|go|rs|rb|java|kt|cs))"
    r"(?:\", line |:)\d+"
)
_LIBRARY = re.compile(
    r"site-packages|dist-packages|node_modules|/lib/python|(?:^|/)(?:hypothesis|_pytest|pluggy)/|"
    r"/go/src/|/pkg/mod/|\.cargo/|rustc/|<frozen"
)
_TEST_FILE = re.compile(
    r"(?:^|/)(?:test_[^/]*|[^/]*_test\.\w+|[^/]*\.(?:test|spec)\.\w+|conftest\.py)$|"
    r"(?:^|/)(?:tests?|__tests__|spec|step_definitions)/"
)
_ISOLATION = (
    "Each generated example must start from fresh state: function-scoped fixtures "
    "(tmp_path, a db or app fixture, beforeEach) are set up once per test, not per "
    "example, so every example after the first sees what the earlier ones wrote; "
    "create the store, temp dir or app inside the test body for each example"
)
# The line that opens the test a skip location sits in.
_DEFINITION = re.compile(
    r"^\s*(?:async\s+)?(?:def|func|fn|function|void|public)\b|\b(?:it|test)\s*\("
)


def _id_pattern(pid: str) -> re.Pattern[str]:
    """``pid`` as a token of a test name: ``test_pr1_x``, ``TestPR1X``, ``'PR1: x'``,
    never ``PR10``."""
    p = re.escape(pid)
    return re.compile(rf"(?<![0-9]){p}(?![0-9])|(?i:(?<![a-z0-9]){p}(?![0-9]))")


def _library_files(workdir: Path, language: str, marker: re.Pattern[str]) -> dict[str, str]:
    """repo path -> text of every source file of ``language`` that uses the library."""
    suffixes = SOURCE_SUFFIXES.get(language, ())
    out: dict[str, str] = {}
    root = Path(workdir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _PRUNE)
        for name in sorted(filenames):
            if not name.endswith(suffixes):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if marker.search(text):
                out[path.relative_to(root).as_posix()] = text
    return out


def _skipped(workdir: Path, ran: str, patterns: Mapping[str, re.Pattern[str]]) -> set[str]:
    """Property ids the run's output reports skipped: a skip line naming the id, or a
    skip location inside a test whose definition line names the id."""
    out = {
        pid
        for line in ran.splitlines()
        if _SKIP_LINE.search(line) and not _SKIP_AT.match(line)
        for pid, pat in patterns.items()
        if pat.search(line)
    }
    root = Path(workdir).resolve()
    for m in _SKIP_AT.finditer(ran):
        path = (root / m.group(1)).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[: int(m.group(2))]):
            if _DEFINITION.search(line):
                out.update(pid for pid, pat in patterns.items() if pat.search(line))
                break
    return out


def _failure_block(output: str, pat: re.Pattern[str]) -> list[str]:
    """The lines of the report block whose header names the property's test."""
    lines = output.splitlines()
    starts = [i for i, ln in enumerate(lines) if _BLOCK_HEAD.match(ln)]
    for n, i in enumerate(starts):
        if pat.search(lines[i]):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            return lines[i:end]
    return []


def _tree_frames(block: Sequence[str], workdir: Path) -> list[str]:
    """The tree's own files the block's traceback frames sit in (library frames out)."""
    root = Path(workdir).resolve()
    out: list[str] = []
    for line in block:
        for m in _FRAME.finditer(line):
            path = m.group(1)
            if _LIBRARY.search(path):
                continue
            if path.startswith("/"):
                try:
                    path = Path(path).resolve().relative_to(root).as_posix()
                except ValueError:
                    continue  # outside the tree: an interpreter or library frame
            out.append(path.removeprefix("./"))
    return out


def _test_side(block: Sequence[str], workdir: Path, tests: Sequence[str]) -> str:
    """Why a property's failure is the test's own, not the invariant's ("" = it is
    the invariant's): an error raised generating an input or in a fixture, state an
    earlier example left behind, or an error (not an assertion) whose frames are all
    in test files."""
    text = "\n".join(block)
    if m := _SETUP_FAILED.search(text):
        return f"the test's own strategy or fixture raised ({m.group(0)})"
    if _ASSERTION.search(text):
        return ""
    if _SHARED_STATE.search(text):
        return "state an earlier generated example left behind made a later one fail"
    frames = _tree_frames(block, workdir)
    if frames and all(f in tests or _TEST_FILE.search(f) for f in frames):
        return "every frame of the traceback is in the test's own code"
    return ""


def property_findings(
    workdir: Path,
    properties: Mapping[str, str],
    language: str,
    output: str = "",
    *,
    ran: str = "",
) -> tuple[dict[str, str], tuple[str, ...]]:
    """(property id -> "covered" | "missing" | "skipped", findings) for ``properties``
    (id -> "<contract>: <statement>"). ``output`` is a red test run's output: a line
    naming a property's id there adds a "failing" finding. ``ran`` is the run's
    output whatever its exit: a property it reports skipped is "skipped". A language
    with no library in its profile checks nothing."""
    lib = property_testing(language)
    if lib is None or not properties:
        return {}, ()
    files = _library_files(workdir, language, re.compile(lib.marker, re.MULTILINE))
    guard = re.compile(lib.guard) if lib.guard else None
    guarded = {p for p, text in files.items() if guard is not None and guard.search(text)}
    status: dict[str, str] = {}
    findings: list[str] = []
    fail_lines = [ln.strip() for ln in output.splitlines() if _FAILED.search(ln)]
    skipped = _skipped(workdir, ran, {pid: _id_pattern(pid) for pid in properties})
    for pid, what in sorted(properties.items()):
        pat = _id_pattern(pid)
        named = [p for p, text in files.items() if pat.search(text)]
        where = [p for p in named if p not in guarded]
        if named and not where:
            status[pid] = "missing"
            findings.append(
                f"property {pid} not covered ({', '.join(named)}): {what} — the test "
                f"imports {lib.library} optionally (try/except ImportError, or a skip "
                "when it is missing), so it can pass without running. Import it "
                "unconditionally at the top of the test module"
                + (f" and declare {lib.dependency}." if lib.dependency else ".")
            )
            continue
        if not where:
            status[pid] = "missing"
            findings.append(
                f"property {pid} missing: {what} — no {lib.library} test names {pid}. "
                f"The ticket implementing the contract writes one property-based test "
                f"for it: {lib.usage}; it generates inputs (no fixed examples) and "
                "calls the real function."
                + (f" Declare {lib.dependency}." if lib.dependency else "")
            )
            continue
        if pid in skipped:
            status[pid] = "skipped"
            findings.append(
                f"property {pid} not run ({', '.join(where)}): {what} — its test was "
                "skipped, and a skipped test proves nothing (a skip on the first input "
                "the library tries skips it every run). Never skip inside a property: "
                "discard the inputs it does not cover with the library's assume() or a "
                "filtered strategy instead"
            )
            continue
        status[pid] = "covered"
        failing = [ln for ln in fail_lines if pat.search(ln)]
        if not failing:
            continue
        why = _test_side(_failure_block(output, pat), workdir, where)
        if why:
            findings.append(
                f"property {pid} failing ({', '.join(where)}): {what} — the TEST is wrong, "
                f"not the implementation: {why}. Fix the test's strategy or fixture, not "
                f"the implementation. {_ISOLATION}: {failing[0][:300]}"
            )
        else:
            findings.append(
                f"property {pid} failing ({', '.join(where)}): {what} — the invariant "
                f"does not hold; fix the implementation, not the test: {failing[0][:300]}"
            )
    return status, tuple(findings)
