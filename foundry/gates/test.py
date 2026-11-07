"""Test gate — class 2 (execution-deterministic). (governance §1/§1c, plan §5.1)

Grounding: zone A invokes the **pinned trusted harness** (this interpreter's
pytest — agents supply test *sources*, never the scoring harness; plugin
autoload is disabled so an agent-dropped plugin cannot become the reporter)
and observes exit code + parsed summary counts. Zone B computes the verdict
from those observations alone.

Adequacy-floor hook: ``min_passed`` — a green run that exercised fewer than
``min_passed`` tests is FAIL, not PASS, so an empty/vacuous suite can never
clear the gate (the dev-grade floor of governance §1c's adequacy/diversity
requirement). The full held-out-mutant + behavioral-invariance + criterion-
fidelity machinery is Phase 2; its reuse-key binding is wired NOW —
``mutant_rotation_id`` (default 0) is bound into the class-2 reuse key via
``TwoZoneGateRunner.reuse_key`` so cached verdicts already invalidate across
a rotation boundary when rotation arrives.

Exit-code semantics (pytest): 0 green, 1 test failures, 2/3/4 harness/usage
errors (→ ERROR: infra, not a verdict), 5 no tests collected (→ adequacy FAIL
when a floor is set). A stack-provided command (``stack_harness``: npm test, go
test, the Cucumber ``bdd`` command) is not pytest: 0 passes, 126/127 (the shell
could not find or run a binary — missing toolchain) is ERROR, anything else FAIL.

Executable-BDD residual limit: the reporter runs in the same interpreter as the
agent's test code, so it raises the bar (no hook, stdout, shadowing, or rewritten-
feature forgery; no catch-all-only binding) but cannot stop code that deliberately
introspects and patches the reporter in-process, nor judge whether a specific step
definition actually asserts anything.
"""

from __future__ import annotations

import ast
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import get_close_matches
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.schemas.implementation import BDD_REPORT_ENV, FEATURE_DIR

from . import cucumber
from .bdd_scaffold import parse_feature, parse_fields, step_matches
from .build import MISSING_BINARY_EXIT_CODES, commands_digest
from .runner import ZoneAObservation, observe

if TYPE_CHECKING:
    from .discriminator import TestSuiteDiscriminator
    from .pydeps import PythonBuildEnv

__all__ = ["TestGate", "parse_pytest_summary"]

# Structured-observation key carrying the discriminator's report (zone A → B).
DISCRIMINATOR_KEY = "discriminator"
# Structured-observation key carrying {scenario_ref: outcome} for every expected
# BDD scenario (outcome ∈ passed / failed / skipped / missing / unmatched / catch-all).
BDD_KEY = "bdd"
# Exit code of the planned Cucumber ``bdd`` command (absent on the pytest-bdd path).
BDD_EXIT_KEY = "bdd_exit_code"

_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")

# pytest exit codes that are harness/usage failures, not verdicts.
_INFRA_EXIT_CODES = frozenset({2, 3, 4})
_NO_TESTS_COLLECTED = 5

# Env key naming the host paths to FENCE OFF from the pinned-harness subprocess.
_DENY_PATHS_ENV = "FOUNDRY_GATE_DENY_PATHS"

# The pinned pytest invocation, run as a bootstrap that ISOLATES generated code from
# ours: it cd's to the workdir and strips the workdir ('' / cwd) and the host source
# checkout (``_DENY_PATHS_ENV``) from ``sys.path``, imports EVERY trusted module it
# relies on, and only THEN puts the workdir first — so a workdir ``json.py`` /
# ``resource.py`` / ``pytest_bdd/`` can never shadow the harness, and a model-named
# module that collides with a host package (e.g. ``foundry.x``) resolves to the
# WORKDIR, never to our code. It also caps cpu/file/proc resources. Run on its own
# (in-process fence) OR inside the namespace sandbox.
_BOOTSTRAP_PREAMBLE = """\
import os, sys
wd = os.environ.get("FOUNDRY_GATE_WORKDIR") or os.getcwd()
os.chdir(wd)
_raw = os.environ.get("FOUNDRY_GATE_DENY_PATHS", "").split(os.pathsep)
deny = {os.path.realpath(p) for p in _raw if p} | {os.path.realpath(wd)}
sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) not in deny]
import resource
for _res, _lim in ((resource.RLIMIT_CPU, (300, 300)),
                   (resource.RLIMIT_FSIZE, (256 << 20, 256 << 20)),
                   (resource.RLIMIT_NPROC, (1024, 1024))):
    try:
        resource.setrlimit(_res, _lim)
    except Exception:
        pass
"""
# Env key naming a Python build's venv site-packages (``pydeps``).
BUILD_SITE_ENV = "FOUNDRY_GATE_SITE"
# Run only AFTER the trusted imports: the build venv's site-packages join sys.path
# as plain paths (no .pth runs) between the stdlib and Foundry's site-packages. After
# the stdlib, so a declared `json` can't replace the stdlib's; before Foundry's, so a
# declared version of a package Foundry also has is the one the project imports.
ADD_BUILD_SITE = f"""\
import site as _site
_sp = {{os.path.realpath(p) for p in _site.getsitepackages()}}
_at = next((i for i, p in enumerate(sys.path) if os.path.realpath(p) in _sp), len(sys.path))
sys.path[_at:_at] = [p for p in os.environ.get("{BUILD_SITE_ENV}", "").split(os.pathsep) if p]
"""
_ISOLATED_PYTEST_BOOTSTRAP = _BOOTSTRAP_PREAMBLE + """\
from pytest import console_main
""" + ADD_BUILD_SITE + """\
sys.path.insert(0, wd)
sys.exit(console_main())
"""

# Env keys handing the BDD bootstrap its gate-owned channel; it pops them before any
# workdir code runs. The report path is in a fresh temp dir OUTSIDE the workdir and is
# opened O_EXCL at startup; the per-run nonce is echoed into the report and checked.
_BDD_REPORT_ENV = BDD_REPORT_ENV
_BDD_NONCE_ENV = "FOUNDRY_BDD_NONCE"
_BDD_EXPECTED_ENV = "FOUNDRY_BDD_EXPECTED"

# Same isolation, plus pytest-bdd registered by MODULE OBJECT (autoload stays off; no
# name lookup once the workdir is on sys.path) and a trusted reporter that wraps
# pytest-bdd's step executor. A scenario passes only if its pytest item passed AND
# every step of the TRUSTED scenario (type + text, parsed by the gate before the run)
# executed through pytest-bdd and returned; hook calls (which test code can fire by
# hand) never mark anything executed. The report is written after pytest returns, then
# the process ``_exit``s so no agent atexit code runs after it; stdout is not consulted.
_ISOLATED_PYTEST_BDD_BOOTSTRAP = _BOOTSTRAP_PREAMBLE + f"""\
import importlib, json, pytest
import pytest_bdd.plugin
_bdd_scenario = importlib.import_module("pytest_bdd.scenario")

_report_path = os.environ.pop("{_BDD_REPORT_ENV}")
_nonce = os.environ.pop("{_BDD_NONCE_ENV}")
with open(os.environ.pop("{_BDD_EXPECTED_ENV}"), encoding="utf-8") as _f:
    _expected = {{k: [tuple(s) for s in v] for k, v in json.load(_f).items()}}
_fd = os.open(_report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
import secrets as _secrets
_alpha = {cucumber._PROBE_ALPHABET!r}



def _probes(words):
    # Three random texts with the step's word count: nothing fixed to dodge.
    return [" ".join("".join(_secrets.choice(_alpha) for _ in range(1 + _secrets.randbelow(10)))
                     for _ in range(max(words, 1))) for _ in range(3)]


_dumps = json.dumps
_execute_step = _bdd_scenario._execute_step_function
{ADD_BUILD_SITE}sys.path.insert(0, wd)


class _BddReport:
    def __init__(self):
        self.claims = set()  # (nodeid, ref)
        self.steps = {{}}      # (nodeid, ref) -> [[type, text, ok, catch_all], ...]
        self.failed, self.passed, self.skipped = set(), set(), set()

    @staticmethod
    def _ref(scenario):
        rel = os.path.relpath(scenario.feature.filename, wd).replace(os.sep, "/")
        return rel + "::" + scenario.name

    @pytest.hookimpl(optionalhook=True)
    def pytest_bdd_before_scenario(self, request, feature, scenario):
        # A claim only (an unbound first step then reads "failed"); never a pass.
        self.claims.add((request.node.nodeid, self._ref(scenario)))

    def execute_step(self, request, scenario, step, context):
        key = (request.node.nodeid, self._ref(scenario))
        self.claims.add(key)
        catch_all = all(
            context.parser.is_matching(p) for p in _probes(len(step.name.split()))
        )
        rec = [step.type, step.name, False, catch_all]
        self.steps.setdefault(key, []).append(rec)
        out = _execute_step(request, scenario, step, context)
        rec[2] = True
        return out

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call":
            (self.passed if report.passed else self.skipped).add(report.nodeid)

    def _outcome(self, nodeid, ref):
        steps = self.steps.get((nodeid, ref), [])
        if nodeid in self.failed or not all(s[2] for s in steps):
            return "failed"
        if nodeid not in self.passed:
            return "skipped" if nodeid in self.skipped else "missing"
        if not steps:
            return "missing"
        if [(s[0], s[1]) for s in steps] != _expected.get(ref):
            return "unmatched"
        if all(s[3] for s in steps) or any(s[3] and s[0] == "then" for s in steps):
            return "catch-all"
        return "passed"

    def outcomes(self):
        rank = ("failed", "passed", "catch-all", "skipped", "unmatched", "missing")
        out = {{}}
        for nodeid, ref in sorted(self.claims):
            o = self._outcome(nodeid, ref)
            if ref not in out or rank.index(o) < rank.index(out[ref]):
                out[ref] = o
        return out


_rep = _BddReport()
_bdd_scenario._execute_step_function = _rep.execute_step
_rc = pytest.main(sys.argv[1:], plugins=[pytest_bdd.plugin, _rep])
_data = _dumps({{"nonce": _nonce, "scenarios": _rep.outcomes()}}, sort_keys=True).encode()
while _data:
    _data = _data[os.write(_fd, _data):]
os.close(_fd)
sys.stdout.flush()
sys.stderr.flush()
os._exit(int(_rc))
"""

_STEP_KEYWORDS = {"Given": "given", "When": "when", "Then": "then"}


def trusted_scenarios(workdir: Path) -> dict[str, list[tuple[str, str]]]:
    """``features/<ticket>.feature::<criterion id>`` -> its ``(type, text)`` steps,
    parsed from the trusted materialized feature files BEFORE any workdir code runs
    (one ``Scenario:`` per criterion; ``And``/``But`` inherit the previous type)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for path in sorted((Path(workdir) / FEATURE_DIR).glob("*.feature")):
        rel = f"{FEATURE_DIR}/{path.name}"
        steps: list[tuple[str, str]] | None = None
        kind = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            st = line.strip()
            head, sep, name = st.partition(":")
            if sep and head == "Scenario":
                steps = out.setdefault(f"{rel}::{name.strip()}", [])
                kind = ""
                continue
            word, _, text = st.partition(" ")
            if steps is None or not text.strip():
                continue
            if word in _STEP_KEYWORDS:
                kind = _STEP_KEYWORDS[word]
            elif word not in ("And", "But") or not kind:
                continue
            steps.append((kind, text.strip()))
    return out


def expected_scenarios(workdir: Path) -> tuple[str, ...]:
    """Every ``features/<ticket>.feature::<criterion id>`` the materialized feature
    files declare (the trusted side writes them; one ``Scenario:`` per criterion)."""
    return tuple(trusted_scenarios(workdir))


def read_bdd_report(path: Path, nonce: str) -> dict[str, str] | None:
    """The reporter's scenario-ref -> outcome map from the gate-owned report file, or
    ``None`` when it is absent, malformed, or does not carry this run's nonce."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("nonce")), nonce):
        return None
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, dict):
        return None
    return {str(k): str(v) for k, v in scenarios.items()}


_BDD_WHY = {
    "missing": (
        "was not executed — bind that exact gate-owned file with pytest-bdd "
        "`scenarios(\"<relative path from the step module to it>\")` called in a test "
        "module pytest collects, tests/test_<name>.py (a conftest.py or a module not "
        "named test_*.py is never collected as a test module; a copy of the feature or "
        "a .feature file of your own is ignored by the gate), and implement every "
        "Given/When/Then step"
    ),
    "unmatched": (
        "ran steps that do not match the trusted feature file's scenario — do not "
        "edit or regenerate features/*.feature; bind it as written"
    ),
    "catch-all": (
        "passed only through catch-all step definitions (a matcher that accepts any "
        "text) — write a specific step definition per Given/When/Then that exercises "
        "the behavior and asserts its outcome. (The gate can only reject matchers "
        "that accept arbitrary text; it cannot verify that a specific step asserts "
        "anything, so a no-op step still defeats it — that is on review.)"
    ),
    # A local model read the FAILED line's generated name (test_sccart1) as a test it
    # had to write, and its plain function replaced the scenario's test.
    "failed": (
        "failed — its pytest test is the one `scenarios(...)` generates (the "
        "test_<scenario name> in the FAILED line): fix the step definitions or the code "
        "under test, and never write a test function of that name yourself (it replaces "
        "the scenario's test, which then never runs)"
    ),
}


# The Cucumber path's wording where the pytest-bdd advice would mislead.
_CUCUMBER_WHY = {
    **_BDD_WHY,
    "missing": (
        "was not executed or has undefined steps — the bdd command must run the "
        "stack's Cucumber runner over features/ from the repo root, writing Cucumber "
        "Messages to $FOUNDRY_BDD_REPORT, with a step definition for every "
        "Given/When/Then"
    ),
    "unmatched": (
        "ran steps that do not match the trusted feature file's scenario — do not "
        "edit or regenerate features/*.feature; run it as written"
    ),
    "failed": "failed",
}


def _output_tail(
    err: str, out: str, rc: int, lines: int = 15, chars: int = 1500, label: str = "bdd"
) -> str:
    text = "\n".join(t for t in (err.strip(), out.strip()) if t)
    tail = "\n".join(text.splitlines()[-lines:])[-chars:]
    return f"{label} command exited {rc}; its last output:\n{tail or '(none)'}"


_PYTEST_EXIT_WHY = {
    2: "pytest was interrupted",
    3: "pytest hit an internal error",
    4: "pytest usage error — a conftest.py that fails to import, a bad pytest.ini "
       "option or an unknown command-line flag stops the run before any test",
    5: "no test modules collected: add tests/test_<name>.py (pytest collects only "
       "test_*.py modules, never conftest.py) holding the tests",
}


def bdd_findings(
    scenarios: dict[str, str], why: dict[str, str] | None = None
) -> tuple[str, ...]:
    """One finding per criterion whose scenario did not pass."""
    why = why or _BDD_WHY
    out: list[str] = []
    for ref, status in sorted(scenarios.items()):
        if status == "passed":
            continue
        criterion = ref.rsplit("::", 1)[-1]
        out.append(f"BDD criterion {criterion}: scenario {ref} {why.get(status, status)}")
    return tuple(out)


def binding_modules(workdir: Path, features: set[str]) -> tuple[str, ...]:
    """The ``.py`` files under ``workdir`` (outside ``features/``) whose text names
    one of ``features`` by file name — the step modules that bind them."""
    names = {PurePosixPath(f).name for f in features}
    root = Path(workdir)
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((f"{FEATURE_DIR}/", ".")) or "/." in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in text for n in names):
            out.append(rel)
    return tuple(out)


def _unexpanded_report_paths(workdir: Path) -> list[str]:
    """Workdir paths whose name holds ``$FOUNDRY_BDD_REPORT`` verbatim: the bdd
    command passed the variable unexpanded (single-quoted) to its runner."""
    root = Path(workdir)
    marks = (f"${_BDD_REPORT_ENV}", f"${{{_BDD_REPORT_ENV}}}")
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if any(m in p.name for m in marks)
    )


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def conftest_binding_note(workdir: Path) -> str | None:
    """A finding naming the conftest.py files that call ``scenarios(``: pytest
    imports a conftest for fixtures but never collects its tests, so a binding
    there leaves every scenario unexecuted."""
    root = Path(workdir)
    hits: list[str] = []
    for path in sorted(root.rglob("conftest.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        # A real call only: a comment or docstring naming `scenarios(...)` binds nothing.
        if any(isinstance(n, ast.Call) and _called_name(n.func) == "scenarios"
               for n in ast.walk(tree)):
            hits.append(rel)
    if not hits:
        return None
    return (
        f"{', '.join(hits)} call(s) pytest-bdd `scenarios(...)`, but conftest.py is "
        "never collected as a test module, so those scenarios never run: move the "
        "`scenarios(...)` call into a tests/test_<name>.py module (step definitions "
        "and fixtures may stay in conftest.py)"
    )


def bdd_test_name(scenario: str) -> str:
    """The test name pytest-bdd's ``scenarios(...)`` first tries for ``scenario``
    (``pytest_bdd.scenario.get_python_name_generator``)."""
    name = re.sub(r"\W", "", scenario.replace(" ", "_"))
    return "test_" + re.sub(r"^\d+_*", "", name).lower()


def _calls_scenarios(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    f = node.value.func
    return (isinstance(f, ast.Name) and f.id == "scenarios") or (
        isinstance(f, ast.Attribute) and f.attr == "scenarios"
    )


def _is_scenario_decorator(d: ast.expr) -> bool:
    f = d.func if isinstance(d, ast.Call) else d
    return (isinstance(f, ast.Name) and f.id == "scenario") or (
        isinstance(f, ast.Attribute) and f.attr == "scenario"
    )


def _rebound_names(node: ast.stmt) -> set[str]:
    """Module-level names ``node`` binds, other than an explicit ``@scenario`` test."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set() if any(map(_is_scenario_decorator, node.decorator_list)) else {node.name}
    if isinstance(node, ast.ClassDef):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def shadowed_scenario_notes(workdir: Path, refs: list[str]) -> tuple[str, ...]:
    """A finding per bound-but-unexecuted scenario whose generated test a later
    module-level definition replaces: ``scenarios(...)`` puts the scenario's test in
    the module as ``test_<name>``, so a ``def test_<name>`` after the call overwrites
    it and the scenario never runs while pytest stays green."""
    by_feature: dict[str, list[str]] = {}
    for ref in refs:
        feature, _, scenario = ref.partition("::")
        by_feature.setdefault(feature, []).append(scenario)
    out: list[str] = []
    root = Path(workdir)
    for feature, names in sorted(by_feature.items()):
        wanted = {bdd_test_name(n): n for n in names}
        for rel in binding_modules(root, {feature}):
            try:
                tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                continue
            body = tree.body
            start = next((i for i, n in enumerate(body) if _calls_scenarios(n)), None)
            if start is None:
                continue
            hits = sorted({name for n in body[start + 1:] for name in _rebound_names(n)}
                          & set(wanted))
            for name in hits:
                out.append(
                    f"{rel} defines `{name}` after its `scenarios(...)` call, but "
                    f"`{name}` is the test pytest-bdd generates for scenario "
                    f"{feature}::{wanted[name]}: the later definition replaces it, so the "
                    f"scenario never runs — delete or rename `{name}` (the binding and "
                    "the step definitions are what run the scenario; never write its "
                    "test function yourself)"
                )
    return tuple(out)


def _host_source_root() -> str | None:
    """The host source-checkout dir to fence off — the directory holding the
    EDITABLE ``foundry`` package (a sibling of its ``pyproject.toml``). Returns
    ``None`` for a normal site-packages install, where the package shares its dir
    with pytest and stripping it would break the harness (use the sandbox there)."""
    try:
        import foundry

        root = Path(foundry.__file__).resolve().parents[1]
    except Exception:
        return None
    return str(root) if (root / "pyproject.toml").is_file() else None


# pytest's own summary section: the only place its FAILED/ERROR lines are its own.
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
# Lines the fix loop reads as structured (failing ids, BDD outcomes): any copy of
# one outside pytest's summary is test output and is quoted so it never parses.
_ID_SHAPED = re.compile(r"^(?:FAILED|ERROR|BDD criterion)\b")


def failure_findings(stdout: str, *, limit: int = 50, summary_ids: bool = True) -> tuple[str, ...]:
    """Human-readable failure lines from pytest output — the failing tests + the
    assertion (expected vs got) — so the fix loop can target the SPECIFIC failure
    instead of re-rolling blind. Bounded; lines are truncated. (Zone A: pure parse.)

    The ``FAILED``/``ERROR`` summary lines come last but name every failing test, so
    they keep their room under ``limit``; the detail lines fill what is left. Only
    the lines of pytest's LAST short-summary section count as summary lines (a test
    can print a header of its own, never after pytest's): an id-shaped line anywhere
    else — a test's captured output — is quoted (``| ``), so a test cannot choose
    which ids the fix loop sees failing, nor which ticket it blames. Without
    ``summary_ids`` (another runner's output) every id-shaped line is quoted."""
    lines = stdout.splitlines()
    start = max((i for i, raw in enumerate(lines) if _SUMMARY_HEADER.match(raw.strip())),
                default=None) if summary_ids else None
    keep: list[str] = []
    summary: list[str] = []
    for i, raw in enumerate(lines):
        s = raw.rstrip()
        st = s.strip()
        if not st:
            continue
        if start is not None and i > start and st.startswith(("FAILED ", "ERROR ")):
            summary.append(s[:300])
        elif (
            st.startswith(("E ", "E\t", ">", "FAILED", "ERROR", "_____", "BDD criterion"))
            or "assert" in st
            or "Error" in st
            or st.startswith("def test")
        ):
            keep.append(f"| {s}"[:300] if _ID_SHAPED.match(st) else s[:300])
    summary = summary[:limit]
    return (*keep[: limit - len(summary)], *summary)


_FIXTURE_NOT_FOUND = re.compile(r"fixture '([A-Za-z_]\w*)' not found")


_STEP_KINDS = ("given", "when", "then")
_FEATURE_STEP = re.compile(r"^\s*(?:Given|When|Then|And|But|\*)\s+(.+?)\s*$")


@dataclass(frozen=True)
class _StepDef:
    module: str
    kind: str
    text: str
    literal: bool  # matches only its own text (no parse fields, not a regex)
    target: str | None
    line: int
    parser: str = ""  # "" (plain text), "parse", "cfparse", "re", ...
    function: str = ""
    params: tuple[str, ...] = ()  # parameters without a default (pytest fills them)


def _step_def(module: str, d: ast.expr) -> _StepDef | None:
    """The pytest-bdd step a decorator registers, when its text is a string literal
    (plain or ``parsers.parse``)."""
    if not isinstance(d, ast.Call) or not d.args:
        return None
    f = d.func
    kind = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    if kind not in _STEP_KINDS:
        return None
    arg, literal, parser = d.args[0], True, ""
    if isinstance(arg, ast.Call) and arg.args:
        p = arg.func
        name = p.id if isinstance(p, ast.Name) else p.attr if isinstance(p, ast.Attribute) else ""
        literal, parser = name == "parse", name or "?"
        arg = arg.args[0]
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        return None
    target = next((k.value.value for k in d.keywords if k.arg == "target_fixture"
                   and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)),
                  None)
    return _StepDef(module, kind, arg.value, literal and "{" not in arg.value, target,
                    d.lineno, parser)


def _required_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    a = node.args
    positional = [*a.posonlyargs, *a.args]
    required = positional[:len(positional) - len(a.defaults)]
    return (*(p.arg for p in required),
            *(p.arg for p, dflt in zip(a.kwonlyargs, a.kw_defaults, strict=True)
              if dflt is None))


def _parse_module(root: Path, rel: str) -> ast.Module | None:
    try:
        return ast.parse((root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None


def _module_step_defs(rel: str, tree: ast.Module) -> list[_StepDef]:
    out: list[_StepDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = _required_params(node)
            out.extend(replace(s, function=node.name, params=params)
                       for d in node.decorator_list if (s := _step_def(rel, d)))
    return out


def _step_defs(root: Path, modules: Iterable[str]) -> list[_StepDef]:
    out: list[_StepDef] = []
    for rel in modules:
        if (tree := _parse_module(root, rel)) is not None:
            out.extend(_module_step_defs(rel, tree))
    return sorted(out, key=lambda s: (s.module, s.line))


def _feature_steps(root: Path, features: Iterable[str]) -> set[str] | None:
    """Every step line's text in ``features``; ``None`` when one cannot be read or
    holds an outline placeholder (its steps' text is only known per example)."""
    out: set[str] = set()
    for rel in features:
        try:
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        for line in lines:
            m = _FEATURE_STEP.match(line)
            if m is None:
                continue
            if re.search(r"<[^<>]+>", m.group(1)):
                return None
            out.add(m.group(1))
    return out


def _unset_target_note(name: str, defs: list[_StepDef], steps: set[str] | None,
                       features: Sequence[str]) -> str | None:
    """Why the step that names ``target_fixture=name`` never ran: a later definition
    with the same text replaces it, or no feature step line reads its text."""
    setters = [d for d in defs if d.target == name]
    for s in setters:
        same = [d for d in defs if (d.module, d.kind, d.text) == (s.module, s.kind, s.text)]
        if same[-1] is not s:
            targets = ", ".join(f"`{d.target}`" if d.target else "none" for d in same)
            return (
                f"step argument `{name}` is never set: `@{s.kind}(\"{s.text}\")` is defined "
                f"{len(same)} times ({s.module}; target_fixture {targets}) and pytest-bdd "
                "runs only the LAST definition of a step text, so the step that sets "
                f"`{name}` never runs — scenarios that share a step line share one step "
                "definition: define it once, with one target_fixture name, and take that "
                "name as the parameter in every later step that needs the value"
            )
    if steps is None or not setters or not all(s.literal for s in setters):
        return None
    if any(s.text in steps for s in setters):
        return None
    s = setters[0]
    close = get_close_matches(s.text, sorted(steps), n=1, cutoff=0.6)
    shared = next((d.target for d in reversed(defs) if close and d.kind == s.kind
                   and d.text == close[0] and d.module == s.module and d.target), None)
    return (
        f"step argument `{name}` is never set: only `@{s.kind}(\"{s.text}\")` in "
        f"{s.module} sets it (target_fixture=\"{name}\"), and no step line in "
        f"{', '.join(features)} reads \"{s.text}\", so that step definition never runs — "
        "a step definition's text must equal the feature's step line exactly (the "
        ".feature file is gate-owned: change the step definition, never the feature)"
        + (f"; the nearest step line is \"{close[0]}\"" if close else "")
        + (f", whose step definition sets `{shared}`: scenarios that share a step line "
           f"share its one step definition, so take `{shared}` as the parameter instead"
           if shared else "")
    )


def step_argument_hints(
    stdout: str, workdir: Path | None = None, features: Iterable[str] = ()
) -> tuple[str, ...]:
    """One line per fixture a pytest-bdd run could not find: almost always a step
    function parameter that no step text, ``target_fixture`` or fixture provides.
    With the ``workdir`` and the bound ``features``, a parameter some step's
    ``target_fixture`` names is explained by why that step never ran."""
    names = dict.fromkeys(_FIXTURE_NOT_FOUND.findall(stdout))
    feats = sorted(features)
    defs: list[_StepDef] = []
    steps: set[str] | None = None
    if names and workdir is not None and feats:
        root = Path(workdir)
        defs = _step_defs(root, binding_modules(root, set(feats)))
        steps = _feature_steps(root, feats)
    return tuple(
        _unset_target_note(name, defs, steps, feats) or (
            f"step argument `{name}` is not provided (fixture '{name}' not found): "
            "pytest-bdd fills a step function's parameter only from the step text via "
            f"`parsers.parse(\"... {{{name}}} ...\")`, from `target_fixture=\"{name}\"` on "
            f"an earlier step (`@given(\"...\", target_fixture=\"{name}\")` whose function "
            f"returns the value), or from a pytest fixture named `{name}`; a step's plain "
            "`return` passes nothing on"
        )
        for name in names
    )


_DUPLICATE_NOTES_MAX = 8


def duplicate_step_notes(workdir: Path, features: Iterable[str]) -> tuple[str, ...]:
    """One line per step text a step module that binds ``features`` defines more than
    once with the same keyword. pytest-bdd runs only the last of them for every
    scenario whose step line reads that text, so a scenario written against an
    earlier one fails on what the last one does or leaves unset (the local shop run:
    two scenarios' ``@then("the user dictionary is returned")``, and one read
    ``context.login_result`` that only the other scenario's When step set)."""
    feats = set(features)
    if not feats:
        return ()
    root = Path(workdir)
    return _duplicate_notes(_step_defs(root, binding_modules(root, feats)))


def _duplicate_notes(defs: Iterable[_StepDef]) -> tuple[str, ...]:
    groups: dict[tuple[str, str, str], list[_StepDef]] = {}
    for d in defs:
        groups.setdefault((d.module, d.kind, d.text), []).append(d)
    out = [
        f"`@{kind}(\"{text}\")` is defined {len(same)} times in {module} (lines "
        f"{', '.join(str(d.line) for d in same)}): pytest-bdd runs only the LAST "
        "definition of a step text, for every scenario whose step line reads it, so "
        "the earlier ones never run and a scenario written against one of them gets "
        "the last one's behaviour (an attribute it never set, an exception it does "
        "not catch) — define each step text once, in a form right for every scenario "
        "that uses the line"
        for (module, kind, text), same in groups.items() if len(same) > 1
    ]
    return tuple(out[:_DUPLICATE_NOTES_MAX])


# Fixtures pytest (and pytest-bdd) provide without a definition in the project.
_BUILTIN_FIXTURES = frozenset({
    "request", "pytestconfig", "cache", "capsys", "capsysbinary", "capfd", "capfdbinary",
    "caplog", "monkeypatch", "recwarn", "tmp_path", "tmp_path_factory", "tmpdir",
    "tmpdir_factory", "record_property", "record_xml_attribute",
    "record_testsuite_property", "doctest_namespace", "pytester", "testdir",
    "datatable", "docstring", "mocker",
})
_FEATURE_NAME = re.compile(r"[\w.-]+\.feature\b")


def _module_features(root: Path, source: str) -> dict[str, str]:
    """``features/<name>`` -> text, for each feature file under ``root`` the module
    source names."""
    out: dict[str, str] = {}
    for name in dict.fromkeys(_FEATURE_NAME.findall(source)):
        path = root / FEATURE_DIR / name
        try:
            out[f"{FEATURE_DIR}/{name}"] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _fixture_names(tree: ast.Module) -> tuple[set[str], bool]:
    """(the fixtures a module defines or imports, whether it star-imports)."""
    names: set[str] = set()
    star = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                call = d.func if isinstance(d, ast.Call) else d
                if (call.id if isinstance(call, ast.Name)
                        else call.attr if isinstance(call, ast.Attribute) else "") != "fixture":
                    continue
                alias = next((k.value.value for k in getattr(d, "keywords", ())
                              if k.arg == "name" and isinstance(k.value, ast.Constant)
                              and isinstance(k.value.value, str)), None)
                names.add(alias or node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                star = star or a.name == "*"
                names.add((a.asname or a.name).split(".")[0])
    return names, star


def _unmatched_notes(defs: Iterable[_StepDef], features: Mapping[str, str]) -> list[str]:
    lines: dict[str, set[str]] = {}
    for text in features.values():
        for sc in parse_feature(text):
            for steps in sc.rendered():
                for s in steps:
                    lines.setdefault(s.kind, set()).add(s.text)
    out: list[str] = []
    for d in defs:
        candidates = lines.get(d.kind, set())
        verdicts = [step_matches(d.parser, d.text, line) for line in candidates]
        if any(v is None for v in verdicts) or any(verdicts):
            continue
        close = get_close_matches(d.text, sorted(candidates), n=1, cutoff=0.6)
        out.append(
            f"`@{d.kind}(\"{d.text}\")` ({d.module}:{d.line}) matches no {d.kind.title()} "
            f"step line of {', '.join(features)}, so it never runs — a step definition's "
            "text must equal the feature's step line exactly (a `parsers.parse` field "
            "stands for the varying part; the .feature file is gate-owned: change the "
            "step definition, never the feature)"
            + (f"; the nearest {d.kind.title()} line is \"{close[0]}\"" if close else "")
        )
    return out


def _unprovided_notes(defs: Sequence[_StepDef], provided: set[str]) -> list[str]:
    targets = {d.target for d in defs if d.target}
    out: list[str] = []
    for d in defs:
        own = set(parse_fields(d.text)) if d.parser in ("parse", "cfparse") else (
            set(re.findall(r"\(\?P<(\w+)>", d.text)) if d.parser == "re" else set())
        if d.parser not in ("", "parse", "cfparse", "re"):
            continue
        for p in d.params:
            if p in own or p in targets or p in provided or p in _BUILTIN_FIXTURES:
                continue
            out.append(
                f"step argument `{p}` of {d.function} (`@{d.kind}(\"{d.text}\")`, "
                f"{d.module}:{d.line}) is provided by nothing: it is not a `{{{p}}}` field "
                "of the step text, no step sets `target_fixture=\"" + p + "\"`, and no "
                f"fixture named `{p}` exists — pytest-bdd fails every scenario that runs "
                "this step; a step's plain `return` passes nothing on"
            )
    return out


def _conftest_fixtures(root: Path) -> tuple[set[str], list[_StepDef]]:
    names: set[str] = set()
    defs: list[_StepDef] = []
    for path in sorted(root.rglob("conftest.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".") or "/." in rel or (tree := _parse_module(root, rel)) is None:
            continue
        names |= _fixture_names(tree)[0]
        defs.extend(_module_step_defs(rel, tree))
    return names, defs


def step_module_notes(
    workdir: Path, module: str, *, params: bool = True,
    conftest: tuple[set[str], list[_StepDef]] | None = None,
) -> tuple[str, ...]:
    """Static (AST-only, nothing runs) findings on one pytest-bdd step module under
    ``workdir``: a step text it defines twice, a step whose text matches no step line
    of the feature files it names, and (with ``params``) a step parameter that is
    neither a field of its step text, a fixture, nor any step's ``target_fixture``
    (any step of the module or a conftest.py: scenario order is not checked). The
    hook for an agent loop to call after it writes a step module."""
    root = Path(workdir)
    tree = _parse_module(root, module)
    if tree is None:
        return ()
    try:
        features = _module_features(root, (root / module).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ()
    defs = sorted(_module_step_defs(module, tree), key=lambda s: s.line)
    out = [*_duplicate_notes(defs), *(_unmatched_notes(defs, features) if features else ())]
    if params:
        fixtures, star = _fixture_names(tree)
        names, shared = conftest if conftest is not None else _conftest_fixtures(root)
        if not star:
            out += _unprovided_notes(
                defs, fixtures | names | {d.target for d in shared if d.target})
    return tuple(out)


def static_step_notes(
    workdir: Path, features: Iterable[str], *, params: bool = True
) -> tuple[str, ...]:
    """:func:`step_module_notes` over every step module that binds ``features``."""
    feats = set(features)
    if not feats:
        return ()
    root = Path(workdir)
    shared = _conftest_fixtures(root) if params else None
    out = [n for m in binding_modules(root, feats)
           for n in step_module_notes(root, m, params=params, conftest=shared)]
    return tuple(out[:_DUPLICATE_NOTES_MAX * 2])


def parse_pytest_summary(stdout: str) -> dict[str, int]:
    """Counts from the pytest tail line (raw observation, zone A side)."""
    counts: dict[str, int] = {}
    for n, kind in _SUMMARY_RE.findall(stdout):
        counts[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    return counts


def pinned_test_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Pinned-harness child env: no agent plugin autoload, no opt injection."""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)  # invocation is pinned, not ambient
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # no agent-supplied plugins (gov §1c)
    if extra:
        env.update(extra)
    return env


class TestGate:
    """PASS iff the pinned harness is green and the adequacy floor is met."""

    name = "test"
    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True

    def __init__(
        self,
        cmd: tuple[str, ...] | None = None,
        *,
        min_passed: int = 1,
        mutant_rotation_id: int = 0,
        discriminator: TestSuiteDiscriminator | None = None,
        timeout_s: float = 600.0,
        seal_key: bytes | None = None,
        bdd_cmd: str | None = None,
        stack_harness: bool = False,
        profile_name: str = "",
        scope: frozenset[str] | None = None,
        pyenv: PythonBuildEnv | None = None,
    ) -> None:
        # Default: the ISOLATED pinned harness (fences the host source off sys.path
        # so generated modules can't collide with / reach ours). An explicit cmd is
        # honored verbatim (tests inject one). With a planned Cucumber ``bdd_cmd``
        # the scenarios run through it; otherwise only the default harness executes
        # them (a profile-pinned foreign harness can't run pytest-bdd).
        self.bdd_cmd = bdd_cmd or None
        self._runs_bdd = cmd is None and self.bdd_cmd is None
        self.cmd = cmd or (
            sys.executable, "-c", _ISOLATED_PYTEST_BOOTSTRAP, "-q", "--tb=short"
        )
        self.min_passed = min_passed
        # The held-out-mutant + behavioral-invariance discriminator: a green
        # pytest run is NECESSARY but NOT SUFFICIENT — a vacuous/gamed-green
        # suite that survives a held-out mutant or violates an invariant is
        # FAILed here even though pytest is green (gov §1c, the malicious-test
        # defense). None → no discriminator wired (legacy: green suffices).
        self._discriminator = discriminator
        # ``mutant_rotation_id`` rotates the held-out family the discriminator
        # draws from; mirror it from the discriminator when one is wired so the
        # reuse key and the substitution family agree.
        self.mutant_rotation_id = (
            discriminator.mutant_rotation_id if discriminator is not None else mutant_rotation_id
        )
        # A stack command is judged by its exit code alone, never pytest's codes.
        self.stack_harness = stack_harness
        self.profile_name = profile_name
        # Scenario refs a per-wave check judges (``None`` = the full gate): only
        # these scenarios run and decide the verdict; other tests are not judged.
        self.scope = scope
        # The command digest: a cached verdict never outlives a change of commands.
        self.scanner_rule_version = "test-harness-v3:" + commands_digest(
            cmd=list(self.cmd), bdd_cmd=self.bdd_cmd, stack_harness=stack_harness,
            profile=profile_name, scope=sorted(scope) if scope is not None else None,
        )
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)
        # A Python build's declared deps, installed by BuildGate into their own venv.
        self._pyenv = pyenv

    def _env(self, workdir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        # The gate-owned env, added on top of the sandbox's host-env allowlist: tell
        # the isolating bootstrap where to run (the workdir) and which host paths to
        # fence off (the source checkout root); absent deny ⇒ nothing stripped.
        env: dict[str, str] = {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",  # no agent-supplied plugins (gov §1c)
            "FOUNDRY_GATE_WORKDIR": str(Path(workdir).resolve()),
        }
        root = _host_source_root()
        if root:
            env[_DENY_PATHS_ENV] = root
        if self._pyenv is not None:
            env.update(self._pyenv.run_env())
        return {**env, **(extra or {})}

    # ---- zone A: pinned harness execution, raw rc + summary ---------------

    def _ground(
        self,
        workdir: Path,
        cmd: tuple[str, ...],
        extra_env: dict[str, str] | None = None,
        binds: tuple[tuple[Path, bool], ...] = (),
    ) -> tuple[int, str, str]:
        """Run the pinned harness over ``workdir`` in the same confined sandbox build and
        smoke use (``sandbox.ground_untrusted``: offline with loopback up, read-only
        fence except the workdir and the gate-owned report ``binds``, allowlisted env);
        ``FOUNDRY_GATE_SANDBOX=inprocess`` runs it on the host with only the sys.path
        fence and the env allowlist. Either way generated code can't import or shadow
        ours."""
        from .sandbox import ground_untrusted

        if self._pyenv is not None:
            binds = (*binds, *self._pyenv.binds)
        return ground_untrusted(
            cmd, workdir, extra_env=self._env(workdir, extra_env), binds=binds,
            timeout_s=self._timeout_s,
        )

    def _ground_bdd(
        self, workdir: Path, cmd: tuple[str, ...], trusted: dict[str, list[tuple[str, str]]]
    ) -> tuple[int, str, str, dict[str, str] | None]:
        """Run the BDD harness with a gate-owned report channel outside the workdir;
        returns the authenticated report (``None`` if absent/forged)."""
        with tempfile.TemporaryDirectory(prefix="foundry-bdd-") as tmp:
            nonce = secrets.token_hex(16)
            report = Path(tmp) / f"report-{secrets.token_hex(16)}.json"
            expected = Path(tmp) / "expected.json"
            expected.write_text(json.dumps(trusted), encoding="utf-8")
            extra = {
                _BDD_REPORT_ENV: str(report),
                _BDD_NONCE_ENV: nonce,
                _BDD_EXPECTED_ENV: str(expected),
            }
            # The reporter creates the report (O_EXCL), so its private dir is writable.
            binds = ((Path(tmp), True), (expected, False))
            rc, out, err = self._ground(workdir, cmd, extra, binds)
            return rc, out, err, read_bdd_report(report, nonce)

    def _cucumber_scenarios(
        self,
        report: Path,
        digest: cucumber.ReportDigest | None,
        trusted: dict[str, list[tuple[str, str]]],
        workdir: Path,
        run: tuple[int, str, str],
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Outcomes from the pinned Cucumber report; an absent, tampered or
        unparseable one marks every trusted scenario "missing". ``run`` is the bdd
        command's (exit code, stdout, stderr)."""
        try:
            envelopes = cucumber.read_verified_report(report, digest, workdir)
        except cucumber.ReportError as exc:
            from .profiles import bdd_runner_hint

            hint = bdd_runner_hint(self.profile_name.removeprefix("stack:")) or (
                "e.g. cucumber-js --format \"message:$FOUNDRY_BDD_REPORT\""
            )
            rc, out, err = run
            notes = [
                f"BDD report rejected: {exc} — every scenario is marked missing. The "
                "bdd command must run the stack's Cucumber runner writing its report "
                f"to $FOUNDRY_BDD_REPORT: {hint}",
                f"bdd command `{self.bdd_cmd}` — {_output_tail(err, out, rc)}",
            ]
            literal = _unexpanded_report_paths(workdir)
            if literal:
                notes.append(
                    f"the bdd command wrote its report to {', '.join(literal)} — a path "
                    f"literally named after ${_BDD_REPORT_ENV}: the variable was not "
                    "expanded, most likely because it sits in single quotes; quote it "
                    f"with double quotes (\"${_BDD_REPORT_ENV}\") so the shell expands it"
                )
            return {ref: "missing" for ref in sorted(trusted)}, tuple(notes)
        return cucumber.scenario_outcomes(envelopes, trusted, workdir)

    def execute(self, workdir: Path) -> ZoneAObservation:
        trusted = trusted_scenarios(workdir) if self._runs_bdd or self.bdd_cmd else {}
        if self.scope is not None:
            trusted = {k: v for k, v in trusted.items() if k in self.scope}
        if trusted and self.bdd_cmd:
            with tempfile.TemporaryDirectory(prefix="foundry-bdd-") as tmp:
                return self._execute(workdir, trusted, Path(tmp))
        return self._execute(workdir, trusted, None)

    def _execute(
        self, workdir: Path, trusted: dict[str, list[tuple[str, str]]], bdd_dir: Path | None
    ) -> ZoneAObservation:
        cmd = self.cmd
        executed: dict[str, str] | None = None
        if self.scope is not None and not trusted:
            # No trusted feature holds a scoped scenario: nothing runs, all missing.
            rc, out, err, executed = 0, "", "", {}
            trusted = {name: [] for name in self.scope}
        elif bdd_dir is not None and self.bdd_cmd:
            # The Cucumber run goes first and its report is pinned (size + sha256)
            # the moment it exits, before any other agent code in this gate runs.
            report_path = cucumber.create_report_file(bdd_dir)
            bdd_rc, bdd_out, bdd_err = self._ground(
                workdir,
                ("/bin/sh", "-c", self.bdd_cmd),
                {_BDD_REPORT_ENV: str(report_path)},
                ((report_path, True),),
            )
            digest = cucumber.record_report(report_path)
            # A scoped check judges the scenarios alone: skip the stack's test run.
            rc, out, err = (0, "", "") if self.scope is not None else self._ground(workdir, cmd)
        elif trusted and self.scope is not None:
            # Only the step modules that bind a scoped feature file run, and one
            # module that fails to import does not hide the others' scenarios.
            targets = binding_modules(workdir, {r.split("::", 1)[0] for r in trusted})
            cmd = (sys.executable, "-c", _ISOLATED_PYTEST_BDD_BOOTSTRAP, "-q", "--tb=short",
                   "--continue-on-collection-errors", *targets)
            if targets:
                rc, out, err, executed = self._ground_bdd(workdir, cmd, trusted)
            else:
                rc, out, err, executed = 0, "", "", {}
        elif trusted:
            cmd = (sys.executable, "-c", _ISOLATED_PYTEST_BDD_BOOTSTRAP, *self.cmd[3:])
            rc, out, err, executed = self._ground_bdd(workdir, cmd, trusted)
        else:
            rc, out, err = self._ground(workdir, cmd)
        structured: dict[str, object] = {
            "cmd": list(cmd),
            "summary": parse_pytest_summary(out),
        }
        findings: tuple[str, ...] = ()
        # A red run carries its WHY: the failing tests + assertions, parsed off the
        # pinned harness's own stdout (zone A) so the fix loop feeds the SPECIFIC
        # failure back to the engineer (zone B still decides the verdict on rc alone).
        if rc != 0:
            from .pydeps import undeclared_import_hints

            bound = {r.split("::", 1)[0] for r in trusted}
            findings = (*undeclared_import_hints(out + err, workdir),
                        *(step_argument_hints(out, workdir, bound)
                          if trusted and not self.bdd_cmd else ()),
                        *(static_step_notes(workdir, bound, params=False)
                          if trusted and not self.bdd_cmd else ()),
                        *failure_findings(out))
            why = None if self.stack_harness else _PYTEST_EXIT_WHY.get(rc)
            if why is not None and rc == _NO_TESTS_COLLECTED and trusted:
                why += " and binding the feature files with pytest-bdd `scenarios(...)`"
            if why is not None or not findings:
                # Usage errors (exit 4) print only to stderr, which failure_findings
                # never sees: without the tail the gate ERRORs with no why at all.
                tail = _output_tail(err, out, rc, label="test")
                findings = (f"{why}\n{tail}" if why else tail, *findings)
        if self.stack_harness and rc in MISSING_BINARY_EXIT_CODES:
            findings = (
                f"test command exited {rc}: command not found or not executable "
                "(missing toolchain) — infra ERROR, not a verdict",
                *findings,
            )
        # The discriminator runs in zone A (no signing key in scope) ONLY when
        # the agent suite is actually green — a red/no-tests run is already a
        # FAIL/ERROR by exit code, so there is nothing to re-verify. The report
        # rides the sealed observation; zone B decides over it.
        if self._discriminator is not None and rc == 0:
            report = self._discriminator.evaluate(
                workdir, site=self._pyenv.site if self._pyenv is not None else None,
                binds=self._pyenv.binds if self._pyenv is not None else (),
            )
            structured[DISCRIMINATOR_KEY] = report.model_dump(mode="json")
            findings = report.findings
        if bdd_dir is not None and self.bdd_cmd:
            scenarios, notes = self._cucumber_scenarios(
                report_path, digest, trusted, workdir, (bdd_rc, bdd_out, bdd_err)
            )
            structured[BDD_KEY] = scenarios
            structured[BDD_EXIT_KEY] = bdd_rc
            structured["bdd_cmd"] = self.bdd_cmd
            bdd_failures = (failure_findings(bdd_out, limit=20, summary_ids=False)
                            if bdd_rc != 0 else ())
            rejected = any(n.startswith("BDD report rejected") for n in notes)
            if bdd_rc != 0 and not bdd_failures and not rejected:
                # Not pytest-shaped output: the runner's own last words are the why
                # (a step-definition load error leaves the report empty).
                bdd_failures = (_output_tail(bdd_err, bdd_out, bdd_rc),)
            if bdd_rc in MISSING_BINARY_EXIT_CODES:
                bdd_failures = (
                    f"bdd command exited {bdd_rc}: command not found or not executable "
                    "(missing toolchain) — infra ERROR, not a verdict",
                    *bdd_failures,
                )
            findings = (
                *notes,
                *bdd_findings(scenarios, _CUCUMBER_WHY),
                *bdd_failures,
                *findings,
                cucumber.CUCUMBER_RESIDUAL,
            )
            out = f"{out}\n--- bdd: {self.bdd_cmd} (exit {bdd_rc}) ---\n{bdd_out}"
            err = f"{err}\n--- bdd ---\n{bdd_err}"
        elif trusted:
            # Each criterion's scenario outcome, keyed by scenario_ref; a scenario the
            # run never executed (unbound feature, collection miss) is "missing".
            got = executed or {}
            scenarios = {ref: got.get(ref, "missing") for ref in sorted(trusted)}
            structured[BDD_KEY] = scenarios
            missing = [ref for ref, o in scenarios.items() if o == "missing"]
            findings = (*shadowed_scenario_notes(workdir, missing),
                        *bdd_findings(scenarios), *findings)
            note = conftest_binding_note(workdir) if missing else None
            if note:
                findings = (note, *findings)
            if executed is None:
                findings = (
                    "BDD report absent or not authentic — the harness did not finish "
                    "normally (a test exited the process or tampered with the run)",
                    *findings,
                )
        return observe(
            self.name,
            exit_codes=(rc,),
            stdout=out,
            stderr=err,
            findings=findings,
            structured=structured,
            seal_key=self._seal_key,
        )

    # ---- zone B: pure decision over rc + counts ----------------------------

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        if len(obs.exit_codes) != 1:
            return GateStatus.ERROR
        rc = obs.exit_codes[0]
        if obs.structured.get(BDD_EXIT_KEY) in MISSING_BINARY_EXIT_CODES:
            return GateStatus.ERROR  # the Cucumber runner's binary is missing
        if self.scope is not None:
            # A per-wave check: the scoped scenarios alone decide (the rest of the
            # suite may need tickets not built yet; the final gate runs it all).
            scoped = obs.structured.get(BDD_KEY) or {}
            return (GateStatus.PASS if scoped and all(v == "passed" for v in scoped.values())
                    else GateStatus.FAIL)
        if self.stack_harness:
            if rc in MISSING_BINARY_EXIT_CODES:
                return GateStatus.ERROR  # missing toolchain: infra, never latched
            if rc != 0:
                return GateStatus.FAIL
        elif rc in _INFRA_EXIT_CODES:
            return GateStatus.ERROR  # harness failure is not a verdict (DS7)
        if rc == _NO_TESTS_COLLECTED:
            # Adequacy floor: an empty suite cannot clear the gate — nor can one
            # that leaves acceptance scenarios unexecuted.
            if self.min_passed > 0 or obs.structured.get(BDD_KEY):
                return GateStatus.FAIL
            return GateStatus.PASS
        if rc != 0:
            return GateStatus.FAIL
        passed = obs.structured.get("summary", {}).get("passed", 0)
        if passed < self.min_passed:
            return GateStatus.FAIL  # adequacy floor: a vacuous green run cannot pass
        # Green + adequate is NECESSARY but NOT SUFFICIENT: a gamed-green suite
        # that survives a held-out mutant or violates a behavioral invariant is
        # FAILed here (gov §1c). A surviving mutant means the suite tested
        # nothing real; the discriminator report rode the sealed observation.
        report = obs.structured.get(DISCRIMINATOR_KEY)
        if report is not None and not report.get("passed", False):
            return GateStatus.FAIL
        # Executable acceptance: every criterion's scenario must have run and passed,
        # and a planned Cucumber run must itself have been green.
        if obs.structured.get(BDD_EXIT_KEY, 0) != 0:
            return GateStatus.FAIL
        scenarios = obs.structured.get(BDD_KEY)
        if scenarios is not None and any(v != "passed" for v in scenarios.values()):
            return GateStatus.FAIL
        return GateStatus.PASS
