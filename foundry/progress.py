"""Per-call progress lines: on stderr when ``FOUNDRY_PROGRESS`` is on, and to the
run's :func:`sink` when one is installed (the Workshop's live view).

A planner pass or an agent session can run for minutes inside one graph node, so
node-level progress alone looks hung; each model call and agent session logs its
start and its end (duration, tokens, run total against the cap)."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

# A ContextVar so concurrent runs in one process each reach their own listener. The
# graph's node threads (LangGraph), the work pool's workers and the planner's epic
# pool each run in a copy of the submitting context, so the sink follows the run.
_SINK: ContextVar[Callable[[str], None] | None] = ContextVar("foundry_progress_sink", default=None)


def env_flag(name: str) -> bool:
    """An opt-in env switch: on unless unset, empty, 0, false or no."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def enabled() -> bool:
    return env_flag("FOUNDRY_PROGRESS")


@contextlib.contextmanager
def sink(cb: Callable[[str], None]) -> Iterator[None]:
    """Send every :func:`log` line in this context to ``cb`` for the duration."""
    token = _SINK.set(cb)
    try:
        yield
    finally:
        _SINK.reset(token)


def log(message: str, *, stderr: bool | None = None) -> None:
    """``stderr``: print whatever ``FOUNDRY_PROGRESS`` says (None), always or never.
    The sink gets the line either way; a failing sink never breaks the run."""
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    if enabled() if stderr is None else stderr:
        print(line, file=sys.stderr, flush=True)
    cb = _SINK.get()
    if cb is not None:
        with contextlib.suppress(Exception):
            cb(line)


def finished(label: str, started: float, usage: Mapping[str, Any] | None, meter: Any) -> None:
    """``label`` done in Ns, with this call's weighted tokens and the run total."""
    if _SINK.get() is None and not enabled():
        return
    from foundry.proxy.token_meter import weighted_tokens

    secs = time.monotonic() - started
    took = f"{secs / 60:.1f}m" if secs >= 90 else f"{secs:.0f}s"
    tokens = ""
    if usage:
        # Raw prompt tokens (cache reads and writes included) beside the output: the
        # bench's input/output ratio per node reads them off this line.
        sent = sum(
            int(usage.get(k, 0) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        out = int(usage.get("output_tokens", 0) or 0)
        tokens = f", {weighted_tokens(usage) / 1000:.0f}k tokens (in {sent}, out {out})"
    total = ""
    if meter is not None:
        of = f" of {meter.cap / 1e6:.1f}M" if meter.cap is not None else ""
        total = f" (run {meter.used / 1e6:.2f}M{of})"
    log(f"done  {label} in {took}{tokens}{total}")
