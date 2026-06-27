"""Real coding-adapter → gate materialization contract.

Two gaps the first real `claude -p` build surfaced:

1. The adapter emits a ``full-file-snapshot`` body for a non-git worktree, but
   the gate materializer only understood the synthetic unified-diff format —
   so a real build crashed at the Build/Test gate.
2. `claude -p` runs headless with NO interactive approval, so file writes are
   DENIED by default and the agent authored nothing — the adapter must pass a
   permission mode so it can write into its throwaway sandbox worktree.
"""

from __future__ import annotations

import json

import pytest

from foundry.execution.adapters.claude_code import ClaudeCodeAdapter
from foundry.graph.gate_wiring import FeatureDiffBodyError, _maybe_snapshot_files


# ---- (1) snapshot materialization -------------------------------------------


def test_full_file_snapshot_is_consumed() -> None:
    body = json.dumps(
        {
            "kind": "full-file-snapshot",
            "files": {
                "app.py": "def add(a, b):\n    return a + b\n",
                "test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            },
        }
    )
    files = _maybe_snapshot_files("cas://x", body)
    assert files is not None
    assert set(files) == {"app.py", "test_app.py"}
    assert files["app.py"].startswith("def add")


def test_unified_diff_falls_through_to_parser() -> None:
    # A non-snapshot body returns None so _diff_files uses the addition parser.
    assert _maybe_snapshot_files("cas://x", "--- /dev/null\n+++ b/a.py\n@@\n+x=1\n") is None
    assert _maybe_snapshot_files("cas://x", "") is None


def test_empty_snapshot_is_empty_not_crash() -> None:
    body = json.dumps({"kind": "full-file-snapshot", "files": {}})
    assert _maybe_snapshot_files("cas://x", body) == {}


def test_malformed_snapshot_fails_closed() -> None:
    body = json.dumps({"kind": "full-file-snapshot", "files": "oops"})
    with pytest.raises(FeatureDiffBodyError):
        _maybe_snapshot_files("cas://x", body)


# ---- (2) adapter passes a permission mode -----------------------------------


class _RecordingSandbox:
    """Captures the argv the adapter would run; no real subprocess."""

    def __init__(self) -> None:
        self.argv: list[str] = []

    def run(self, cmd, *, timeout_s=None, **_kw):  # type: ignore[no-untyped-def]
        self.argv = list(cmd)
        raise RuntimeError("stop after capture")


def test_adapter_passes_accept_edits_by_default() -> None:
    a = ClaudeCodeAdapter(run_id="t")
    assert a._permission_mode == "acceptEdits"


def test_adapter_permission_mode_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_CLAUDE_PERMISSION_MODE", "bypassPermissions")
    a = ClaudeCodeAdapter(run_id="t")
    assert a._permission_mode == "bypassPermissions"


def test_adapter_argv_includes_permission_flag() -> None:
    # The run() argv must carry --permission-mode so headless claude can WRITE.
    a = ClaudeCodeAdapter(run_id="t")
    a._bin = "claude"
    sb = _RecordingSandbox()

    class _Pkt:
        task = {"id": "T1"}
        pinned_constraints = ()
        retrieved_spans = ()

    with pytest.raises(RuntimeError, match="stop after capture"):
        a.run(_Pkt(), sb)  # type: ignore[arg-type]
    assert "--permission-mode" in sb.argv
    assert sb.argv[sb.argv.index("--permission-mode") + 1] == "acceptEdits"
