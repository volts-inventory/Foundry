"""The state backbone: typed channels + merge semantics. Build-order step 1.

Lazy re-exports: `schemas/` modules import `foundry.state.seq`, and
`factory_state` imports `schemas/` — an eager re-export here would make that
a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.state.factory_state import FactoryState

__all__ = ["FactoryState", "Seq"]


def __getattr__(name: str) -> Any:
    if name == "FactoryState":
        from foundry.state.factory_state import FactoryState

        return FactoryState
    raise AttributeError(name)
