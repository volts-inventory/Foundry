"""The fix-until-cap policy: when a fix loop stops (:func:`fix_stop`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from foundry.agents._runtime import AgentRuntime
from foundry.schemas.gates import FixPass

if TYPE_CHECKING:
    from collections.abc import Callable


# ---- fix until the time cap ---------------------------------------------------
#
# Instead of a fixed number of passes, a fix loop runs until a dead-end rule says
# the next pass would only repeat what failed, or until the run's time cap leaves
# too little for another pass. The gate node decides (FixPass.stop) and the routing
# edge reads that decision, so both agree and a resume re-decides on its own check.

FIX_STOP_TRIAGE = "fix-stop"
_PASS_HEADROOM = 1.3


@dataclass(frozen=True)
class FixPolicy:
    """How a fix loop is bounded. ``until_cap=False`` is the legacy bound
    (``max_fix_attempts`` per boundary plus :func:`fix_cap` run-wide). With
    ``until_cap`` those counters are still bumped but never halt; the loop halts on
    the first dead-end rule (counted from the window's latest restart):
    ``no_edit_limit`` passes in a row edited no file, ``revisit_limit`` passes
    landed on a tree the window already checked, one failure signature came back
    ``same_sig_limit`` times from one base tree, ``plateau`` passes went by since
    the best score last improved (a lateral move is not one), ``error_limit``
    passes in a row ERRORed; or when the time cap leaves less than
    ``_PASS_HEADROOM`` x the window's median pass (``default_pass_s`` before any)
    — with ``wave_share``, a wave's loop gets only its share of the time left
    when it started, ``1 / (unbuilt waves + 2)``, and running out of it moves the
    run on to the next wave (:data:`WAVE_ADVANCE_STOPS`) instead of halting.
    Without a time cap a window stops after ``window_ceiling`` passes. ``meter``
    returns the run's token meter.
    ``restarts`` lets a loop rebuild a stuck cluster's tickets from scratch once
    (:func:`plan_next_pass`), in every mode."""

    until_cap: bool = False
    restarts: bool = True
    no_edit_limit: int = 2
    revisit_limit: int = 2
    same_sig_limit: int = 3
    plateau: int = 6
    error_limit: int = 3
    window_ceiling: int = 20
    wave_share: bool = True
    default_pass_s: float = 600.0
    meter: Callable[[], Any] | None = None

    def bound(self, rt: AgentRuntime) -> FixPolicy:
        """This policy reading ``rt``'s token meter, unless it names its own."""
        if self.meter is not None:
            return self
        from dataclasses import replace

        return replace(self, meter=lambda: getattr(rt.proxy, "token_meter", None))

    def _meter(self) -> Any:
        return self.meter() if self.meter is not None else None

    def remaining_s(self) -> float | None:
        meter = self._meter()
        return None if meter is None else meter.remaining_s


def _median(values: list[float]) -> float:
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def fix_stop(
    passes: list[FixPass],
    policy: FixPolicy,
    *,
    unbuilt_waves: int | None = None,
) -> tuple[str, str] | None:
    """``(rule, why)`` when a fix-until-cap loop must not start another pass after
    the newest of ``passes`` (one window, in pass order), else ``None``."""
    start = max((i for i, p in enumerate(passes) if p.restart), default=0)
    run = passes[start:]
    judged = [(start + 1 + i, p) for i, p in enumerate(run[1:]) if p.outcome != "restored"]
    tail = [p for _, p in judged]

    def streak(pred: Callable[[FixPass], bool]) -> int:
        n = 0
        for p in reversed(tail):
            if not pred(p):
                break
            n += 1
        return n

    if streak(lambda p: not p.touched) >= policy.no_edit_limit:
        return ("no-edit", f"the last {policy.no_edit_limit} fix passes edited no file")
    revisits = sum(any(q.tree == p.tree for q in passes[:i]) for i, p in judged)
    if revisits >= policy.revisit_limit:
        return ("revisit", f"{revisits} fix passes landed on a tree this loop had already checked")
    sigs: dict[tuple[str, tuple[str, ...]], int] = {}
    for i, p in judged:
        key = (passes[i - 1].tree, p.failing)
        sigs[key] = sigs.get(key, 0) + 1
        if p.failing and sigs[key] >= policy.same_sig_limit:
            return (
                "same-signature",
                f"{sigs[key]} fix passes from one tree left the same checks failing",
            )
    since = streak(lambda p: p.outcome != "improved")
    if since >= policy.plateau:
        return ("plateau", f"{since} fix passes went by without improving on the best tree")
    if streak(lambda p: p.outcome == "error") >= policy.error_limit:
        return ("errors", f"the last {policy.error_limit} checks ERRORed")
    meter = policy._meter()
    remaining = None if meter is None else meter.remaining_s
    if remaining is None:
        if len(tail) >= policy.window_ceiling:
            return (
                "window-ceiling",
                f"{len(tail)} fix passes without a time cap (the "
                f"ceiling is {policy.window_ceiling})",
            )
        return None
    spent = [p.elapsed_s for p in passes if p.elapsed_s is not None]
    need = _PASS_HEADROOM * (_median(spent) if spent else policy.default_pass_s)
    left, rule = float(remaining), "time"
    first = run[0]
    if (
        policy.wave_share
        and unbuilt_waves is not None
        and first.clock_s is not None
        and meter.time_cap_s is not None
    ):
        share = (meter.time_cap_s - first.clock_s) / (unbuilt_waves + 2)
        wave_left = first.clock_s + share - meter.elapsed_s
        if wave_left < left:
            left, rule = wave_left, "wave-share"
    if left < need:
        what = "this wave's share of the time cap" if rule == "wave-share" else "the time cap"
        return (
            rule,
            f"{max(left, 0) / 60:.1f} min left of {what}, under the "
            f"{need / 60:.1f} min another pass takes",
        )
    return None
