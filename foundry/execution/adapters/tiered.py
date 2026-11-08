"""Per-ticket coding substrate for the model modes (workshop plan, Phase 4b).

A ticket's tier picks its level; the level decides the substrate. A cloud level
runs the cloud adapter (Claude Code under ``claude-cli``), a local level runs the
``proxy_agent`` loop, whose every call rides the proxy's local transport. A packet
with no tier (the fixer) runs on T1.

One local model is loaded, so local sessions queue: each holds the proxy's local
slot for its whole run while cloud sessions keep running in parallel.

The sandbox's credential grant is per substrate too: :meth:`for_tier` is what the
sandbox factory asks, so a local session's cage gets no model credential even
when the cloud substrate declares one. The adapter's OWN declarations are empty,
so any caller that skips ``for_tier`` gets the scrubbed cage (fail closed).

Escalation (hybrid only): ``escalation_tier`` names the cloud level a local ticket
re-runs on once when the local model can't finish it. This adapter only declares
it and recognizes a local session that stopped (:func:`session_stop_reason`); the
engineering node keeps a stopped session's work (marked :data:`STOPPED_TRIAGE`
for the next wave check) and owns any re-run, so it gets a fresh sandbox built
for the cloud tier and is admitted through the work pool like any other session.
"""

from __future__ import annotations

import contextlib
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from foundry.execution.adapter_contract import AdapterResult, TaskPacket
    from foundry.execution.sandbox import Sandbox

__all__ = [
    "ESCALATED_TRIAGE",
    "ESCALATE_DECLINED_TRIAGE",
    "ESCALATE_FALLBACK_TRIAGE",
    "ESCALATE_REQUEST_TRIAGE",
    "STOPPED_TRIAGE",
    "TieredAdapter",
    "session_stop_reason",
]

# A ticket re-run on the cloud level (one per ticket per run), and a wave check's
# request for one.
ESCALATED_TRIAGE = "escalated"
ESCALATE_REQUEST_TRIAGE = "escalate-request"
# A request the work pool's quota refused: settled without spending the escalation.
ESCALATE_DECLINED_TRIAGE = "escalate-declined"
# Beside a request the whole-wave fallback made (no ticket named): the fallback
# runs once per wave, whatever named or stopped-early escalations came before.
ESCALATE_FALLBACK_TRIAGE = "escalate-fallback"
# A local session that stopped early but whose partial work was kept for the wave
# check: a failing check escalates it on its first failure instead of its second.
STOPPED_TRIAGE = "stopped-early"
# How a proxy-agent session reports that it ended without finishing (the last: it
# spent its gate checks and its own work was still red, proxy_agent.CHECK_EXHAUSTED).
_STOP_MARKERS = ("stopped early:", "turn budget exhausted", "check budget exhausted")


def session_stop_reason(triage: Iterable[Any]) -> str | None:
    """The adapter-report note saying a session hit its turn cap, was stopped by
    the repeat / read-thrash detector, or ran out of gate checks still red, else
    None."""
    for note in triage:
        text = str(getattr(note, "explanation", ""))
        if text.startswith(_STOP_MARKERS):
            return text
    return None


class TieredAdapter:
    """Dispatch each packet to the cloud or the local substrate by its tier."""

    name = "tiered"
    credential_grant: tuple[str, ...] = ()
    home_passthrough: bool = False
    toolchain_env: bool = False

    def __init__(
        self,
        *,
        cloud: Any,
        local: Any,
        local_tiers: Iterable[str],
        local_slot: Callable[[], AbstractContextManager[Any]],
        default_tier: str = "T1",
        escalation_tier: str | None = None,
    ) -> None:
        self.cloud = cloud
        self.local = local
        self._local_tiers = frozenset(local_tiers)
        self._local_slot = local_slot
        self._default_tier = default_tier
        self.escalation_tier = escalation_tier
        self.supports_turn_snapshots = bool(
            getattr(cloud, "supports_turn_snapshots", False)
            and getattr(local, "supports_turn_snapshots", False)
        )

    def available(self) -> bool:
        return bool(self.cloud.available() and self.local.available())

    def is_local(self, tier: str | None) -> bool:
        return (tier or self._default_tier) in self._local_tiers

    def for_tier(self, tier: str | None) -> Any:
        return self.local if self.is_local(tier) else self.cloud

    @property
    def usage(self) -> list[dict[str, int]]:
        return list(getattr(self.cloud, "usage", ()))

    @property
    def token_meter(self) -> Any:
        return getattr(self.cloud, "token_meter", None)

    @token_meter.setter
    def token_meter(self, meter: Any) -> None:
        if hasattr(self.cloud, "token_meter"):
            self.cloud.token_meter = meter

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tier = packet.task.get("tier")
        if self.is_local(str(tier) if tier else None):
            from foundry.gates.self_check import local_session

            # The session's opening gate check runs before it holds the local slot.
            prepared = getattr(self.local, "prepared", None)
            with (
                local_session(),
                prepared(packet) if callable(prepared) else contextlib.nullcontext(),
                self._local_slot(),
            ):
                result: AdapterResult = self.local.run(packet, sandbox)
            return result
        cloud_result: AdapterResult = self.cloud.run(packet, sandbox)
        return cloud_result
