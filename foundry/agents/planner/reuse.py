"""Checked-plan reuse: a fresh run of an identical request replays the planner's
model replies from an earlier run whose plan built and passed every gate, instead
of paying for planning again (35-60 min a pass on a local model).

What is stored is each planner call's parsed reply, keyed by its request (SOP,
schema, prompt, effort), under a content key over every planner input the prompts
do not fully carry. The plan's records are not stored: each is stamped with its
run's ``Seq``, so the replayed replies go through the same deterministic assembly
and come out as this run's records. A reply is replayed only for a byte-identical
request; any other call goes to the model as usual.

A run's replies are staged per planning pass under ``pending/<run>/`` and
promoted to the store only once the whole run has passed
(:func:`promote_if_passed`), so a plan whose build or tests failed is never
reused. ``FOUNDRY_PLAN_REUSE=0`` turns it off.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from foundry import progress
from foundry.agents.planner.schema import NODE_ID

REUSE_ENV = "FOUNDRY_PLAN_REUSE"
PLANS_DIR = "plans"
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
    local_thinking: str | None,
    base_urls: Mapping[str, str | None],
) -> str:
    """The content key: request/spec text, pinned scope, stack inputs (the pinned
    stack and this host's toolchains), the planner-relevant level table (which
    tiers are local — ticket size ceilings depend on it — the planner's own level,
    each level's server and the difficulty -> tier table), the local-thinking
    setting and the planner code digest."""
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
            "local_thinking": local_thinking,
            "base_urls": dict(base_urls),
            "code": code_digest(),
        }
    )[:32]


def call_key(sop: str, schema: Mapping[str, Any], content: str, effort: Any) -> str:
    return _digest([sop, schema, content, effort])


def _read(path: Path) -> dict[str, Any]:
    """The JSON object at ``path``; {} when it is missing. One that does not parse
    is moved aside to ``<name>.corrupt`` and logged, never silently dropped or
    overwritten."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        progress.log(f"planner: plan store {path} unreadable ({exc}); not reused")
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if isinstance(raw, dict):
        return raw
    aside = path.with_name(path.name + ".corrupt")
    with contextlib.suppress(OSError):
        path.replace(aside)
    progress.log(f"planner: plan store {path} is corrupt; moved to {aside.name}")
    return {}


def _write(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically: a unique temp file in the same directory, then one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive cross-process lock on ``path`` (held on ``<name>.lock``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def newest_prd(values: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The Seq of the planner's newest PRD on ``values``: the run's final plan."""
    seqs = [
        a.seq
        for a in values.get("artifacts") or ()
        if getattr(a, "kind", None) == "prd" and getattr(a.seq, "node_id", None) == NODE_ID
    ]
    return tuple(max(seqs)) if seqs else None


class PlanReuse:
    """One run's view of the plan store under ``<state>/plans``: replays from the
    checked plan stored under ``key``, stages this run's own replies per planning
    pass (``pending/<run>/<key>.json`` = ``{"passes": [{"prd", "calls"}, ...]}``)."""

    def __init__(self, root: Path, key: str, run: str) -> None:
        self.key = key
        self._stored_path = root / f"{key}.json"
        self._pending_path = root / "pending" / run / f"{key}.json"
        self._stored: dict[str, Any] | None = None
        passes = _read(self._pending_path).get("passes")
        self._passes: list[dict[str, Any]] = [
            p
            for p in (passes if isinstance(passes, list) else ())
            if isinstance(p, dict) and isinstance(p.get("calls"), dict)
        ]
        self._lock = threading.Lock()

    def replay(self, ck: str) -> dict[str, Any] | None:
        with self._lock:
            if self._stored is None:
                self._stored = _read(self._stored_path)
            hit = self._stored.get(ck)
        # A copy: the same reply may be replayed twice in one run.
        return json.loads(json.dumps(hit)) if isinstance(hit, dict) else None

    def begin_pass(self) -> None:
        with self._lock:
            self._passes.append({"prd": None, "calls": {}})

    def record(self, ck: str, parsed: dict[str, Any]) -> None:
        with self._lock:
            if not self._passes:
                self._passes.append({"prd": None, "calls": {}})
            self._passes[-1]["calls"][ck] = parsed
            _write(self._pending_path, {"passes": self._passes})

    def end_pass(self, prd: tuple[Any, ...] | None) -> None:
        """Stamp the open pass with the Seq of the PRD it emitted."""
        with self._lock:
            if self._passes:
                self._passes[-1]["prd"] = list(prd) if prd is not None else None
                _write(self._pending_path, {"passes": self._passes})

    def promote(self, values: Mapping[str, Any]) -> bool:
        """Merge into the store the replies that produced the run's final plan: the
        planning pass that emitted the newest PRD on ``values`` and the passes before
        it, whose replies (and the findings they drew) lead a replay up to it. A
        later pass is dropped. On a clash the stored reply wins, so a replay's chain
        stays one run's. Returns whether anything was promoted."""
        final = newest_prd(values)
        with self._lock:
            end = next(
                (
                    i
                    for i in range(len(self._passes) - 1, -1, -1)
                    if final is not None
                    and isinstance(self._passes[i].get("prd"), list)
                    and tuple(self._passes[i]["prd"]) == final
                ),
                None,
            )
            if end is None:
                return False
            chain: dict[str, Any] = {}
            for p in self._passes[: end + 1]:
                chain.update(p["calls"])
            if chain:
                with _locked(self._stored_path):
                    stored = _read(self._stored_path)
                    _write(self._stored_path, {**chain, **stored})
            self._stored = None
            self._pending_path.unlink(missing_ok=True)
            self._passes = []
            return bool(chain)


def promote_if_passed(reuse: PlanReuse, values: Mapping[str, Any], *, passed: bool) -> None:
    """A plan counts as checked only once the whole run built on it passed: a
    Spec-gate PASS alone would replay a plan whose Build or Test later failed."""
    if passed:
        reuse.promote(values)
