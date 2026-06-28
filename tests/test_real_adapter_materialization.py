"""Coding-adapter → gate materialization contract.

The gate materializer must consume a ``full-file-snapshot`` body (the file set an
adapter authored in a non-git worktree) as well as the synthetic unified-diff
format, and must fail-closed on a malformed body rather than clear the gate on a
partial tree.
"""

from __future__ import annotations

import json

import pytest

from foundry.graph.gate_wiring import FeatureDiffBodyError, _maybe_snapshot_files

# ---- (1) snapshot materialization -------------------------------------------


def test_full_file_snapshot_is_consumed() -> None:
    test_src = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    body = json.dumps(
        {
            "kind": "full-file-snapshot",
            "files": {
                "app.py": "def add(a, b):\n    return a + b\n",
                "test_app.py": test_src,
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
