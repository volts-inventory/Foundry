"""Worker adapters — the swappable substrate seam (architecture.md §2)."""

from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.execution.adapters.raw_react import RawReactAdapter

__all__ = ["ClaudeCodeAdapter", "RawReactAdapter"]
