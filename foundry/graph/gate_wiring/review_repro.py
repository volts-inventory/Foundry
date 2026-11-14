"""Attest a review's backed findings by reproducing them (:mod:`foundry.gates.repro`).

The review verdict is a model's word: it never fails a run alone, so the final
boundary ignores it. A finding the proxy kept as blocking on a reproduction
(``[evidence: `cmd` expected 'C', got 'O']``) is re-run here against the final
tree; a reproduced one is an attested ``repro`` FAIL, which the supervisor sends
to the fixer at most ``max_review_fixes`` times (:func:`route_review_check`) and
which fails the release (``factory_graph.terminal_gates_pass``) while it stands.
A check that cannot run them (an ERROR, after one retry) holds the release too: a
blocking finding with a reproduction never ships unverified.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING, Any

from foundry.gates.repro import REPRO_GATE, REPRODUCED_PREFIX, Reproduction, ReproGate, tag_line
from foundry.graph.gate_wiring.routing import ADVANCE, FIX, HALT
from foundry.graph.gate_wiring.runs import (
    _GATE_TIMEOUT_S,
    _build_env,
    _run_gates,
    gate_profile,
    launch_gate,
)
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus
from foundry.schemas.quality import ReviewFindings

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.durability.artifact_store import ArtifactStore
    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState

REVIEW_CHECK_NODE = "review_check"
# Each re-run can boot the app and wait on a command: keep one check bounded.
_MAX_REPRODUCTIONS = 5
_ERROR_RETRIES = 1

_STR = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""
# The proxy's evidence suffix (model_provider_proxy._verdict_findings), which the
# repro gate's own reproduced lines repeat so a later check can re-run them.
_EVIDENCE = re.compile(
    rf"^(?P<text>.*) \[evidence: `(?P<cmd>.+)` expected (?P<correct>{_STR}), "
    rf"got (?P<observed>{_STR})\]$",
    re.S,
)


def reproductions(findings: Iterable[str]) -> tuple[Reproduction, ...]:
    """The findings backed by a reproduction command, deduplicated, at most
    :data:`_MAX_REPRODUCTIONS`."""
    out: dict[tuple[str, str, str], Reproduction] = {}
    for line in findings:
        m = _EVIDENCE.match(line)
        if m is None:
            continue
        try:
            correct, observed = ast.literal_eval(m["correct"]), ast.literal_eval(m["observed"])
        except (ValueError, SyntaxError):
            continue
        key = (m["cmd"], str(correct), str(observed))
        if key not in out:
            out[key] = Reproduction(
                finding=m["text"].removeprefix(REPRODUCED_PREFIX),
                command=m["cmd"],
                correct=str(correct),
                observed=str(observed),
            )
    return tuple(out.values())[:_MAX_REPRODUCTIONS]


def _newest(state: FactoryState, name: str) -> GateResult | None:
    return max(
        (g for g in state.get("gates") or () if g.name == name),
        key=lambda g: g.seq,
        default=None,
    )


def _newest_review(state: FactoryState, cas: ArtifactStore | None) -> ReviewFindings | None:
    """The newest review's findings record. Read off the reviewer's
    ``review_findings`` artifact, not the ``gates`` channel: a request-changes
    verdict is negative-latched as it is committed, and ``merge_gates`` then drops
    it from the channel. Its findings are only the commands to re-run; what can
    fail the run is this gate's attested re-run. The reviewer stores it in the
    run's CAS (``rt.cas``); without one there is nothing to read."""
    from foundry.agents.quality.reviewer import REVIEW_FINDINGS_KIND

    handles = [a for a in state.get("artifacts") or () if a.kind == REVIEW_FINDINGS_KIND]
    if cas is None or not handles:
        return None
    body = cas.resolve(max(handles, key=lambda a: a.seq))
    return body if isinstance(body, ReviewFindings) else None


def review_candidates(
    state: FactoryState, cas: ArtifactStore | None
) -> tuple[str, tuple[Reproduction, ...]]:
    """The newest review's tag and what to re-run: its own backed reproductions (a
    request-changes) plus those the last check reproduced, so a re-review that drops
    an unfixed finding cannot clear it."""
    review = _newest_review(state, cas)
    own = (
        reproductions(review.blocking)
        if review is not None and review.status == GateStatus.FAIL.value
        else ()
    )
    # The newest check that decided: one that ERRORed (a hang, no driver result)
    # cleared nothing, so the findings reproduced before it still carry.
    last = max(
        (
            g
            for g in state.get("gates") or ()
            if g.name == REPRO_GATE and g.status is not GateStatus.ERROR
        ),
        key=lambda g: g.seq,
        default=None,
    )
    carried = reproductions(last.findings) if last and last.status is GateStatus.FAIL else ()
    merged = {(r.command, r.correct, r.observed): r for r in (*carried, *own)}
    tag = f"review verdict #{review.verdict_seq.emission_epoch}" if review else "no review"
    return tag, tuple(merged.values())[:_MAX_REPRODUCTIONS]


def _checked(state: FactoryState, tag: str, tree: str | None) -> bool:
    return any(
        g.name == REPRO_GATE and g.run_against == tree and g.findings[:1] == (tag_line(tag),)
        for g in state.get("gates") or ()
    )


def review_check_pending(state: FactoryState, cas: ArtifactStore | None) -> bool:
    """True iff the newest review left reproductions not yet re-run on the tree the
    final gates judged last (the newest build verdict's)."""
    tag, repros = review_candidates(state, cas)
    build = _newest(state, "build")
    return bool(repros) and not _checked(state, tag, build.run_against if build else None)


def run_review_check(
    state: FactoryState,
    config: RunnableConfig,
    *,
    rt: AgentRuntime,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    workdir: Path,
    profile: VerificationProfile | None = None,
) -> dict[str, Any]:
    """Re-run the candidates on the final tree through the runner: ``{"gates":
    [<attested repro verdict>]}``. An http app is started the way the launch gate
    starts it; any other runs the commands alone."""
    from foundry.gates.launch import LaunchGate
    from foundry.graph.gate_wiring.materialize import materialize_workdir

    tag, repros = review_candidates(state, rt.cas)
    run_against = materialize_workdir(state, workdir, resolve=resolve, resolve_blob=resolve_blob)
    profile = gate_profile(state, profile)
    results: list[GateResult] = []
    # An ERROR is no verdict, and while backed blocking findings stand it holds the
    # release (``terminal_gates_pass``): a transient boot or install failure gets one
    # more run first.
    for _ in range(1 + _ERROR_RETRIES):
        with _build_env(profile, workdir, rt) as pyenv:
            launch = launch_gate(state, resolve, profile, runner.seal_key, pyenv)
            target = launch.target if isinstance(launch, LaunchGate) else None
            app = target.cmd if target is not None and target.kind == "http" else ""
            # The gate installs the deps into ``pyenv`` itself: the run's venv for this
            # manifest, which the final gates' build already filled (reused, no pip).
            gate = ReproGate(
                repros,
                tag=tag,
                app_cmd=app,
                seal_key=runner.seal_key,
                pyenv=pyenv,
                install_timeout_s=_GATE_TIMEOUT_S,
            )
            results = _run_gates(
                [(gate, REVIEW_CHECK_NODE)],
                rt=rt,
                runner=runner,
                config=config,
                run_against=run_against,
                workdir=workdir,
            )
        if not any(g.status is GateStatus.ERROR for g in results):
            break
    return {"gates": results}


def route_review_check(state: FactoryState, *, max_review_fixes: int) -> str:
    """After a review check: a reproduced finding goes to the fixer while
    ``budget.review_fixes`` (bumped by :func:`bump_review_fixes` for every
    reproduced check, the current one included) is within ``max_review_fixes``,
    then halts; a check that reproduced nothing advances. One that ERRORed (after
    its retry) halts: its backed findings are unverified, which holds the release."""
    last = _newest(state, REPRO_GATE)
    if last is not None and last.status is GateStatus.ERROR:
        return HALT
    if last is None or last.status is not GateStatus.FAIL:
        return ADVANCE
    budget = state.get("budget") or BudgetLedger()
    return FIX if budget.review_fixes <= max_review_fixes else HALT


def bump_review_fixes(state: FactoryState, out: dict[str, Any], *, max_review_fixes: int) -> None:
    """Count a reproduced check on ``out``'s budget; when it gets a fix pass, count
    that pass in the shared fix counters too, as any other gate's would be."""
    if not any(g.name == REPRO_GATE and g.status is GateStatus.FAIL for g in out["gates"]):
        return
    ledger = state.get("budget") or BudgetLedger()
    fixes = ledger.review_fixes + 1
    fixing = fixes <= max_review_fixes
    out["budget"] = ledger.model_copy(
        update={
            "review_fixes": fixes,
            "fix_attempts_since_all_green": ledger.fix_attempts_since_all_green + fixing,
            "fix_attempts_total": ledger.fix_attempts_total + fixing,
        }
    )
