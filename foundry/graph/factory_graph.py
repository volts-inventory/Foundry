r"""Factory graph — compile the full Phase-1 spine (T-M1-14).

(architecture.md §1/§5/§7, governance §1/§5, implementation-plan §7)

This is the TOP-LEVEL graph: it mounts the supervisor (the phase-routing spine
across the four role subgraphs + the deterministic gate boundaries; T-M1-13) as
a single subgraph node, appends an ASSISTANT-MODE emit node, and compiles the
whole thing with the shared durable checkpointer so the run is resumable.

Topology::

    START
      -> factory     # the supervisor subgraph (triage -> ... -> build+test gate)
      -> emit        # assistant-mode: emit the PR-equivalent ExternalAction
      -> END

The ``emit`` node runs ONLY after the supervisor's phases complete. It is the
ONLY place a Phase-1 external EFFECT (a PR) is requested, and it does so exactly
the way ``foundry/graph/skeleton.py`` does — by writing an :class:`ExternalAction`
to the ``actions`` channel with a DETERMINISTIC idempotency key derived via
:func:`content_digest` (NOT wall clock). The outbox relay (``foundry/durability``)
is the SOLE component that ever touches the outside world; nothing here makes a
GitHub / network call. A kill-then-resume re-folds the committed ``emit`` write
and re-derives the SAME idempotency key, so ``union_actions`` (keyed by
``idempotency_key``) coalesces it — no duplicate PR (Phase-0 exit criterion).

PASS GUARD (governance §1/§5): the PR action is emitted ONLY when the terminal
gate state is a clean PASS — Spec AND Build AND Test each have a newest-by-``Seq``
PASS verdict on the trust-filtered ``gates`` channel. A run that halted without
passing (a stable-FAIL latch, an unresolved ERROR, the convergence bound spent)
emits NOTHING — we never open a PR for a failed run.

The ``add_node`` mypy seam (LEARNINGS SG2/IM5): a closure typed as the ``NodeFn``
``Callable`` alias trips ``add_node``'s overload resolution, so the emit node is a
thin concrete ``def`` (no ``cast``, no private import). The supervisor subgraph is
added as a compiled graph, which ``add_node`` accepts directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.canonical import content_digest
from foundry.graph.gate_wiring import FEATURE_DIFF_KIND
from foundry.graph.supervisor import build_supervisor_graph
from foundry.schemas.base import ExternalAction, IdempotencyPath
from foundry.schemas.gates import GateStatus
from foundry.state.factory_state import FactoryState

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from foundry.agents._runtime import AgentRuntime
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.context import SymbolIndex
    from foundry.gates.runner import TwoZoneGateRunner
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact

__all__ = [
    "TERMINAL_PASS_GATES",
    "build_factory_graph",
    "terminal_gates_pass",
]

# Supervisor subgraph node id + the assistant-mode emit node id.
_FACTORY = "factory"
_EMIT = "emit"

# The PR is opened only when EVERY terminal gate has a PASS verdict (governance
# §1/§5): the Spec gate (product/architecture boundary) AND the Build + Test
# gates (implementation/quality boundary). The reviewer's ``review`` verdict is
# advisory here — the deterministic gates own the release bit (LEARNINGS L5).
TERMINAL_PASS_GATES: frozenset[str] = frozenset({"spec", "build", "test"})


def _latest_by_name(state: FactoryState) -> dict[str, Any]:
    """Newest-by-``Seq`` verdict per gate name on the (trust-filtered) channel.

    ``gates`` is admitted only for runner-attested records (``merge_gates``); pick
    the latest by ``Seq`` per name so a re-verification run's verdict (bumped
    epoch) wins over the stale record it supersedes.

    NB: this collapses by ``name`` ALONE and takes the global newest-by-``Seq``,
    whereas ``merge_gates`` keys by ``(name, run_against)``. That is deliberate
    release semantics: across a fix-loop the channel may hold same-name verdicts
    for different ``run_against`` trees; the newest integrated tree's verdict is
    the one that gates the release, so cross-``run_against`` newest-wins is correct.
    """
    latest: dict[str, Any] = {}
    for g in state.get("gates") or ():
        prev = latest.get(g.name)
        if prev is None or g.seq > prev.seq:
            latest[g.name] = g
    return latest


def terminal_gates_pass(state: FactoryState) -> bool:
    """True iff EVERY terminal gate has a newest-by-``Seq`` PASS verdict.

    Fail-closed: a missing terminal verdict, or any non-PASS status, means the
    run did NOT complete a clean release boundary — no PR. Reads only the
    trust-filtered ``gates`` channel (verdicts are runner-attested; an unsigned
    write never reaches here, L5), so the guard cannot be tricked by an
    unattested status-flipped copy.
    """
    latest = _latest_by_name(state)
    for name in TERMINAL_PASS_GATES:
        verdict = latest.get(name)
        if verdict is None or verdict.status is not GateStatus.PASS:
            return False
    return True


def _pr_subject(state: FactoryState) -> Artifact | None:
    """The integrated FeatureDiff handle the PR opens against (newest-by-``Seq``).

    ``artifacts`` folds by ``merge_by_uri`` (insertion order, not ``Seq``), so we
    pick the ``feature_diff`` handle with the max ``Seq`` deterministically
    (mirrors ``gate_wiring`` / ``reviewer_critic`` newest-by-Seq selection) —
    NOT ``artifacts[-1]``.
    """
    subject: Artifact | None = None
    for a in state.get("artifacts") or ():
        if a.kind != FEATURE_DIFF_KIND:
            continue
        if subject is None or a.seq > subject.seq:
            subject = a
    return subject


def build_factory_graph(
    rt: AgentRuntime,
    *,
    runner: TwoZoneGateRunner,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    source_dir: str,
    checkpointer: BaseCheckpointSaver | None = None,
    index: SymbolIndex | None = None,
    max_fix_attempts: int = 3,
) -> CompiledStateGraph[FactoryState]:
    """Compile the full Phase-1 factory graph.

    Mounts :func:`build_supervisor_graph` as the ``factory`` subgraph node, then
    an assistant-mode ``emit`` node, then ``END`` (``START -> factory -> emit ->
    END``), and compiles with the injected ``checkpointer`` so the run is durable
    + resumable.

    Parameters
    ----------
    rt:
        The shared :class:`AgentRuntime` (one ``EpochCounter`` + one model egress;
        AR1/L6) threaded into every layer subgraph, the gate nodes, AND the emit
        node's ``Seq`` minting. Its ``adapter`` must be set (the implementation
        layer's Junior requires it).
    runner:
        The :class:`TwoZoneGateRunner` the gate boundaries attest verdicts through
        (verdicts come ONLY from it; L5/GW2).
    resolve / resolve_blob:
        The two materialization seams (GW1a/D-R1-02), forwarded to the supervisor.
    source_dir:
        Base tree cloned into each per-task sandbox (implementation layer; INFRA).
    checkpointer:
        The durable saver attached at compile so the graph is resumable; a
        kill-then-resume re-folds committed writes (including the ``emit``
        ExternalAction) and the keyed ``union_actions`` reducer coalesces the
        re-emitted PR (same idempotency key) — no duplicate PR. ``None`` compiles
        a non-checkpointed graph (callers normally pass a
        :class:`FoundryCheckpointer.saver` or a dev ``MemorySaver``).
    index:
        Context retriever for the Principal Architect (``None`` = greenfield).
    max_fix_attempts:
        Convergence bound forwarded to the supervisor's gate routing.

    Returns the COMPILED top-level graph.
    """
    supervisor = build_supervisor_graph(
        rt,
        runner=runner,
        resolve=resolve,
        resolve_blob=resolve_blob,
        source_dir=source_dir,
        index=index,
        max_fix_attempts=max_fix_attempts,
    ).compile()

    def emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        """Assistant-mode emit: request the PR-equivalent external effect.

        Runs AFTER the supervisor's phases complete. Guards on the terminal gate
        PASS (governance §1/§5): only on a clean Spec+Build+Test PASS do we emit
        the ``open_pr`` ExternalAction; a halted/failed run emits NOTHING (no PR).

        The idempotency key is DETERMINISTIC — ``content_digest`` over
        (``run_id``, the integrated artifact's identity) — so a kill-then-resume
        re-derives the SAME key (NOT a wall-clock value, L7) and the keyed
        ``union_actions`` reducer coalesces the re-emission. No real GitHub/network
        call happens here: the action is a request on the ``actions`` channel; the
        outbox relay is the sole component that touches the outside world.

        Phase ownership: the supervisor owns ``phase`` during its phases; this
        emit node is the TERMINAL ``phase`` writer (``done``/``halted``). The two
        run in distinct supersteps (supervisor subgraph node, then emit), so the
        single-writer ``phase`` channel is never double-written within a superstep.
        """
        if not terminal_gates_pass(state):
            # Halted / failed run: do NOT open a PR. Mark the phase only.
            return {"phase": "halted"}
        subject = _pr_subject(state)
        if subject is None:
            # No reviewed artifact to open a PR against — fail-closed (no PR).
            return {"phase": "halted"}

        run_id = config["configurable"]["thread_id"]
        # Process-stable key derived from run + artifact identity (L7 — never
        # hash()/wall clock), so resume re-emits the SAME key and dedups.
        key = content_digest({"run_id": run_id, "artifact_uri": subject.uri})
        action = ExternalAction(
            idempotency_key=f"open_pr:{key}",
            kind="open_pr",
            payload_uri=subject.uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            seq=rt.seq(config, _EMIT, "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    g: StateGraph[FactoryState] = StateGraph(FactoryState)
    g.add_node(_FACTORY, supervisor)
    g.add_node(_EMIT, emit)
    g.add_edge(START, _FACTORY)
    g.add_edge(_FACTORY, _EMIT)
    g.add_edge(_EMIT, END)

    return g.compile(checkpointer=checkpointer)
