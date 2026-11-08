"""The per-wave integration check the implementation subgraph runs."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.graph.gate_wiring.best import keep_best
from foundry.graph.gate_wiring.diffs import MaterializationError, _diff_ops
from foundry.graph.gate_wiring.fix_policy import FixPolicy
from foundry.graph.gate_wiring.ledger import (
    WAVE_ADVANCE_STOPS,
    advanced_from,
    fix_window,
    revisited_tree,
)
from foundry.graph.gate_wiring.materialize import FEATURE_DIFF_KIND, _of_kind, _resolve_typed
from foundry.graph.gate_wiring.restarts import (
    fizzled_restart,
    pending_restarts,
    restart_cluster,
    restart_set,
)
from foundry.graph.gate_wiring.routing import (
    ADVANCE,
    BUILD_BOUNDARY,
    BUILD_TEST_BOUNDARY,
    ESCALATE,
    FIX,
    HALT,
    RESTART,
    _fix_attempts,
    _unchanged_tree_recheck,
    failing_streak,
    failure_files,
    fix_cap,
    no_fix_progress,
    route_after_gates,
    settle_fix_attempts,
)
from foundry.graph.gate_wiring.runs import (
    _BUILD_NODE,
    _wave_test_gate,
    gate_profile,
    run_build_gate,
    wave_scenarios,
)
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import GateResult, GateStatus, GateTriage
from foundry.schemas.implementation import FeatureDiff
from foundry.state.reducers import merge_gates, union_fix_passes

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langchain_core.runnables import RunnableConfig

    from foundry.gates.profiles import VerificationProfile
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver
    from foundry.state.factory_state import FactoryState


@dataclass(frozen=True)
class WaveBuildCheck:
    """The per-wave integration check (plan §2b) the implementation subgraph runs
    between ticket waves: the attested Build gate on the integrated tree, through
    the SAME runner as the supervisor's gates. Each wave has its own fix budget: a
    passing check is a green state for that boundary and resets the counter, so wave
    repairs never eat the next wave's or the final build/test gate's budget. A wave
    gets ``max_fix_attempts - 1`` fixer passes (the default 3 gives 2), and every
    build-side fix also counts toward :func:`fix_cap`, the run-wide bound.

    In hybrid mode a check that fails twice in a row asks for the newest wave's
    local tickets (those the failures name, else all of them) to re-run once on
    the cloud level (``escalate-request`` triage; :data:`ESCALATE` routes back to
    the engineering node), before any further fixer pass or a halt. An
    escalation spends none of the wave's fix attempts. A ticket that can still be
    restarted (:func:`restart_set`) is first rebuilt once on its own tier instead
    (:data:`RESTART`, a fix pass like any other); it escalates if that fails too. A
    ticket whose
    kept local session stopped early (turn cap, repeat/read-thrash) is asked for on
    the FIRST failure that names it (or, alone in its wave, on any first failure).

    With ``check_scenarios`` the check also runs the Test gate scoped to the
    scenarios whose tickets are all built (:func:`wave_scenarios`), so an unbound or
    failing scenario reaches the wave's fixer instead of piling up for the final gate.

    ``run`` is the node (verdict + budget bump); ``route`` returns
    :data:`ADVANCE` / :data:`FIX` / :data:`HALT` for the :data:`BUILD_BOUNDARY`, or
    :data:`BUILD_TEST_BOUNDARY` when the check judged scenarios."""

    rt: AgentRuntime
    runner: TwoZoneGateRunner
    resolve: ArtifactResolver
    resolve_blob: BlobResolver
    max_fix_attempts: int = 3
    profile: VerificationProfile | None = None
    check_scenarios: bool = True
    # The engineering node's difficulty -> tier table (escalation picks local tickets).
    ticket_tiers: Mapping[Any, str] | None = None
    fix_policy: FixPolicy = FixPolicy()  # noqa: RUF009 - frozen, so a shared default is safe

    @property
    def _policy(self) -> FixPolicy:
        return self.fix_policy.bound(self.rt)

    def _unbuilt_waves(self, state: FactoryState) -> int:
        from foundry.agents.implementation.tickets import (
            built_task_ids,
            next_wave,
            read_tickets,
            task_id_for_ticket,
        )

        tickets = read_tickets(state, self.resolve)
        done = set(built_task_ids(state))
        waves = 0
        while wave := next_wave(tickets, done):
            done.update(task_id_for_ticket(t.id) for t in wave)
            waves += 1
        return waves

    def _scenarios(self, state: FactoryState) -> frozenset[str]:
        if not self.check_scenarios:
            return frozenset()
        scope = wave_scenarios(state, self.resolve)
        if (
            not scope
            or _wave_test_gate(gate_profile(state, self.profile), self.runner, scope) is None
        ):
            return frozenset()
        return scope

    def _check(
        self, state: FactoryState, config: RunnableConfig, scenarios: frozenset[str]
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="foundry_wave_gate_") as tmp:
            return run_build_gate(
                state,
                config,
                rt=self.rt,
                runner=self.runner,
                resolve=self.resolve,
                resolve_blob=self.resolve_blob,
                workdir=Path(tmp),
                profile=self.profile,
                scenarios=scenarios,
            )

    def run(self, state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        scenarios = self._scenarios(state)
        out = self._check(state, config, scenarios)
        boundary = BUILD_TEST_BOUNDARY if scenarios else BUILD_BOUNDARY
        # Escalation and the no-progress note judge this pass, not a restored tree.
        own: list[GateResult] = list(out.get("gates") or ())
        window = fix_window(state, boundary, scenarios)
        unbuilt = self._unbuilt_waves(state)
        base = advanced_from(state, window) if self.fix_policy.until_cap else None
        reverted = keep_best(
            state,
            config,
            out,
            boundary=boundary,
            scope=scenarios,
            rt=self.rt,
            resolve=self.resolve,
            resolve_blob=self.resolve_blob,
            rerun=lambda s: self._check(s, config, scenarios),
            policy=self._policy,
            unbuilt_waves=unbuilt,
            carried=frozenset(base.failing) if base else frozenset(),
        )
        settle_fix_attempts(
            state,
            config,
            out,
            boundary=boundary,
            records=self.runner.records,
            max_fix_attempts=self.max_fix_attempts,
            total_cap=fix_cap(state, self.resolve, self.max_fix_attempts),
            fix_policy=self._policy,
        )
        # The whole state, not just gates + budget: _route's fix_cap counts the
        # plan's waves off its tickets, and with none it read as a cap of
        # max_fix_attempts, so after that many fixes run-wide this check routed HALT
        # here (while route() saw FIX) and never recorded a fix pass that changed
        # nothing — the wave spent its last pass on the unchanged tree.
        post: FactoryState = {
            **state,
            "gates": merge_gates(state.get("gates"), out.get("gates")),
            "fix_passes": union_fix_passes(state.get("fix_passes"), out.get("fix_passes")),
            "budget": out.get("budget") or state.get("budget") or BudgetLedger(),
        }
        decision = self._route(post, config, boundary)
        judged: FactoryState = {**post, "gates": merge_gates(state.get("gates"), own)}
        # The trees before the wave this one builds on advanced are not this wave's
        # failures to escalate on.
        streak = (
            failing_streak(judged, boundary=boundary, since=base.tree if base else None)
            if decision != ADVANCE
            else 0
        )
        picked: tuple[str, ...] = ()
        newest = (out.get("fix_passes") or [None])[-1]
        restarting = decision == FIX and newest is not None and bool(newest.restart_tickets)
        if self._advances(post, decision, window, unbuilt):
            # No escalation either: its rebuild and re-check would open a new window
            # with a new share, the overrun the share exists to prevent. A boundary
            # left behind hands the next one a whole budget, as a green one does.
            ledger = out.get("budget") or state.get("budget") or BudgetLedger()
            out["budget"] = ledger.model_copy(update={"fix_attempts_since_all_green": 0})
        elif streak >= 1 and not restarting:
            from foundry.agents.implementation.tickets import escalation_pick, stopped_tickets
            from foundry.execution.adapters.tiered import (
                ESCALATE_FALLBACK_TRIAGE,
                ESCALATE_REQUEST_TRIAGE,
            )

            failed = ", ".join(
                sorted({g.name for g in own if g.status in (GateStatus.FAIL, GateStatus.ERROR)})
            )
            # A ticket whose local session stopped early escalates on the first
            # failure; any other waits for a second.
            stops = stopped_tickets(state)
            picked, fallback = escalation_pick(
                {**state, "gates": judged["gates"]},
                self.resolve,
                self.rt,
                self.ticket_tiers,
                stopped_only=streak < 2,
            )
            # Restart once on the ticket's own tier before moving it up a tier (a
            # stopped-early session escalates at once: its tier ran out of turns).
            if (
                streak >= 2
                and picked
                and not fallback
                and decision == FIX
                and self.fix_policy.restarts
                and newest is not None
                and (again := restart_set(state, self.resolve, picked))
            ):
                # The rebuild is credited to the cluster it rebuilds (FixPass.cluster
                # of the next check), not to the one the fixer was aimed at.
                out["fix_passes"][-1] = newest.model_copy(
                    update={
                        "restart_tickets": again,
                        "target": restart_cluster(state, self.resolve, own, boundary, again),
                    }
                )
                picked, restarting = (), True
            for tid in picked:
                why = (
                    f"the wave check failed twice in a row ({failed})"
                    if streak >= 2
                    else f"the wave check failed ({failed}) after the local session "
                    f"stopped early: {stops[tid]}"
                )
                out.setdefault("gate_triage", []).append(
                    GateTriage(
                        name=ESCALATE_REQUEST_TRIAGE,
                        run_against=f"task/{tid}",
                        explanation=why[:2000],
                        produced_by=_BUILD_NODE,
                        seq=self.rt.seq(config, _BUILD_NODE, f"{ESCALATE_REQUEST_TRIAGE}:{tid}"),
                    )
                )
                if fallback:
                    out["gate_triage"].append(
                        GateTriage(
                            name=ESCALATE_FALLBACK_TRIAGE,
                            run_against=f"task/{tid}",
                            explanation="no failure named a ticket: the whole wave escalates",
                            produced_by=_BUILD_NODE,
                            seq=self.rt.seq(
                                config, _BUILD_NODE, f"{ESCALATE_FALLBACK_TRIAGE}:{tid}"
                            ),
                        )
                    )
        if picked:
            # This check routes ESCALATE, not FIX: the rebuild is the ticket's extra
            # attempt and must leave the wave's fixer passes unspent.
            out.pop("budget", None)
        # Fixing until the cap, a pass that made no progress counts toward the
        # dead-end rules (fix_stop) instead of halting the wave on its own.
        elif (
            decision == FIX
            and not self.fix_policy.until_cap
            and not restarting
            and not self.capped_off_target({**state, "gates": post["gates"]})
            and not fizzled_restart(post, self.resolve)
            and (
                revisited_tree(state, out)
                or (
                    not reverted
                    and (
                        no_fix_progress(post, config, boundary=boundary)
                        or _unchanged_tree_recheck(state, out, config, boundary=boundary)
                    )
                )
            )
        ):
            # After a restore the newest build is the restored tree's: route() reads
            # the note off the current tree.
            build = max((g for g in out["gates"] if g.name == "build"), key=lambda g: g.seq)
            out.setdefault("gate_triage", []).append(
                GateTriage(
                    name=NO_FIX_PROGRESS_TRIAGE,
                    run_against=build.run_against,
                    explanation="the last fix pass left the same checks failing; the wave "
                    "stops fixing here instead of spending the remaining attempts",
                    produced_by=_BUILD_NODE,
                    seq=self.rt.seq(
                        config, _BUILD_NODE, f"{NO_FIX_PROGRESS_TRIAGE}:{build.run_against}"
                    ),
                )
            )
        return out

    def _route(self, state: FactoryState, config: RunnableConfig, boundary: str) -> str:
        return route_after_gates(
            state,
            config,
            boundary=boundary,
            records=self.runner.records,
            max_fix_attempts=self.max_fix_attempts,
            total_cap=fix_cap(state, self.resolve, self.max_fix_attempts),
            fix_policy=self._policy,
        )

    def route(self, state: FactoryState, config: RunnableConfig) -> str:
        """:func:`route_after_gates`, except a pending escalation request routes
        :data:`ESCALATE`, a planned ticket restart (:func:`pending_restarts`)
        routes :data:`RESTART`, and a fix pass that made no progress
        (:func:`no_fix_progress`, or the :data:`NO_FIX_PROGRESS_TRIAGE` ``run``
        recorded on this tree for a fix that changed nothing) halts as a spent bound
        does — unless fixing until the cap, where the dead-end rules decide."""
        from foundry.agents.implementation.tickets import pending_escalations

        scenarios = self._scenarios(state)
        boundary = BUILD_TEST_BOUNDARY if scenarios else BUILD_BOUNDARY
        decision = self._route(state, config, boundary)
        if self._advances(
            state, decision, fix_window(state, boundary, scenarios), self._unbuilt_waves(state)
        ):
            return ADVANCE
        if decision != ADVANCE and pending_escalations(state, self.resolve):
            return ESCALATE
        if decision == FIX and pending_restarts(state, self.resolve):
            return RESTART
        if (
            decision == FIX
            and not self.fix_policy.until_cap
            and not self.capped_off_target(state)
            and not fizzled_restart(state, self.resolve)
            and (
                no_fix_progress(state, config, boundary=boundary)
                or self._stopped_on_current_tree(state, config)
            )
        ):
            return HALT
        return decision

    @staticmethod
    def _advances(state: FactoryState, decision: str, window: str, unbuilt: int) -> bool:
        """True iff a spent loop moves on to the next wave instead of halting the
        run: its newest check stopped on a :data:`WAVE_ADVANCE_STOPS` rule with a
        wave still to build."""
        if decision != HALT or not unbuilt:
            return False
        passes = [p for p in state.get("fix_passes") or () if p.window == window]
        return bool(passes) and max(passes, key=lambda p: p.seq).stop in WAVE_ADVANCE_STOPS

    def capped_off_target(self, state: FactoryState) -> bool:
        """True iff the newest fix pass ran out of turns without editing any file
        the current failure names (:func:`failure_files`): it never reached the
        fix, so an unchanged failure is not "no progress" and the next pass runs,
        told to edit those files first. Still bounded: that pass spends a fix
        attempt like any other."""
        fixes = [
            a
            for a in _of_kind(state, FEATURE_DIFF_KIND)
            if a.task_id and a.task_id.startswith("fix-")
        ]
        if not fixes:
            return False
        prev = max(fixes, key=lambda a: a.seq)
        # proxy_agent's note for a session that hit packet.turn_budget.
        if not any(
            t.run_against == f"task/{prev.task_id}"
            and t.explanation.startswith("turn budget exhausted")
            for t in state.get("gate_triage") or ()
        ):
            return False
        from foundry.agents.implementation.junior_engineer import _failed_gates

        named = set(failure_files(_failed_gates(state)))
        if not named:
            return False
        try:
            ops = _diff_ops(
                _resolve_typed(prev, self.resolve, FeatureDiff, FEATURE_DIFF_KIND),
                self.resolve_blob,
            )
        except MaterializationError:
            return False
        return not named & {*ops.writes, *ops.deletes, *ops.patches}

    @staticmethod
    def _stopped_on_current_tree(state: FactoryState, config: RunnableConfig) -> bool:
        builds = [g for g in state.get("gates") or () if g.name == "build"]
        if _fix_attempts(state, config) < 2 or not builds:
            return False
        tree = max(builds, key=lambda g: g.seq).run_against
        return any(
            t.name == NO_FIX_PROGRESS_TRIAGE and t.run_against == tree
            for t in state.get("gate_triage") or ()
        )


NO_FIX_PROGRESS_TRIAGE = "fix-no-progress"
