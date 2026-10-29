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

import hmac
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.schemas.implementation import BDD_REPORT_ENV, FEATURE_DIR

from . import cucumber
from .build import MISSING_BINARY_EXIT_CODES, commands_digest
from .runner import ZoneAObservation, observe

if TYPE_CHECKING:
    from .discriminator import TestSuiteDiscriminator

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
_ISOLATED_PYTEST_BOOTSTRAP = _BOOTSTRAP_PREAMBLE + """\
from pytest import console_main
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
sys.path.insert(0, wd)


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
        "`scenarios(\"<relative path from the step module to it>\")` (a copy of it or a "
        ".feature file of your own is ignored by the gate) and implement every "
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


def failure_findings(stdout: str, *, limit: int = 50) -> tuple[str, ...]:
    """Human-readable failure lines from pytest output — the failing tests + the
    assertion (expected vs got) — so the fix loop can target the SPECIFIC failure
    instead of re-rolling blind. Bounded; lines are truncated. (Zone A: pure parse.)"""
    keep: list[str] = []
    for raw in stdout.splitlines():
        s = raw.rstrip()
        st = s.strip()
        if not st:
            continue
        if (
            st.startswith(("E ", "E\t", ">", "FAILED", "ERROR", "_____"))
            or "assert" in st
            or "Error" in st
            or st.startswith("def test")
        ):
            keep.append(s[:300])
        if len(keep) >= limit:
            break
    return tuple(keep)


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
        # Scenario refs a per-wave check judges (``None`` = the full gate): only
        # these scenarios run and decide the verdict; other tests are not judged.
        self.scope = scope
        # The command digest: a cached verdict never outlives a change of commands.
        self.scanner_rule_version = "test-harness-v2:" + commands_digest(
            cmd=list(self.cmd), bdd_cmd=self.bdd_cmd, stack_harness=stack_harness,
            profile=profile_name, scope=sorted(scope) if scope is not None else None,
        )
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)

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

    @staticmethod
    def _cucumber_scenarios(
        report: Path,
        digest: cucumber.ReportDigest | None,
        trusted: dict[str, list[tuple[str, str]]],
        workdir: Path,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        """Outcomes from the pinned Cucumber Messages report; an absent, tampered or
        unparseable one marks every trusted scenario "missing"."""
        try:
            envelopes = cucumber.read_verified_report(report, digest)
        except cucumber.ReportError as exc:
            note = (
                f"BDD report rejected: {exc} — every scenario is marked missing. The "
                "bdd command must run the stack's Cucumber runner writing Cucumber "
                "Messages NDJSON to $FOUNDRY_BDD_REPORT (e.g. cucumber-js "
                '--format "message:$FOUNDRY_BDD_REPORT")'
            )
            return {ref: "missing" for ref in sorted(trusted)}, (note,)
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
            trusted = dict.fromkeys(self.scope, [])
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
            findings = failure_findings(out)
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
            report = self._discriminator.evaluate(workdir)
            structured[DISCRIMINATOR_KEY] = report.model_dump(mode="json")
            findings = report.findings
        if bdd_dir is not None and self.bdd_cmd:
            scenarios, notes = self._cucumber_scenarios(report_path, digest, trusted, workdir)
            structured[BDD_KEY] = scenarios
            structured[BDD_EXIT_KEY] = bdd_rc
            structured["bdd_cmd"] = self.bdd_cmd
            bdd_failures = failure_findings(bdd_out, limit=20) if bdd_rc != 0 else ()
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
            findings = bdd_findings(scenarios) + findings
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
