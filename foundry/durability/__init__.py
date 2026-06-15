"""Durability layer: transactional outbox + shared-transaction checkpointer.

architecture.md §5 — resume restores *internal* state; the dual-write between
the checkpoint DB and the outside world is handled by a transactional outbox,
not by hope. The :class:`OutboxRelay` is the ONLY component in Foundry that
touches external systems.
"""

from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.outbox import (
    DispatchResult,
    GateBearingPath3Error,
    OutboxError,
    OutboxRelay,
    OutboxStatus,
    PayloadMismatchError,
    append_audit,
    enqueue,
    ensure_outbox_schema,
    verify_audit_chain,
)

__all__ = [
    "DispatchResult",
    "FoundryCheckpointer",
    "GateBearingPath3Error",
    "OutboxError",
    "OutboxRelay",
    "OutboxStatus",
    "PayloadMismatchError",
    "append_audit",
    "enqueue",
    "ensure_outbox_schema",
    "verify_audit_chain",
]
