"""Checked-plan reuse (foundry.agents.planner.reuse): a fresh run of an identical
request replays a plan that passed the Spec gate; any differing input, an
unchecked plan, the opt-out or a bench round plans afresh."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import progress, telemetry
from foundry.agents.planner import make_planner_node, reuse
from foundry.agents.planner.reuse import PLANS_DIR, PlanReuse, plan_key
from foundry.cli import run
from foundry.schemas.gates import GateStatus
from tests.test_bench import bench
from tests.test_planner import EXPECTED_KINDS, PLAN, _spec_verdict, config_for, runtime_for

_INPUTS: dict[str, Any] = {
    "request": "build a todo app",
    "spec": "",
    "scope": "auto",
    "stack": {"profile": "auto"},
    "languages": frozenset({"python"}),
    "local_tiers": frozenset({"T1", "T2", "T3"}),
    "planner_level": {"backend": "local", "model": "qwen"},
    "ticket_tiers": None,
}


def _plan_once(root: Path, run_key: str, payload: dict[str, Any] = PLAN) -> tuple[Any, Any]:
    rt, tx = runtime_for(payload)
    rt.plan_reuse = PlanReuse(root, "k", run_key)
    writes = make_planner_node(rt)({"request": "build a todo app"}, config_for(run_key))
    return writes, tx


def test_a_checked_plan_is_replayed_without_a_model_call(tmp_path: Path) -> None:
    first, tx = _plan_once(tmp_path, "run-a")
    assert len(tx.requests) == 1
    assert not (tmp_path / "k.json").exists()  # staged only, until the Spec gate passes
    reuse.promote_if_checked(
        PlanReuse(tmp_path, "k", "run-a"), {"gates": [_spec_verdict(GateStatus.PASS, (), 1)]}
    )
    assert (tmp_path / "k.json").exists()

    lines: list[str] = []
    with telemetry.counting() as counts, progress.sink(lines.append):
        second, tx2 = _plan_once(tmp_path, "run-b", {})
    assert tx2.requests == []
    assert counts.snapshot().get("planner.reused") == 1
    assert any("planner: reused checked plan k" in line for line in lines)
    assert {a.kind for a in second["artifacts"]} == EXPECTED_KINDS
    # The same plan, stamped as this run's records.
    assert {a.seq.run_id for a in second["artifacts"]} == {"run-b"}
    assert sorted(a.kind for a in second["artifacts"]) == sorted(a.kind for a in first["artifacts"])


def test_a_plan_the_spec_gate_failed_is_never_stored(tmp_path: Path) -> None:
    _plan_once(tmp_path, "run-a")
    reuse.promote_if_checked(
        PlanReuse(tmp_path, "k", "run-a"),
        {"gates": [_spec_verdict(GateStatus.FAIL, ("bad",), 1)]},
    )
    assert not (tmp_path / "k.json").exists()
    _, tx = _plan_once(tmp_path, "run-b")
    assert len(tx.requests) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request", "build a notes app"),
        ("spec", "a design spec"),
        ("scope", "demo"),
        ("stack", {"profile": "node"}),
        ("languages", frozenset({"python", "go"})),
        ("local_tiers", frozenset({"T2", "T3"})),
        ("planner_level", {"backend": "claude-cli", "model": "opus"}),
        ("ticket_tiers", {"standard": "T1"}),
    ],
)
def test_each_planner_input_changes_the_key(field: str, value: Any) -> None:
    assert plan_key(**_INPUTS) != plan_key(**{**_INPUTS, field: value})


def test_planner_code_changes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = plan_key(**_INPUTS)
    monkeypatch.setattr(reuse, "code_digest", lambda: "other")
    assert plan_key(**_INPUTS) != before


def test_the_code_digest_covers_the_planner_and_its_schemas() -> None:
    reuse.code_digest.cache_clear()
    assert len(reuse.code_digest()) == 64


def test_a_fresh_run_of_the_same_request_reuses_its_checked_plan(tmp_path: Path) -> None:
    first = run("build a todo cli", thread_id="t-a", state_dir=str(tmp_path))
    assert first.passed
    assert list((tmp_path / PLANS_DIR).glob("*.json"))
    lines: list[str] = []
    second = run("build a todo cli", thread_id="t-b", state_dir=str(tmp_path), log_cb=lines.append)
    assert second.passed
    assert second.hint_counts.get("planner.reused") == 1
    assert any("planner: reused checked plan" in line for line in lines)
    assert any("model call:" in line for line in lines)  # the rest of the run still calls
    assert not any("model call: planner" in line for line in lines)
    other = run("build a notes cli", thread_id="t-c", state_dir=str(tmp_path))
    assert "planner.reused" not in other.hint_counts
    pinned = run("build a todo cli", thread_id="t-d", state_dir=str(tmp_path), scope="demo")
    assert "planner.reused" not in pinned.hint_counts


def test_the_opt_out_neither_stores_nor_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run("build a todo cli", thread_id="t-a", state_dir=str(tmp_path))
    monkeypatch.setenv("FOUNDRY_PLAN_REUSE", "0")
    second = run("build a todo cli", thread_id="t-b", state_dir=str(tmp_path))
    assert "planner.reused" not in second.hint_counts
    run("build a notes cli", thread_id="t-c", state_dir=str(tmp_path))
    monkeypatch.delenv("FOUNDRY_PLAN_REUSE")
    notes = run("build a notes cli", thread_id="t-d", state_dir=str(tmp_path))
    assert "planner.reused" not in notes.hint_counts


@pytest.mark.parametrize(("flag", "want"), [([], "0"), (["--plan-reuse"], "1")])
def test_bench_turns_plan_reuse_off_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str], want: str
) -> None:
    envs: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        envs.append(kw["env"]["FOUNDRY_PLAN_REUSE"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("FOUNDRY_PLAN_REUSE", "1")
    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench.main(["--mode", "local", "--cases", "dice", "--out-dir", str(tmp_path), *flag])
    assert envs == [want]
    assert json.loads((tmp_path / "local" / "dice" / "run.json").read_text())["exit_code"] == 0
