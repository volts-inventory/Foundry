"""Execution layer: sandbox cage + coding-agent substrate contract.

(architecture.md §2/§2b, implementation-plan.md §3.2/§3.4)
"""

from foundry.execution.adapter_contract import (
    AdapterResult,
    AdapterUnavailableError,
    TaskPacket,
    WorkerAdapter,
    assemble_prompt,
)
from foundry.execution.sandbox import Sandbox, SandboxResult

__all__ = [
    "AdapterResult",
    "AdapterUnavailableError",
    "Sandbox",
    "SandboxResult",
    "TaskPacket",
    "WorkerAdapter",
    "assemble_prompt",
]
