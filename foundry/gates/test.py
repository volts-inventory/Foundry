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
when a floor is set).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.schemas.gates import GateStatus, VerdictClass
from foundry.schemas.implementation import FEATURE_DIR

from .runner import ZoneAObservation, observe, run_grounding_command

if TYPE_CHECKING:
    from .discriminator import TestSuiteDiscriminator

__all__ = ["TestGate", "parse_pytest_summary"]

# Structured-observation key carrying the discriminator's report (zone A → B).
DISCRIMINATOR_KEY = "discriminator"
# Structured-observation key carrying {scenario_ref: outcome} for every expected
# BDD scenario (outcome ∈ passed / failed / skipped / missing).
BDD_KEY = "bdd"

_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")

# pytest exit codes that are harness/usage failures, not verdicts.
_INFRA_EXIT_CODES = frozenset({2, 3, 4})
_NO_TESTS_COLLECTED = 5

# Env key naming the host paths to FENCE OFF from the pinned-harness subprocess.
_DENY_PATHS_ENV = "FOUNDRY_GATE_DENY_PATHS"

# The pinned pytest invocation, run as a bootstrap that ISOLATES generated code from
# ours: it cd's to the workdir, removes the host source checkout (``_DENY_PATHS_ENV``)
# from ``sys.path`` and puts the workdir first BEFORE importing pytest — so a model-
# named module that collides with a host package (e.g. ``foundry.x``) resolves to the
# WORKDIR and can never reach or be shadowed by our code — then caps cpu/file/proc
# resources. Run on its own (in-process fence) OR inside the namespace sandbox.
_BOOTSTRAP_PREAMBLE = """\
import os, sys, resource
wd = os.environ.get("FOUNDRY_GATE_WORKDIR") or os.getcwd()
os.chdir(wd)
_raw = os.environ.get("FOUNDRY_GATE_DENY_PATHS", "").split(os.pathsep)
deny = {os.path.realpath(p) for p in _raw if p}
sys.path[:] = [p for p in sys.path if os.path.realpath(p) not in deny]
sys.path.insert(0, wd)
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
sys.exit(console_main())
"""

# Executed scenarios are reported on one stdout line, keyed by the feature file's
# workdir-relative path + scenario name — i.e. by scenario_ref(ticket, criterion).
BDD_REPORT_MARKER = "FOUNDRY-BDD-REPORT "

# Same isolation, plus pytest-bdd loaded explicitly (plugin autoload stays off) and a
# trusted in-bootstrap reporter that records each executed scenario's outcome.
_ISOLATED_PYTEST_BDD_BOOTSTRAP = _BOOTSTRAP_PREAMBLE + f"""\
import json, pytest


class _BddReport:
    def __init__(self):
        self.ref, self.out = {{}}, {{}}

    @pytest.hookimpl(optionalhook=True)
    def pytest_bdd_before_scenario(self, request, feature, scenario):
        rel = os.path.relpath(feature.filename, wd).replace(os.sep, "/")
        self.ref[request.node.nodeid] = rel + "::" + scenario.name

    def pytest_runtest_logreport(self, report):
        ref = self.ref.get(report.nodeid)
        if ref is None or self.out.get(ref) == "failed":
            return
        if report.failed:
            self.out[ref] = "failed"
        elif report.when == "call":
            self.out[ref] = "passed" if report.passed else "skipped"

    def pytest_unconfigure(self, config):
        sys.stdout.write("\\n{BDD_REPORT_MARKER}" + json.dumps(self.out, sort_keys=True) + "\\n")
        sys.stdout.flush()


sys.exit(pytest.main(sys.argv[1:], plugins=[_BddReport()]))
"""

_BDD_PLUGIN_ARGS = ("-p", "pytest_bdd.plugin")


def expected_scenarios(workdir: Path) -> tuple[str, ...]:
    """Every ``features/<ticket>.feature::<criterion id>`` the materialized feature
    files declare (the trusted side writes them; one ``Scenario:`` per criterion)."""
    refs: list[str] = []
    for path in sorted((Path(workdir) / FEATURE_DIR).glob("*.feature")):
        rel = f"{FEATURE_DIR}/{path.name}"
        for line in path.read_text(encoding="utf-8").splitlines():
            head, sep, name = line.strip().partition(":")
            if sep and head == "Scenario":
                refs.append(f"{rel}::{name.strip()}")
    return tuple(refs)


def parse_bdd_report(stdout: str) -> dict[str, str]:
    """The reporter's scenario-ref -> outcome map (last marker line wins; none ⇒ {})."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(BDD_REPORT_MARKER):
            try:
                data = json.loads(line[len(BDD_REPORT_MARKER):])
            except json.JSONDecodeError:
                return {}
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return {}


def bdd_findings(scenarios: dict[str, str]) -> tuple[str, ...]:
    """One finding per criterion whose scenario did not pass."""
    out: list[str] = []
    for ref, status in sorted(scenarios.items()):
        if status == "passed":
            continue
        criterion = ref.rsplit("::", 1)[-1]
        if status == "missing":
            why = (
                "was not executed — bind the feature with pytest-bdd `scenarios(...)` "
                "and implement every Given/When/Then step"
            )
        else:
            why = status
        out.append(f"BDD criterion {criterion}: scenario {ref} {why}")
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
    ) -> None:
        # Default: the ISOLATED pinned harness (fences the host source off sys.path
        # so generated modules can't collide with / reach ours). An explicit cmd is
        # honored verbatim (tests inject one). Only the default harness executes BDD
        # scenarios (a profile-pinned foreign harness can't run pytest-bdd).
        self._runs_bdd = cmd is None
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
        self.scanner_rule_version = "pytest-harness-v1"
        self._timeout_s = timeout_s
        self._seal_key = seal_key  # zone-A observation seal (None ⇒ unsealed channel)

    def _env(self, workdir: Path) -> dict[str, str]:
        # Tell the isolating bootstrap where to run (the workdir) and which host paths
        # to fence off (the source checkout root); absent deny ⇒ nothing stripped.
        extra: dict[str, str] = {"FOUNDRY_GATE_WORKDIR": str(Path(workdir).resolve())}
        root = _host_source_root()
        if root:
            extra[_DENY_PATHS_ENV] = root
        return pinned_test_env(extra)

    # ---- zone A: pinned harness execution, raw rc + summary ---------------

    def _ground(self, workdir: Path, cmd: tuple[str, ...]) -> tuple[int, str, str]:
        """Run the pinned harness over ``workdir``. By default the isolated pytest runs
        inside the namespace sandbox (no network, isolated mount/pid/ipc/uts — see
        ``sandbox.py``); ``FOUNDRY_GATE_SANDBOX=inprocess`` runs it in-process with only
        the sys.path fence. Either way generated code can't import or shadow ours."""
        from .sandbox import run_in_sandbox, sandbox_enabled

        env = self._env(workdir)
        if sandbox_enabled():
            return run_in_sandbox(cmd, workdir, env=env, timeout_s=self._timeout_s)
        return run_grounding_command(cmd, workdir, env=env, timeout_s=self._timeout_s)

    def execute(self, workdir: Path) -> ZoneAObservation:
        expected = expected_scenarios(workdir) if self._runs_bdd else ()
        cmd = self.cmd
        if expected:
            cmd = (
                sys.executable, "-c", _ISOLATED_PYTEST_BDD_BOOTSTRAP, *self.cmd[3:],
                *_BDD_PLUGIN_ARGS,
            )
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
        # The discriminator runs in zone A (no signing key in scope) ONLY when
        # the agent suite is actually green — a red/no-tests run is already a
        # FAIL/ERROR by exit code, so there is nothing to re-verify. The report
        # rides the sealed observation; zone B decides over it.
        if self._discriminator is not None and rc == 0:
            report = self._discriminator.evaluate(workdir)
            structured[DISCRIMINATOR_KEY] = report.model_dump(mode="json")
            findings = report.findings
        if expected:
            # Each criterion's scenario outcome, keyed by scenario_ref; a scenario the
            # run never executed (unbound feature, collection miss) is "missing".
            executed = parse_bdd_report(out)
            scenarios = {ref: executed.get(ref, "missing") for ref in sorted(expected)}
            structured[BDD_KEY] = scenarios
            findings = bdd_findings(scenarios) + findings
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
        if rc in _INFRA_EXIT_CODES:
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
        # Executable acceptance: every criterion's scenario must have run and passed.
        scenarios = obs.structured.get(BDD_KEY)
        if scenarios is not None and any(v != "passed" for v in scenarios.values()):
            return GateStatus.FAIL
        return GateStatus.PASS
