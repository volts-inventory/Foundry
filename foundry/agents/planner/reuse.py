"""Checked-plan reuse: a fresh run of an identical request replays the planner's
model replies from an earlier run whose plan passed the Spec gate, instead of
paying for planning again (35-60 min a pass on a local model).

What is stored is each planner call's parsed reply, keyed by its request (SOP,
schema, prompt, effort), under a content key over every planner input the prompts
do not fully carry. The plan's records are not stored: each is stamped with its
run's ``Seq``, so the replayed replies go through the same deterministic assembly
and come out as this run's records. A reply is replayed only for a byte-identical
request; any other call goes to the model as usual.

A run's replies are staged under ``pending/<run>/`` and promoted to the store only
once the run's Spec gate has passed (:func:`promote_if_checked`), so a plan the
gate never accepted is never reused. ``FOUNDRY_PLAN_REUSE=0`` turns it off.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from foundry.schemas.gates import GateStatus

REUSE_ENV = "FOUNDRY_PLAN_REUSE"
PLANS_DIR = "plans"
_SPEC_GATE = "spec"
_PACKAGE = Path(__file__).resolve().parent
_SCHEMAS = _PACKAGE.parent.parent / "schemas"


def reuse_enabled() -> bool:
    return os.environ.get(REUSE_ENV, "").strip() != "0"


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


@functools.cache
def code_digest() -> str:
    """Every planner module plus the record schemas: a change to either may change
    what the same replies assemble into."""
    h = hashlib.sha256()
    for root in (_PACKAGE, _SCHEMAS):
        for path in sorted(root.rglob("*.py")):
            h.update(str(path.relative_to(root.parent)).encode() + b"\0")
            h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def plan_key(
    *,
    request: str,
    spec: str,
    scope: str,
    stack: Mapping[str, Any] | None,
    languages: frozenset[str],
    local_tiers: frozenset[str],
    planner_level: Mapping[str, Any],
    ticket_tiers: Mapping[Any, str] | None,
) -> str:
    """The content key: request/spec text, pinned scope, stack inputs (the pinned
    stack and this host's toolchains), the planner-relevant level table (which
    tiers are local — ticket size ceilings depend on it — the planner's own level
    and the difficulty -> tier table) and the planner code digest."""
    return _digest(
        {
            "request": request,
            "spec": spec,
            "scope": scope,
            "stack": dict(stack) if stack is not None else None,
            "languages": sorted(languages),
            "local_tiers": sorted(local_tiers),
            "planner_level": dict(planner_level),
            "ticket_tiers": (
                {str(getattr(k, "value", k)): v for k, v in ticket_tiers.items()}
                if ticket_tiers is not None
                else None
            ),
            "code": code_digest(),
        }
    )[:32]


def call_key(sop: str, schema: Mapping[str, Any], content: str, effort: Any) -> str:
    return _digest([sop, schema, content, effort])


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, calls: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(calls, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class PlanReuse:
    """One run's view of the plan store under ``<state>/plans``: replays from the
    checked plan stored under ``key``, stages this run's own replies."""

    def __init__(self, root: Path, key: str, run: str) -> None:
        self.key = key
        self._stored_path = root / f"{key}.json"
        self._pending_path = root / "pending" / run / f"{key}.json"
        self._stored: dict[str, Any] | None = None
        self._pending = _read(self._pending_path)
        self._lock = threading.Lock()

    def replay(self, ck: str) -> dict[str, Any] | None:
        with self._lock:
            if self._stored is None:
                self._stored = _read(self._stored_path)
            hit = self._stored.get(ck)
        # A copy: the same reply may be replayed twice in one run.
        return json.loads(json.dumps(hit)) if isinstance(hit, dict) else None

    def record(self, ck: str, parsed: dict[str, Any]) -> None:
        with self._lock:
            self._pending[ck] = parsed
            _write(self._pending_path, self._pending)

    def promote(self) -> None:
        """Merge this run's staged replies into the store (the stored reply wins a
        clash, so a replay's chain stays one run's)."""
        with self._lock:
            if not self._pending:
                return
            stored = _read(self._stored_path)
            _write(self._stored_path, {**self._pending, **stored})
            self._stored = None
            self._pending_path.unlink(missing_ok=True)
            self._pending = {}


def spec_passed(values: Mapping[str, Any]) -> bool:
    """True when the run's newest Spec-gate verdict is a PASS."""
    specs = [g for g in values.get("gates") or () if getattr(g, "name", None) == _SPEC_GATE]
    return bool(specs) and max(specs, key=lambda g: g.seq).status is GateStatus.PASS


def promote_if_checked(reuse: PlanReuse, values: Mapping[str, Any]) -> None:
    if spec_passed(values):
        reuse.promote()
