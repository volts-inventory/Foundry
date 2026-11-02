"""Durability layer: the shared-transaction checkpointer, CAS, and file-backed state.

architecture.md §5 — resume restores *internal* state from the checkpoint DB.
"""

from foundry.durability.checkpointer import FoundryCheckpointer

__all__ = ["FoundryCheckpointer"]
