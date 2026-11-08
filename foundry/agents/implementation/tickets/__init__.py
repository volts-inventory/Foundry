"""Ticket-driven scheduling (plan §2 / §2b): tickets -> tasks -> waves.

The planner emits one ``ticket`` artifact per atomic, Jira-style unit of work.
This module turns them into :class:`Task` records (deterministically — the task
id IS the ticket id, so it is stable across re-plans of the same ticket) and runs
them wave by wave from the implementation subgraph's looping ``engineering``
node:

* A **wave** is every ticket whose dependencies all have a FeatureDiff, minus
  write-scope collisions: two tickets whose ``files`` overlap (the same path, a
  ``dir/`` entry and a path under it, or an empty — unrestricted — scope) never run
  in one wave — the later one (by Seq) waits for the next wave and builds on the first.
* Each wave runs through :class:`LocalWorkPool` with ``lineage = specialty`` and a
  per-specialty slot limit under the global ``max_slots``.
* Each ticket's sandbox is seeded with the FULL integrated tree as it stands after
  the previous waves (the same ``materialize_workdir`` the gates and the fixer use),
  so the agent can read everything built so far. It may only WRITE its ``files``:
  the diff is cut back to them in-worker and a triage note names what was dropped.
* The packet carries the ticket, its scenarios, and the bodies of the interface
  contracts it implements / consumes, verbatim, after the pinned constraints.

The node returns after each wave, so the graph checkpoints per wave and a resume
(token cap, crash) skips every ticket that already has a FeatureDiff.

A ticket that fails never costs its wave-mates their work: every successful
sibling's diff is still returned. An ordinary failure is noted and retried next
pass, up to :data:`MAX_TICKET_ATTEMPTS`, then skipped so the loop ends and the gates
decide. A spent token cap is noted too (:func:`cap_stopped`); the subgraph then
exits and the supervisor raises it, after this wave's writes are committed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry import progress
from foundry.agents._common import journal_entry
from foundry.agents._runtime import AgentRuntime, NodeFn
from foundry.agents.implementation.diagnosis import diagnose, diagnosis_tier
from foundry.agents.implementation.junior_engineer import (
    _adapter_name,
    _finalize_diff,
    _fix_summary,
    _prior_failures,
    _sandbox_for,
    ready_tasks,
    touched_files,
)
from foundry.agents.implementation.senior_engineer import NODE_ID as SENIOR_NODE_ID
from foundry.agents.implementation.stubs import is_stub
from foundry.agents.implementation.tickets.adapter import EmptyTicketError as EmptyTicketError
from foundry.agents.implementation.tickets.adapter import (
    _require_scope_written as _require_scope_written,
)
from foundry.agents.implementation.tickets.adapter import _ScopedAdapter
from foundry.agents.implementation.tickets.constants import (
    _CRITERION_KIND,
    _DEFAULT_COST_BUDGET_MICROUSD,
    _DEFAULT_TURN_BUDGET,
    _FEATURE_DIFF_KIND,
    _LOCAL_TURN_BUDGET,
    CAP_TRIAGE,
    CONTRACT_DUP_TRIAGE,
    CONTRACT_KIND,
    DEFAULT_WORKERS,
    FAIL_TRIAGE,
    MAX_TICKET_ATTEMPTS,
    NODE_ID,
    OVERLAY_TRIAGE,
    SKIP_TRIAGE,
    TICKET_KIND,
)
from foundry.agents.implementation.tickets.contracts import (
    _flag_contract_duplicates as _flag_contract_duplicates,
)
from foundry.agents.implementation.tickets.contracts import contract_duplicates, contract_members
from foundry.agents.implementation.tickets.escalation import (
    _escalated_packet,
    _escalation_context,
    _is_local,
    escalated_task_ids,
    escalation_candidates,
    escalation_pick,
    escalation_target,
    pending_escalations,
    stopped_tickets,
)
from foundry.agents.implementation.tickets.packet import (
    _failure_notes,
    _feature_texts,
    _integrated_overlay,
    _newest_by,
    _ticket_check,
    _ticket_context,
    _ticket_packet,
    _ticket_scenarios,
    bdd_scaffold_spans,
    ticket_signature_map,
)
from foundry.agents.implementation.tickets.restart import (
    _land_reset,
    _restart_prep,
    _retry_restarts,
)
from foundry.agents.implementation.tickets.rules import CHECK_RULE as CHECK_RULE
from foundry.agents.implementation.tickets.rules import (
    group_failures_by_ticket,
    verify_commands,
    verify_rules,
)
from foundry.agents.implementation.tickets.rules import scenario_rules as scenario_rules
from foundry.agents.implementation.tickets.rules import scenario_verify as scenario_verify
from foundry.agents.implementation.tickets.rules import (
    unwritten_ticket_files as unwritten_ticket_files,
)
from foundry.agents.implementation.tickets.scope import _LOCKFILES as _LOCKFILES
from foundry.agents.implementation.tickets.scope import _is_gate_input as _is_gate_input
from foundry.agents.implementation.tickets.scope import (
    in_write_scope,
    note_fix_writes,
    trim_to_write_scope,
)
from foundry.agents.implementation.tickets.skeleton import (
    _skeleton_diff,
    skeleton_files,
    stub_rules,
)
from foundry.agents.implementation.tickets.tasks import (
    _skipped_task_ids,
    _ticket_notes,
    built_task_ids,
    cap_stopped,
    has_tickets,
    read_tickets,
    task_from_ticket,
    task_id_for_ticket,
    tier_for,
)
from foundry.agents.implementation.tickets.waves import (
    _order_after_integrated,
    _round_robin,
    next_wave,
    pending_wave,
    plan_wave_count,
    tickets_settled,
)
from foundry.agents.implementation.tickets.waves import _scopes_overlap as _scopes_overlap
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.tiered import (
    ESCALATE_DECLINED_TRIAGE,
    ESCALATE_REQUEST_TRIAGE,
    ESCALATED_TRIAGE,
    STOPPED_TRIAGE,
    session_stop_reason,
)
from foundry.execution.work_pool import (
    BudgetExceededError,
    ExecutionTask,
    LocalWorkPool,
    QuotaPolicy,
    TaskEventHook,
    allocate_lineage_quotas,
)
from foundry.graph.gate_wiring import apply_overlay
from foundry.proxy.token_meter import RunCapExceeded
from foundry.schemas.architecture import InterfaceContract
from foundry.schemas.base import Artifact, BudgetLedger
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import (
    CONTRACTS_TICKET_ID,
    SKELETON_TASK_ID,
    FeatureDiff,
    Task,
    Ticket,
)
from foundry.schemas.product import AcceptanceCriterion

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.execution.sandbox import Sandbox
    from foundry.gates.self_check import CheckBase, CheckSpec, SelfCheckSettings
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.architecture import PinnedConstraint
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]

__all__ = [
    "CONTRACT_DUP_TRIAGE",
    "CONTRACT_KIND",
    "DEFAULT_WORKERS",
    "NODE_ID",
    "TICKET_KIND",
    "built_task_ids",
    "cap_stopped",
    "contract_duplicates",
    "escalated_task_ids",
    "escalation_candidates",
    "escalation_pick",
    "has_tickets",
    "in_write_scope",
    "make_engineering_node",
    "next_wave",
    "note_fix_writes",
    "pending_escalations",
    "pending_wave",
    "plan_wave_count",
    "read_tickets",
    "stopped_tickets",
    "task_from_ticket",
    "task_id_for_ticket",
    "tickets_settled",
    "tier_for",
    "trim_to_write_scope",
    "verify_commands",
    "verify_rules",
]


def make_engineering_node(
    rt: AgentRuntime,
    *,
    source_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver | None = None,
    snapshot_store: str | None = None,
    turn_budget: int = _DEFAULT_TURN_BUDGET,
    cost_budget_microusd: int = _DEFAULT_COST_BUDGET_MICROUSD,
    max_slots: int = 1,
    cost_cap_microusd: int | None = None,
    workers: Mapping[str, int] | None = None,
    ticket_tiers: Mapping[Any, str] | None = None,
    on_task: TaskEventHook | None = None,
    self_check: SelfCheckSettings | None = None,
) -> NodeFn:
    """Build the looping ``engineering`` node: each call builds ONE wave.

    ``workers`` caps concurrent tickets per specialty (default
    :data:`DEFAULT_WORKERS`) under the global ``max_slots``; ``ticket_tiers`` maps
    difficulty -> model tier; ``on_task`` receives each ticket's start/done/failed
    :class:`TaskEvent`. ``cost_cap_microusd`` is the run's envelope, split across
    specialties by ticket count and spent down across waves. ``self_check`` gives
    each ticket session the gates as a ``check`` tool (the proxy-agent loop), run on
    the integrated tree the wave builds on; ``None`` gives none."""
    if rt.adapter is None:
        raise ValueError("engineering requires rt.adapter (a WorkerAdapter)")
    limits = {**DEFAULT_WORKERS, **(workers or {})}

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        tickets = read_tickets(state, resolve)
        known = [t.id for t in tickets]
        on_channel = {t.id: t for t in ready_tasks(state)}
        tasks: dict[str, Task] = {}
        new_tasks: list[dict[str, Any]] = []
        for i, ticket in enumerate(tickets):
            tid = task_id_for_ticket(ticket.id)
            # The tasks channel keeps a task's FIRST record; a re-planned ticket's
            # tier and dependencies come from its newest version, re-derived here.
            existing = on_channel.get(tid)
            seq = (
                existing.seq if existing else rt.seq(config, SENIOR_NODE_ID, f"task:{tid}", local=i)
            )
            tasks[tid] = task_from_ticket(ticket, seq, ticket_tiers=ticket_tiers, known=known)
            if existing is None:
                new_tasks.append(tasks[tid].model_dump(mode="json"))
        writes: dict[str, Any] = {"tasks": new_tasks} if new_tasks else {}

        done = built_task_ids(state)
        target = escalation_target(rt)
        already = escalated_task_ids(state)
        # A wave check that failed twice asked for its local tickets to re-run on the
        # cloud level: this pass rebuilds just those, then the check runs again.
        requested = pending_escalations(state, resolve) if target else ()
        from foundry.graph.gate_wiring import RESTART_PREFIX, pending_restarts

        # A fix loop planned to rebuild these tickets from scratch (a restart).
        restarts = () if requested else pending_restarts(state, resolve)
        if requested:
            wave = tuple(t for t in tickets if task_id_for_ticket(t.id) in requested)
        elif restarts:
            wave = tuple(t for t in tickets if task_id_for_ticket(t.id) in restarts)
        else:
            wave = next_wave(tickets, done, _skipped_task_ids(state))
        if not wave:
            return writes

        constraints: Sequence[PinnedConstraint] = state.get("pinned_constraints") or ()
        blobs = resolve_blob or (rt.cas.resolve_blob if rt.cas is not None else None)
        overlay: dict[str, str | None] = {}
        overlay_error: str | None = None
        if blobs is not None:
            overlay, overlay_error = _integrated_overlay(state, resolve, blobs)
        reset: tuple[dict[str, str], tuple[str, ...]] = ({}, ())
        restart_memo: dict[str, tuple[str, ...]] = {}
        merged: dict[str, list[Any]] = {}
        if restarts:
            reset, restart_memo, notes = _restart_prep(rt, config, state, resolve, blobs, wave)
            merged["gate_triage"] = notes
            # The rebuilt tickets start from the tree before them, not their old code.
            overlay.update(reset[0])
            for rel in reset[1]:
                overlay.pop(rel, None)
        criteria = _newest_by(state, resolve, _CRITERION_KIND, AcceptanceCriterion, "id")
        contracts = _newest_by(state, resolve, CONTRACT_KIND, InterfaceContract, "name")
        prior = _prior_failures(state)
        failures = _ticket_notes(state, FAIL_TRIAGE)
        from foundry.graph.gate_wiring import contract_paths

        contract_files = contract_paths(state, resolve)
        built_contracts = {f: c for f in contract_files if isinstance(c := overlay.get(f), str)}
        skeleton = skeleton_files(state, resolve, blobs)

        # The run's envelope, split across specialties by ticket count; spend of
        # tickets built in earlier waves stays committed so the cap spans the run.
        cap = cost_cap_microusd or 0
        weights: dict[str, int] = {}
        spent: dict[str, int] = {}
        for t in tickets:
            sp = t.specialty.value
            weights[sp] = weights.get(sp, 0) + 1
            # A ticket rebuilt (on the cloud level, or restarted) replaces its own
            # estimate.
            tid = task_id_for_ticket(t.id)
            if tid in done and tid not in requested and tid not in restarts:
                spent[sp] = spent.get(sp, 0) + cost_budget_microusd
        quota = QuotaPolicy(
            ledger=BudgetLedger(cap_microusd=cap),
            lineage_quota_microusd=allocate_lineage_quotas(weights, cap),
            _committed_total=sum(spent.values()),
            _committed_by_lineage=spent,
        )

        bases: dict[str, str] = {}
        escalations: dict[str, str] = {}  # task id -> why it escalated (admitted only)
        reasons: dict[str, str] = {}
        rebuild: dict[str, tuple[str, ...]] = {}
        if requested:
            from foundry.graph.gate_wiring import (
                FEATURE_DIFF_KIND,
                MaterializationError,
                _diff_ops,
                _fix_attempts,
                _of_kind,
                _resolve_typed,
            )

            fix_context = (
                *_fix_summary(
                    state,
                    resolve,
                    blobs or (lambda _u: b""),
                    attempt=_fix_attempts(state, config),
                    scoped=False,
                ),
                *group_failures_by_ticket(prior, tickets),
            )
            asked = {
                t.run_against.removeprefix("task/"): t.explanation
                for t in state.get("gate_triage") or ()
                if t.name == ESCALATE_REQUEST_TRIAGE
            }
            newest = {
                a.task_id: a
                for a in sorted(_of_kind(state, FEATURE_DIFF_KIND), key=lambda a: a.seq)
            }
            for tid in requested:
                reasons[tid] = asked.get(tid, "the wave check failed twice")
                diff = newest.get(tid)
                touched = "no files"
                if diff is not None and blobs is not None:
                    try:
                        touched = touched_files(
                            _diff_ops(
                                _resolve_typed(diff, resolve, FeatureDiff, FEATURE_DIFF_KIND), blobs
                            )
                        )
                    except MaterializationError:
                        touched = "an unreadable diff"
                rebuild[tid] = _escalation_context(
                    tier=tasks[tid].tier,
                    target=str(target),
                    reason=reasons[tid],
                    touched=touched,
                    reported=[
                        t.explanation
                        for t in sorted(state.get("gate_triage") or (), key=lambda t: t.seq)
                        if t.run_against == f"task/{tid}" and t.name == "adapter-report"
                    ],
                    kept=True,
                    failures=fix_context,
                )

        def make_sandbox(etask: ExecutionTask) -> Sandbox:
            tier = etask.packet.task.get("tier")
            sandbox = _sandbox_for(rt, source_dir, snapshot_store, str(tier) if tier else None)
            if overlay:
                apply_overlay(sandbox.worktree, overlay)
            bases[str(etask.packet.task["id"])] = sandbox.snapshot()
            return sandbox

        names: dict[str, tuple[Mapping[str, Sequence[str]], bool]] = {}
        for t in wave:
            if t.id != CONTRACTS_TICKET_ID or not t.files:
                continue
            required = {
                n: m
                for n in t.implements
                if (c := contracts.get(n)) is not None and (m := contract_members(c.signature))
            }
            if required:
                tid = task_id_for_ticket(t.id)
                names[tid] = (required, len(failures.get(tid, ())) + 1 < MAX_TICKET_ATTEMPTS)
        stubs: dict[str, tuple[Mapping[str, str], bool]] = {}
        for t in wave:
            # Judged against the stub as the ticket finds it (a wave-0 fixer may have
            # re-signed it to satisfy the type checker), unless that is no longer a stub.
            stubbed = {
                p: cur if isinstance(cur := overlay.get(p), str) and is_stub(cur) else text
                for p, text in skeleton.items()
                if p in t.files and text
            }
            if stubbed:
                tid = task_id_for_ticket(t.id)
                stubs[tid] = (stubbed, len(failures.get(tid, ())) + 1 < MAX_TICKET_ATTEMPTS)
        for table in (names, stubs):
            for tid in restarts:
                if tid in table:  # a restart's first attempt is strict, its retry lenient
                    table[tid] = (table[tid][0], True)  # type: ignore[assignment]
        # The integrated tree the sessions' gate checks run on, removed with the pool.
        base_scope = contextlib.ExitStack()
        base: CheckBase | None = None
        if self_check is not None and blobs is not None and self_check.enabled():
            from foundry.gates.self_check import check_base

            # A restart's sessions start on the reset tree: so do their checks.
            base = base_scope.enter_context(
                check_base(
                    state,
                    resolve=resolve,
                    resolve_blob=blobs,
                    rt=rt,
                    settings=self_check,
                    reset=reset,
                )
            )
        checks: dict[str, CheckSpec] = {}
        scoped = _ScopedAdapter(
            rt,
            {task_id_for_ticket(t.id): t.files for t in wave},
            built_contracts,
            names,
            stubs,
            skeleton,
            checks,
        )
        adapter_name = _adapter_name(rt)
        submitted: dict[str, str] = {}
        packets: dict[str, TaskPacket] = {}
        diagnosed: dict[str, str] = {}  # task id -> the cloud diagnosis its retry got
        if overlay_error is not None:
            # This wave builds on the bare base, not on the earlier waves: say so.
            merged.setdefault("gate_triage", []).append(
                GateTriage(
                    name=OVERLAY_TRIAGE,
                    run_against=f"wave/{wave[0].id}",
                    explanation="the integrated tree could not be materialized, so this wave "
                    f"builds on the bare base without the earlier waves' work: {overlay_error}"[
                        :2000
                    ],
                    produced_by=NODE_ID,
                    seq=rt.seq(config, NODE_ID, f"overlay:{wave[0].id}"),
                )
            )
        with (
            base_scope,
            LocalWorkPool(
                sandbox_factory=make_sandbox,
                adapter_factory=lambda _name: scoped,
                max_slots=min(len(wave), max_slots),
                quota=quota,
                limits=limits,
                on_task=on_task,
            ) as pool,
        ):
            for ticket in _round_robin(wave):
                tid = task_id_for_ticket(ticket.id)
                spans, features = _ticket_context(rt, ticket, criteria, contracts)
                owned = _ticket_scenarios(ticket, criteria)
                sigmap = ticket_signature_map(ticket, tickets, overlay, contract_files)
                spans = (
                    *spans,
                    *((sigmap,) if sigmap else ()),
                    *bdd_scaffold_spans(
                        ticket.files,
                        _feature_texts(owned, overlay, criteria),
                        state.get("stack"),
                        ticket.id,
                    ),
                )
                # A retry must see why its last attempt failed: an identical packet is an
                # identical model request, which the call cache replays turn for turn.
                own = tuple(
                    n.explanation for n in sorted(failures.get(tid, ()), key=lambda n: n.seq)
                )
                packet = _ticket_packet(
                    tasks[tid],
                    ticket,
                    constraints,
                    spans=spans,
                    feature_files=features,
                    prior_failures=(*own, *prior),
                    turn_budget=(
                        max(turn_budget, _LOCAL_TURN_BUDGET)
                        if _is_local(rt, tasks[tid].tier)
                        else turn_budget
                    ),
                    cost_budget_microusd=cost_budget_microusd,
                    stack=state.get("stack"),
                    contract_files=contract_files,
                    scenarios=owned,
                )
                if base is not None:
                    checks[tid] = _ticket_check(
                        base, state, resolve, tasks[tid], ticket, criteria, contract_files, owned
                    )
                if extra := stub_rules(ticket, skeleton, contracts):
                    packet = packet.model_copy(
                        update={"task": {**packet.task, "rules": [*packet.task["rules"], *extra]}}
                    )
                if tid in restart_memo:
                    packet = packet.model_copy(update={"prior_failures": restart_memo[tid]})
                if tid in rebuild:
                    packet = _escalated_packet(packet, str(target), rebuild[tid], turn_budget)
                elif target and tid in already and _is_local(rt, tasks[tid].tier):
                    # Its escalation is spent and its local attempt was discarded: a
                    # retry stays on the cloud level rather than repeat the local stop.
                    packet = _escalated_packet(packet, target, packet.prior_failures, turn_budget)
                elif own and (cloud := diagnosis_tier(rt, tasks[tid].tier)) is not None:
                    found = diagnose(
                        rt,
                        cloud,
                        label=f"{tid} retry",
                        summary=own,
                        files=(*ticket.files, *ticket.reads),
                        tree=overlay,
                        allowed=ticket.files,
                    )
                    if found is not None:
                        diagnosed[tid] = f"{cloud}: {found.summary()}"
                        packet = packet.model_copy(
                            update={
                                "prior_failures": (*found.packet_lines(), *packet.prior_failures)
                            }
                        )
                packets[tid] = packet
                etask = ExecutionTask(
                    packet=packet,
                    adapter=adapter_name,
                    lineage=ticket.specialty.value,
                    est_cost_microusd=cost_budget_microusd,
                )
                try:
                    submitted[tid] = pool.submit(etask)
                except BudgetExceededError as exc:
                    if tid in restart_memo:  # already built: keep it as it is
                        progress.log(f"restart of {tid} not admitted: {exc}")
                        continue
                    if tid in rebuild:  # already built: decline the escalation, keep it
                        progress.log(f"escalation of {tid} not admitted: {exc}")
                        merged.setdefault("gate_triage", []).append(
                            GateTriage(
                                name=ESCALATE_DECLINED_TRIAGE,
                                run_against=f"task/{tid}",
                                explanation=f"escalation not admitted: {exc}"[:2000],
                                produced_by=NODE_ID,
                                seq=rt.seq(config, NODE_ID, f"declined:{tid}"),
                            )
                        )
                        continue
                    merged.setdefault("gate_triage", []).append(
                        GateTriage(
                            name=SKIP_TRIAGE,
                            run_against=f"task/{tid}",
                            explanation=f"not built: {exc}",
                            produced_by=NODE_ID,
                            seq=rt.seq(config, NODE_ID, f"skip:{tid}"),
                        )
                    )
                    continue
                if tid in rebuild:
                    escalations[tid] = reasons[tid]
                    progress.log(
                        f"escalate {tid}: {tasks[tid].tier} -> {target} ({reasons[tid][:120]})"
                    )
            results: dict[str, AdapterResult | Exception] = {}
            for tid, pool_id in submitted.items():
                try:
                    results[tid] = pool.result(pool_id)
                except Exception as exc:
                    results[tid] = exc
            if restart_memo:
                _retry_restarts(
                    pool, scoped, results, packets, restart_memo, adapter_name, cost_budget_microusd
                )
            stopped: dict[str, str] = {}
            if target and not requested and not restarts:
                # A local session that hit its turn cap or was stopped early keeps its
                # partial work: the next wave check escalates it on a first failure
                # (a final wave's is left to the supervisor's gates and cloud fixer).
                # One that wrote none of its files has nothing to check, so it re-runs
                # once on the cloud level now, in a fresh cloud-tier sandbox.
                retry: dict[str, str] = {}
                for tid, got in results.items():
                    if tid in already or not _is_local(rt, tasks[tid].tier):
                        continue
                    if isinstance(got, AdapterResult):
                        if (stop := session_stop_reason(got.triage)) is not None:
                            stopped[tid] = stop
                        continue
                    if (reason := scoped.empty.get(tid)) is None:
                        continue
                    ctx = _escalation_context(
                        tier=tasks[tid].tier,
                        target=target,
                        reason=reason,
                        touched="none of its files",
                        reported=[],
                        kept=False,
                        failures=prior,
                    )
                    epacket = _escalated_packet(packets[tid], target, ctx, turn_budget)
                    try:
                        retry[tid] = pool.submit(
                            ExecutionTask(
                                packet=epacket,
                                adapter=adapter_name,
                                lineage=packets[tid].task["specialty"],
                                est_cost_microusd=cost_budget_microusd,
                            )
                        )
                    except BudgetExceededError as exc:
                        progress.log(f"escalation of {tid} not admitted: {exc}")
                        continue
                    escalations[tid] = reason
                    progress.log(f"escalate {tid}: {tasks[tid].tier} -> {target} ({reason[:120]})")
                for tid, pool_id in retry.items():
                    try:
                        results[tid] = pool.result(pool_id)
                    except Exception as exc:
                        results[tid] = exc
            for tid, why in sorted(escalations.items()):
                merged.setdefault("gate_triage", []).append(
                    GateTriage(
                        name=ESCALATED_TRIAGE,
                        run_against=f"task/{tid}",
                        explanation=f"escalated from {tasks[tid].tier if tid in tasks else '?'} "
                        f"to {target}: {why}"[:2000],
                        produced_by=NODE_ID,
                        seq=rt.seq(config, NODE_ID, f"escalated:{tid}"),
                    )
                )
            # Finalize in Seq order, single-threaded: rt.seq and rt.cas body writes
            # never race. A failed ticket never takes its siblings' diffs with it.
            built: list[Ticket] = []
            for ticket in wave:
                tid = task_id_for_ticket(ticket.id)
                if tid not in results:
                    continue
                try:
                    got = results[tid]
                    if isinstance(got, Exception):
                        raise got
                    task = tasks[tid]
                    if tid in restart_memo:
                        # Its own id would open a new fix loop (fix_window): the rebuild
                        # is a pass of the loop that planned it.
                        rid = f"{RESTART_PREFIX}{tid}"
                        task = task.model_copy(update={"id": rid, "branch": f"task/{rid}"})
                    out = _finalize_diff(rt, task, constraints, bases[tid], got, config)
                    if tid in restart_memo:
                        out.pop("tasks", None)
                except RunCapExceeded as exc:
                    merged.setdefault("gate_triage", []).append(
                        GateTriage(
                            name=CAP_TRIAGE,
                            run_against=f"task/{tid}",
                            explanation=str(exc),
                            produced_by=NODE_ID,
                            seq=rt.seq(config, NODE_ID, f"cap:{tid}"),
                        )
                    )
                    continue
                except Exception as exc:
                    if tid in restart_memo:  # its earlier build stays integrated
                        progress.log(f"restart of {tid} failed: {exc}")
                        continue
                    merged.setdefault("gate_triage", []).extend(
                        _failure_notes(rt, config, tid, len(failures.get(tid, ())) + 1, exc)
                    )
                    continue
                built.append(ticket)
                for channel, records in out.items():
                    merged.setdefault(channel, []).extend(records)
                if tid in stopped:
                    merged.setdefault("gate_triage", []).append(
                        GateTriage(
                            name=STOPPED_TRIAGE,
                            run_against=f"task/{tid}",
                            explanation=stopped[tid][:2000],
                            produced_by=NODE_ID,
                            seq=rt.seq(config, NODE_ID, f"{STOPPED_TRIAGE}:{tid}"),
                        )
                    )

        stack = state.get("stack")
        built_ids = {t.id for t in built}
        if (
            CONTRACTS_TICKET_ID in built_ids
            and not skeleton
            and blobs is not None
            and (stack is None or stack.language == "python")
            and not any(
                a.kind == _FEATURE_DIFF_KIND and a.task_id == SKELETON_TASK_ID
                for a in state.get("artifacts") or ()
            )
        ):
            # Right after the contracts build, in the same wave, so this wave's check
            # type-checks the stubs against the contracts before any ticket fills them.
            sk_writes, sk_concerns = _skeleton_diff(
                rt,
                config,
                tickets,
                contracts,
                contract_files,
                overlay,
                [
                    r
                    for t in built
                    if isinstance(r := results.get(task_id_for_ticket(t.id)), AdapterResult)
                ],
                blobs,
                bases.get(task_id_for_ticket(CONTRACTS_TICKET_ID), ""),
            )
            for channel, records in sk_writes.items():
                merged.setdefault(channel, []).extend(records)
            skeleton_concerns = sk_concerns
        else:
            skeleton_concerns = ()
        _order_after_integrated(state, merged)
        if restarts and len(built) < len(wave):
            # The reset covers every restarted ticket's files: landing it without each
            # rebuild would delete the code of a ticket whose rebuild failed.
            merged["artifacts"] = [
                a
                for a in merged.get("artifacts") or ()
                if not (a.task_id or "").startswith(RESTART_PREFIX)
            ]
        elif restarts and (reset[0] or reset[1]):
            _land_reset(rt, config, state, resolve, merged, reset, wave)
        for channel, records in merged.items():
            writes.setdefault(channel, []).extend(records)
        writes.setdefault("agent_journal", []).append(
            journal_entry(
                node_id=NODE_ID,
                role="Engineering",
                seq=rt.seq(config, NODE_ID, f"journal:wave:{wave[0].id}"),
                summary=f"Built {len(built)} ticket(s) this wave: "
                + ", ".join(
                    f"{t.id} ({t.specialty.value}, {tasks[task_id_for_ticket(t.id)].tier})"
                    for t in built
                ),
                concerns=(
                    *(
                        (f"Integrated tree unavailable, built on the bare base: {overlay_error}",)
                        if overlay_error
                        else ()
                    ),
                    *(
                        f"{tid} escalated from {tasks[tid].tier if tid in tasks else '?'} to "
                        f"{target}: {why[:300]}"
                        for tid, why in sorted(escalations.items())
                    ),
                    *(
                        f"{tid} stopped early on {tasks[tid].tier}, its partial work kept: "
                        f"{why[:300]}"
                        for tid, why in sorted(stopped.items())
                        if any(task_id_for_ticket(t.id) == tid for t in built)
                    ),
                    *skeleton_concerns,
                    *(f"{tid} retry diagnosed on {why}" for tid, why in sorted(diagnosed.items())),
                ),
            )
        )
        return writes

    return _node
