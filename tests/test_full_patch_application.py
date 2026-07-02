"""Phase 2 (ROADMAP): full patch application at the integration boundary.

The Phase-1 applier accepted pure file ADDITIONS only. Integration now applies
each FeatureDiff's full op set in Seq order — whole-file writes, deletes (from a
real deletion section or a snapshot's ``deleted`` list), and context-verified
modification hunks — while staying fail-closed on renames, blind patches, and
context mismatches (no fuzzing: the gate fix loop is the recovery path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from foundry.graph.gate_wiring import (
    FeatureDiffBodyError,
    _apply_hunks,
    _diff_ops,
    _parse_patch_diff,
    _write_sources,
)
from foundry.schemas.base import Artifact
from foundry.schemas.implementation import FeatureDiff
from foundry.state.seq import Seq


def a_seq(local: int = 0) -> Seq:
    return Seq("run", 1, 0, "junior_engineer", local)


def feature_diff(uri: str, local: int = 0) -> FeatureDiff:
    return FeatureDiff(
        diff_uri=uri, task_id=f"t-{local}", branch=f"task/t-{local}",
        base_tree_sha="base", seq=a_seq(local),
    )


def diff_of(uri: str, body: str) -> tuple[FeatureDiff, Any]:
    return feature_diff(uri), (lambda u: {uri: body}[u])


MOD_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 111..222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " import os\n"
    "-VALUE = 1\n"
    "+VALUE = 2\n"
    " print(VALUE)\n"
)

DEL_DIFF = "--- a/dead.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-DEAD = 1\n"

RENAME_DIFF = (
    "diff --git a/old.py b/new.py\n"
    "rename from old.py\n"
    "rename to new.py\n"
)


# ---- parser ------------------------------------------------------------------


def test_modification_section_parses_to_patch_ops() -> None:
    ops = _parse_patch_diff("cas://x", MOD_DIFF)
    assert not ops.writes and not ops.deletes
    (hunks,) = [ops.patches["app.py"]]
    assert hunks[0].old_start == 1
    assert [t for t, _ in hunks[0].lines] == [" ", "-", "+", " "]


def test_deletion_section_parses_to_delete_op() -> None:
    ops = _parse_patch_diff("cas://x", DEL_DIFF)
    assert ops.deletes == ("dead.py",)


def test_addition_section_parses_to_write_op() -> None:
    ops = _parse_patch_diff("cas://x", "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+N = 1\n")
    assert ops.writes == {"new.py": "N = 1\n"}


def test_rename_fails_closed() -> None:
    with pytest.raises(FeatureDiffBodyError, match="rename"):
        _parse_patch_diff("cas://x", RENAME_DIFF)


def test_implicit_rename_via_mismatched_paths_fails_closed() -> None:
    body = "--- a/old.py\n+++ b/new.py\n@@ -1,1 +1,1 @@\n-A\n+B\n"
    with pytest.raises(FeatureDiffBodyError, match="rename"):
        _parse_patch_diff("cas://x", body)


# ---- hunk application ----------------------------------------------------------


def test_apply_hunks_exact() -> None:
    original = "import os\nVALUE = 1\nprint(VALUE)\n"
    ops = _parse_patch_diff("cas://x", MOD_DIFF)
    out = _apply_hunks(original, ops.patches["app.py"], diff_uri="cas://x", rel="app.py")
    assert out == "import os\nVALUE = 2\nprint(VALUE)\n"


def test_apply_hunks_context_mismatch_fails_closed() -> None:
    ops = _parse_patch_diff("cas://x", MOD_DIFF)
    with pytest.raises(FeatureDiffBodyError, match="context mismatch"):
        _apply_hunks(
            "import sys\nVALUE = 7\n", ops.patches["app.py"], diff_uri="cas://x", rel="app.py"
        )


def test_apply_hunks_no_newline_marker() -> None:
    body = "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-old\n+new\n\\ No newline at end of file\n"
    ops = _parse_patch_diff("cas://x", body)
    out = _apply_hunks("old\n", ops.patches["f.txt"], diff_uri="cas://x", rel="f.txt")
    assert out == "new"  # trailing newline stripped per the marker


# ---- _diff_ops dispatch ----------------------------------------------------------


def test_snapshot_with_deleted_list() -> None:
    diff, blob = diff_of(
        "cas://s", '{"kind": "full-file-snapshot", "files": {"a.py": "A\\n"}, "deleted": ["b.py"]}'
    )
    ops = _diff_ops(diff, blob)
    assert ops.writes == {"a.py": "A\n"} and ops.deletes == ("b.py",)


def test_additions_only_body_still_uses_legacy_parser() -> None:
    diff, blob = diff_of("cas://a", "--- /dev/null\n+++ b/x.py\n@@\n+X = 1\n")
    assert _diff_ops(diff, blob).writes == {"x.py": "X = 1\n"}


def test_mixed_real_diff_dispatches_to_full_parser() -> None:
    diff, blob = diff_of("cas://m", MOD_DIFF + DEL_DIFF)
    ops = _diff_ops(diff, blob)
    assert "app.py" in ops.patches and ops.deletes == ("dead.py",)


# ---- end-to-end materialization ---------------------------------------------------


def write_sources(tmp_path: Path, bodies: dict[str, str]) -> Path:
    """Materialize N diffs (Seq order = insertion order) into a workdir."""
    uris = list(bodies)
    artifacts = [
        Artifact(kind="feature_diff", uri=f"cas://feature_diff/{i}", produced_by="junior_engineer",
                 seq=a_seq(i))
        for i, _ in enumerate(uris)
    ]
    diffs = {a.uri: feature_diff(uris[i], i) for i, a in enumerate(artifacts)}
    dest = tmp_path / "workdir"
    dest.mkdir()
    _write_sources(
        dest,
        {"artifacts": artifacts},
        lambda h: diffs[h.uri],
        lambda u: bodies[u],
    )
    return dest


def test_second_diff_edits_a_file_the_first_created(tmp_path: Path) -> None:
    # THE Phase-2 exit criterion: task 2 modifies a file task 1 added, and the
    # merged tree reflects the patch.
    dest = write_sources(
        tmp_path,
        {
            "cas://one": "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,3 @@\n"
                         "+import os\n+VALUE = 1\n+print(VALUE)\n",
            "cas://two": MOD_DIFF,
        },
    )
    assert (dest / "app.py").read_text() == "import os\nVALUE = 2\nprint(VALUE)\n"


def test_snapshot_delete_removes_prior_write(tmp_path: Path) -> None:
    dest = write_sources(
        tmp_path,
        {
            "cas://one": '{"kind": "full-file-snapshot", "files": {"tmp.py": "T\\n"}}',
            "cas://two": '{"kind": "full-file-snapshot", "files": {}, "deleted": ["tmp.py"]}',
        },
    )
    assert not (dest / "tmp.py").exists()


def test_delete_of_absent_path_is_tolerated(tmp_path: Path) -> None:
    # Overlay semantic: deleting a base-tree file that never entered the overlay
    # is "contributes nothing", not an error.
    dest = write_sources(
        tmp_path,
        {"cas://one": '{"kind": "full-file-snapshot", "files": {"a.py": "A\\n"}, '
                      '"deleted": ["never-existed.py"]}'},
    )
    assert (dest / "a.py").exists()


def test_patch_of_missing_target_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FeatureDiffBodyError, match="not in the integrated tree"):
        write_sources(tmp_path, {"cas://one": MOD_DIFF})


def test_delete_path_is_containment_checked(tmp_path: Path) -> None:
    from foundry.graph.gate_wiring import PathContainmentError

    body = '{"kind": "full-file-snapshot", "files": {}, "deleted": ["../escape.py"]}'
    with pytest.raises(PathContainmentError):
        write_sources(tmp_path, {"cas://one": body})
