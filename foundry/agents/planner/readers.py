"""Lenient readers of the model's payload and of this node's own records."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from foundry.agents.planner.schema import NODE_ID
from foundry.schemas.product import IntentBrief

if TYPE_CHECKING:
    from foundry.agents._runtime import AgentRuntime
    from foundry.state.factory_state import FactoryState


# CLI backends don't grammar-enforce the schema, so one off-schema field must
# degrade to a default rather than raise and lose the whole plan.

_E = TypeVar("_E", bound=StrEnum)


def _str(raw: Any, default: str = "") -> str:
    return default if raw is None else str(raw)


def _strs(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x) for x in raw if x is not None)


def _enum(cls: type[_E], raw: Any, default: _E) -> _E:
    key = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    for member in cls:
        if key in (member.value.lower(), member.name.lower().replace("_", "-")):
            return member
    return default


def _int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(float(str(raw).strip().replace(",", "").replace("_", "")))
    except (ValueError, OverflowError):  # also NaN / inf
        return 0


def _bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1"}
    return bool(raw)


def _objs(raw: Any) -> list[dict[str, Any]]:
    # CLI backends don't grammar-enforce the schema; skip a non-object.
    return [item for item in raw or () if isinstance(item, dict)]


def _section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _steps(raw: Any) -> tuple[str, ...]:
    return tuple(x.strip() for x in _strs(raw) if x.strip())


def _uniq(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in items if x.strip()))


_M = TypeVar("_M")


def _newest_by_id(state: FactoryState, rt: AgentRuntime, kind: str, cls: type[_M]) -> dict[str, _M]:
    """This node's newest record per id (``name`` for contracts) of ``kind``, in
    Seq order of each id's first record; empty without a store."""
    if rt.cas is None:
        return {}
    handles = sorted(
        (a for a in state.get("artifacts") or () if a.kind == kind and a.seq.node_id == NODE_ID),
        key=lambda a: a.seq,
    )
    newest: dict[str, _M] = {}
    for a in handles:
        try:
            body = rt.cas.resolve(a)
        except Exception:
            continue
        if isinstance(body, cls):
            newest[str(getattr(body, "id", None) or getattr(body, "name", ""))] = body
    return newest


def _prose(rt: AgentRuntime, uri: str) -> str:
    try:
        return str(getattr(rt.cas.get_body(uri), "text", "")) if rt.cas else ""
    except Exception:
        return ""


def _newest_intent(state: FactoryState, rt: AgentRuntime) -> IntentBrief | None:
    briefs = list(_newest_by_id(state, rt, "intent_brief", IntentBrief).values())
    return max(briefs, key=lambda b: b.seq) if briefs else None
