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
from collections.abc import Mapping
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
        if failing:
            findings.append(
                f"property {pid} failing ({', '.join(where)}): {what} — the invariant "
                f"does not hold; fix the implementation, not the test: {failing[0][:300]}"
            )
    return status, tuple(findings)
