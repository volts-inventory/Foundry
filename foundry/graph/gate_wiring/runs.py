"""Construct and run the Spec / Build / Test / Smoke gates at a boundary, and
scope a wave check's build to what the wave has built."""

from __future__ import annotations

import contextlib
import shlex
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.gates.build import BuildGate
from foundry.gates.pydeps import PythonBuildEnv, python_build_env
from foundry.gates.sandbox import toolchain_scope
from foundry.gates.spec import SpecGate
from foundry.gates.test import TestGate
from foundry.graph.gate_wiring.materialize import (
    _current_criteria,
    _toolchain_digest,
    materialize_workdir,
    planned_files,
)
from foundry.schemas.gates import GateResult, GateStatus, GateTriage, VerdictClass
from foundry.schemas.implementation import FEATURE_DIR, StackDecision, scenario_ref
from foundry.state.tree_sha import tree_sha

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.discriminator import DiscriminatorFactory
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import GateImpl, TwoZoneGateRunner, ZoneAObservation
    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


# ---- gate-run helpers (verdicts ONLY from the runner) -----------------------

# Spec is keyed under the criteria/diff emission point; build/test under their
# own keys so each verdict gets an independent resume-monotonic epoch stream.
_SPEC_NODE = "spec_gate"
_BUILD_NODE = "build_gate"
_TEST_NODE = "test_gate"
_SMOKE_NODE = "smoke_gate"


def _run_gates(
    gates: list[tuple[GateImpl, str]],
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    config: RunnableConfig,
    run_against: str,
    workdir: Path,
) -> list[GateResult]:
    """Run one boundary's ``(gate, node_id)`` list, in order, via the runner and
    return their ATTESTED verdicts (possibly the runner's reused originals).

    Mints each verdict's ``Seq`` via ``rt.seq`` (resume-monotonic epoch, AR2)
    keyed on the gate's grounding so a re-execution — or a reuse — outranks the
    stale record it supersedes. The runner attests; we never sign or construct one.
    """
    units = [(g, rt.seq(config, node_id, f"verdict:{g.name}")) for g, node_id in gates]
    return runner.run_unit(units, run_against, workdir)


def gate_profile(
    state: FactoryState, fallback: VerificationProfile | None
) -> VerificationProfile | None:
    """The gate commands for this run, read at gate time from the ``stack`` channel.

    A ``--profile`` pin runs that profile unchanged; a planner stack in a language
    this host has runs its own commands; a stack in an undetected language is an
    ``unavailable`` profile (every gate an infra ERROR naming the missing toolchain —
    never a silent switch to another stack's commands); no stack keeps ``fallback``
    (the profile the graph was built with)."""
    from foundry.gates.profiles import PROFILES, VerificationProfile, stack_profile
    from foundry.toolchains import available_languages

    stack = state.get("stack")
    if stack is None:
        return fallback
    if stack.profile:
        return PROFILES.get(stack.profile, fallback)
    available = available_languages()
    if stack.language not in available:
        return VerificationProfile(
            name=f"stack:{stack.language}",
            unavailable=(
                f"missing toolchain: the planned stack's language {stack.language!r} "
                f"is not installed on the gate host (detected: "
                f"{', '.join(sorted(available)) or 'none'})"
            ),
        )
    return stack_profile(stack)


class UnavailableGate:
    """A gate that cannot run on this host: zone A raises ``GroundingUnavailable`` so
    the runner signs an infra ERROR (bounded retry, never latched) naming ``reason``."""

    verdict_class = VerdictClass.EXECUTION_DETERMINISTIC
    decision_deterministic = True
    execution_deterministic = True
    mutant_rotation_id = 0
    scanner_rule_version = "unavailable-v1"

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def execute(self, workdir: Path) -> ZoneAObservation:
        from foundry.gates.runner import GroundingUnavailable

        raise GroundingUnavailable(self._reason)

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.ERROR


class UnpinnedSmokeGate:
    """The smoke verdict for a stack with no run command: a deterministic PASS that
    says so. Nothing executes; the verdict still comes from the runner, attested,
    so the boundary's smoke slot is never filled by an unsigned record."""

    name = "smoke"
    verdict_class = VerdictClass.CONFIG_EVALUABLE
    decision_deterministic = True
    execution_deterministic = False

    def __init__(self, *, seal_key: bytes | None = None) -> None:
        self._seal_key = seal_key

    def execute(self, workdir: Path) -> ZoneAObservation:
        from foundry.gates.runner import observe

        return observe(
            self.name,
            structured={"pinned": False},
            findings=("smoke not pinned: the stack declares no run command",),
            seal_key=self._seal_key,
        )

    def decide(self, obs: ZoneAObservation) -> GateStatus:
        return GateStatus.PASS if obs.structured.get("pinned") is False else GateStatus.ERROR


_GATE_TIMEOUT_S = 600.0


def _build_gate(
    profile: VerificationProfile | None,
    runner: TwoZoneGateRunner | None,
    pyenv: PythonBuildEnv | None = None,
    planned: Iterable[str] = (),
    timeout_s: float = _GATE_TIMEOUT_S,
) -> GateImpl:
    """``planned``: the ticket files a wave check's tree may not hold yet, whose
    modules the build's import check counts as the project's own. No ``runner``:
    an unsealed gate for an unattested run (an agent's in-session check).
    ``timeout_s`` bounds the build step; the install keeps the gate's own bound."""
    first_party = frozenset(PurePosixPath(p).name.split(".", 1)[0] for p in planned_paths(planned))
    seal_key = runner.seal_key if runner is not None else None
    if profile is None:
        return BuildGate(
            seal_key=seal_key,
            pyenv=pyenv,
            python=True,
            first_party=first_party,
            timeout_s=timeout_s,
            install_timeout_s=_GATE_TIMEOUT_S,
        )
    if profile.unavailable:
        return UnavailableGate("build", profile.unavailable)
    return BuildGate(
        cmd=profile.build_cmd,
        install_cmd=profile.install_cmd,
        seal_key=seal_key,
        profile_name=profile.name,
        pyenv=pyenv,
        python=profile.python,
        first_party=first_party if profile.python else frozenset(),
        timeout_s=timeout_s,
        install_timeout_s=_GATE_TIMEOUT_S,
    )


@contextlib.contextmanager
def _build_env(
    profile: VerificationProfile | None, workdir: Path, rt: AgentRuntime
) -> Iterator[PythonBuildEnv | None]:
    """The build's venv when it is a Python build that declares dependencies: the
    run's (``rt.python_envs``) for the same manifests, so checks reuse one install.
    Every gate step inside shares the run's toolchain cache (``rt.toolchain_cache``):
    what the install step downloads, the offline steps can read."""
    python = profile is None or (profile.python and not profile.unavailable)
    with (
        toolchain_scope(rt.toolchain_cache),
        python_build_env(workdir, python=python, cache=rt.python_envs) as pyenv,
    ):
        yield pyenv


def run_spec_gate(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
) -> dict[str, Any]:
    """Run the Spec gate at a product / architecture exit boundary.

    Materializes the workdir (criteria.json + any sources present) into
    ``workdir`` and runs :class:`SpecGate` through the runner against the
    integrated ``tree_sha``. Returns ``{"gates": [<attested GateResult>]}`` —
    the ONLY thing that reaches the channel, and only because the runner
    attested it (an unsigned write would be dropped, L5).
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    results = _run_gates(
        [(SpecGate(seal_key=runner.seal_key), _SPEC_NODE)],
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
    )
    return {"gates": results}


def run_build_test_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    min_passed: int = 1,
    discriminator_factory: DiscriminatorFactory | None = None,
    profile: VerificationProfile | None = None,
) -> dict[str, Any]:
    """Run the Build + Test + Smoke gates at an implementation /
    quality exit boundary.

    Materializes the integrated tree (code + test sources + criteria.json) into
    ``workdir`` ONCE and runs :class:`BuildGate` then :class:`TestGate` against
    its ``tree_sha`` (the same integrated digest, so both verdicts latch/bind to
    one state). ``min_passed`` is the Test gate's adequacy floor — a vacuous
    (0-test) suite is FAIL, never PASS (governance §1c). Returns
    ``{"gates": [<attested build>, <attested test>, <attested smoke>]}``; the
    commands come from :func:`gate_profile` (the stack on state, else ``profile``).

    ``discriminator_factory`` is the OPTIONAL malicious-test defense seam (the
    A-M4-01 fix). When provided, it produces a
    :class:`~foundry.gates.discriminator.TestSuiteDiscriminator` (the curated
    held-out mutants + behavioral invariants for the subject under test) that the
    constructed :class:`TestGate` CONSULTS in zone A: a gamed-green suite that
    clears the adequacy floor but does NOT kill a held-out mutant (or violates an
    invariant) is FAILed by the COMPILED gate — not merely in the unit test (gov
    §1c). The held-out mutant SET is a dev-grade CURATED set the factory supplies
    (a real mutation engine is heavier; plan §0.3). When ``None`` (the default),
    behavior is EXACTLY as before — the adequacy floor alone (back-compat): no
    discriminator is wired and a green + adequate suite suffices.
    """
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    # Profile-pinned commands (ROADMAP Phase 3): a stack-appropriate profile
    # swaps WHAT the gates execute, never the two-zone trust seam. None keeps
    # each gate's historical default — byte-identical to the pre-profile factory.
    profile = gate_profile(state, profile)
    with _build_env(profile, workdir, rt) as pyenv:
        return _build_test_smoke(
            state,
            config,
            rt=rt,
            runner=runner,
            workdir=workdir,
            run_against=run_against,
            min_passed=min_passed,
            discriminator_factory=discriminator_factory,
            profile=profile,
            pyenv=pyenv,
        )


def _build_test_smoke(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    workdir: Path,
    run_against: str,
    min_passed: int,
    discriminator_factory: DiscriminatorFactory | None,
    profile: VerificationProfile | None,
    pyenv: PythonBuildEnv | None,
) -> dict[str, Any]:
    test_cmd = profile.test_cmd if profile is not None else None
    # Wire the held-out-mutant + behavioral-invariance discriminator into the
    # COMPILED Test gate when a factory is injected (A-M4-01): a gamed-green suite
    # that clears the floor but survives a held-out mutant FAILs here, not just in
    # the unit test. Absent a factory the gate is the legacy adequacy-floor-only
    # Test gate (back-compat).
    discriminator = discriminator_factory() if discriminator_factory is not None else None
    # A profile-pinned FOREIGN test harness (npm test / terraform validate) has no
    # pytest summary to count, so the adequacy floor drops to exit-code-only there
    # (min_passed=0; the floor is pytest-specific). The python default keeps it.
    effective_min = 0 if test_cmd is not None else min_passed
    test_gate: GateImpl
    if profile is not None and (profile.unavailable or profile.test_unavailable):
        test_gate = UnavailableGate("test", profile.unavailable or profile.test_unavailable)
    else:
        test_gate = TestGate(
            cmd=test_cmd,
            min_passed=effective_min,
            discriminator=discriminator,
            seal_key=runner.seal_key,
            bdd_cmd=profile.bdd_cmd if profile is not None else None,
            # Profile commands (npm test, go test, ...) are judged by exit code, not
            # pytest's codes; None keeps the pinned pytest harness and its semantics.
            stack_harness=test_cmd is not None,
            profile_name=profile.name if profile is not None else "",
            pyenv=pyenv,
        )
    # Every run carries a smoke verdict: the stack's own run command, else an
    # attested "not pinned" PASS (the boundary is always build/test/smoke).
    from foundry.gates.smoke import SmokeGate

    smoke: GateImpl
    if profile is not None and profile.unavailable:
        smoke = UnavailableGate("smoke", profile.unavailable)
    elif profile is not None and profile.smoke_cmd is not None:
        smoke = SmokeGate(profile.smoke_cmd, seal_key=runner.seal_key, pyenv=pyenv)
    else:
        smoke = UnpinnedSmokeGate(seal_key=runner.seal_key)
    gates = _run_gates(
        [
            (_build_gate(profile, runner, pyenv), _BUILD_NODE),
            (test_gate, _TEST_NODE),
            (smoke, _SMOKE_NODE),
        ],
        rt=rt,
        runner=runner,
        config=config,
        run_against=run_against,
        workdir=workdir,
    )
    test = gates[1]
    out: dict[str, Any] = {"gates": gates}
    if (
        profile is not None
        and test_cmd is not None
        and profile.bdd_cmd is None
        and any((workdir / FEATURE_DIR).glob("*.feature"))
    ):
        # Only the pytest harness executes scenarios; say so rather than pass silently.
        out["gate_triage"] = [
            GateTriage(
                name=test.name,
                run_against=run_against,
                explanation=(
                    "ADVISORY: features/*.feature hold the tickets' acceptance scenarios, "
                    f"but the {profile.name} test harness does not execute them — they "
                    "are not enforced as executable for this stack."
                ),
                produced_by=_TEST_NODE,
                seq=rt.seq(config, _TEST_NODE, "triage:bdd"),
            )
        ]
    return out


def wave_scenarios(
    state: FactoryState, resolve: ArtifactResolver, also: Iterable[str] = ()
) -> frozenset[str]:
    """The scenario refs a per-wave check can judge: those of criteria whose every
    carrier (the owning ticket and each ticket listing it in ``scenario_ids``, i.e.
    its paired test ticket) is built, counting the tickets ``also`` names as built
    (a ticket's own session checks its scenarios). Anything still waiting on a
    ticket is left to a later wave or the final gate."""
    from foundry.agents.implementation.tickets import built_task_ids, read_tickets

    built = built_task_ids(state) | set(also)
    carriers: dict[str, set[str]] = {}
    for t in read_tickets(state, resolve):
        for cid in t.scenario_ids:
            carriers.setdefault(cid, set()).add(t.id)
    return frozenset(
        scenario_ref(c.ticket_id, c.id)
        for c in _current_criteria(state, resolve).values()
        if c.ticket_id and {c.ticket_id, *carriers.get(c.id, ())} <= built
    )


def _wave_test_gate(
    profile: VerificationProfile | None,
    runner: TwoZoneGateRunner | None,
    scope: frozenset[str],
    pyenv: PythonBuildEnv | None = None,
    timeout_s: float = _GATE_TIMEOUT_S,
) -> TestGate | None:
    """The scoped Test gate for a wave check, or ``None`` when this stack's harness
    does not execute scenarios (a foreign test command with no ``bdd``)."""
    if profile is not None and (
        profile.unavailable
        or profile.test_unavailable
        or (profile.test_cmd is not None and profile.bdd_cmd is None)
    ):
        return None
    return TestGate(
        cmd=profile.test_cmd if profile is not None else None,
        seal_key=runner.seal_key if runner is not None else None,
        bdd_cmd=profile.bdd_cmd if profile is not None else None,
        stack_harness=profile is not None and profile.test_cmd is not None,
        profile_name=profile.name if profile is not None else "",
        scope=scope,
        pyenv=pyenv,
        timeout_s=timeout_s,
    )


def wave_stack(
    state: FactoryState, files: Iterable[str], workdir: Path
) -> tuple[StackDecision | None, list[tuple[str, str]]]:
    """The stack a check of a partly built tree runs, and a (triage name, text) note
    per step it scoped: a planned non-Python manifest not built yet skips the
    install, build and typecheck; the build and typecheck skip planned targets no
    ticket has built yet. A pinned profile, or no stack, runs unchanged."""
    stack = state.get("stack")
    notes: list[tuple[str, str]] = []
    if stack is None or stack.profile:
        return stack, notes
    files = list(files)
    # Python installs only what a built manifest declares; its build needs none.
    missing = () if stack.language == "python" else unbuilt_manifests(files, workdir)
    if missing:
        # No manifest, nothing to install from; the build and the type checker
        # need the dependencies the install would have fetched.
        skip = ("install", "build", "typecheck")
        dropped = [f"{k} `{getattr(stack, k)}`" for k in skip if getattr(stack, k)]
        if dropped:
            stack = stack.model_copy(update=dict.fromkeys(skip, ""))
            notes.append(
                (
                    WAVE_MANIFEST_TRIAGE,
                    (
                        f"this wave's check skips {', '.join(dropped)}: the planned "
                        f"manifest {', '.join(missing)} is not built yet; the final gate "
                        "runs every step"
                    ),
                )
            )
    for step in ("build", "typecheck"):
        full = getattr(stack, step)
        if not full:
            continue
        scoped = wave_typecheck(full, planned_paths(files), workdir)
        if scoped != full:
            stack = stack.model_copy(update={step: scoped})
            notes.append(
                (
                    WAVE_TYPECHECK_TRIAGE,
                    (
                        f"this wave's {step} skips planned targets no ticket has built "
                        f"yet: `{scoped or '(skipped: none built yet)'}` instead of `{full}`"
                    ),
                )
            )
    return stack, notes


def run_build_gate(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    profile: VerificationProfile | None = None,
    scenarios: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Run the Build gate on the integrated tree (the per-wave check, §2b), plus,
    when ``scenarios`` is non-empty, the Test gate scoped to those scenarios, so an
    unbound or failing scenario is caught in the wave that built it.

    The full Test gate is left out on purpose: scenarios of tickets not yet built
    would fail it. Keyed under the same nodes as the final gates, so the final
    verdicts (a later epoch) supersede every intermediate one."""
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    out: dict[str, Any] = {}
    files = planned_files(state, resolve)
    stack, notes = wave_stack(state, files, workdir)
    if notes:
        # The scoped commands are what this verdict judges, so they key the digest.
        state = {**state, "stack": stack}
        run_against = tree_sha(workdir, toolchain_digest=_toolchain_digest(stack))
        out["gate_triage"] = [
            GateTriage(
                name=name,
                run_against=run_against,
                explanation=text[:2000],
                produced_by=_BUILD_NODE,
                seq=rt.seq(config, _BUILD_NODE, f"{name}:{run_against}"),
            )
            for name, text in notes
        ]
    profile = gate_profile(state, profile)
    with _build_env(profile, workdir, rt) as pyenv:
        gates: list[tuple[GateImpl, str]] = [
            (_build_gate(profile, runner, pyenv, planned=files), _BUILD_NODE)
        ]
        test = _wave_test_gate(profile, runner, scenarios, pyenv) if scenarios else None
        if test is not None:
            # Same tree digest as the build, so the fixer reads both verdicts as this
            # pass's. A scoped FAIL latching the tree is safe: the final Test gate only
            # ever runs on a later tree (a failing wave check is fixed or halts).
            gates.append((test, _TEST_NODE))
        out["gates"] = _run_gates(
            gates,
            rt=rt,
            runner=runner,
            config=config,
            run_against=run_against,
            workdir=workdir,
        )
    return out


WAVE_TYPECHECK_TRIAGE = "wave-typecheck-scope"
WAVE_MANIFEST_TRIAGE = "wave-manifest-scope"
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
# The package manifests a non-Python stack's install, build and typecheck need.
_MANIFESTS = frozenset({"package.json", "pyproject.toml", "Cargo.toml", "go.mod"})


def unbuilt_manifests(files: Iterable[str], workdir: Path) -> tuple[str, ...]:
    """The planned package manifests no ticket has written into ``workdir`` yet."""
    return tuple(
        sorted(
            {
                p
                for p in (_norm_target(f) for f in files)
                if PurePosixPath(p).name in _MANIFESTS and not (workdir / p).exists()
            }
        )
    )


def planned_paths(files: Iterable[str]) -> frozenset[str]:
    """Every path the plan will create: each ticket file and its parent dirs
    (``app/db.py`` plans ``app`` too), normalized with no ``./`` or trailing ``/``."""
    out: set[str] = set()
    for f in files:
        p = PurePosixPath(_norm_target(f))
        if str(p) in ("", "."):
            continue
        out.add(str(p))
        out.update(str(d) for d in p.parents if str(d) != ".")
    return frozenset(out)


def _norm_target(token: str) -> str:
    return token.removeprefix("./").rstrip("/")


def wave_typecheck(typecheck: str, planned: frozenset[str], workdir: Path) -> str:
    """The stack's typecheck for an intermediate wave: arguments naming a planned
    path that no ticket has built yet are dropped (``mypy app`` before the wave
    that writes ``app/`` would fail on the missing target, not on the code). If
    that leaves none of the command's planned targets, the step is skipped (``""``).
    Anything else — a target outside the plan, a path already built — is kept, so
    a real type error or a bad target still fails the check; the final Build gate
    always runs the full command."""
    try:
        tokens = shlex.split(typecheck)
    except ValueError:
        return typecheck

    def targeted(tok: str) -> bool:
        return _norm_target(tok) in planned

    def unbuilt(tok: str) -> bool:
        return targeted(tok) and not (workdir / _norm_target(tok)).exists()

    if not any(unbuilt(t) for t in tokens):
        return typecheck
    kept = [t for t in tokens if not unbuilt(t)]
    if not any(targeted(t) for t in kept):
        return ""
    return " ".join(t if t in _SHELL_OPERATORS else shlex.quote(t) for t in kept)
