"""Phase-1 agent layer — shared runtime + per-agent node factories.

Re-exports the shared seam (``AgentRuntime`` / ``NodeFn``) for convenience.
Kept import-cheap: ``_runtime`` does no heavy imports at module top, and the
per-agent factory modules are imported by their callers, not eagerly here.
"""

from __future__ import annotations

from foundry.agents._runtime import AgentRuntime, NodeFn, run_id_of

__all__ = ["AgentRuntime", "NodeFn", "run_id_of"]
