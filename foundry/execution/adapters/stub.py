"""Stub worker adapter — deterministic, egress-free substrate double. (plan §0.3)

The companion to :class:`~foundry.proxy.test_doubles.MockTransport`: where the
mock transport lets the proxy run with no live API key, this lets the
substrate seam run with no ``claude -p`` binary and no model call AT ALL. It is
the lowest-cost ``WorkerAdapter`` for driving the graph/CLI end-to-end and for
determinism property tests.

Contract honored (``foundry.execution.adapter_contract``):

* ``supports_turn_snapshots = True`` — eligibility flag for the substrate.
* :meth:`run` writes a tiny DETERMINISTIC diff into the sandbox worktree (so the
  ``snapshot_digest`` reflects real on-disk state), records the diff as a
  content-addressed ``cas://`` blob, returns advisory ``GateTriage`` ONLY (never
  a ``GateResult`` — LEARNINGS L5; agents do not sign verdicts), the
  ``turns_used``, and a ``snapshot_digest`` from ``sandbox.snapshot()``.

Determinism: the written diff is a pure function of the packet (its task body),
so identical inputs yield an identical worktree and therefore an identical
content-addressed ``snapshot_digest`` (``normalized_source_tree`` is timestamp-
and permission-free). All digests/keys use ``content_digest`` — never builtin
``hash()`` (LEARNINGS L7).
"""

from __future__ import annotations

from pathlib import Path

from foundry.canonical import content_digest
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

__all__ = ["StubAdapter"]


class StubAdapter:
    """Single deterministic file write -> recorded outputs. Holds no egress."""

    supports_turn_snapshots: bool = True
    name = "stub"

    def __init__(
        self,
        *,
        run_id: str = "dev",
        node_id: str = "stub",
        output_path: str = "STUB_CHANGE.txt",
        triage_note: str = "stub adapter: deterministic no-op change",
        self_reported_model: str | None = None,
    ) -> None:
        self._run_id = run_id
        self._node_id = node_id
        self._output_path = output_path
        self._triage_note = triage_note
        # Carried into AdapterResult but IGNORED for lineage (arch §2b).
        self._self_reported_model = self_reported_model

    def available(self) -> bool:
        return True  # no binary, no credential, no endpoint required

    def _payload(self, packet: TaskPacket) -> str:
        """Deterministic body derived purely from the task identity."""
        task_id = str(packet.task.get("id", "unknown"))
        digest = content_digest({"task": packet.task, "path": self._output_path})
        return f"stub change for task {task_id}\ndigest {digest}\n"

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        task_id = str(packet.task.get("id", "unknown"))
        run_against = f"task/{task_id}"

        # Write a tiny deterministic change into the worktree so the snapshot
        # reflects real on-disk state. cwd is pinned to the worktree by the
        # sandbox; resolve against it explicitly so we never touch the source.
        body = self._payload(packet)
        target = Path(sandbox.worktree) / self._output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

        # Record the change as a content-addressed blob (DS9: bodies out-of-band).
        diff_text = f"--- /dev/null\n+++ b/{self._output_path}\n@@\n+{body}"
        diff_uri = sandbox.put_blob(diff_text.encode("utf-8"))

        triage = (
            GateTriage(
                name="adapter-report",
                run_against=run_against,
                explanation=self._triage_note,
                produced_by=self.name,
                seq=Seq(self._run_id, 0, 0, self._node_id, 0),
            ),
        )

        return AdapterResult(
            diff_uri=diff_uri,
            test_source_uris=(),
            triage=triage,  # advisory ONLY (L5) — never a GateResult
            adapter_self_reported_model=self._self_reported_model,
            turns_used=1,
            snapshot_digest=sandbox.snapshot(),
        )
