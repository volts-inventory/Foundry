"""Shared agent-runtime scaffolding (T-M1-04a) — the seam the four Phase-1
agent waves are built against.

Drives the REAL :class:`AgentRuntime` over a REAL :class:`ModelProviderProxy`
backed by :class:`MockTransport` (no live API key, LEARNINGS D1). Asserts the
``rt.seq`` semantics match ``foundry/graph/skeleton.py`` (right run_id,
resume-monotonic ``emission_epoch`` per ``(node_id, key)``, independent key
streams).
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from foundry.agents import AgentRuntime, run_id_of
from foundry.gates.attestation import ZoneBSigner
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport

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


def test_default_epochs_are_distinct_per_runtime() -> None:
    assert make_runtime().epochs is not make_runtime().epochs


# ---- seq() semantics mirror skeleton.py ------------------------------------


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


def test_epoch_streams_are_independent_per_key_and_run() -> None:
    rt = make_runtime()
    config = config_for("run-A")
    assert rt.seq(config, "demo", "artifact:a").emission_epoch == 1
    assert rt.seq(config, "demo", "artifact:b").emission_epoch == 1  # independent key
    assert rt.seq(config, "demo", "artifact:a").emission_epoch == 2
    assert rt.seq(config_for("run-B"), "demo", "artifact:a").emission_epoch == 1  # run
    assert rt.seq(config, "demo", "artifact:a").emission_epoch == 3
