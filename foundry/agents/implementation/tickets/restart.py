"""A fix loop's ticket restart."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from foundry import progress
from foundry.agents._runtime import AgentRuntime
from foundry.agents.implementation.tickets.adapter import _ScopedAdapter
from foundry.agents.implementation.tickets.constants import (
    _FEATURE_DIFF_KIND,
    FAIL_TRIAGE,
    NODE_ID,
)
from foundry.agents.implementation.tickets.escalation import session_digests
from foundry.agents.implementation.tickets.scope import in_write_scope
from foundry.agents.implementation.tickets.tasks import (
    _ticket_notes,
    task_id_for_ticket,
    unbuilt_task_ids,
)
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.tiered import DIGEST_CAP
from foundry.execution.work_pool import BudgetExceededError, ExecutionTask, LocalWorkPool
from foundry.schemas.base import Artifact
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import Ticket

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.graph.gate_wiring import BlobResolver
    from foundry.state.factory_state import FactoryState

    ArtifactResolver = Callable[[Artifact], Any]


_RESTART_FAILURE_LINES = 30


def _restart_prep(
    rt: AgentRuntime,
    config: RunnableConfig,
    state: FactoryState,
    resolve: ArtifactResolver,
    blobs: BlobResolver | None,
    wave: Sequence[Ticket],
) -> tuple[tuple[dict[str, str], tuple[str, ...]], dict[str, tuple[str, ...]], list[GateTriage]]:
    """For a planned restart of ``wave``: the reset of its files to the tree before
    it (:func:`~foundry.graph.gate_wiring.ticket_reset`), each ticket's memo of what
    the fix loop tried (its pass log and the failures left), and the notes that
    spend each ticket's one restart."""
    from foundry.agents.implementation.junior_engineer import _pass_log, fix_failures
    from foundry.graph.gate_wiring import (
        RESTARTED_TRIAGE,
        MaterializationError,
        ticket_reset,
        window_passes,
    )

    reset: tuple[dict[str, str], tuple[str, ...]] = ({}, ())
    why = ""
    if blobs is not None:
        try:
            reset = ticket_reset(state, wave, resolve=resolve, resolve_blob=blobs)
        except MaterializationError as exc:
            why = (
                " (the tree before it could not be built, so it starts from the current "
                f"one: {exc})"
            )
    passes = window_passes(state, resolve)
    tried = sum(p.outcome not in ("initial", "restored") for p in passes)
    log = _pass_log(state, passes, resolve, blobs) if blobs is not None and tried else []
    failing = fix_failures(state, resolve)[:_RESTART_FAILURE_LINES]
    paths = sorted({*reset[0], *reset[1]})
    digests = session_digests(state)
    unbuilt = unbuilt_task_ids(state)
    attempts = _ticket_notes(state, FAIL_TRIAGE)
    memo: dict[str, tuple[str, ...]] = {}
    notes: list[GateTriage] = []
    for t in wave:
        tid = task_id_for_ticket(t.id)
        files = ", ".join(p for p in paths if in_write_scope(p, t.files)) or ", ".join(t.files)
        if tid in unbuilt:
            failed = tuple(
                n.explanation for n in sorted(attempts.get(tid, ()), key=lambda n: n.seq)
            )
            lead: tuple[str, ...] = (
                f"ticket {t.id} was never built: its {len(failed)} attempt(s) failed, so it "
                "was skipped and its scenarios fail unexecuted. Build it now: write its "
                f"files ({files}) from the ticket's spec and criteria, as the tree has them "
                "(a repair pass may have touched them). Its attempts failed with "
                "(do not repeat these):",
                *failed,
            )
            what = f"built after it was skipped ({len(failed)} failed attempt(s))"
        else:
            lead = (
                f"repairing ticket {t.id} did not converge, so it is built again from "
                f"scratch: its files ({files}) were reset to the tree before it was first "
                f"built{why}. Write them again from the ticket's spec and criteria.",
            )
            what = f"rebuilt from scratch after {tried} fix pass(es) did not converge"
        memo[tid] = (
            "[fresh restart]",
            *lead,
            *log,
            *(
                (f"its first build's session ended early: {digests[tid][:DIGEST_CAP]}",)
                if tid in digests
                else ()
            ),
            "the failures the earlier attempts left (the rebuild must not repeat them):",
            *failing,
        )
        notes.append(
            GateTriage(
                name=RESTARTED_TRIAGE,
                run_against=f"task/{tid}",
                explanation=f"{what}; files reset: {files or 'none'}"[:2000],
                produced_by=NODE_ID,
                seq=rt.seq(config, NODE_ID, f"{RESTARTED_TRIAGE}:{tid}"),
            )
        )
        progress.log(f"restart {tid} ({what}; reset {files or 'nothing'})")
    return reset, memo, notes


def _retry_restarts(
    pool: LocalWorkPool,
    scoped: _ScopedAdapter,
    results: dict[str, AdapterResult | Exception],
    packets: Mapping[str, TaskPacket],
    restart_memo: Mapping[str, tuple[str, ...]],
    adapter_name: str,
    est_cost: int,
) -> None:
    """A restart whose session failed (an empty session, a changed planned
    signature, contract drift) gets one more attempt in the same pass, told why,
    with the signature and contract checks lenient (they note instead of failing):
    the restart is spent once planned, so a failed first attempt must not throw the
    rebuild away. Both attempts failing leaves the earlier build integrated."""
    again: dict[str, str] = {}
    for tid, got in results.items():
        if tid not in restart_memo or not isinstance(got, Exception):
            continue
        progress.log(f"restart of {tid} failed, retrying once: {got}")
        for table in (scoped._names, scoped._stubs):
            if tid in table:
                table[tid] = (table[tid][0], False)  # type: ignore[assignment]
        packet = packets[tid].model_copy(
            update={
                "prior_failures": (
                    *packets[tid].prior_failures,
                    f"the restart's first attempt failed (fix this first): {str(got)[:1500]}",
                )
            }
        )
        try:
            again[tid] = pool.submit(
                ExecutionTask(
                    packet=packet,
                    adapter=adapter_name,
                    lineage=packet.task["specialty"],
                    est_cost_microusd=est_cost,
                )
            )
        except BudgetExceededError as exc:
            progress.log(f"restart retry of {tid} not admitted: {exc}")
    for tid, pool_id in again.items():
        try:
            results[tid] = pool.result(pool_id)
        except Exception as exc:
            results[tid] = exc


def _land_reset(
    rt: AgentRuntime,
    config: RunnableConfig,
    state: FactoryState,
    resolve: ArtifactResolver,
    merged: dict[str, list[Any]],
    reset: tuple[dict[str, str], tuple[str, ...]],
    wave: Sequence[Ticket],
) -> None:
    """Put the restart's reset diff on ``merged``, ordered between the diffs it
    resets and the rebuilt tickets' diffs (which were built on the reset tree)."""
    from foundry.graph.gate_wiring import RESET_PREFIX, snapshot_diff, window_passes

    passes = window_passes(state, resolve)
    ids = "+".join(sorted(task_id_for_ticket(t.id) for t in wave))
    art = snapshot_diff(
        state,
        config,
        reset[0],
        reset[1],
        task_id=f"{RESET_PREFIX}{ids}",
        base_tree=passes[-1].tree if passes else "",
        salt=ids,
        rt=rt,
        node_id=NODE_ID,
    )
    top = art.seq.emission_epoch
    merged["artifacts"] = [
        art,
        *(
            a.model_copy(update={"seq": a.seq._replace(emission_epoch=top + 1)})
            if a.kind == _FEATURE_DIFF_KIND and a.seq.emission_epoch <= top
            else a
            for a in merged.get("artifacts") or ()
        ),
    ]
