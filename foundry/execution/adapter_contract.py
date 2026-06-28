"""Coding-agent substrate contract. (arch §2/§2b, plan §3.4)

The per-node inner loop is a pluggable adapter, not a core dependency:

* **in** — a `TaskPacket`: task + `pinned_constraints` (VERBATIM text) +
  retrieved spans + a scoped sandboxed worktree + a turn/cost budget + (at
  most) the proxy endpoint, the only egress an adapter may be told about.
* **out** — an `AdapterResult`: a content-addressed diff handle + test
  *sources* + `GateTriage` (advisory, never a verdict — gov §1c) + a turn
  `snapshot_digest`.

LINEAGE IS PROXY-OBSERVED, NEVER SELF-REPORTED (arch §2b). The
`adapter_self_reported_model` field exists so the self-report can be carried
for *cross-checking* (an adapter-declared family that disagrees with proxy
observation is rejected), but it is IGNORED for `served_family` / manifest
stamping: the mediating proxy — the only credential holder — observes the
actual provider/model each request hits and stamps lineage outside the
adapter. Nothing downstream may read this field as lineage.

Adapters never sign a verdict. Deterministic verdicts are computed and signed
by the trusted two-zone runner; judgment verdicts carry a proxy-stamped
binding. The adapter's output gate channel is `GateTriage` only.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from foundry.execution.sandbox import Sandbox
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateTriage

__all__ = [
    "AdapterResult",
    "AdapterUnavailableError",
    "TaskPacket",
    "WorkerAdapter",
    "assemble_prompt",
]


class AdapterUnavailableError(RuntimeError):
    """The adapter's substrate (e.g. its CLI binary) is not present here."""


class TaskPacket(FrozenModel):
    """Everything an adapter receives. Immutable: a packet is superseded,
    never edited mid-run."""

    task: dict[str, Any]  # schema-lite task body (id, title, instructions, ...)
    pinned_constraints: tuple[str, ...] = Field(
        default=(),
        description=(
            "Architecture-pinned constraint texts, injected into the adapter "
            "prompt VERBATIM (byte-for-byte) and FIRST — never paraphrased, "
            "summarized, or reordered behind task text (arch §2, plan §3.4)."
        ),
    )
    retrieved_spans: tuple[str, ...] = ()
    prior_failures: tuple[str, ...] = Field(
        default=(),
        description=(
            "Findings from a PRIOR failed gate verification (the fix loop): the "
            "specific failing tests + assertions from the last attempt, so this "
            "attempt fixes THAT failure instead of re-rolling blind. Empty on the "
            "first attempt."
        ),
    )
    worktree_path: str
    turn_budget: int
    cost_budget_microusd: int  # integer micro-USD, never floats (arch §8)
    proxy_endpoint: str | None = Field(
        default=None,
        description=(
            "The mediating model-provider proxy — the ONLY egress an adapter "
            "may be told about (arch §2b). None = no egress at all."
        ),
    )


class AdapterResult(FrozenModel):
    """Everything an adapter returns. Diff and test sources travel as
    content-addressed handles (DS9: bodies never live in state)."""

    diff_uri: str  # content-addressed handle (cas://sha256/<hex>)
    test_source_uris: tuple[str, ...] = ()
    triage: tuple[GateTriage, ...] = ()  # advisory notes; NEVER a verdict (gov §1c)
    adapter_self_reported_model: str | None = Field(
        default=None,
        description=(
            "Carried but IGNORED for lineage: served_family/served_model_id "
            "come from proxy observation ONLY (arch §2b). Kept solely so a "
            "self-report that DISAGREES with the proxy can be rejected."
        ),
    )
    turns_used: int = 1
    snapshot_digest: str  # content-addressed turn snapshot (Sandbox.snapshot)


def assemble_prompt(packet: TaskPacket, *, epilogue: str = "") -> str:
    """Canonical prompt scaffold for adapters.

    Pinned constraints go FIRST and VERBATIM — each constraint string is
    embedded byte-for-byte, never paraphrased — then the task body, then the
    retrieved spans, then any adapter-specific output-format epilogue.
    """
    parts: list[str] = []
    if packet.pinned_constraints:
        parts.append("PINNED CONSTRAINTS (verbatim, non-negotiable):")
        parts.extend(packet.pinned_constraints)
        parts.append("")
    parts.append("TASK:")
    parts.append(json.dumps(packet.task, indent=2, sort_keys=True, ensure_ascii=False))
    if packet.prior_failures:
        parts.append("")
        parts.append(
            "YOUR PREVIOUS ATTEMPT FAILED VERIFICATION. Fix these SPECIFIC failures "
            "(make the implementation and tests agree — change whichever is wrong):"
        )
        parts.extend(packet.prior_failures)
    if packet.retrieved_spans:
        parts.append("")
        parts.append("RETRIEVED CONTEXT SPANS:")
        parts.extend(packet.retrieved_spans)
    if epilogue:
        parts.append("")
        parts.append(epilogue)
    return "\n".join(parts)


@runtime_checkable
class WorkerAdapter(Protocol):
    """The swappable substrate seam (Claude Code, Codex CLI, OpenHands, raw
    ReAct loop, ...).

    Long-running adapters must support incremental resumable progress via
    content-addressed turn snapshots and EXTERNAL metering (proxy token/USD +
    sandbox hard-kill; adapter-reported budget is advisory only — arch §2).
    An adapter that cannot meet that contract is ineligible for the substrate;
    `supports_turn_snapshots` is the dev-grade eligibility flag.
    """

    supports_turn_snapshots: bool

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        """Execute the task inside the sandbox; return diff + triage + snapshot."""
        ...
