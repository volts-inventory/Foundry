"""Phase 0 skeleton graph — the smallest end-to-end spine.

Exit criterion (implementation-plan.md §7, Phase 0): "a trivial task runs
end-to-end, traced, checkpointed; kill-then-resume produces no duplicate
commit/PR." This graph is intake → work → emit-action with the real
FactoryState channels, real Seq emission, and external effects expressed
ONLY as `actions` channel writes (the outbox relay — durability/ — is the
sole component that touches the outside world).

The real role subgraphs (Phase 1) replace `work`; the channel/merge
semantics they inherit are exactly the ones exercised here.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from foundry.canonical import content_digest
from foundry.schemas.base import Artifact, ExternalAction, IdempotencyPath
from foundry.state.factory_state import FactoryState
from foundry.state.seq import EpochCounter, Seq


def _superstep(config: RunnableConfig) -> int:
    # Read from run config metadata at emit time (arch §3); NOT monotonic
    # across resume — Seq's emission_epoch is what makes re-execution outrank.
    return int(config.get("metadata", {}).get("langgraph_step", 0))


def build_skeleton_graph(
    checkpointer: BaseCheckpointSaver | None = None,
    epochs: EpochCounter | None = None,
):
    """A trivial 3-node factory spine over the real FactoryState."""
    epochs = epochs or EpochCounter()

    def _seq(config: RunnableConfig, node_id: str, key: str, local: int = 0) -> Seq:
        run_id = config["configurable"]["thread_id"]
        return Seq(
            run_id=run_id,
            emission_epoch=epochs.bump(run_id, f"{node_id}/{key}"),
            superstep_index=_superstep(config),
            node_id=node_id,
            local=local,
        )

    def intake(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        return {"phase": "intake", "route": "trivial"}

    def work(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        seq = _seq(config, "work", "artifact:result")
        # NB: content_digest, never hash() — Python's str hash is seed-
        # randomized per process, which would silently change idempotency
        # keys across a kill-then-resume in a fresh process.
        art = Artifact(
            kind="result",
            uri=f"cas://result/{content_digest(state['request'])[:16]}",
            produced_by="work",
            seq=seq,
        )
        return {"phase": "work", "artifacts": [art]}

    def emit(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        # External effect = an actions-channel write with a DETERMINISTIC
        # idempotency key (derived from run + artifact identity, not wall
        # clock), so kill-then-resume re-emits the SAME key and the keyed
        # union + outbox dedup coalesce it — no duplicate commit/PR.
        run_id = config["configurable"]["thread_id"]
        art_uri = state["artifacts"][0].uri
        action = ExternalAction(
            idempotency_key=f"open_pr:{run_id}:{art_uri}",
            kind="open_pr",
            payload_uri=art_uri,
            path=IdempotencyPath.EMBEDDED_MARKER,
            seq=_seq(config, "emit", "action:open_pr"),
        )
        return {"phase": "done", "actions": [action]}

    g = StateGraph(FactoryState)
    g.add_node("intake", intake)
    g.add_node("work", work)
    g.add_node("emit", emit)
    g.add_edge(START, "intake")
    g.add_edge("intake", "work")
    g.add_edge("work", "emit")
    g.add_edge("emit", END)
    return g.compile(checkpointer=checkpointer)
