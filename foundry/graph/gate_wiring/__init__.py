r"""Graph-side gate wiring — run the deterministic gates at phase boundaries.

(governance §1c, architecture.md §3/§4/§7, implementation-plan §3.1)

This is the TRUST SEAM on the graph side: it materializes the integrated tree
from the content-addressed artifacts on ``state``, runs the Spec / Build / Test
gates through the :class:`~foundry.gates.runner.TwoZoneGateRunner` at a phase
boundary, and folds the runner-ATTESTED verdicts onto the ``gates`` channel.

What this module is careful about (the invariants the next waves reuse):

* **Verdicts come ONLY from the runner (LEARNINGS L5).** Nothing here ever
  constructs a :class:`GateResult` — the runner executes zone A, decides +
  signs in zone B, and returns an attested record. ``merge_gates`` admits a
  deterministic-gate write iff it carries a valid zone-B attestation, so an
  unsigned ``GateResult`` is dropped fail-closed. We only ever forward what the
  runner handed back.

* **Fail-closed materialization.** ``materialize_workdir`` writes the
  integrated tree the gates run against. The Spec gate's ``criteria.json`` is
  written from the AcceptanceCriterion artifacts; the code + test sources come
  from the FeatureDiff artifacts via the injected ``resolve`` (the SAME
  ``ArtifactResolver`` seam senior_engineer / qa use, never a parallel one). A
  handle present on state but unresolvable is an ERROR we RAISE (fail-closed) —
  the boundary must never silently run the gates against a partial tree and
  mint a vacuous PASS.

* **Integrated digest is the ``run_against``.** The materialized tree's
  ``tree_sha`` is the integrated digest the verdict binds + (on FAIL) latches
  against — never a per-branch ``task/<id>`` ref. (architecture.md §4)

* **Routing is fail-closed + convergence-bounded.** ``route_after_gates`` reads
  the boundary's relevant verdicts off ``state["gates"]`` and returns one of
  ``advance`` / ``fix`` / ``halt``. A stable-FAIL latch (committed cross-fork)
  is terminal → ``halt``; an exhausted ``fix_attempts`` bound → ``halt``; an
  ERROR (infra, not a verdict — DS7) routes to ``fix`` (bounded retry, never a
  latch); a flake-quarantine ERROR (N=2 split) does NOT fail the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foundry.graph.gate_wiring.best import keep_best, materialize_best
from foundry.graph.gate_wiring.diffs import (
    FeatureDiffBodyError,
    MaterializationError,
    PathContainmentError,
    _apply_hunks,
    _contained_target,
    _diff_ops,
    _DiffOps,
    _maybe_snapshot_files,
    _parse_addition_diff,
    _parse_patch_diff,
)
from foundry.graph.gate_wiring.fix_policy import FIX_STOP_TRIAGE, FixPolicy, fix_stop
from foundry.graph.gate_wiring.ledger import (
    RESTARTED_TRIAGE,
    REVERT_SKIPPED_TRIAGE,
    _outcome,
    _snapshot_blob,
    best_entry,
    draft_best,
    fix_score,
    fix_window,
    regressions,
    restore_diff,
    snapshot_diff,
    typecheck_only,
    window_passes,
)
from foundry.graph.gate_wiring.materialize import (
    CRITERIA_CRITIQUE_KIND,
    CRITERION_KIND,
    FEATURE_DIFF_KIND,
    GATE_INPUT_FILES,
    RESET_PREFIX,
    RESTART_PREFIX,
    TICKET_KIND,
    _criteria_list,
    _criteria_payload,
    _of_kind,
    _resolve_typed,
    _scaffold_manifest,
    _with_base_features,
    _write_sources,
    contract_paths,
    integrated_files,
    lift_epoch,
    materialize_workdir,
    planned_files,
    render_feature,
)
from foundry.graph.gate_wiring.restarts import (
    FailureCluster,
    failure_clusters,
    narrow_reset,
    pending_restarts,
    plan_next_pass,
    restart_cluster,
    restart_set,
    ticket_reset,
)
from foundry.graph.gate_wiring.routing import (
    _BOUNDARY_GATES,
    _PY_FRAME,
    _SOURCE_LINE,
    _VOLATILE,
    ADVANCE,
    BUILD_BOUNDARY,
    BUILD_TEST_BOUNDARY,
    BUILD_TEST_SMOKE_BOUNDARY,
    ESCALATE,
    FIX,
    HALT,
    PIP_MANIFESTS,
    RESTART,
    SPEC_BOUNDARY,
    _fix_attempts,
    _unchanged_tree_recheck,
    bump_fix_attempts,
    failing_ids,
    failing_streak,
    failure_files,
    fix_cap,
    flake_quarantined,
    no_fix_progress,
    pip_manifest_failure,
    route_after_gates,
    settle_fix_attempts,
)
from foundry.graph.gate_wiring.runs import (
    UnpinnedSmokeGate,
    _build_env,
    _build_gate,
    _wave_test_gate,
    gate_profile,
    planned_paths,
    run_build_gate,
    run_build_test_gates,
    run_spec_gate,
    unbuilt_manifests,
    wave_scenarios,
    wave_stack,
    wave_typecheck,
)
from foundry.graph.gate_wiring.wave_check import NO_FIX_PROGRESS_TRIAGE, WaveBuildCheck

if TYPE_CHECKING:
    from foundry.graph.gate_wiring.diffs import BlobResolver
    from foundry.graph.gate_wiring.materialize import ArtifactResolver

__all__ = [
    "ADVANCE",
    "BUILD_BOUNDARY",
    "BUILD_TEST_BOUNDARY",
    "BUILD_TEST_SMOKE_BOUNDARY",
    "CRITERIA_CRITIQUE_KIND",
    "CRITERION_KIND",
    "ESCALATE",
    "FEATURE_DIFF_KIND",
    "FIX",
    "FIX_STOP_TRIAGE",
    "GATE_INPUT_FILES",
    "HALT",
    "NO_FIX_PROGRESS_TRIAGE",
    "PIP_MANIFESTS",
    "RESET_PREFIX",
    "RESTART",
    "RESTARTED_TRIAGE",
    "RESTART_PREFIX",
    "REVERT_SKIPPED_TRIAGE",
    "SPEC_BOUNDARY",
    "TICKET_KIND",
    "_BOUNDARY_GATES",
    "_PY_FRAME",
    "_SOURCE_LINE",
    "_VOLATILE",
    "ArtifactResolver",
    "BlobResolver",
    "FailureCluster",
    "FeatureDiffBodyError",
    "FixPolicy",
    "MaterializationError",
    "PathContainmentError",
    "UnpinnedSmokeGate",
    "WaveBuildCheck",
    "_DiffOps",
    "_apply_hunks",
    "_build_env",
    "_build_gate",
    "_contained_target",
    "_criteria_list",
    "_criteria_payload",
    "_diff_ops",
    "_fix_attempts",
    "_maybe_snapshot_files",
    "_of_kind",
    "_outcome",
    "_parse_addition_diff",
    "_parse_patch_diff",
    "_resolve_typed",
    "_scaffold_manifest",
    "_snapshot_blob",
    "_unchanged_tree_recheck",
    "_wave_test_gate",
    "_with_base_features",
    "_write_sources",
    "best_entry",
    "bump_fix_attempts",
    "contract_paths",
    "draft_best",
    "failing_ids",
    "failing_streak",
    "failure_clusters",
    "failure_files",
    "fix_cap",
    "fix_score",
    "fix_stop",
    "fix_window",
    "flake_quarantined",
    "gate_profile",
    "integrated_files",
    "keep_best",
    "lift_epoch",
    "materialize_best",
    "materialize_workdir",
    "narrow_reset",
    "no_fix_progress",
    "pending_restarts",
    "pip_manifest_failure",
    "plan_next_pass",
    "planned_files",
    "planned_paths",
    "regressions",
    "render_feature",
    "restart_cluster",
    "restart_set",
    "restore_diff",
    "route_after_gates",
    "run_build_gate",
    "run_build_test_gates",
    "run_spec_gate",
    "settle_fix_attempts",
    "snapshot_diff",
    "ticket_reset",
    "typecheck_only",
    "unbuilt_manifests",
    "wave_scenarios",
    "wave_stack",
    "wave_typecheck",
    "window_passes",
]
