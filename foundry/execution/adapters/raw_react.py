"""Raw ReAct adapter — minimal provider-agnostic single-loop substrate.

Deliberately thin (arch §2: the inner-loop sophistication lives in the
substrate by design; this adapter exists as the lowest-common-denominator
seam and as the test double's host). One completion per run:

* `complete_fn(prompt) -> str` is the injected proxy seam — in production it
  is a closure over the §2b mediating proxy (the only credential holder); in
  tests it is a fake. This adapter holds NO credentials and knows NO endpoint
  beyond what the packet/sandbox expose.
* The model is asked for a unified diff plus triage notes in fenced blocks.
  The adapter APPLIES NOTHING — it records the diff as a content-addressed
  blob, the triage notes as `GateTriage` (advisory, never a verdict), and a
  content-addressed turn snapshot of the (untouched) worktree.

Self-reported model is carried but IGNORED for lineage — served family comes
from proxy observation only (arch §2b).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
    assemble_prompt,
    packet_tier_scope,
)
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

__all__ = ["RawReactAdapter"]

_EPILOGUE = (
    "Respond with EXACTLY two fenced blocks:\n"
    "```diff\n<a unified diff implementing the task>\n```\n"
    "```triage\n<one advisory remediation note per line>\n```"
)

_DIFF_RE = re.compile(r"```diff\n(.*?)```", re.DOTALL)
_TRIAGE_RE = re.compile(r"```triage\n(.*?)```", re.DOTALL)


class RawReactAdapter:
    """Single prompt -> single completion -> recorded outputs. Nothing more."""

    supports_turn_snapshots: bool = True
    name = "raw-react"

    def __init__(
        self,
        complete_fn: Callable[[str], str],
        *,
        run_id: str = "dev",
        node_id: str = "raw-react",
        self_reported_model: str | None = None,
    ) -> None:
        self._complete = complete_fn
        self._run_id = run_id
        self._node_id = node_id
        # Carried into AdapterResult but IGNORED for lineage (arch §2b).
        self._self_reported_model = self_reported_model

    def available(self) -> bool:
        return True  # nothing but the injected completion seam is required

    def build_prompt(self, packet: TaskPacket) -> str:
        return assemble_prompt(packet, epilogue=_EPILOGUE)

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        with packet_tier_scope(packet):  # the completion closure picks the packet's model tier
            response = self._complete(self.build_prompt(packet))

        diff_match = _DIFF_RE.search(response)
        diff_text = diff_match.group(1) if diff_match else ""
        diff_uri = sandbox.put_blob(diff_text.encode("utf-8"))

        run_against = f"task/{packet.task.get('id', 'unknown')}"
        triage_match = _TRIAGE_RE.search(response)
        notes = (
            [line.strip() for line in triage_match.group(1).splitlines() if line.strip()]
            if triage_match
            else [f"unparseable response recorded verbatim: {response[:500]!r}"]
        )
        triage = tuple(
            GateTriage(
                name="adapter-report",
                run_against=run_against,
                explanation=note,
                produced_by=self.name,
                seq=Seq(self._run_id, 0, 0, self._node_id, i),
            )
            for i, note in enumerate(notes)
        )

        return AdapterResult(
            diff_uri=diff_uri,
            test_source_uris=(),
            triage=triage,
            adapter_self_reported_model=self._self_reported_model,
            turns_used=1,
            snapshot_digest=sandbox.snapshot(),
        )
