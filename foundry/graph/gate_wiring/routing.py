"""Boundary routing (advance / fix / halt) and the fix-attempt counters, plus
the failure parsing routing and the fix loop read."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from foundry.graph.gate_wiring.fix_policy import FixPolicy
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus
from foundry.state.reducers import merge_gates, union_fix_passes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


# ---- routing predicate (fail-closed, convergence-bounded) -------------------

# Which gate verdicts each phase boundary is responsible for.
SPEC_BOUNDARY = "spec"
BUILD_TEST_BOUNDARY = "build_test"
# The per-wave integration check (§2b): the build verdict alone.
BUILD_BOUNDARY = "build"
# The last wave's check: the whole app is built, so it is also launched.
BUILD_LAUNCH_BOUNDARY = "build_launch"
BUILD_TEST_LAUNCH_BOUNDARY = "build_test_launch"
# The final boundary: build, test, smoke and launch (a stack without a run command
# gets an attested "not pinned" smoke PASS, a library a "not pinned" launch PASS),
# so a missing/FAILed smoke or launch never advances.
BUILD_TEST_SMOKE_BOUNDARY = "build_test_smoke"

_BOUNDARY_GATES: dict[str, frozenset[str]] = {
    SPEC_BOUNDARY: frozenset({"spec"}),
    BUILD_BOUNDARY: frozenset({"build"}),
    BUILD_TEST_BOUNDARY: frozenset({"build", "test"}),
    BUILD_LAUNCH_BOUNDARY: frozenset({"build", "launch"}),
    BUILD_TEST_LAUNCH_BOUNDARY: frozenset({"build", "test", "launch"}),
    BUILD_TEST_SMOKE_BOUNDARY: frozenset({"build", "test", "smoke", "launch"}),
}

# Routing outcomes.
ADVANCE = "advance"  # all relevant gates PASS — proceed to the next phase
FIX = "fix"  # a FAIL/ERROR within the convergence bound — back to the layer
HALT = "halt"  # bound exhausted or a stable-FAIL latch — escalate / stop
# Hybrid only: the wave check failed twice; re-run its local tickets once on the
# cloud level (WaveBuildCheck.route; never returned by route_after_gates).
ESCALATE = "escalate"
# A fix loop's ticket restart (plan_next_pass): the engineering node rebuilds the
# planned tickets from scratch on their own tier (WaveBuildCheck.route; at the final
# gate a FIX re-entry reaches the engineering node instead of the fixer).
RESTART = "restart"


def _relevant_latest(state: FactoryState, gate_names: frozenset[str]) -> dict[str, GateResult]:
    """Newest-by-Seq verdict per relevant gate name on ``state['gates']``.

    The channel is already trust-filtered (``merge_gates`` admitted only
    runner-attested records) and keyed by ``(name, run_against)``; pick the
    latest by ``Seq`` per name so a re-run's verdict (bumped epoch) wins.
    """
    latest: dict[str, GateResult] = {}
    for g in state.get("gates") or ():
        if g.name not in gate_names:
            continue
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def _is_flake_quarantine(g: GateResult) -> bool:
    """An execution gate's ERROR caused by flake quarantine, not a hard verdict.

    The N=2 flake rule (E2E) and a quarantining Test run surface a split/flaky
    outcome as ERROR (DS7 — a flake is a property of the execution, not the
    tree). Such an ERROR must NOT fail the boundary: it quarantines (routes to
    bounded retry), never latches, never advances. Detected by gate family +
    a quarantine/flake marker in the runner findings.
    """
    if g.status is not GateStatus.ERROR or g.name not in {"test", "e2e"}:
        return False
    blob = " ".join(g.findings).lower()
    return "flak" in blob or "quarantin" in blob


def flake_quarantined(state: FactoryState, *, boundary: str) -> bool:
    """True iff a relevant Test/E2E verdict at ``boundary`` is flake-quarantined.

    A supervisor seam: distinguishes a transient flake-quarantine ERROR (retry
    without disqualifying the digest — it never latches and must NOT fail the
    boundary, governance §1a) from a hard FAIL / infra ERROR. The routing
    predicate already keeps a quarantine ERROR out of ADVANCE; this exposes the
    REASON so the supervisor can quarantine-and-retry rather than report a
    failure.
    """
    gate_names = _BOUNDARY_GATES.get(boundary, frozenset())
    return any(_is_flake_quarantine(g) for g in _relevant_latest(state, gate_names).values())


def _fix_attempts(state: FactoryState, config: RunnableConfig) -> int:
    """The convergence bound's spent attempts (architecture.md §7).

    Read from the budget ledger's global ``fix_attempts_since_all_green`` if
    present, else from the run config — the supervisor owns the counter; the
    routing predicate only READS it (fold-order independent)."""
    budget = state.get("budget")
    if budget is not None:
        return int(budget.fix_attempts_since_all_green)
    configurable = config.get("configurable") or {}
    return int(configurable.get("fix_attempts", 0))


def _fix_total(state: FactoryState) -> int:
    budget = state.get("budget")
    return int(budget.fix_attempts_total) if budget is not None else 0


def fix_cap(state: FactoryState, resolve: ArtifactResolver, max_fix_attempts: int) -> int:
    """The run's overall fix bound: ``max_fix_attempts`` per wave check plus the
    final gate's, so per-boundary resets can never add up to an unbounded loop."""
    from foundry.agents.implementation.tickets import plan_wave_count, read_tickets

    return max_fix_attempts * (plan_wave_count(read_tickets(state, resolve)) + 1)


def route_after_gates(
    state: FactoryState,
    config: RunnableConfig,
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int = 3,
    total_cap: int | None = None,
    fix_policy: FixPolicy | None = None,
) -> str:
    """Route a phase boundary on its relevant gate verdicts. Fail-closed.

    Returns:

    * :data:`ADVANCE` — every relevant gate has a PASS verdict on a non-disqualified
      digest; proceed to the next phase.
    * :data:`FIX` — a FAIL / (non-flake) ERROR, a missing relevant verdict, OR a
      stable-FAIL-latched digest, WITHIN the convergence bound; route back to the
      responsible layer to fix FORWARD into a new tree.
    * :data:`HALT` — the ``max_fix_attempts`` convergence bound is exhausted (escalate
      with the best draft); a non-fixable terminal condition. ``total_cap`` bounds the
      build-side fixes of the whole run (``budget.fix_attempts_total``), which the
      per-boundary resets never clear.

    The stable-FAIL latch (committed cross-fork; ``records``) permanently DISQUALIFIES
    the failing digest — a later PASS on that SAME tree never clears it (governance §5,
    anti-gaming) — but it no longer ends the run on its own: the bounded fix loop lets
    the engineers produce a NEW, passing tree (a real-shop fix-forward), and HALT fires
    only when the bound is spent. A flake-quarantine ERROR (N=2 split) is likewise a
    bounded retry, never a boundary FAIL/advance on its own.
    """
    gate_names = _BOUNDARY_GATES.get(boundary)
    if gate_names is None:
        raise ValueError(f"unknown phase boundary: {boundary!r}")

    latest = _relevant_latest(state, gate_names)

    if fix_policy is not None and fix_policy.until_cap:
        bound_spent = _until_cap_spent(state, config, boundary, fix_policy)
    else:
        bound_spent = _fix_attempts(state, config) >= max_fix_attempts or (
            total_cap is not None and _fix_total(state) >= total_cap
        )

    # A relevant gate needs a fix when it FAILs, ERRORs, is MISSING (never advance on
    # an absent verdict), OR its digest carries a stable-FAIL latch. A latched digest
    # is permanently DISQUALIFIED — it can never be admitted, and a later (flaky/gamed)
    # PASS on that SAME tree never clears it (governance §5, anti-gaming preserved). But
    # a disqualified tree does NOT end the run: like a real shop whose CI failed, the
    # engineers fix FORWARD into a NEW tree (a new digest the latch never touched). We
    # therefore route to the BOUNDED fix loop and HALT only once ``max_fix_attempts`` is
    # spent — escalate then, rather than shipping nothing on the first failure.
    needs_fix = False
    for name in gate_names:
        verdict = latest.get(name)
        if verdict is None:
            needs_fix = True  # missing relevant verdict: never advance (fail-closed)
            continue
        if verdict.status in (GateStatus.FAIL, GateStatus.ERROR):
            # FAIL, infra ERROR, OR flake-quarantine ERROR all route to bounded
            # retry — none advances the boundary (fail-closed).
            needs_fix = True
        elif records.stable_fail_latched(verdict.run_against, verdict.name):
            # A disqualified digest showing a PASS must NOT advance — route to a fresh
            # attempt instead (the latch can never be cleared on this tree).
            needs_fix = True

    if needs_fix:
        return HALT if bound_spent else FIX

    # All relevant gates PASS on a non-disqualified digest.
    return ADVANCE


def _until_cap_spent(
    state: FactoryState,
    config: RunnableConfig,
    boundary: str,
    policy: FixPolicy,
) -> bool:
    """A fix-until-cap loop's bound: the stop its gate node recorded on the newest
    check (:func:`keep_best`). A check that recorded no pass (no verdict to judge)
    still stops past the window ceiling when there is no time cap to end it."""
    passes = [p for p in state.get("fix_passes") or () if p.boundary == boundary]
    if passes and max(passes, key=lambda p: p.seq).stop is not None:
        return True
    return policy.remaining_s() is None and _fix_attempts(state, config) > policy.window_ceiling


_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)")
# pytest's summary line for a test module that failed to collect (an ImportError,
# a SyntaxError): it names the module only, and hides every test in it.
_COLLECTION_ERROR = re.compile(r"^ERROR ([^\s:]+\.py)(?: - |$)")
_BDD_SCENARIO = re.compile(r"^BDD criterion \S+: scenario (\S+)")
# foundry.gates.properties' findings for a contract property the tests do not prove.
_PROPERTY = re.compile(r"^property (\S+) (?:missing|not covered|not run|failing)\b")


def failing_ids(findings: Iterable[str]) -> tuple[str, ...]:
    """The failing pytest node ids, the test modules that failed to collect, the
    BDD scenario refs and the contract properties (``property:<id>``) a gate's
    findings name."""
    out: list[str] = []
    for line in findings:
        m = (
            _BDD_SCENARIO.match(line)
            or _FAILED_TEST.match(line.strip())
            or _COLLECTION_ERROR.match(line.strip())
        )
        ident = m.group(1) if m is not None else None
        if ident is None and (p := _PROPERTY.match(line)) is not None:
            ident = f"property:{p.group(1)}"
        if ident is not None and ident not in out:
            out.append(ident)
    return tuple(out)


def is_collection_error(ident: str) -> bool:
    """True iff a :func:`failing_ids` id is a test module that failed to collect."""
    return _COLLECTED_MODULE.fullmatch(ident) is not None


_COLLECTED_MODULE = re.compile(r"[^\s:]+\.py")


# foundry/gates/build.py's head for a pip install that failed on the project's own
# manifest (not the network): the fix is a manifest edit, never a re-run.
_PIP_MANIFEST_FAIL = "declared dependencies cannot be installed"
PIP_MANIFESTS = ("requirements.txt", "pyproject.toml")
# A relative source path with a line number: mypy/pyright/tsc/go/rustc/javac errors.
_SOURCE_LINE = re.compile(
    r"(?<![\w./-])(?:\./)?((?:[\w-][\w.-]*/)*[\w-][\w.-]*\."
    r"(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|cs|c|cc|cpp|h|hpp|rb))[:(]\d+"
)
# A Python compile error / traceback frame: File "./app.py", line 3.
_PY_FRAME = re.compile(r'File "(?:\./)?([^"/][^"]*\.pyi?)", line \d+')
_VENDORED = ("node_modules", "site-packages", ".venv", "venv", "vendor")
# pytest-bdd's errors for a step its test module cannot run: a step argument nothing
# provides, a step line no definition matches.
_STEP_BINDING_ERROR = re.compile(r"fixture '\w+' not found|StepDefinitionNotFoundError")


def pip_manifest_failure(gates: Iterable[GateResult]) -> bool:
    """True iff a FAILed gate says pip could not install the project's declared
    dependencies (a bad pin or a package that does not exist)."""
    return any(
        g.status is GateStatus.FAIL and any(_PIP_MANIFEST_FAIL in f for f in g.findings)
        for g in gates
    )


def failure_files(gates: Iterable[GateResult]) -> tuple[str, ...]:
    """The files a failure names, in order: the pip manifests for a manifest FAIL,
    then the sources a build gate's compile/type errors point at, then the test
    modules of a test gate's step-binding errors. Other test-gate tracebacks are
    left out: they name the test file, rarely where the fix goes; a step a test
    module cannot bind is fixed in that module."""
    gates = list(gates)
    out: list[str] = list(PIP_MANIFESTS) if pip_manifest_failure(gates) else []
    for g in gates:
        if g.status not in (GateStatus.FAIL, GateStatus.ERROR):
            continue
        if g.name == "test":
            if any(_STEP_BINDING_ERROR.search(f) for f in g.findings):
                out.extend(
                    dict.fromkeys(
                        p
                        for p in (i.split("::", 1)[0] for i in failing_ids(g.findings))
                        if p.endswith(".py") and p not in out
                    )
                )
            continue
        if g.name != "build":
            continue
        for line in g.findings:
            for m in (*_SOURCE_LINE.finditer(line), *_PY_FRAME.finditer(line)):
                path = m.group(1)
                parts = path.split("/")
                if ".." in parts or any(p in _VENDORED for p in parts) or path in out:
                    continue
                out.append(path)
    return tuple(out)


# Run-varying bits of a raw finding line (object addresses, timings): two runs of
# the same failure must compare equal.
_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\b\d+(?:\.\d+)?m?s\b")


def _failure_signature(verdicts: Iterable[GateResult]) -> frozenset[tuple[str, str]] | None:
    """What one tree fails on: (gate, failing test/scenario id) per FAILed gate, or
    (gate, normalized finding line) for a gate whose findings name no id. ``None``
    when a verdict ERRORed: an infra error or a flake quarantine is never "no
    progress"."""
    sig: set[tuple[str, str]] = set()
    for g in verdicts:
        if g.status is GateStatus.ERROR:
            return None
        if g.status is GateStatus.FAIL:
            ids = failing_ids(g.findings) or tuple(_VOLATILE.sub("#", f) for f in g.findings)
            sig.update((g.name, i) for i in ids)
    return frozenset(sig)


def _unchanged_tree_recheck(
    state: FactoryState, out: dict[str, Any], config: RunnableConfig, *, boundary: str
) -> bool:
    """True iff ``out`` re-checks a tree ``state`` already failed on, after a fix pass
    in between: the fixer changed nothing, so the same checks fail by definition.
    (:func:`no_fix_progress` cannot see this: the re-check replaces the earlier
    verdicts under the same ``(name, run_against)`` key.) ERRORs never count."""
    if _fix_attempts(state, config) < 1:
        return False
    names = _BOUNDARY_GATES[boundary]
    new = [g for g in out.get("gates") or () if g.name in names]
    if not new:
        return False
    tree = new[0].run_against
    old = [g for g in state.get("gates") or () if g.name in names and g.run_against == tree]
    prev, cur = _failure_signature(old), _failure_signature(new)
    return bool(old) and bool(prev) and bool(cur)


def failing_streak(state: FactoryState, *, boundary: str, since: str | None = None) -> int:
    """How many of the newest trees in a row the code itself fails at ``boundary``:
    a FAIL in the newest verdict per gate on the tree. ERROR (an infra error or a
    flake quarantine, see :func:`_failure_signature`) is not a code failure and,
    like a pass, ends the streak; so does the tree ``since`` (one an earlier wave
    advanced from still failing, :data:`WAVE_ADVANCE_STOPS`)."""
    names = _BOUNDARY_GATES[boundary]
    trees: dict[str, dict[str, GateResult]] = {}
    for g in state.get("gates") or ():
        if g.name not in names:
            continue
        newest = trees.setdefault(g.run_against, {})
        if g.name not in newest or g.seq > newest[g.name].seq:
            newest[g.name] = g
    streak = 0
    for verdicts in sorted(
        trees.values(), key=lambda v: max(g.seq for g in v.values()), reverse=True
    ):
        statuses = {g.status for g in verdicts.values()}
        if (
            GateStatus.FAIL not in statuses
            or GateStatus.ERROR in statuses
            or next(iter(verdicts.values())).run_against == since
        ):
            break
        streak += 1
    return streak


def no_fix_progress(state: FactoryState, config: RunnableConfig, *, boundary: str) -> bool:
    """True iff the newest tree fails ``boundary`` on exactly what the tree before it
    failed on, after a fix pass in between: the fixer made no progress.

    Only once this boundary's budget window has spent a fix (two failing checks in
    it), so a resumed run's fresh budget re-check never counts as a pass."""
    if _fix_attempts(state, config) < 2:
        return False
    names = _BOUNDARY_GATES[boundary]
    trees: dict[str, dict[str, GateResult]] = {}
    for g in sorted((g for g in state.get("gates") or () if g.name in names), key=lambda g: g.seq):
        trees.setdefault(g.run_against, {})[g.name] = g
    order = sorted(trees.values(), key=lambda v: max(g.seq for g in v.values()))
    if len(order) < 2:
        return False
    prev, cur = (_failure_signature(v.values()) for v in order[-2:])
    return bool(cur) and cur == prev


def bump_fix_attempts(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int,
    total_cap: int | None = None,
    fix_policy: FixPolicy | None = None,
) -> None:
    """Bump ``budget.fix_attempts_since_all_green`` on ``out`` when ``boundary``
    will route ``fix`` after ``out`` folds in — and, outside the Spec boundary, the
    run-wide ``fix_attempts_total`` that ``total_cap`` bounds.

    The gate node writing ``out`` is the sole ``budget`` writer of its superstep, and
    the conditional edge that fires in that same superstep reads the post-fold state.
    So the decision folds the prior ``gates`` with the node's delta through the REAL
    reducer — never a hand-assembled subset — so it can never diverge from the edge's
    read (E-R1-01)."""
    ledger = state.get("budget") or BudgetLedger()
    post: FactoryState = {
        "gates": merge_gates(state.get("gates"), out.get("gates")),
        "fix_passes": union_fix_passes(state.get("fix_passes"), out.get("fix_passes")),
        "budget": ledger,
    }
    decision = route_after_gates(
        post,
        config,
        boundary=boundary,
        records=records,
        max_fix_attempts=max_fix_attempts,
        total_cap=total_cap,
        fix_policy=fix_policy,
    )
    if decision == FIX:
        out["budget"] = ledger.model_copy(
            update={
                "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + 1,
                "fix_attempts_total": ledger.fix_attempts_total + (boundary != SPEC_BOUNDARY),
            }
        )


def settle_fix_attempts(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    records: Any,
    max_fix_attempts: int,
    total_cap: int | None = None,
    fix_policy: FixPolicy | None = None,
) -> None:
    """:func:`bump_fix_attempts`, plus a reset to 0 when ``boundary`` ADVANCEs: a
    green boundary hands the next one the whole budget, so the attempts one
    boundary spent (a Spec re-plan, an earlier wave's repair) never leave a later
    failure with fewer fixes. A spent bound (HALT) keeps the ledger unchanged."""
    bump_fix_attempts(
        state,
        config,
        out,
        boundary=boundary,
        records=records,
        max_fix_attempts=max_fix_attempts,
        total_cap=total_cap,
        fix_policy=fix_policy,
    )
    ledger = state.get("budget") or BudgetLedger()
    if "budget" in out or not ledger.fix_attempts_since_all_green:
        return
    post: FactoryState = {
        "gates": merge_gates(state.get("gates"), out.get("gates")),
        "budget": ledger,
    }
    if (
        route_after_gates(
            post,
            config,
            boundary=boundary,
            records=records,
            max_fix_attempts=max_fix_attempts,
            total_cap=total_cap,
        )
        == ADVANCE
    ):
        out["budget"] = ledger.model_copy(update={"fix_attempts_since_all_green": 0})
