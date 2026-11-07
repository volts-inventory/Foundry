"""Fixing until the time cap: the build-side fix loops run past max_fix_attempts
and stop on a dead-end rule, on too little time for another pass, on a wave's share
of the time, or (without a time cap) on the window ceiling."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END

from foundry import cli
from foundry.durability.artifact_store import ArtifactStore
from foundry.graph.gate_wiring import (
    BUILD_BOUNDARY,
    BUILD_TEST_SMOKE_BOUNDARY,
    FIX_STOP_TRIAGE,
    NO_FIX_PROGRESS_TRIAGE,
    FixPolicy,
    fix_stop,
)
from foundry.modes import preset_levels
from foundry.proxy.token_meter import TokenMeter
from foundry.schemas.base import BudgetLedger
from foundry.schemas.gates import FixPass, GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.seq import Seq
from tests.test_fix_loop import _OK, _RED, _diff_art, _gate
from tests.test_ticket_scheduler import _Recorder, _runtime, _ticket, _wave_run

_UNTIL_CAP = FixPolicy(until_cap=True)
_MODELS = {"T1": "m1", "T2": "m2", "T3": "m3"}


@dataclass
class _Meter:
    elapsed_s: float = 0.0
    time_cap_s: float | None = 3600.0

    @property
    def remaining_s(self) -> float | None:
        return None if self.time_cap_s is None else max(0.0, self.time_cap_s - self.elapsed_s)


# ---- the default and its overrides --------------------------------------------


def test_fixing_until_the_cap_is_the_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_FIX_UNTIL_CAP", raising=False)
    local = preset_levels("local", cloud_backend="mock", cloud_models=_MODELS)
    hybrid = preset_levels("hybrid", cloud_backend="mock", cloud_models=_MODELS)

    def until(mode: str | None, levels: Any = None, table: Any = None,
              explicit: bool | None = None) -> bool:
        return cli.fix_until_cap_for(explicit, model_mode=mode, levels=levels,
                                     level_table=table)

    assert until("local", table=local)
    assert not until("hybrid", table=hybrid) and not until("cloud") and not until(None)
    # Custom levels: on iff every level runs locally.
    assert until(None, levels={"x": 1}, table=local)
    assert not until(None, levels={"x": 1}, table=hybrid)
    monkeypatch.setenv("FOUNDRY_FIX_UNTIL_CAP", "0")
    assert not until("local", table=local)
    assert until("local", table=local, explicit=True)  # the flag beats the env
    monkeypatch.setenv("FOUNDRY_FIX_UNTIL_CAP", "1")
    assert until("cloud") and not until("cloud", explicit=False)
    monkeypatch.setenv("FOUNDRY_FIX_UNTIL_CAP", "yes")
    with pytest.raises(ValueError, match="FOUNDRY_FIX_UNTIL_CAP"):
        until("cloud")


@pytest.mark.parametrize(("flag", "want"), [("--fix-until-cap", True),
                                            ("--no-fix-until-cap", False), (None, None)])
def test_the_flag_reaches_the_run(monkeypatch: pytest.MonkeyPatch, flag: str | None,
                                  want: bool | None) -> None:
    seen: dict[str, Any] = {}

    def fake_run(request: str, **kw: Any) -> Any:
        seen.update(kw)
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main([*([flag] if flag else []), "build a todo app"]) == 2
    assert seen["fix_until_cap"] is want


# ---- the dead-end rules -------------------------------------------------------


def _pass(n: int, tree: str, outcome: Any, *, touched: tuple[str, ...] = ("app.py",),
          failing: tuple[str, ...] = ("test:t::a",), restart: bool = False,
          elapsed: float | None = None, clock: float | None = None) -> FixPass:
    score = None if outcome == "error" else (-1, 1, len(failing))
    return FixPass(window="w", boundary=BUILD_BOUNDARY, pass_no=n, tree=tree,
                   fix_task=None if outcome == "initial" else f"fix-{n}", score=score,
                   failing=failing, outcome=outcome,
                   touched=() if outcome == "initial" else touched, restart=restart,
                   elapsed_s=elapsed, clock_s=clock, seq=Seq("run", n + 1, 0, "fix_pass", 0))


def _rule(passes: list[FixPass], policy: FixPolicy = _UNTIL_CAP, **kw: Any) -> str | None:
    stop = fix_stop(passes, policy, **kw)
    return stop[0] if stop is not None else None


def test_each_dead_end_rule_stops_the_loop_and_names_itself() -> None:
    first = _pass(0, "t0", "initial", restart=True)
    assert _rule([first, _pass(1, "t1", "lateral", touched=()),
                  _pass(2, "t2", "lateral", touched=())]) == "no-edit"
    assert _rule([first, _pass(1, "t1", "lateral"), _pass(2, "t0", "lateral"),
                  _pass(3, "t1", "lateral")]) == "revisit"
    # The same failure from the same base tree, three times (each pass reverted).
    same = [first]
    for n in range(1, 4):
        same += [_pass(2 * n - 1, f"w{n}", "worse", failing=("test:t::b",)),
                 _pass(2 * n, "t0", "restored")]
    assert _rule(same[:-1]) == "same-signature"
    flat = [first, _pass(1, "t1", "improved")]
    flat += [_pass(n, f"t{n}", "lateral") for n in range(2, 8)]
    assert _rule(flat[:-1]) is None
    assert _rule(flat) == "plateau"
    assert _rule([first, *(_pass(n, f"e{n}", "error") for n in range(1, 4))]) == "errors"
    # Counted from the latest restart (a resume): earlier no-edit passes are spent.
    resumed = [first, _pass(1, "t1", "lateral", touched=()),
               _pass(2, "t2", "lateral", touched=()), _pass(3, "t2", "no-change",
                                                            restart=True)]
    assert _rule(resumed) is None


def test_without_a_time_cap_a_window_stops_at_its_ceiling() -> None:
    policy = FixPolicy(until_cap=True, plateau=99, window_ceiling=5)
    passes = [_pass(0, "t0", "initial", restart=True),
              *(_pass(n, f"t{n}", "lateral") for n in range(1, 5))]
    assert _rule(passes, policy) is None
    assert _rule([*passes, _pass(5, "t5", "lateral")], policy) == "window-ceiling"
    timed = FixPolicy(until_cap=True, plateau=99, window_ceiling=5,
                      meter=lambda: _Meter(elapsed_s=0.0))
    assert _rule([*passes, _pass(5, "t5", "lateral")], timed) is None


def test_no_pass_starts_without_time_for_one() -> None:
    passes = [_pass(0, "t0", "initial", restart=True, clock=0.0),
              _pass(1, "t1", "improved", elapsed=100.0), _pass(2, "t2", "improved",
                                                                elapsed=300.0)]
    meter = _Meter(elapsed_s=3600.0 - 260.0)  # 260 s left; a pass takes ~1.3 x 200
    assert _rule(passes, FixPolicy(until_cap=True, meter=lambda: meter)) is None
    meter.elapsed_s += 5.0
    stop = fix_stop(passes, FixPolicy(until_cap=True, meter=lambda: meter))
    assert stop is not None and stop[0] == "time" and "min left of the time cap" in stop[1]
    # No pass timed yet: assume the default pass.
    fresh = [_pass(0, "t0", "initial", restart=True, clock=0.0)]
    assert _rule(fresh, FixPolicy(until_cap=True, default_pass_s=600.0,
                                  meter=lambda: _Meter(elapsed_s=3000.0))) == "time"


def test_a_wave_loop_stops_at_its_share_of_the_time_left() -> None:
    # The window started with the whole hour left and two waves still to build:
    # a quarter of it (two waves + the final gate + slack) is this wave's.
    passes = [_pass(0, "t0", "initial", restart=True, clock=0.0),
              _pass(1, "t1", "improved", elapsed=60.0)]
    policy = FixPolicy(until_cap=True, meter=lambda: meter)
    meter = _Meter(elapsed_s=800.0)
    assert _rule(passes, policy, unbuilt_waves=2) is None
    meter.elapsed_s = 850.0  # 50 s of the 900 s share left, under 1.3 x 60
    assert _rule(passes, policy, unbuilt_waves=2) == "wave-share"
    assert _rule(passes, policy) is None  # the final gate has the whole cap
    assert _rule(passes, FixPolicy(until_cap=True, wave_share=False, meter=lambda: meter),
                 unbuilt_waves=2) is None


# ---- the wave loop ------------------------------------------------------------

_FIVE = {f"m{i}.py": f"def m{i}(:\n" for i in range(5)}
_FIVE_TICKETS = [_ticket("T-001", 0, files=tuple(_FIVE)),
                 _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]


def _fixes_one_at_a_time() -> dict[str, dict[str, str]]:
    return {"T-001": dict(_FIVE), **{f"fix-{i + 1}": {f"m{i}.py": f"def m{i}():\n    pass\n"}
                                    for i in range(5)}}


def test_fixing_until_the_cap_goes_past_max_fix_attempts(tmp_path: Path) -> None:
    rec = _Recorder(writes=_fixes_one_at_a_time())
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=2, tickets=_FIVE_TICKETS,
                            fix_policy=_UNTIL_CAP)
    assert ran.count("fixer") == 5 and ran[-1] == "engineering"  # the wave built
    assert "fix-5" in rec.calls and "T-002" in rec.calls
    # The counters still count every pass.
    assert out["budget"].fix_attempts_total == 5
    (tmp_path / "legacy").mkdir()
    _, ran, _ = _wave_run(tmp_path / "legacy", _Recorder(writes=_fixes_one_at_a_time()),
                               max_fix_attempts=2, tickets=_FIVE_TICKETS)
    assert ran.count("fixer") == 1 and "engineering" not in ran[1:]


def test_a_dead_end_halts_the_wave_with_its_rule_named(tmp_path: Path) -> None:
    # The fixer writes only what the tree already holds: the same tree, every pass.
    broken = {"app.py": "def broken(:\n"}
    rec = _Recorder(writes={"T-001": broken, "fix-1": broken, "fix-2": broken})
    tickets = [_ticket("T-001", 0, files=("app.py", "rogue.txt")),
               _ticket("T-002", 1, files=("app.py",), deps=("T-001",))]
    out, ran, _ = _wave_run(tmp_path, rec, max_fix_attempts=9, tickets=tickets,
                            fix_policy=_UNTIL_CAP)
    assert ran == ["engineering", "wave_gate", "fixer", "wave_gate", "fixer", "wave_gate"]
    newest = max(out["fix_passes"], key=lambda p: p.seq)
    assert newest.stop == "revisit"
    [note] = [t for t in out["gate_triage"] if t.name == FIX_STOP_TRIAGE]
    assert note.explanation.startswith("the fix loop stops (revisit): ")
    # The dead-end rules decide, not the legacy no-progress halt.
    assert not any(t.name == NO_FIX_PROGRESS_TRIAGE for t in out["gate_triage"])


def test_a_wave_with_no_time_left_for_a_pass_halts_before_it(tmp_path: Path) -> None:
    meter = _Meter(elapsed_s=3540.0)  # one minute left
    policy = FixPolicy(until_cap=True, wave_share=False, meter=lambda: meter)
    rec = _Recorder(writes={"T-001": {"app.py": "def broken(:\n"}})
    out, ran, _ = _wave_run(tmp_path, rec, fix_policy=policy)
    assert ran == ["engineering", "wave_gate"]
    assert rec.calls == ["T-001"]
    [entry] = out["fix_passes"]
    assert entry.stop == "time" and entry.clock_s == 3540.0


def test_a_fix_pass_is_timed_on_the_run_meter(tmp_path: Path) -> None:
    meter = TokenMeter(None, time_cap_s=36_000)
    rec = _Recorder(writes=_fixes_one_at_a_time())
    rt_meter = FixPolicy(until_cap=True, meter=lambda: meter)
    out, _, _ = _wave_run(tmp_path, rec, tickets=_FIVE_TICKETS, fix_policy=rt_meter)
    passes = sorted(out["fix_passes"], key=lambda p: p.seq)
    assert passes[0].elapsed_s is None
    assert all(p.elapsed_s is not None and p.elapsed_s >= 0 for p in passes[1:])


# ---- the final gate's review --------------------------------------------------


def _routes(until_cap: bool) -> dict[str, Any]:
    from foundry.graph.supervisor import build_supervisor_graph
    from foundry.state.global_records import GlobalRecords
    from tests.test_supervisor import _runner

    store = ArtifactStore()
    graph = build_supervisor_graph(
        _runtime(_Recorder(), store), runner=_runner(GlobalRecords()), resolve=store.resolve,
        resolve_blob=store.resolve_blob, source_dir=tempfile.mkdtemp(),
        fix_policy=FixPolicy(until_cap=until_cap))
    return {node: next(iter(b.values())).path.func for node, b in graph.branches.items()}


def _final_state(*trees: tuple[str, GateStatus]) -> FactoryState:
    store = ArtifactStore()
    gates = []
    for i, (tree, status) in enumerate(trees):
        gates += [_gate(n, tree, 10 * i + k, status if n == "test" else _OK)
                  for k, n in enumerate(("build", "test", "smoke"))]
    return {"artifacts": [_diff_art(store, "T-001", 1, {"app.py": "x\n"})], "gates": gates,
            "budget": BudgetLedger(fix_attempts_since_all_green=len(trees) - 1)}


def test_fixing_until_the_cap_reviews_once_the_gates_pass() -> None:
    """Each final-gate fix re-entry ran the LLM review before the gates, and a loop
    fixing until the cap takes many passes: the review is skipped on re-entries and
    runs once, on the tree that passes."""
    cfg = {"configurable": {"thread_id": "run"}}
    until, legacy = _routes(True), _routes(False)
    first: FactoryState = {**_final_state(), "gates": []}
    assert until["implementation"](first) == legacy["implementation"](first) == "quality"
    refix = _final_state(("t0", _RED))
    assert until["implementation"](refix) == "build_test_gate"
    assert legacy["implementation"](refix) == "quality"
    green = _final_state(("t0", _RED), ("t1", _OK))
    assert until["build_test_gate"](green, cfg) == "quality"
    assert legacy["build_test_gate"](green, cfg) == "advance"
    assert until["quality"](green, cfg) == END  # reviewed: the run is done
    assert until["quality"](first, cfg) == legacy["quality"](green, cfg) == "build_test_gate"
    # Green on the first check: the review already ran before it.
    assert until["build_test_gate"](_final_state(("t0", _OK)), cfg) == "advance"


def test_the_final_gate_ignores_the_attempt_bound_until_its_loop_stops() -> None:
    from foundry.graph.gate_wiring import route_after_gates
    from foundry.state.global_records import GlobalRecords

    records = GlobalRecords()
    state = _final_state(("t0", _RED), ("t1", _RED))
    state["budget"] = BudgetLedger(fix_attempts_since_all_green=7, fix_attempts_total=30)
    args: dict[str, Any] = {"boundary": BUILD_TEST_SMOKE_BOUNDARY, "records": records,
                            "max_fix_attempts": 3, "total_cap": 6}
    cfg: Any = {"configurable": {}}
    assert route_after_gates(state, cfg, **args) == "halt"
    assert route_after_gates(state, cfg, **args, fix_policy=_UNTIL_CAP) == "fix"
    stopped = _pass(1, "t1", "lateral").model_copy(
        update={"boundary": BUILD_TEST_SMOKE_BOUNDARY, "stop": "plateau"})
    assert route_after_gates({**state, "fix_passes": [stopped]}, cfg, **args,
                             fix_policy=_UNTIL_CAP) == "halt"
    # A check that recorded no pass still stops past the ceiling without a time cap.
    state["budget"] = BudgetLedger(fix_attempts_since_all_green=21)
    assert route_after_gates(state, cfg, **args, fix_policy=_UNTIL_CAP) == "halt"
