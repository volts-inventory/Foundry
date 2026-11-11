"""Checked-plan reuse (foundry.agents.planner.reuse): a fresh run of an identical
request replays the plan of an earlier run that passed; any differing input, a
run that did not pass, the opt-out or a bench round plans afresh."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import progress, telemetry
from foundry.agents.planner import NODE_ID, make_planner_node, reuse
from foundry.agents.planner.reuse import PLANS_DIR, PlanReuse, plan_key
from foundry.cli import result as cli_result
from foundry.cli import run
from foundry.cli.runner import _plan_reuse
from foundry.modes import Level
from foundry.schemas.gates import GateStatus
from foundry.state.seq import Seq
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
    "local_thinking": None,
    "base_urls": {"T1": "http://localhost:11434/v1"},
}


def _plan_once(root: Path, run_key: str, payload: dict[str, Any] = PLAN) -> tuple[Any, Any]:
    rt, tx = runtime_for(payload)
    rt.plan_reuse = PlanReuse(root, "k", run_key)
    writes = make_planner_node(rt)({"request": "build a todo app"}, config_for(run_key))
    return writes, tx


def _prd(epoch: int) -> Any:
    return SimpleNamespace(kind="prd", seq=Seq("r", epoch, epoch, NODE_ID, 0))


def _stored(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / "k.json").read_text())
    return data


def test_a_checked_plan_is_replayed_without_a_model_call(tmp_path: Path) -> None:
    first, tx = _plan_once(tmp_path, "run-a")
    assert len(tx.requests) == 1
    assert not (tmp_path / "k.json").exists()  # staged only, until the run passes
    reuse.promote_if_passed(PlanReuse(tmp_path, "k", "run-a"), first, passed=True)
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


def test_a_spec_pass_alone_does_not_store_the_plan(tmp_path: Path) -> None:
    first, _ = _plan_once(tmp_path, "run-a")
    # The Spec gate accepted it, but the run's Build/Test did not pass.
    values = {**first, "gates": [_spec_verdict(GateStatus.PASS, (), 1)]}
    reuse.promote_if_passed(PlanReuse(tmp_path, "k", "run-a"), values, passed=False)
    assert not (tmp_path / "k.json").exists()
    _, tx = _plan_once(tmp_path, "run-b")
    assert len(tx.requests) == 1


def test_a_run_that_does_not_pass_stores_no_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli_result._result

    def failed(*args: Any, **kwargs: Any) -> Any:
        from dataclasses import replace

        return replace(real(*args, **kwargs), passed=False)

    monkeypatch.setattr(cli_result, "_result", failed)
    run("build a todo cli", thread_id="t-a", state_dir=str(tmp_path))
    assert not list((tmp_path / PLANS_DIR).glob("*.json"))


def test_promotion_takes_the_passes_up_to_the_final_plan(tmp_path: Path) -> None:
    store = PlanReuse(tmp_path, "k", "run-a")
    for epoch, ck in ((1, "first"), (2, "replan"), (3, "after")):
        store.begin_pass()
        store.record(ck, {"pass": epoch})
        store.end_pass(tuple(_prd(epoch).seq))
    store.begin_pass()
    store.record("unfinished", {"pass": 4})
    # A resumed process sees the same staged passes.
    resumed = PlanReuse(tmp_path, "k", "run-a")
    # The final plan is pass 2's: pass 3 never reached the run's state.
    assert resumed.promote({"artifacts": [_prd(1), _prd(2)]})
    assert set(_stored(tmp_path)) == {"first", "replan"}
    assert not (tmp_path / "pending" / "run-a" / "k.json").exists()


def test_a_run_whose_final_plan_was_never_staged_promotes_nothing(tmp_path: Path) -> None:
    store = PlanReuse(tmp_path, "k", "run-a")
    store.begin_pass()
    store.record("a", {})
    store.end_pass(tuple(_prd(1).seq))
    assert not store.promote({"artifacts": [_prd(1), _prd(2)]})
    assert not (tmp_path / "k.json").exists()


def test_concurrent_promotions_keep_every_run(tmp_path: Path) -> None:
    stores = []
    for i in range(8):
        s = PlanReuse(tmp_path, "k", f"run-{i}")
        s.begin_pass()
        s.record(f"call-{i}", {"i": i})
        s.end_pass(tuple(_prd(1).seq))
        stores.append(s)
    barrier = threading.Barrier(len(stores))

    def promote(s: PlanReuse) -> None:
        barrier.wait()
        s.promote({"artifacts": [_prd(1)]})

    threads = [threading.Thread(target=promote, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(_stored(tmp_path)) == {f"call-{i}" for i in range(8)}
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".k.json.*"))


def test_a_corrupt_store_is_kept_aside_and_logged(tmp_path: Path) -> None:
    (tmp_path / "k.json").write_text("{not json")
    store = PlanReuse(tmp_path, "k", "run-a")
    store.begin_pass()
    store.record("a", {"x": 1})
    store.end_pass(tuple(_prd(1).seq))
    lines: list[str] = []
    with progress.sink(lines.append):
        assert store.replay("a") is None
        store.promote({"artifacts": [_prd(1)]})
    assert (tmp_path / "k.json.corrupt").read_text() == "{not json"
    assert any("is corrupt" in line for line in lines)
    assert _stored(tmp_path) == {"a": {"x": 1}}


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
        ("local_thinking", "off"),
        ("base_urls", {"T1": "http://other:11434/v1"}),
    ],
)
def test_each_planner_input_changes_the_key(field: str, value: Any) -> None:
    assert plan_key(**_INPUTS) != plan_key(**{**_INPUTS, field: value})


def test_the_run_keys_thinking_servers_and_host_languages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import foundry.toolchains

    proxy = SimpleNamespace(local_tiers=frozenset({"T1"}), tier_models={})

    def key(url: str = "http://a/v1", thinking: str | None = None) -> str:
        levels = {"T1": Level("local", "qwen", url), "T3": Level("local", "qwen", url)}
        return _plan_reuse(
            tmp_path,
            "run",
            request="r",
            spec="",
            scope="auto",
            profile="auto",
            level_table=levels,
            backend="mock",
            proxy=proxy,
            ticket_tiers=None,
            local_thinking=thinking,
        ).key

    monkeypatch.delenv("FOUNDRY_LOCAL_THINKING", raising=False)
    base = key()
    assert key(url="http://b/v1") != base
    assert key(thinking="off") != base
    monkeypatch.setenv("FOUNDRY_LOCAL_THINKING", "off")
    assert key() == key(thinking="off")  # unset reads the env, as the proxy does
    monkeypatch.delenv("FOUNDRY_LOCAL_THINKING")
    monkeypatch.setattr(foundry.toolchains, "available_languages", lambda: frozenset({"cobol"}))
    assert key() != base


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
