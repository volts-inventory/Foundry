from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "bench", Path(__file__).resolve().parent.parent / "scripts" / "bench.py"
)
assert _spec and _spec.loader
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

LOG = """\
  [   0.0s] · factory ▸ triage
[21:21:13] start model call: planner (T1, ideation)
[21:33:20] done  model call: planner (T1, ideation) in 12.1m, 14k tokens (run 0.00M)
[21:33:20] start model call: junior_engineer (T3, ideation)
[21:33:35] done  model call: junior_engineer (T3, ideation) in 15s, 1k tokens (run 0.00M)
[21:33:36] reuse model call: junior_engineer (T3, ideation) (finished before a restart)
  [ 398.3s] · factory:implementation ▸ wave_gate
  [ 400.0s] · factory:implementation ▸ fixer
  [ 500.0s] · factory:implementation ▸ fixer
stopped: time cap reached: 45.3 of 45.0 minutes used.
"""


def _case(tmp_path: Path, exit_code: int, files: bool) -> Path:
    root = tmp_path / "local" / "dice"
    (root / "state" / "calls" / "abc").mkdir(parents=True)
    (root / "run.log").write_text(LOG)
    (root / "run.json").write_text(json.dumps({"wall_s": 90.0, "exit_code": exit_code}))
    calls = root / "state" / "calls" / "abc"
    u = {"served_model": "m", "text": "x", "usage": {"input_tokens": 100, "output_tokens": 7}}
    (calls / "a.json").write_text(json.dumps(u))
    (calls / "b.json").write_text(json.dumps(u))
    (calls / "c.json").write_text(json.dumps({"used": 1, "elapsed_s": 2}))
    (root / "out" / ".git").mkdir(parents=True)
    (root / "out" / ".git" / "HEAD").write_text("x")
    if files:
        (root / "out" / "main.py").write_text("print(1)")
    return root


def test_summarize_case(tmp_path: Path) -> None:
    s = bench.summarize_case(_case(tmp_path, 3, True))
    assert s["model_calls"] == 2
    assert s["calls_by_node"] == {"planner": 1, "junior_engineer": 1}
    assert s["calls_by_tier"] == {"T1": 1, "T3": 1}
    assert s["reused_calls"] == 1
    assert s["call_seconds_by_node"] == {"planner": 726.0, "junior_engineer": 15.0}
    assert s["tokens"] == {"input": 200, "output": 14}
    assert (s["fix_passes"], s["wave_gates"]) == (2, 1)
    assert s["stopped_by_time_cap"] is True
    assert s["out_files"] == 1
    assert s["verdict"] == "stopped"
    assert s["released"] is False


def test_released_requires_files_and_exit_zero(tmp_path: Path) -> None:
    assert bench.summarize_case(_case(tmp_path, 0, True))["released"] is True


def test_no_files_not_released(tmp_path: Path) -> None:
    s = bench.summarize_case(_case(tmp_path, 0, False))
    assert s["out_files"] == 0 and s["released"] is False


def test_summarize_only_writes_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _case(tmp_path, 0, True)
    rc = bench.main(
        ["--mode", "local", "--out-dir", str(tmp_path), "--cases", "dice,todo", "--summarize-only"]
    )
    assert rc == 0
    data = json.loads((tmp_path / "local" / "summary.json").read_text())
    assert list(data) == ["dice"]
    assert "dice" in capsys.readouterr().out


def test_a_rerun_resumes_with_the_local_cap_and_keeps_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        kw["stdout"].write(f"attempt {len(calls)}\n")
        return SimpleNamespace(returncode=3 if len(calls) == 1 else 0)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench.main(["--mode", "local", "--cases", "dice", "--out-dir", str(tmp_path)])
    bench.main(["--mode", "local", "--cases", "dice", "--out-dir", str(tmp_path)])
    assert all(c[c.index("--time-cap") + 1] == "600" for c in calls)
    root = tmp_path / "local" / "dice"
    assert (root / "run.log").read_text() == "attempt 1\nattempt 2\n"
    assert json.loads((root / "run.json").read_text())["exit_code"] == 0
    assert bench.summarize_case(root)["stopped_by_time_cap"] is False  # resumed to a pass
