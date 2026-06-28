"""CLI ``--out`` / ``run(output_dir=)`` — write the accepted source tree to disk.

(EXECUTION-PLAN.md §4 [1]; SPEC.md "SMOKE-TEST CORE PATH")

A PASSING build materializes the FINAL accepted integrated tree (the SAME tree
the terminal gates ran against, via ``materialize_workdir`` over the reviewed
FeatureDiff handle + the run's resolve/resolve_blob seams) and writes the real
source files to ``output_dir``. A HALTED run writes NOTHING — there is no
accepted tree to emit. The Spec gate's ``criteria.json`` input is excluded; the
written path is surfaced on ``RunResult.output_root`` and in ``summary()``.
"""

from __future__ import annotations

from pathlib import Path

from foundry.cli import main, run
from foundry.schemas.gates import GateStatus

# Mirrors tests/test_cli_smoke.py: a compiling module whose test COLLECTION fails
# (Build PASS, Test ERROR) -> bounded fix, never advances -> halted, no PR.
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


def test_passing_run_writes_source_files(tmp_path: Path) -> None:
    out = tmp_path / "generated"
    result = run(
        "build a tiny URL-shortener service",
        thread_id="cli-out-pass",
        output_dir=str(out),
    )

    assert result.passed is True
    # The written path is surfaced on the result (like doc_root).
    assert result.output_root is not None
    assert Path(result.output_root) == out.resolve()

    # The real source files the diff added are on disk.
    app = out / "app.py"
    test_app = out / "test_app.py"
    assert app.is_file()
    assert test_app.is_file()
    assert "VALUE = 42" in app.read_text(encoding="utf-8")

    # The Spec gate's criteria.json INPUT is not a source file — it is excluded.
    assert not (out / "criteria.json").exists()

    # The written path is reported in the human-readable summary.
    assert str(out.resolve()) in result.summary()


def test_halted_run_writes_a_usable_draft(tmp_path: Path) -> None:
    out = tmp_path / "generated"
    result = run(
        "build a broken service",
        thread_id="cli-out-halt",
        diff_body=_ERROR_DIFF,
        max_fix_attempts=2,
        output_dir=str(out),
    )

    assert result.gate_statuses.get("test") is GateStatus.ERROR
    assert result.passed is False
    # NOT shipped: there is no accepted tree.
    assert result.output_root is None
    assert "source tree written to" not in result.summary()
    # But never empty-handed: a usable DRAFT (best attempt + a what's-failing note).
    assert result.draft_root == str(out.resolve())
    assert (out / "DRAFT-STATUS.md").is_file()
    assert "held at the gate" in (out / "DRAFT-STATUS.md").read_text(encoding="utf-8").lower()
    assert "DRAFT written to" in result.summary()


def test_no_output_dir_writes_nothing(tmp_path: Path) -> None:
    """Default (output_dir unset) leaves output_root None and writes no tree."""
    result = run("build a tiny URL-shortener service", thread_id="cli-out-default")
    assert result.passed is True
    assert result.output_root is None
    assert not any(tmp_path.iterdir())


def test_main_out_flag_writes_tree(tmp_path: Path) -> None:
    out = tmp_path / "main-out"
    code = main(["--out", str(out), "build a tiny URL-shortener service"])
    assert code == 0
    assert (out / "app.py").is_file()
    assert not (out / "criteria.json").exists()


def test_main_out_flag_requires_argument() -> None:
    assert main(["--out"]) == 2
