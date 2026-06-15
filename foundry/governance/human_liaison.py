"""Human liaison — HITL escalation packaging. (gov §6, plan §0.2)

`package_escalation` wraps a deterministic `Trip` + evidence handles into an
immutable `EscalationPacket` and records it on the liaison's queue — a packet
is NEVER silently dropped:

- Default / gated profiles: `delivery="blocking"` — dispatch must wait for a
  human decision.
- **Self-judge zero-HITL profile** (plan §0.2): the liaison still records the
  packet (the trade is *owned and recorded*, not erased) but marks
  `delivery="deferred_log"` — a no-op delivery path the operator reviews
  asynchronously.
- **Regulated path** (`regulated=True`, the pack's mode veto): forces
  `delivery="blocking"` even under the self-judge profile — the regulated
  pack force-disables zero-HITL.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from foundry.monitors import Trip
from foundry.schemas.base import FrozenModel


class DeliveryMode(StrEnum):
    BLOCKING = "blocking"  # dispatch halts until a human decides
    DEFERRED_LOG = "deferred_log"  # recorded for async review; no-op delivery


class EscalationPacket(FrozenModel):
    """Frozen HITL packet: the trip + the evidence the human reviews."""

    trip: Trip
    evidence_uris: tuple[str, ...]
    delivery: DeliveryMode
    profile: str  # "default" | "self_judge"


class HumanLiaison:
    """Packages deterministic trips for human review.

    `queue` is the durable-record stand-in: every packet lands there
    regardless of delivery mode (never silently dropped).
    """

    def __init__(self, *, self_judge: bool = False, regulated: bool = False) -> None:
        self.self_judge = self_judge
        self.regulated = regulated
        self.queue: list[EscalationPacket] = []

    @property
    def profile(self) -> str:
        return "self_judge" if self.self_judge else "default"

    def package_escalation(self, trip: Trip, evidence_uris: Iterable[str]) -> EscalationPacket:
        # Regulated pack veto wins over the self-judge profile — forced blocking:
        if self.regulated or not self.self_judge:
            delivery = DeliveryMode.BLOCKING
        else:
            delivery = DeliveryMode.DEFERRED_LOG
        packet = EscalationPacket(
            trip=trip,
            evidence_uris=tuple(evidence_uris),
            delivery=delivery,
            profile=self.profile,
        )
        self.queue.append(packet)  # recorded unconditionally — never dropped
        return packet
