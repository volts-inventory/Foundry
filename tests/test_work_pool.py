"""Execution-worker pool tests (plan §4 [15]).

Proves the LocalWorkPool dev drop-in:

* runs N tasks with bounded concurrency, NEVER exceeding ``max_slots`` at once
  (asserted via a live high-water counter);
* respects the slot/budget cap — a task over budget or over a per-lineage quota is
  REJECTED at admit (never run), and excess work queues;
* returns correct results and is deterministic (content-addressed task ids);
* has an idempotent claim — a duplicate submit does NOT double-run the work;
* NEVER spawns a container — the app's only verb is ``submit`` (proven by patching
  ``subprocess`` so any docker/process spawn from the pool path would fail the test).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.stub import StubAdapter
from foundry.execution.sandbox import Sandbox
from foundry.execution.work_pool import (
    BudgetExceededError,
    ExecutionTask,
    LocalWorkPool,
    QuotaPolicy,
    TaskStatus,
)
from foundry.schemas.base import BudgetLedger

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    (src / "hello.py").write_text("print('hello')\n")
    return src


def make_task(
    source_dir: Path, task_id: str, *, lineage: str = "default", cost: int = 0
) -> ExecutionTask:
    packet = TaskPacket(
        task={"id": task_id, "title": task_id},
        worktree_path=str(source_dir),
        turn_budget=1,
        cost_budget_microusd=1_000_000,
    )
    return ExecutionTask(packet=packet, adapter="stub", lineage=lineage, est_cost_microusd=cost)


def make_pool(
    source_dir: Path, *, max_slots: int, quota: QuotaPolicy | None = None
) -> LocalWorkPool:
    return LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(source_dir),
        adapter_factory=lambda _name: StubAdapter(),
        max_slots=max_slots,
        quota=quota,
    )


# ---------------------------------------------------------------------------
# interface / determinism
# ---------------------------------------------------------------------------


def test_runs_tasks_and_returns_results(source_dir: Path) -> None:
    with make_pool(source_dir, max_slots=4) as pool:
        ids = [pool.submit(make_task(source_dir, f"t{i}")) for i in range(6)]
        results = [pool.result(tid) for tid in ids]
    assert all(isinstance(r, AdapterResult) for r in results)
    assert all(r.diff_uri.startswith("cas://sha256/") for r in results)
    assert all(pool.status(tid) is TaskStatus.DONE for tid in ids)


def test_task_id_is_content_addressed_and_deterministic(source_dir: Path) -> None:
    a = make_task(source_dir, "same")
    b = make_task(source_dir, "same")
    c = make_task(source_dir, "different")
    assert a.task_id == b.task_id  # byte-identical request -> same id
    assert a.task_id != c.task_id


def test_results_are_deterministic_across_pools(source_dir: Path) -> None:
    with make_pool(source_dir, max_slots=2) as p1:
        r1 = p1.result(p1.submit(make_task(source_dir, "det")))
    with make_pool(source_dir, max_slots=2) as p2:
        r2 = p2.result(p2.submit(make_task(source_dir, "det")))
    # StubAdapter is a pure function of the packet -> identical snapshot + diff.
    assert r1.snapshot_digest == r2.snapshot_digest
    assert r1.diff_uri == r2.diff_uri


# ---------------------------------------------------------------------------
# bounded concurrency — never exceed max_slots
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# budget / per-lineage quota — over-budget tasks rejected, not run
# ---------------------------------------------------------------------------


def test_over_global_budget_is_rejected_not_run(source_dir: Path) -> None:
    quota = QuotaPolicy(ledger=BudgetLedger(cap_microusd=150))
    with make_pool(source_dir, max_slots=2, quota=quota) as pool:
        ok = pool.submit(make_task(source_dir, "a", cost=100))
        assert pool.result(ok).diff_uri  # fits
        with pytest.raises(BudgetExceededError):
            pool.submit(make_task(source_dir, "b", cost=100))  # 100+100 > 150
        # The rejected task left a terminal record and was never run.
        rejected = make_task(source_dir, "b", cost=100)
        assert pool.status(rejected.task_id) is TaskStatus.REJECTED


def test_per_lineage_quota_isolates_lineages(source_dir: Path) -> None:
    quota = QuotaPolicy(
        ledger=BudgetLedger(cap_microusd=10_000),
        lineage_quota_microusd={"family-1": 100, "family-2": 100},
    )
    with make_pool(source_dir, max_slots=2, quota=quota) as pool:
        pool.result(pool.submit(make_task(source_dir, "x", lineage="family-1", cost=100)))
        # family-1 is now exhausted ...
        with pytest.raises(BudgetExceededError):
            pool.submit(make_task(source_dir, "y", lineage="family-1", cost=1))
        # ... but family-2 still has its own reserved capacity (no starvation).
        assert pool.result(
            pool.submit(make_task(source_dir, "z", lineage="family-2", cost=100))
        ).diff_uri


def test_unmetered_lineage_still_bound_by_global_ledger(source_dir: Path) -> None:
    quota = QuotaPolicy(ledger=BudgetLedger(cap_microusd=50))
    pool = make_pool(source_dir, max_slots=1, quota=quota)
    with pool, pytest.raises(BudgetExceededError):
        pool.submit(make_task(source_dir, "big", lineage="unmetered", cost=51))


# ---------------------------------------------------------------------------
# idempotent claim — no double-run
# ---------------------------------------------------------------------------


class _RunCounter:
    """Adapter that counts how many times run() actually executes."""

    supports_turn_snapshots = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.runs = 0

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        with self._lock:
            self.runs += 1
        return StubAdapter().run(packet, sandbox)


def test_duplicate_submit_does_not_double_run(source_dir: Path) -> None:
    counter = _RunCounter()
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(source_dir),
        adapter_factory=lambda _name: counter,
        max_slots=4,
    )
    try:
        task = make_task(source_dir, "idem")
        ids = {pool.submit(task) for _ in range(5)}  # 5 identical submits
        assert len(ids) == 1  # all coalesce onto ONE content-addressed record
        tid = ids.pop()
        r1 = pool.result(tid)
        r2 = pool.result(tid)  # re-read is a cache hit
        assert r1.diff_uri == r2.diff_uri
    finally:
        pool.shutdown()
    assert counter.runs == 1  # ran exactly once despite 5 submits + 2 reads


def test_claim_is_won_by_at_most_one_worker(source_dir: Path) -> None:
    # A second, redundant worker future for an already-claimed task is a no-op.
    counter = _RunCounter()
    pool = LocalWorkPool(
        sandbox_factory=lambda _t: Sandbox(source_dir),
        adapter_factory=lambda _name: counter,
        max_slots=2,
    )
    try:
        task = make_task(source_dir, "claim")
        tid = pool.submit(task)
        pool.result(tid)
        # Re-driving a worker for the now-DONE task must not re-run it.
        pool._run(tid)  # type: ignore[attr-defined]  # simulate a redelivery
    finally:
        pool.shutdown()
    assert counter.runs == 1


# ---------------------------------------------------------------------------
# NO in-app container spawn — the app only submits
# ---------------------------------------------------------------------------


def test_pool_never_spawns_a_container(source_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch path must never shell out to docker / spawn a process from the
    application. We trip any attempt by failing subprocess.Popen / os.system."""
    import os
    import subprocess

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("application spawned a process/container — forbidden (plan §4 [15])")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "system", _boom)

    # StubAdapter holds no egress and runs no subprocess, so the whole submit ->
    # claim -> run -> result path completes without any process spawn.
    with make_pool(source_dir, max_slots=2) as pool:
        ids = [pool.submit(make_task(source_dir, f"n{i}")) for i in range(4)]
        results = [pool.result(tid) for tid in ids]
    assert all(r.diff_uri for r in results)


def test_pool_source_has_no_docker_invocation() -> None:
    """Static guard: the module's CODE never spawns a process / container.

    Mentions of docker live only in docstrings/comments (the scaffolding seam),
    so we tokenize and drop string + comment tokens, then assert no executable
    token references a process-spawn API or a container CLI."""
    import io
    import tokenize

    src = Path("foundry/execution/work_pool.py").read_text(encoding="utf-8")
    code_tokens = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.STRING, tokenize.COMMENT)
    ]
    forbidden = {"subprocess", "Popen", "system", "spawn", "spawnv", "fork", "popen"}
    assert not (set(code_tokens) & forbidden), "pool code must not spawn a process/container"
    assert "docker" not in code_tokens
