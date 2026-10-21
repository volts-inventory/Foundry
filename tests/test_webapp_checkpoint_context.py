"""The evidence the Workshop hands a human at a checkpoint pause.

An approval dialog that shows only a title and one sentence asks for a decision
without the facts behind it. These cover the context the server assembles for that
dialog: the advisory cost/elapsed meter for the work the human is being asked to
judge.

All of it is display-only — nothing here is read back into a build, and a broken
meter degrades to zeroes rather than blocking a pause.
"""

from __future__ import annotations

import time

import pytest

from webapp import server
from webapp.server import Project, _checkpoint_spend


@pytest.fixture(autouse=True)
def clean_meter(monkeypatch):
    """A zeroed global meter + no other running builds, so deltas are exact."""
    monkeypatch.setattr(server, "_USAGE", {"calls": 0, "input": 0, "output": 0,
                                           "cache_read": 0})
    monkeypatch.setattr(server, "_PROJECTS", {})
    monkeypatch.setattr(server, "_USAGE_HOOKED", [False])


def _project(**kw):
    return Project(id="p1", request="r", mode="assistant", supervised=True, floor=1, **kw)


def _meter(calls=0, tokens=0):
    server._USAGE.update({"calls": calls, "input": tokens, "output": 0, "cache_read": 0})


# --- the advisory cost / elapsed meter ---------------------------------------- #

def test_spend_reports_stage_and_build_separately() -> None:
    # `stage` is the work since the LAST pause (what this decision is about);
    # `build` is the whole run. A first pause has no prior mark, so they agree.
    p = _project()
    p.start_mono = time.monotonic() - 300
    p.usage_mark = {"calls": 0, "input": 0, "output": 0, "cache_read": 0}
    _meter(calls=10, tokens=5000)
    first = _checkpoint_spend(p)
    assert first["stage_calls"] == 10 and first["build_calls"] == 10
    assert first["build_s"] >= 300 and first["stage_s"] >= 300

    # After that pause closes, the next stage counts only ITS own work.
    p.cp_mono = time.monotonic()
    p.usage_cp = {"calls": 10, "input": 5000, "output": 0, "cache_read": 0}
    _meter(calls=14, tokens=9000)
    second = _checkpoint_spend(p)
    assert second["stage_calls"] == 4 and second["build_calls"] == 14
    assert second["stage_tokens"] == 4000 and second["build_tokens"] == 9000
    assert second["stage_s"] < second["build_s"]


def test_a_reset_meter_reads_as_zero_never_negative() -> None:
    # The meter is in-memory: a restart between snapshots zeroes it. A delta must
    # clamp rather than show a human a negative token count.
    p = _project()
    p.usage_mark = {"calls": 99, "input": 99_000, "output": 0, "cache_read": 0}
    _meter(calls=2, tokens=100)
    spend = _checkpoint_spend(p)
    assert spend["build_calls"] == 0 and spend["build_tokens"] == 0


def test_concurrent_builds_mark_the_figure_as_shared() -> None:
    # One global egress serves every floor, so with several builds running the
    # per-build token figure is an upper bound. Say so instead of overstating.
    p = _project()
    p.usage_mark = {"calls": 0, "input": 0, "output": 0, "cache_read": 0}
    _meter(calls=5, tokens=1000)
    assert _checkpoint_spend(p)["shared"] is False

    other = _project()
    other.id = "p2"
    server._PROJECTS.update({"p1": p, "p2": other})
    p.status = other.status = "running"
    spend = _checkpoint_spend(p)
    assert spend["shared"] is True and spend["builds"] == 2


def test_cost_cap_is_surfaced_in_dollars_only_when_set() -> None:
    p = _project()
    assert "cap_usd" not in _checkpoint_spend(p)
    capped = _project(cost_cap_microusd=2_500_000)
    assert _checkpoint_spend(capped)["cap_usd"] == 2.5


def test_meter_hook_accumulates_usage_per_call(monkeypatch) -> None:
    # The proxy fires its advisory observer once per successful call; the server
    # folds those into the global meter the checkpoint dialog reads.
    from foundry.proxy import model_provider_proxy as mpp

    prev = mpp.on_call_usage
    try:
        server._hook_usage_meter()
        assert mpp.on_call_usage is not None
        mpp.on_call_usage("pm", "T3", {"input_tokens": 100, "output_tokens": 20,
                                       "cache_read_input_tokens": 80})
        mpp.on_call_usage("arch", "T2", {"input_tokens": 50, "output_tokens": 5})
        snap = server._usage_snapshot()
    finally:
        mpp.on_call_usage = prev
    assert snap == {"calls": 2, "input": 150, "output": 25, "cache_read": 80}
    # Snapshots are copies — a later call can't mutate one already handed out.
    mpp_snapshot = server._usage_snapshot()
    mpp_snapshot["calls"] = 999
    assert server._usage_snapshot()["calls"] == 2
