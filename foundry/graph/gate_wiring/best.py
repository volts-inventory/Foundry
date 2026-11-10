"""Keep the fix loop on its best tree (:func:`keep_best`) and materialize it."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.graph.gate_wiring.diffs import MaterializationError, _contained_target
from foundry.graph.gate_wiring.fix_policy import FIX_STOP_TRIAGE, FixPolicy, fix_stop
from foundry.graph.gate_wiring.ledger import (
    _FIX_PASS_NODE,
    _PASS_KEY,
    CARRIED_STOP,
    REVERT_SKIPPED_TRIAGE,
    WAVE_ADVANCE_STOPS,
    _failing,
    _newest_per_name,
    _outcome,
    _pass_fix,
    _pass_order,
    _snapshot,
    best_entry,
    fix_score,
    fix_window,
    regressions,
    restore_diff,
    snapshot_files,
    typecheck_only,
)
from foundry.graph.gate_wiring.materialize import (
    _scaffold_manifest,
    _text_files,
    materialize_workdir,
)
from foundry.graph.gate_wiring.restarts import (
    cluster_owners,
    failure_clusters,
    plan_next_pass,
    restart_set,
    unbuilt_restart,
)
from foundry.graph.gate_wiring.routing import _BOUNDARY_GATES, _fix_attempts
from foundry.schemas.gates import FixPass, GateTriage
from foundry.telemetry import count

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


def keep_best(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    scope: frozenset[str],
    rt: AgentRuntime,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    rerun: Callable[[FactoryState], dict[str, Any]],
    policy: FixPolicy | None = None,
    unbuilt_waves: int | None = None,
    carried: frozenset[str] = frozenset(),
) -> bool:
    """Record the check ``out`` holds in the fix-pass ledger and, when its tree is
    worse than the window's best, restore the best tree: a ``revert-<n>`` diff on
    ``out``, the gates re-run on the restored tree through ``rerun`` (so the
    runner-signed newest verdicts are the restored tree's), both kept only if the
    re-run's digest is the best tree's. Returns True iff the tree was restored.

    The new entries are stamped with the run clock once the check (and any
    restore) is done, the pass's own entry with its cost since the window's
    previous check: the fixer session, the check and the restore. Under a
    fix-until-cap ``policy`` a failing check whose loop must stop (:func:`fix_stop`;
    ``unbuilt_waves`` set for a wave check) gets the rule on its newest entry and a
    :data:`FIX_STOP_TRIAGE` note naming it. ``carried`` (a wave check with waves
    left, :func:`advanced_from`): failures an earlier wave advanced with. A
    check failing on those alone stops (:data:`CARRIED_STOP`), and no pass is
    aimed at a cluster of them while the wave has failures of its own.

    Call before the budget is settled, on ``out``'s own verdicts."""
    known = len(out.get("fix_passes") or ())
    window = fix_window(state, boundary, scope)
    prior = [p for p in state.get("fix_passes") or () if p.window == window]
    restored = _keep_best(
        state,
        config,
        out,
        boundary=boundary,
        scope=scope,
        rt=rt,
        resolve=resolve,
        resolve_blob=resolve_blob,
        rerun=rerun,
    )
    new = list((out.get("fix_passes") or [])[known:])
    if not new:
        return restored
    # The meter's clock, unlike a process clock, survives a resume and leaves out
    # the time the run was stopped.
    meter = policy._meter() if policy is not None else getattr(rt.proxy, "token_meter", None)
    clock = float(meter.elapsed_s) if meter is not None else None
    last = max(prior, key=_pass_order).clock_s if prior else None
    elapsed = clock - last if clock is not None and last is not None and clock >= last else None
    # The pass this check judges was aimed at the cluster the last check planned; a
    # fresh budget's first check (a resume) judges no pass.
    aimed = max(prior, key=_pass_order).target if prior else None
    new = [
        p.model_copy(
            update={
                "clock_s": clock,
                "elapsed_s": elapsed if i == 0 else None,
                "cluster": aimed if i == 0 and not p.restart else None,
            }
        )
        for i, p in enumerate(new)
    ]
    newest = new[-1]
    until_cap = policy is not None and policy.until_cap
    passes = sorted([*prior, *new], key=_pass_order)
    stop = None
    if until_cap and (newest.score is None or any(newest.score[1:])):
        assert policy is not None
        stop = fix_stop(passes, policy, unbuilt_waves=unbuilt_waves)
        if (
            stop is None
            and carried
            and unbuilt_waves
            and newest.score is not None
            and set(newest.failing) <= carried
        ):
            stop = (
                CARRIED_STOP,
                "this wave's checks fail only on what the wave it builds on advanced with",
            )
    if newest.score is not None and any(newest.score[1:]):
        from foundry.agents.implementation.tickets import read_tickets

        names = _BOUNDARY_GATES[boundary]
        verdicts = _newest_per_name(g for g in out.get("gates") or () if g.name in names)
        clusters = failure_clusters(
            verdicts.values(), read_tickets(state, resolve), cluster_owners(state)
        )
        # An earlier wave's leftovers already had that wave's share: this wave's
        # passes go to its own failures, the final gates to the rest.
        clusters = tuple(c for c in clusters if not set(c.items) <= carried) or clusters
        # A pass that got a stage further may have broken ids the best tree passed:
        # the next pass goes after those first.
        before = best_entry(prior, new[0].window)
        if new[0].outcome == "improved" and before is not None:
            broke = set(regressions(before.failing, new[0].failing))
            clusters = tuple(sorted(clusters, key=lambda c: not broke & set(c.items)))
        dead_end = stop is not None and stop[0] in _DEAD_ENDS
        restarts = policy is None or policy.restarts
        # Fixing until the cap a cluster gets two repairs before its restart. The
        # legacy budget gives a loop two passes: the restart takes the second when
        # the first did not improve the tree. A skipped ticket is built at once.
        target, restart = plan_next_pass(
            passes,
            clusters,
            restart_after=2 if until_cap else 1,
            dead_end=dead_end,
            restartable=lambda c: (
                restart_set(state, resolve, c.tickets, tests_ok=c.in_tests) if restarts else ()
            ),
            unimproved_only=not until_cap,
            unbuilt=lambda c: unbuilt_restart(state, resolve, c) if restarts else (),
        )
        if restart and dead_end:
            stop = None
        newest = newest.model_copy(
            update={"target": target, "restart_tickets": () if stop else restart}
        )
        new[-1] = newest
    if stop is not None:
        rule, why = stop
        count(f"fix_stop.{rule}")
        if rule in WAVE_ADVANCE_STOPS and unbuilt_waves:
            why += "; the run moves on to the next wave and the final gates fix the rest"
        new[-1] = newest.model_copy(update={"stop": rule})
        out.setdefault("gate_triage", []).append(
            GateTriage(
                name=FIX_STOP_TRIAGE,
                run_against=newest.tree,
                explanation=f"the fix loop stops ({rule}): {why}"[:2000],
                produced_by=_FIX_PASS_NODE,
                seq=rt.seq(config, _FIX_PASS_NODE, FIX_STOP_TRIAGE),
            )
        )
    out["fix_passes"][known:] = new
    return restored


def _keep_best(
    state: FactoryState,
    config: RunnableConfig,
    out: dict[str, Any],
    *,
    boundary: str,
    scope: frozenset[str],
    rt: AgentRuntime,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    rerun: Callable[[FactoryState], dict[str, Any]],
) -> bool:
    names = _BOUNDARY_GATES[boundary]
    verdicts = _newest_per_name(g for g in out.get("gates") or () if g.name in names)
    if not verdicts:
        return False
    tree = next(iter(verdicts.values())).run_against
    window = fix_window(state, boundary, scope)
    prior = sorted(
        (p for p in state.get("fix_passes") or () if p.window == window), key=_pass_order
    )
    score = fix_score(verdicts, names)
    best = best_entry(prior, window)
    failing = _failing(verdicts)
    # A type error alone is not a broken check that reverts the pass: the score
    # already counts it, and the final gates still require the build green.
    judged = (
        tuple(f for f in failing if not f.startswith("build:"))
        if typecheck_only(verdicts.get("build"))
        else failing
    )
    outcome = _outcome(prior, tree, score, best, judged)
    fix_task, touched = _pass_fix(state, prior, resolve, resolve_blob)
    pass_no = max((p.pass_no for p in prior), default=-1) + 1
    snapshot = None
    # An all-PASS tree ends the loop (it advances), so it never needs restoring.
    if (
        score is not None
        and any(score[1:])
        and outcome != "worse"
        and (best is None or score <= tuple(best.score or ()))
    ):
        snapshot = _snapshot(state, prior, tree, rt=rt, resolve=resolve, resolve_blob=resolve_blob)
    passes = [
        FixPass(
            window=window,
            boundary=boundary,
            pass_no=pass_no,
            tree=tree,
            fix_task=fix_task,
            score=score,
            failing=failing,
            outcome=outcome,
            touched=touched,
            snapshot_uri=snapshot,
            restart=_fix_attempts(state, config) == 0,
            seq=rt.seq(config, _FIX_PASS_NODE, _PASS_KEY),
        )
    ]
    out.setdefault("fix_passes", []).extend(passes)
    # A tree worse than a failing best is never all-PASS, so it never advances.
    if outcome != "worse" or best is None or best.snapshot_uri is None:
        return False
    try:
        revert = restore_diff(
            state, best, config, tree=tree, rt=rt, resolve=resolve, resolve_blob=resolve_blob
        )
        if revert is None:
            return False
        again = rerun({**state, "artifacts": [*(state.get("artifacts") or ()), revert]})
    except MaterializationError:
        return False
    restored = _newest_per_name(g for g in again.get("gates") or () if g.name in names)
    if not restored or any(g.run_against != best.tree for g in restored.values()):
        got = next(iter(restored.values())).run_against if restored else "no verdict"
        out.setdefault("gate_triage", []).append(
            GateTriage(
                name=REVERT_SKIPPED_TRIAGE,
                run_against=tree,
                explanation=(
                    f"this pass left the tree worse than the best one ({best.tree}), but "
                    f"restoring it built {got}: the newer tree is kept"
                )[:2000],
                produced_by=_FIX_PASS_NODE,
                seq=rt.seq(config, _FIX_PASS_NODE, f"{REVERT_SKIPPED_TRIAGE}:{tree}"),
            )
        )
        return False
    out["artifacts"] = [*(out.get("artifacts") or ()), revert]
    out["gates"] = [*(out.get("gates") or ()), *(again.get("gates") or ())]
    if again.get("gate_triage"):
        out.setdefault("gate_triage", []).extend(again["gate_triage"])
    out["fix_passes"].append(
        FixPass(
            window=window,
            boundary=boundary,
            pass_no=pass_no + 1,
            tree=best.tree,
            fix_task=revert.task_id,
            score=fix_score(restored, names),
            failing=_failing(restored),
            outcome="restored",
            snapshot_uri=best.snapshot_uri,
            seq=rt.seq(config, _FIX_PASS_NODE, _PASS_KEY),
        )
    )
    return True


# fix_stop rules that mean the fixer is stuck, not out of time: a restart that is
# still available is tried before the loop stops on one.
_DEAD_ENDS = frozenset({"no-edit", "revisit", "same-signature", "plateau"})


def materialize_best(
    state: FactoryState,
    dest: Path,
    best: FixPass,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> None:
    """Write ``best``'s tree into ``dest``: the integrated tree with the snapshot laid
    over it (binary files the snapshot leaves out stay as integrated), then the
    dependency manifest scaffolded as for the gates."""
    assert best.snapshot_uri is not None
    materialize_workdir(
        state, dest, resolve=resolve, resolve_blob=resolve_blob, scaffold_manifest=False
    )
    files = snapshot_files(best.snapshot_uri, resolve_blob)
    for rel in set(_text_files(dest)) - set(files):
        _contained_target(dest, rel).unlink()
    for rel, content in files.items():
        target = _contained_target(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    stack = state.get("stack")
    if stack is None or stack.language == "python":
        _scaffold_manifest(dest)
