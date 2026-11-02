"""CLI smoke test — the one-line request runs the SPEC core path end to end.

(SPEC.md "SMOKE-TEST CORE PATH"; T-M1-15)

Drives :func:`foundry.cli.run` with NO live API key (MockTransport) and NO
``claude -p`` binary (StubAdapter), asserting the SPEC core path completes:

* a content-addressed reviewed FeatureDiff artifact (the PR subject);
* EXACTLY ONE emitted ``open_pr`` ExternalAction (idempotency key + payload uri);
* attested Spec / Build / Test PASS.

Also asserts ``import foundry`` and ``import foundry.cli`` are clean, and that a
failing diff body halts the run with NO PR (the PASS guard, governance §1/§5).
"""

from __future__ import annotations

import importlib

from foundry.cli import RunResult, run
from foundry.graph.supervisor import MODE_ASSISTANT
from foundry.schemas.gates import GateStatus

# A compiling module whose test COLLECTION fails (imports a missing module):
# Build PASS, Test ERROR -> bounded fix, never advances -> no PR.
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


def test_imports_are_clean() -> None:
    assert importlib.import_module("foundry") is not None
    assert importlib.import_module("foundry.cli") is not None


def test_run_completes_spec_core_path_and_opens_one_pr() -> None:
    result = run("build a tiny URL-shortener service", thread_id="cli-smoke-pass")

    assert isinstance(result, RunResult)
    assert result.mode == MODE_ASSISTANT
    # Attested Spec / Build / Test PASS.
    assert result.gate_statuses.get("spec") is GateStatus.PASS
    assert result.gate_statuses.get("build") is GateStatus.PASS
    assert result.gate_statuses.get("test") is GateStatus.PASS
    assert result.passed is True
    assert result.phase == "done"

    # The PR opens against the content-addressed reviewed FeatureDiff handle.
    assert result.artifact_uri is not None
    assert result.artifact_uri.startswith("cas://feature_diff/")

    # EXACTLY ONE emitted open_pr action — its key + payload uri are reported.
    assert result.open_pr_idempotency_key is not None
    assert result.open_pr_idempotency_key.startswith("open_pr:")
    assert result.open_pr_payload_uri == result.artifact_uri

    # The human-readable summary mentions the PR action.
    summary = result.summary()
    assert "open_pr" in summary
    assert result.artifact_uri in summary

    # No output_dir => no source tree is written.
    assert result.output_root is None


def test_idempotency_key_is_deterministic_across_runs() -> None:
    """Two runs with the SAME thread_id derive the SAME content-addressed key
    (L7 — process-stable, never builtin hash())."""
    a = run("build a tiny URL-shortener service", thread_id="cli-smoke-stable")
    b = run("build a tiny URL-shortener service", thread_id="cli-smoke-stable")
    assert a.open_pr_idempotency_key == b.open_pr_idempotency_key
    assert a.artifact_uri == b.artifact_uri


def test_halted_run_emits_no_pr() -> None:
    """A Test ERROR routes to bounded fix, never reaches an attested PASS; the
    emit guard opens NO PR (governance §1/§5)."""
    result = run(
        "build a broken service",
        thread_id="cli-smoke-halt",
        diff_body=_ERROR_DIFF,
        max_fix_attempts=2,
    )
    assert result.gate_statuses.get("test") is GateStatus.ERROR
    assert result.passed is False
    assert result.open_pr_idempotency_key is None
    assert result.artifact_uri is None
