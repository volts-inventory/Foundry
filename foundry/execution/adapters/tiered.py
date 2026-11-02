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
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from foundry.execution.adapter_contract import AdapterResult, TaskPacket
    from foundry.execution.sandbox import Sandbox

__all__ = ["TieredAdapter"]


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
    ) -> None:
        self.cloud = cloud
        self.local = local
        self._local_tiers = frozenset(local_tiers)
        self._local_slot = local_slot
        self._default_tier = default_tier
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
            with self._local_slot():
                result: AdapterResult = self.local.run(packet, sandbox)
            return result
        cloud_result: AdapterResult = self.cloud.run(packet, sandbox)
        return cloud_result
