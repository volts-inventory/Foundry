"""Phase 2b (ROADMAP): the shared evolving tree — serial tasks compose.

Each serial task's sandbox base now includes its predecessors' slices (merge
order), applied BEFORE the base snapshot so the recorded base — and the
idempotency key — reflect what the task actually built against. The pooled
(parallel) path stays independent by design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.implementation import Task
from foundry.state.seq import Seq

from foundry.agents._runtime import AgentRuntime
from foundry.durability.artifact_store import ArtifactStore
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport

from tests.test_agents_implementation import KEY, KEY_ID, config_for, two_constraints
from foundry.gates import ZoneBSigner


def cas_runtime(adapter: Any) -> AgentRuntime:
    proxy = ModelProviderProxy(MockTransport(default_text="{}"), signer=ZoneBSigner(KEY, KEY_ID))
    return AgentRuntime(proxy=proxy, adapter=adapter, cas=ArtifactStore())


def a_seq(local: int) -> Seq:
    return Seq("run", 1, 0, "senior_engineer", local)


def a_task(tid: str, local: int) -> Task:
    return Task(id=tid, spec_uri=f"cas://{tid}", criterion_ids=(), branch=f"task/{tid}", seq=a_seq(local))


class SnapshotWritingAdapter:
    """Writes one file per task into the worktree and records, per task, which
    predecessor files were VISIBLE in its sandbox — the assertion target."""

    supports_turn_snapshots = True
    name = "test-snapshot-writer"

    def __init__(self) -> None:
        self.visible: dict[str, list[str]] = {}

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task["id"])
        worktree = Path(sandbox.worktree)
        self.visible[tid] = sorted(
            p.name for p in worktree.iterdir() if p.is_file() and p.suffix == ".py"
        )
        (worktree / f"{tid}.py").write_text(f"# authored by {tid}\n")
        blob = json.dumps(
            {"kind": "full-file-snapshot", "files": {f"{tid}.py": f"# authored by {tid}\n"}}
        )
        return AdapterResult(
            diff_uri=sandbox.put_blob(blob.encode("utf-8")),
            snapshot_digest=sandbox.snapshot(),
        )


def test_serial_tasks_compose_on_shared_tree(tmp_path: Path) -> None:
    (tmp_path / "seed").mkdir()
    (tmp_path / "seed" / "README.md").write_text("seed\n")
    adapter = SnapshotWritingAdapter()
    rt = cas_runtime(adapter)

    from foundry.agents.implementation.junior_engineer import make_junior_engineer_node

    node = make_junior_engineer_node(rt, source_dir=str(tmp_path / "seed"))
    writes = node(
        {
            "tasks": [a_task("t-one", 0).model_dump(mode="json"),
                      a_task("t-two", 1).model_dump(mode="json")],
            "pinned_constraints": two_constraints(),
        },
        config_for("run-shared"),
    )
    assert adapter.visible["t-one"] == []  # first task: scaffold only
    assert adapter.visible["t-two"] == ["t-one.py"]  # second task SEES the first's slice
    # The overlay precedes snapshot(): the two tasks record DIFFERENT base tree
    # digests, so a predecessor change correctly supersedes the successor's key.
    markers = {t["id"]: t["base_tree_sha"] for t in writes["tasks"]}
    assert markers["t-one"] != markers["t-two"]
