"""Every typed record reachable from a ``FactoryState`` channel round-trips.

The checkpoint serializer allowlists types by ``(module, qualname)``; a type NOT
on the list silently degrades to a plain ``dict`` on load. For an out-of-band
artifact BODY that fails loudly — the store rejects it fail-closed (D6). For a
record that rides a CHANNEL there is no such guard: the resumed run just gets a
dict where it expected a frozen model, and blows up at the first attribute
access, deep into a build. That is how ``risk_tier`` (read as ``tier.tier`` by
``assurance_gate_set``) and ``sign_offs`` (filtered on ``s.role`` at the release
boundary) were both missing from the allowlist without a test noticing.

So rather than assert a hand-written list of types — which is the same list that
went stale — this walks the channel annotations, recurses through the fields of
every frozen model it finds, and requires that everything typed it reaches is
allowlisted. A new channel, or a new typed field on an existing record, is
covered the day it is added.
"""

from __future__ import annotations

import types
from enum import Enum
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from foundry.durability.serde import FOUNDRY_MSGPACK_MODULES
from foundry.state.factory_state import FactoryState

# `factory_state` uses `from __future__ import annotations`, so its raw
# `__annotations__` are STRINGS. Resolve them to real types once, keeping
# `Annotated` extras so the fan-in channels' `list[X]` survives the unwrap.
CHANNELS: dict[str, Any] = get_type_hints(FactoryState, include_extras=True)

# Types the msgpack serializer handles natively — never allowlisted.
_BUILTIN = (str, int, float, bool, bytes, type(None), dict, list, tuple, set, frozenset)


def _unwrap(annotation: Any) -> list[Any]:
    """Every concrete type inside an annotation.

    Flattens the shapes FactoryState actually uses: ``Annotated[list[X], reducer]``
    (a fan-in channel with its merge reducer), ``X | None`` (an optional
    single-writer channel), and the plain ``list[X]`` / ``X`` cases.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _unwrap(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return [t for arg in get_args(annotation) for t in _unwrap(arg)]
    if origin in (list, tuple, set, frozenset):
        return [t for arg in get_args(annotation) for t in _unwrap(arg)]
    return [annotation]


def _typed_records(annotation: Any, seen: set[type]) -> set[type]:
    """Frozen models / enums / named tuples reachable from ``annotation``.

    Recurses into a model's own fields, so a record is covered along with every
    typed thing it carries (the ``ChangeRiskTier`` enum inside ``TierDecision``
    is exactly the case a shallow check misses)."""
    found: set[type] = set()
    for candidate in _unwrap(annotation):
        if not isinstance(candidate, type) or candidate in seen or candidate in _BUILTIN:
            continue
        seen.add(candidate)
        if issubclass(candidate, BaseModel):
            found.add(candidate)
            for field in candidate.model_fields.values():
                found |= _typed_records(field.annotation, seen)
        elif issubclass(candidate, Enum):
            found.add(candidate)
        elif issubclass(candidate, tuple) and hasattr(candidate, "_fields"):
            found.add(candidate)  # a NamedTuple channel record (Seq)
    return found


def test_every_typed_channel_record_is_allowlisted() -> None:
    seen: set[type] = set()
    reachable: set[type] = set()
    for annotation in CHANNELS.values():
        reachable |= _typed_records(annotation, seen)

    allowed = set(FOUNDRY_MSGPACK_MODULES)
    missing = sorted(
        f"{t.__module__}.{t.__qualname__}"
        for t in reachable
        if (t.__module__, t.__qualname__) not in allowed
    )

    assert not missing, (
        "types reachable from a FactoryState channel but absent from "
        f"FOUNDRY_MSGPACK_MODULES (they degrade to dict on resume): {missing}"
    )


def test_the_walk_actually_reaches_nested_records() -> None:
    # Guards the test above against silently passing because the walk found
    # nothing: these are the records the two known resume bugs involved, and
    # ChangeRiskTier is only reachable THROUGH TierDecision's fields.
    seen: set[type] = set()
    reachable = {
        f"{t.__module__}.{t.__qualname__}"
        for annotation in CHANNELS.values()
        for t in _typed_records(annotation, seen)
    }

    assert "foundry.schemas.governance.TierDecision" in reachable
    assert "foundry.schemas.governance.ChangeRiskTier" in reachable
    assert "foundry.schemas.governance.SignOffRecord" in reachable
    assert "foundry.schemas.governance.ScopeDecision" in reachable
    assert "foundry.state.seq.Seq" in reachable
