"""Per-call progress lines on stderr when ``FOUNDRY_PROGRESS`` is on.

A planner pass or an agent session can run for minutes inside one graph node, so
node-level progress alone looks hung; each model call and agent session logs its
start and its end (duration, tokens, run total against the cap)."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from typing import Any


def enabled() -> bool:
    return os.environ.get("FOUNDRY_PROGRESS", "").strip().lower() not in ("", "0", "false", "no")


def log(message: str) -> None:
    if enabled():
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def finished(label: str, started: float, usage: Mapping[str, Any] | None, meter: Any) -> None:
    """``label`` done in Ns, with this call's weighted tokens and the run total."""
    if not enabled():
        return
    from foundry.proxy.token_meter import weighted_tokens

    secs = time.monotonic() - started
    took = f"{secs / 60:.1f}m" if secs >= 90 else f"{secs:.0f}s"
    tokens = f", {weighted_tokens(usage) / 1000:.0f}k tokens" if usage else ""
    total = (f" (run {meter.used / 1e6:.2f}M of {meter.cap / 1e6:.1f}M)"
             if meter is not None else "")
    log(f"done  {label} in {took}{tokens}{total}")
