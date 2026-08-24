"""Shared agent-runtime scaffolding (T-M1-04a) — the seam the four Phase-1
agent waves are built against.

Drives the REAL :class:`AgentRuntime` over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` (no live API key, LEARNINGS D1). Asserts the
``rt.seq`` semantics match ``foundry/graph/skeleton.py`` (right run_id,
resume-monotonic ``emission_epoch`` per ``(node_id, key)``, independent key
streams) and that a node built by the documented ``make_<agent>_node(rt)``
convention drops straight into a ``StateGraph(FactoryState)`` and runs.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.agents import AgentRuntime, NodeFn, run_id_of
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.state.factory_state import FactoryState
from foundry.state.seq import EpochCounter

KEY = b"dev-zone-b-runtime-key"
KEY_ID = "dev-key-runtime"


def make_runtime(transport: MockTransport | None = None) -> AgentRuntime:
    proxy = ModelProviderProxy(
        transport or MockTransport(default_text="ok"),
        signer=ZoneBSigner(KEY, KEY_ID),
    )
    return AgentRuntime(proxy=proxy)


def config_for(run_id: str, step: int = 1) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"langgraph_step": step},
    }


# ---- 1. construction (no live API key) --------------------------------------


def test_constructs_with_mock_backed_proxy() -> None:
    rt = make_runtime()
    assert isinstance(rt.proxy, ModelProviderProxy)
    assert isinstance(rt.epochs, EpochCounter)  # default-factory fresh counter
    assert rt.adapter is None


def test_default_epochs_are_distinct_per_runtime() -> None:
    assert make_runtime().epochs is not make_runtime().epochs


# ---- 2. seq() semantics mirror skeleton.py ----------------------------------


def test_seq_carries_run_id_and_metadata() -> None:
    rt = make_runtime()
    config = config_for("run-A", step=7)
    seq = rt.seq(config, "demo", "artifact:result")
    assert seq.run_id == "run-A" == run_id_of(config)
    assert seq.node_id == "demo"
    assert seq.superstep_index == 7
    assert seq.local == 0


def test_seq_emission_epoch_is_monotonic_per_key() -> None:
    rt = make_runtime()
    config = config_for("run-A")
    epochs = [rt.seq(config, "demo", "artifact:result").emission_epoch for _ in range(3)]
    assert epochs == [1, 2, 3]  # bump on every (re-)emission of the key


def test_epoch_counter_is_thread_safe_under_concurrent_bump() -> None:
    """D1/D7 prerequisite: LangGraph runs parallel branches on a thread pool that
    share one EpochCounter. Concurrent bumps of the SAME key must mint distinct,
    contiguous epochs (no collision, no skip) — else resume-monotonic ordering
    corrupts. Without the lock this races; with it, the returned set is exactly
    {1..N}."""
    import threading

    from foundry.state.seq import EpochCounter

    counter = EpochCounter()
    n_threads, per_thread = 8, 200
    total = n_threads * per_thread
    results: list[int] = []
    results_lock = threading.Lock()
    start = threading.Barrier(n_threads)

    def worker() -> None:
        start.wait()  # maximize contention
        local = [counter.bump("run-A", "shared-key") for _ in range(per_thread)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == total
    assert sorted(results) == list(range(1, total + 1))  # distinct + contiguous, no races


def test_distinct_keys_have_independent_epoch_streams() -> None:
    rt = make_runtime()
    config = config_for("run-A")
    assert rt.seq(config, "demo", "artifact:a").emission_epoch == 1
    assert rt.seq(config, "demo", "artifact:b").emission_epoch == 1  # independent
    assert rt.seq(config, "demo", "artifact:a").emission_epoch == 2


def test_distinct_runs_have_independent_epoch_streams() -> None:
    rt = make_runtime()
    assert rt.seq(config_for("run-A"), "demo", "k").emission_epoch == 1
    assert rt.seq(config_for("run-B"), "demo", "k").emission_epoch == 1
    assert rt.seq(config_for("run-A"), "demo", "k").emission_epoch == 2


# ---- 3. the make_<agent>_node convention is graph-compatible -----------------


def make_demo_node(rt: AgentRuntime) -> NodeFn:
    """A trivial example node factory following the documented convention:
    returns channel writes (never mutates state), mints its Seq via rt.seq."""

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        seq = rt.seq(config, "demo", "phase")
        return {"phase": "demo", "route": f"e{seq.emission_epoch}"}

    return _node


def test_demo_node_writes_channels_directly() -> None:
    rt = make_runtime()
    node = make_demo_node(rt)
    writes = node({"request": "x"}, config_for("run-A", step=2))
    assert writes == {"phase": "demo", "route": "e1"}


def test_demo_node_runs_inside_a_state_graph() -> None:
    rt = make_runtime()
    g = StateGraph(FactoryState)
    g.add_node("demo", make_demo_node(rt))
    g.add_edge(START, "demo")
    g.add_edge("demo", END)
    app = g.compile()

    final = app.invoke({"request": "hello"}, config_for("run-A"))
    assert final["phase"] == "demo"
    assert final["route"].startswith("e")
