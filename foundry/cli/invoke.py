"""Graph drivers: streaming invoke with stage progress, resume seeding from a
checkpoint, and the transient-failure auto-resume loop."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from foundry import progress
from foundry.graph.supervisor import RESUME_PHASE
from foundry.proxy.model_provider_proxy import is_transient_failure
from foundry.proxy.token_meter import BuildStopped, TimeCapExceeded, TokenMeter

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.state.factory_state import FactoryState


# ---- graph drivers ----------------------------------------------------------


def _progress_enabled() -> bool:
    """``FOUNDRY_PROGRESS`` truthy ⇒ stream per-node stage progress to stderr.

    A real backend build makes many sequential model calls and can run for
    minutes with NO output (the summary prints only at the end) — on a Pi /
    over the LAN that looks hung. This surfaces each node as it completes so the
    operator can SEE the build advancing. Off by default (byte-identical)."""
    return progress.enabled()


def _invoke_streaming(app: Any, seed: FactoryState | None, config: RunnableConfig) -> FactoryState:
    """Drive the graph with ``stream`` (updates mode), printing each completed
    node + elapsed time to stderr, then return the terminal channel state."""
    import time

    t0 = time.monotonic()
    # subgraphs=True surfaces the INNER stage nodes (planner/implementation/…), not
    # just the top-level supernode; each item is (namespace_tuple, {node: update}).
    for item in app.stream(seed, config, stream_mode="updates", subgraphs=True):
        ns, chunk = item if isinstance(item, tuple) else ((), item)
        if not isinstance(chunk, dict):
            continue
        scope = ":".join(str(n).split(":")[0] for n in ns) if ns else ""
        for node in chunk:
            label = f"{scope} ▸ {node}" if scope else str(node)
            print(f"  [{time.monotonic() - t0:6.1f}s] · {label}", file=sys.stderr, flush=True)
    return cast("FactoryState", app.get_state(config).values)


def _unpack_stream_item(item: Any) -> tuple[tuple[Any, ...], str, Any]:
    """Normalise a ``.stream`` item to ``(namespace, mode, payload)`` across the
    shapes langgraph yields: with ``subgraphs=True`` and a LIST of stream modes it
    is ``(namespace, mode, payload)``; with one mode ``(namespace, payload)``;
    without subgraphs ``(mode, payload)`` or bare ``payload``. Defaults the mode to
    ``"updates"`` so an unexpected shape still flows down the completion path."""
    if isinstance(item, tuple):
        if len(item) == 3:
            return item[0], str(item[1]), item[2]
        if len(item) == 2:
            a, b = item
            if isinstance(a, tuple):  # (namespace, payload) — single mode
                return a, "updates", b
            return (), str(a), b  # (mode, payload) — no subgraphs
    return (), "updates", item


def _stream_segment(
    app: Any,
    x: Any,
    config: RunnableConfig,
    progress_cb: Callable[..., None],
) -> None:
    """Drive the graph with ``stream``, forwarding each node's START and FINISH to
    ``progress_cb(node, scope, phase=..., result=...)`` for LIVE telemetry (so the
    floor seats an agent the moment its node begins and clears it, with a summary,
    when the node completes).

    The ``tasks`` stream mode emits a task event on start (no ``result``) and on
    finish (carries ``result``). ``subgraphs=True`` surfaces the inner stage nodes;
    the namespace's leading scope names the stage/room."""
    for item in app.stream(x, config, stream_mode="tasks", subgraphs=True):
        ns, _mode, payload = _unpack_stream_item(item)
        scope = ":".join(str(n).split(":")[0] for n in ns) if ns else ""
        if isinstance(payload, dict) and payload.get("name"):
            node = str(payload["name"])
            finished = ("result" in payload) or ("error" in payload)
            phase = "finish" if finished else "start"
            result = payload.get("result") if finished else None
            # telemetry is advisory — never let a UI callback break the build
            with contextlib.suppress(Exception):
                progress_cb(node, scope, phase=phase, result=result)


def _invoke(
    app: Any,
    seed: FactoryState | None,
    config: RunnableConfig,
    *,
    progress_cb: Callable[..., None] | None = None,
) -> FactoryState:
    """Drive the compiled graph to completion; stream per-node telemetry when a
    ``progress_cb`` is given (or ``FOUNDRY_PROGRESS`` is set)."""
    if progress_cb is not None:
        _stream_segment(app, seed, config, progress_cb)
        return cast("FactoryState", app.get_state(config).values)
    if _progress_enabled():
        return _invoke_streaming(app, seed, config)
    return cast("FactoryState", app.invoke(seed, config))


def _has_pending_run(app: Any, config: RunnableConfig) -> bool:
    """True when this thread has a checkpoint with nodes still to run."""
    try:
        return bool(app.get_state(config).next)
    except Exception:  # no checkpointer/thread: a fresh run
        return False


def _prior_values(app: Any, config: RunnableConfig) -> dict[str, Any]:
    """This thread's checkpointed channel values ({} for a fresh thread)."""
    try:
        return dict(app.get_state(config).values or {})
    except Exception:  # no checkpointer/thread: a fresh run
        return {}


def _resume_seed(app: Any, config: RunnableConfig, seed: FactoryState) -> FactoryState:
    """A rerun of a HALTED thread with the same request resumes it at the boundary
    that halted (the supervisor's triage honours ``RESUME_PHASE``) instead of
    planning from scratch. A thread with nodes pending is continued with ``None``
    by :func:`_invoke_with_autoresume`; a new thread, request or spec plans fresh."""
    if _has_pending_run(app, config):
        return seed
    prior = _prior_values(app, config)
    if (
        prior.get("phase") == "halted"
        and prior.get("request") == seed.get("request")
        and prior.get("spec") == seed.get("spec")
    ):
        return {"request": seed["request"], "phase": RESUME_PHASE}
    return seed


class RunCrashed(RuntimeError):
    """A ValueError from inside the graph: a bug, not a bad argument."""


def _invoke_with_autoresume(
    app: Any,
    seed: FactoryState,
    config: RunnableConfig,
    *,
    progress_cb: Callable[..., None] | None,
    resumable: bool,
    cancel: threading.Event | None = None,
    meter: TokenMeter | None = None,
) -> FactoryState:
    """Wrap :func:`_invoke` so a TRANSIENT provider failure does
    not kill a DURABLE run. A spent Max usage/session window (or a provider blip)
    surfaces as ProxyUnavailable; the durable checkpoint is intact, so back off
    and RE-INVOKE — langgraph continues the graph from its last superstep.

    This mirrors the Workshop server's in-process auto-resume
    (``webapp/server.py``) so an unattended ``foundry run --state-dir …``
    survives a window reset instead of dying and needing a manual
    restart. Bounded with a growing backoff (5,10,20,40,60,60 min). A NON-durable
    run has no checkpoint to resume from, so the budget is 0 and behaviour is
    byte-identical to a bare invoke. Tunable via ``FOUNDRY_CLI_AUTORESUME``
    (default 6; 0 disables). A back-off that would end past ``meter``'s time cap
    stops the run now instead of sleeping into it."""
    import time

    budget = 0
    if resumable:
        try:
            budget = max(0, int(os.environ.get("FOUNDRY_CLI_AUTORESUME") or 6))
        except (TypeError, ValueError):
            budget = 6
    used = 0
    while True:
        try:
            # LangGraph starts a NEW run whenever it gets input, so resuming an
            # interrupted thread (a retry here, or a rerun after a crash or token-cap
            # stop) must pass None; otherwise it re-plans and builds a second plan.
            inp = None if _has_pending_run(app, config) else seed
            return _invoke(app, inp, config, progress_cb=progress_cb)
        except Exception as exc:  # re-raised unless transient+budget
            if used >= budget or not is_transient_failure(exc):
                raise
            wait = min(3600, 300 * (2**used))  # 5,10,20,40,60,60 min
            cap = meter.time_cap_s if meter is not None else None
            if meter is not None and cap is not None and meter.elapsed_s + wait >= cap:
                raise TimeCapExceeded(
                    f"time cap reached: a {wait // 60} min back-off after "
                    f"{type(exc).__name__} would end past the {cap / 60:.1f} minute cap"
                ) from exc
            used += 1
            resume_at = time.strftime("%H:%M", time.localtime(time.time() + wait))
            note = (
                f"[autoresume] transient provider failure ({type(exc).__name__}) "
                f"— waiting {wait // 60} min (resuming ~{resume_at}) "
                f"[{used}/{budget}]. The durable checkpoint is intact; nothing is "
                f"lost."
            )
            print(note, file=sys.stderr, flush=True)
            progress.log(note, stderr=False)
            if cancel is None:
                time.sleep(wait)
            elif cancel.wait(wait):  # Stop / Pause during the back-off
                raise BuildStopped("stopped by the operator") from exc
