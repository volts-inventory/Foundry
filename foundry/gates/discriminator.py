"""Held-out-mutant + behavioral-invariance discriminator. (gov §1c, plan §5.1)

The **malicious/weak-test defense** behind ``mutant_rotation_id``. A pytest run
that is "green" is *necessary but not sufficient*: an adversarial agent (a
Phase-4 adversarial-exit attack) can supply a vacuous / value-pinned / gamed
test suite that passes on the real tree while exercising essentially nothing.
This module is the trusted, agent-opaque machinery that separates a genuine
suite from a gamed-green one. It runs ENTIRELY in zone A's executor over the
materialized workdir (no signing key in scope — the runner decides over the
observation it returns).

Two reinforcing controls (gov §1c, SEC-R11-F1 / SEC-R12-F1):

- **Held-out mutants.** Small *semantic* mutations of the code under test, drawn
  from a rotating family keyed by ``mutant_rotation_id`` and **never exposed to
  the test-authoring model** (so a fingerprint cannot be pre-baked). For each
  mutant the suite is re-run against a *mutated copy* of the tree: a genuine
  suite must FAIL (the mutant is *killed*); a suite that still PASSES against the
  mutated code has *survived* the mutant — it tested nothing real. The fraction
  of killed mutants is the **kill score**; below the class-scaled bar ⇒ the
  discriminator FAILs (the gamed-green suite is rejected). The mutant set is the
  trusted input here — a real production mutation engine is heavier; this is the
  dev-grade curated-set + simple text/AST substitution (plan §0.3).
- **Behavioral invariance.** A family of golden input→output properties the spec
  implies but the agent's tests did not necessarily encode. The runner derives
  them independently and checks the *real* code satisfies them; a violation ⇒
  FAIL (the produced code is wrong even though the suite is green). This is the
  invariant the agent could not see coming.

**Gate-class-aware (domains.md §1).** The discriminator applies ONLY to class 2
(execution-deterministic). Its strictness scales with the verdict class via
:func:`required_kill_ratio`: class 2 demands a hard kill ratio; class 1
(config-evaluable) and class 3 (statistical) carry no held-out-mutant
discriminator (the behavioral-invariance / mutation-adequacy assumption is
class-2-only, domains.md §1) — for those classes the discriminator is vacuously
satisfied so it never rejects a statistical or perceptual test.

Dev-grade scope (plan §0.3): the mutation logic is a real text substitution over
the materialized tree (curated mutant set supplied by the caller), and the
suite is re-run under the SAME pinned pytest harness the Test gate uses. The
production hardening (a family-decorrelated non-LLM mutant generator, opaque
runtime substitution in a normalized tree, trusted-infra-fuzzed input adequacy)
is heavier; the seam and the discriminator logic are real.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import VerdictClass

from .runner import run_grounding_command
from .test import pinned_test_env

__all__ = [
    "BehavioralInvariant",
    "DiscriminatorReport",
    "HeldOutMutant",
    "TestSuiteDiscriminator",
    "default_kill_bar",
    "required_kill_ratio",
]

# A failing pytest run uses this rc; the mutant re-runs key "killed" off it.
_PYTEST_FAILED = 1

#: Default minimum fraction of held-out mutants a genuine suite must kill.
default_kill_bar: float = 1.0


def required_kill_ratio(verdict_class: VerdictClass, *, bar: float = default_kill_bar) -> float:
    """Class-scaled held-out-mutant kill ratio the suite must clear. (domains.md §1)

    The held-out-mutant + behavioral-invariance discriminator is a **class-2**
    (execution-deterministic) control. For class 1 (config-evaluable) and class 3
    (statistical) the assumption does not hold, so the discriminator carries no
    kill requirement (ratio ``0.0``) — it never rejects a statistical/perceptual
    test. Class 2 demands the full ``bar`` (default: every held-out mutant must be
    killed; a single surviving mutant reveals a weak suite).
    """
    if verdict_class is VerdictClass.EXECUTION_DETERMINISTIC:
        return bar
    return 0.0


class HeldOutMutant(FrozenModel):
    """A held-out semantic mutation of the code under test (rotated, opaque).

    Dev-grade: a literal text substitution applied to ``target`` in a *copy* of
    the workdir. ``find`` must occur in the file (else the mutant is inapplicable
    — fail-closed: an inapplicable mutant cannot be silently treated as "killed").
    A genuine suite must FAIL against the mutated tree (the mutant is *killed*).
    """

    mutant_id: str
    target: str  # workdir-relative path of the file under test
    find: str
    replace: str


class BehavioralInvariant(FrozenModel):
    """A golden input→output property the produced code must satisfy.

    The runner derives these independently of the agent's tests. ``module`` is a
    workdir-relative importable path (dotted or ``a/b.py``-style), ``call`` the
    callable name, ``args`` the positional inputs, ``expected`` the required
    output. A violation ⇒ the code is wrong even though the agent's suite is
    green ⇒ the discriminator FAILs.
    """

    invariant_id: str
    module: str  # workdir-relative, e.g. "calc.py" or "pkg/calc.py"
    call: str
    args: tuple[object, ...] = ()
    expected: object = None


class DiscriminatorReport(FrozenModel):
    """The discriminator's verdict over a workdir — a pure observation.

    Carries no signing power: the Test gate folds this into its zone-A
    observation and the runner decides. ``passed`` is the AND of the kill-score
    bar and behavioral invariance.
    """

    passed: bool
    kill_score: float
    required_kill_ratio: float
    mutants_total: int
    mutants_killed: int
    survived: tuple[str, ...] = ()
    invariants_total: int = 0
    invariants_violated: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


@dataclass
class TestSuiteDiscriminator:
    """Re-runs the agent suite against held-out mutants + checks invariants.

    Gate-class-aware: ``verdict_class`` scales the kill ratio via
    :func:`required_kill_ratio`. The mutant set + invariant set are TRUSTED
    inputs supplied by the runner (never the agent). ``mutant_rotation_id`` is
    the opaque rotation handle the curated set is keyed under — bound into the
    Test gate's class-2 reuse key so a rotation boundary invalidates a cached
    verdict.
    """

    mutants: Sequence[HeldOutMutant] = ()
    invariants: Sequence[BehavioralInvariant] = ()
    verdict_class: VerdictClass = VerdictClass.EXECUTION_DETERMINISTIC
    mutant_rotation_id: int = 0
    kill_bar: float = default_kill_bar
    cmd: tuple[str, ...] = field(
        default_factory=lambda: (sys.executable, "-m", "pytest", "-q", "--tb=no")
    )
    timeout_s: float = 600.0

    # ---- held-out mutants: a surviving mutant means a weak/gamed suite -------

    def _run_suite(self, workdir: Path) -> int:
        """Run the agent suite under the pinned harness; return the pytest rc."""
        rc, _out, _err = run_grounding_command(
            self.cmd, workdir, env=pinned_test_env(), timeout_s=self.timeout_s
        )
        return rc

    def _apply_mutant(self, src: Path, dest: Path, mutant: HeldOutMutant) -> bool:
        """Copy ``src`` → ``dest`` with ``mutant`` applied to its target file.

        Returns False (fail-closed) if the mutant does not apply — an
        inapplicable mutant must never be counted as killed.

        Compiled caches (``__pycache__`` / ``.pytest_cache``) are NOT copied: a
        stale ``calc.pyc`` from the agent suite's own run records the original
        source mtime, and a same-second ``write_text`` of the mutated source can
        leave that mtime unchanged — Python would then import the UNMUTATED
        bytecode and the mutant would spuriously "survive." Excluding caches
        forces a recompile of the mutated source every time.
        """
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        target = dest / mutant.target
        if not target.is_file():
            return False
        text = target.read_text(encoding="utf-8", errors="strict")
        if mutant.find not in text:
            return False
        target.write_text(text.replace(mutant.find, mutant.replace, 1), encoding="utf-8")
        return True

    def _kill_results(self, workdir: Path) -> tuple[int, int, list[str], list[str]]:
        """For each held-out mutant: killed iff the suite FAILS against it."""
        killed = 0
        total = 0
        survived: list[str] = []
        findings: list[str] = []
        for mutant in self.mutants:
            total += 1
            with tempfile.TemporaryDirectory(prefix="mutant-") as tmp:
                dest = Path(tmp) / "tree"
                if not self._apply_mutant(workdir, dest, mutant):
                    findings.append(
                        f"mutant {mutant.mutant_id!r} inapplicable to {mutant.target!r} "
                        "(fail-closed: not counted as killed)"
                    )
                    survived.append(mutant.mutant_id)
                    continue
                rc = self._run_suite(dest)
            # The suite KILLS the mutant iff it fails against the mutated tree.
            # A green run (or no tests collected) against mutated code means the
            # mutant survived — the suite tested nothing real.
            if rc == _PYTEST_FAILED:
                killed += 1
            else:
                survived.append(mutant.mutant_id)
                findings.append(
                    f"mutant {mutant.mutant_id!r} SURVIVED (suite still green on mutated "
                    f"{mutant.target!r}, pytest rc={rc}) — weak/gamed suite"
                )
        return killed, total, survived, findings

    # ---- behavioral invariance: the property the agent's tests didn't encode -

    def _invariant_results(self, workdir: Path) -> tuple[int, list[str], list[str]]:
        """Check each golden input→output property against the REAL tree."""
        violated: list[str] = []
        findings: list[str] = []
        total = 0
        for inv in self.invariants:
            total += 1
            ok, detail = _check_invariant(workdir, inv, self.timeout_s)
            if not ok:
                violated.append(inv.invariant_id)
                findings.append(f"invariant {inv.invariant_id!r} violated: {detail}")
        return total, violated, findings

    # ---- the combined discriminator verdict (a pure observation) ------------

    def evaluate(self, workdir: Path) -> DiscriminatorReport:
        """Held-out-mutant kill score AND behavioral invariance over ``workdir``."""
        ratio = required_kill_ratio(self.verdict_class, bar=self.kill_bar)
        killed, total, survived, kill_findings = self._kill_results(workdir)
        inv_total, violated, inv_findings = self._invariant_results(workdir)

        kill_score = 1.0 if total == 0 else killed / total
        kill_ok = kill_score >= ratio
        invariants_ok = not violated
        return DiscriminatorReport(
            passed=kill_ok and invariants_ok,
            kill_score=kill_score,
            required_kill_ratio=ratio,
            mutants_total=total,
            mutants_killed=killed,
            survived=tuple(survived),
            invariants_total=inv_total,
            invariants_violated=tuple(violated),
            findings=(*kill_findings, *inv_findings),
        )


def _check_invariant(
    workdir: Path, inv: BehavioralInvariant, timeout_s: float
) -> tuple[bool, str]:
    """Run one invariant in a child interpreter under the pinned env.

    Imports ``inv.module`` from ``workdir``, calls ``inv.call(*inv.args)``, and
    asserts equality with ``inv.expected``. Runs in a subprocess so agent code is
    never imported into the trusted decision process; exit 0 ⇒ satisfied, exit 1
    ⇒ violated, anything else ⇒ a grounding error surfaced as a violation
    (fail-closed: an invariant the runner cannot evaluate is not satisfied).
    """
    mod_path = inv.module
    if mod_path.endswith(".py"):
        mod_path = mod_path[:-3]
    mod_name = mod_path.replace("/", ".").replace("\\", ".")
    payload = {
        "module": mod_name,
        "call": inv.call,
        "args": list(inv.args),
        "expected": inv.expected,
    }
    script = (
        "import importlib, json, sys\n"
        f"P = json.loads({json.dumps(json.dumps(payload))})\n"
        "m = importlib.import_module(P['module'])\n"
        "fn = getattr(m, P['call'])\n"
        "got = fn(*P['args'])\n"
        "sys.exit(0 if got == P['expected'] else 1)\n"
    )
    rc, _out, err = run_grounding_command(
        (sys.executable, "-c", script),
        workdir,
        env=pinned_test_env({"PYTHONPATH": str(workdir)}),
        timeout_s=timeout_s,
    )
    if rc == 0:
        return True, ""
    if rc == 1:
        return False, f"{inv.call}{tuple(inv.args)!r} != {inv.expected!r}"
    return False, f"could not evaluate invariant (rc={rc}): {err.strip()[:200]}"


# A discriminator the Test gate consults; None means "no discriminator wired"
# (legacy: a green run is then sufficient as before).
DiscriminatorFactory = Callable[[], TestSuiteDiscriminator]
