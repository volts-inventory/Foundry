"""Shared agent-runtime scaffolding — the seam ALL Phase-1 agent nodes share.

Every Phase-1 agent node (product / architecture / implementation / quality)
is built by a NODE FACTORY following one convention, so the role subgraphs and
``factory_graph`` wire uniformly:

    def make_<agent>_node(rt: AgentRuntime, ...) -> NodeFn:
        def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
            seq = rt.seq(config, "<node_id>", "<artifact-or-action-key>")
            ...                       # rt.proxy.complete(...) / rt.proxy.judge(...)
            return {"<channel>": [<writes>]}   # channel writes, never mutate state
        return _node

The closure is a plain LangGraph node — ``(state, config) -> dict`` returning
channel writes — exactly the shape ``foundry/graph/skeleton.py`` uses, so a
factory-built node drops straight into a ``StateGraph(FactoryState)``.

What an :class:`AgentRuntime` carries (the shared dependencies, injected once):

* ``proxy`` — the single model egress (LEARNINGS L6). Agents call
  ``rt.proxy.complete(...)`` for ideation/gate text and ``rt.proxy.judge(...)``
  for the trusted, proxy-stamped judgment path. Agents NEVER import the
  Anthropic SDK and NEVER hold a credential.
* ``epochs`` — the per-``(run_id, key)`` :class:`EpochCounter` that mints the
  resume-monotonic ``emission_epoch`` for every :class:`Seq` (see
  ``foundry/state/seq.py`` for why epoch outranks ``superstep_index``). Shared
  across all nodes of a run so re-execution after a resume strictly outranks
  the stale record it supersedes.
* ``adapter`` — the swappable coding substrate (``WorkerAdapter``); only the
  implementation-layer agents use it. ``None`` for non-implementation agents.

Convention rules the four waves MUST follow (LEARNINGS L5/L6):

* Agents emit ``GateTriage`` (advisory) to the ``gate_triage`` channel — NEVER
  a ``GateResult`` to the ``gates`` channel. Verdicts are computed + signed by
  the two-zone gate runner / proxy; an unsigned ``GateResult`` write is dropped
  fail-closed by ``_merge_gates``.
* All model calls go through ``rt.proxy``. No agent imports ``anthropic``.

This module holds NO agent logic — only the shared seam (``AgentRuntime`` +
``NodeFn`` + ``rt.seq``). It is import-cheap: no heavy imports at module top.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from foundry.state.seq import EpochCounter, Seq

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.durability.artifact_store import ArtifactStore
    from foundry.execution.adapter_contract import WorkerAdapter
    from foundry.proxy.model_provider_proxy import ModelProviderProxy
    from foundry.state.factory_state import FactoryState

__all__ = ["AgentRuntime", "NodeFn", "run_id_of"]

# A LangGraph node, as built by a `make_<agent>_node` factory: it reads the
# shared state snapshot + run config and returns channel writes (never mutates
# state). Identical in shape to the nodes in foundry/graph/skeleton.py.
NodeFn = Callable[["FactoryState", "RunnableConfig"], dict[str, Any]]


def run_id_of(config: RunnableConfig) -> str:
    """The run identity for ``Seq``: LangGraph's ``thread_id`` (skeleton.py)."""
    return str(config["configurable"]["thread_id"])


def _superstep(config: RunnableConfig) -> int:
    # Read from run config metadata at emit time (arch §3). NOT monotonic across
    # resume — Seq.emission_epoch is what makes a re-executed node outrank the
    # stale record it supersedes (see foundry/state/seq.py).
    metadata = config.get("metadata") or {}
    return int(metadata.get("langgraph_step", 0))


@dataclass
class AgentRuntime:
    """Shared dependencies every Phase-1 agent node is built against.

    Injected once and threaded into each ``make_<agent>_node(rt, ...)`` factory;
    the resulting node closures call ``rt.proxy`` for model egress, ``rt.seq``
    to mint causal-order ``Seq`` values, and (implementation agents only)
    ``rt.adapter`` for the coding substrate.
    """

    proxy: ModelProviderProxy
    epochs: EpochCounter = field(default_factory=EpochCounter)
    adapter: WorkerAdapter | None = None
    # ADVISORY live-worker telemetry (observability only — like the adapter's on_activity
    # seam). The implementation pool calls this once when a worker STARTS running one
    # coding task and once when it FINISHES, so the UI can seat one desk per concurrent
    # worker (≤ max_slots per lane). It is fired from worker threads, so the sink MUST be
    # thread-safe; it MUST NOT raise (callers guard it) and MUST NOT influence any write —
    # the build is byte-identical whether or not it is wired. None ⇒ no live-worker events.
    worker_cb: Callable[[dict[str, Any]], None] | None = None
    # PAUSE gate (UI pause button). The coding pool calls this once BEFORE it submits
    # each task; the callback BLOCKS while the run is paused and returns when resumed,
    # so a pause drains the in-flight workers (they finish) without starting new ones —
    # a finer-grained pause than the between-node boundary the graph driver already
    # honours. Must be a no-op when not paused; None ⇒ never pauses inside a node.
    pause_gate: Callable[[], None] | None = None
    # The content-addressed body store written on emit (DECISIONS D6). When set,
    # emit_artifact(..., store=rt.cas) ALSO stores the typed body so a real run's
    # resolve/resolve_blob can reconstitute it; None ⇒ mint-only (backward-
    # compatible with the synthetic-resolver tests). The cli / factory_graph wire
    # rt.cas.resolve / rt.cas.resolve_blob as the real materialization seams.
    cas: ArtifactStore | None = None

    def seq(
        self,
        config: RunnableConfig,
        node_id: str,
        key: str,
        local: int = 0,
    ) -> Seq:
        """Mint a ``Seq`` for a record emitted by ``node_id`` under ``key``.

        Reproduces ``foundry/graph/skeleton.py``'s ``_seq`` exactly: ``run_id``
        from the run's ``thread_id``, ``emission_epoch`` bumped on the shared
        ``EpochCounter`` keyed ``f"{node_id}/{key}"`` (resume-monotonic),
        ``superstep_index`` from run metadata. ``key`` names the artifact/action
        being emitted (e.g. ``"artifact:result"``), so distinct emissions of one
        node get independent epoch streams.
        """
        run_id = run_id_of(config)
        return Seq(
            run_id=run_id,
            emission_epoch=self.epochs.bump(run_id, f"{node_id}/{key}"),
            superstep_index=_superstep(config),
            node_id=node_id,
            local=local,
        )
