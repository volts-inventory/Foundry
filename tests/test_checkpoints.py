"""Checkpoint / HITL mode — pause at stage boundaries, resume on a human decision.

(plan [10]; lifecycle.md §2 human steering; governance.md §1/§5/§6)

Drives the checkpoint mode through the SAME dev wiring the CLI smoke path uses
(``foundry.cli.run`` — a real proxy/MockTransport, a real two-zone gate runner
admitted through the REAL ``merge_gates`` path, a durable checkpointer), so the
pause/resume is exercised against the real compiled factory graph end-to-end with
NO interactive stdin (a programmatic responder feeds each decision).

Asserts the [10] acceptance + the HARD TRUST CONSTRAINT:

* a checkpointed run PAUSES at a boundary and surfaces the READ-ONLY inspection
  payload (artifacts + attested gates-so-far + the advisory agent_journal);
* resume-APPROVE drives the run to completion (a PR is emitted);
* a REJECT HALTS the run (no PR), even at the RELEASE boundary where the gates
  have already PASSED;
* CRUCIALLY a checkpoint APPROVE on a build whose Test gate FAILs still does NOT
  release (the gates remain the sole trust authority — an approve can never turn
  a FAIL into a pass);
* the default (no checkpoints) run is unchanged (no pauses, a clean PR).
"""

from __future__ import annotations

from typing import Any

from foundry.cli import run
from foundry.graph.factory_graph import checkpoint_blocks_release
from foundry.schemas.checkpoint import (
    CHECKPOINT_BOUNDARIES,
    CheckpointBoundary,
    CheckpointDecision,
    CheckpointVerdict,
    inspection_payload,
)
from foundry.schemas.gates import GateStatus
from foundry.state.factory_state import FactoryState
from foundry.state.reducers import union_checkpoint_decisions
from foundry.state.seq import Seq

# A compiling module whose test COLLECTION fails (imports a missing module):
# Build PASS, Test ERROR -> routes to bounded fix, never advances to a PASS.
_ERROR_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,3 @@\n"
    "+import this_module_does_not_exist_xyz\n\n\ndef test_x():\n    assert True\n"
)


def _seq(local: int = 0) -> Seq:
    return Seq(run_id="t", emission_epoch=1, superstep_index=1, node_id="ckpt", local=local)


def _decision(boundary: CheckpointBoundary, verdict: CheckpointVerdict) -> CheckpointDecision:
    return CheckpointDecision(
        boundary=boundary, verdict=verdict, state_sha="sha", seq=_seq()
    )


# ---- end-to-end: pause + surface inspection payload -------------------------


def test_checkpointed_run_pauses_and_surfaces_inspection_payload() -> None:
    """With checkpoints on and NO responder, the run PAUSES at the first boundary
    and the inspection payload is surfaced (artifacts + gates + advisory journal)."""
    seen: list[dict[str, Any]] = []

    def capture(payload: dict[str, Any]) -> str:
        seen.append(payload)
        return "reject"  # not reached on the first pause (we stop before resuming)

    # No responder -> the run pauses at the first (product) boundary and returns.
    res = run("build a todo app", checkpoints=["product"], thread_id="ck-pause-1")
    assert res.paused_at == "product"
    assert res.passed is False  # paused -> no PR yet
    assert res.checkpoints == ("product",)

    # WITH a responder we can READ the surfaced payload at the boundary.
    res2 = run(
        "build a todo app",
        checkpoints=["product"],
        checkpoint_responder=capture,
        thread_id="ck-pause-2",
    )
    assert seen, "the responder must be handed the inspection payload at the pause"
    payload = seen[0]
    assert payload["boundary"] == "product"
    assert "artifacts" in payload and "gates" in payload
    assert "agent_journal" in payload  # the advisory [11] journal rides along
    # The product reject halts the run -> no PR.
    assert res2.passed is False
    assert res2.open_pr_idempotency_key is None


# ---- end-to-end: approve drives to completion (PR) --------------------------


def test_resume_approve_drives_to_completion_and_opens_pr() -> None:
    """Approve at every stage boundary -> the run completes with a PR; the run
    pauses once per enabled boundary, in SOP order."""
    order: list[str] = []

    def approve(payload: dict[str, Any]) -> str:
        order.append(str(payload["boundary"]))
        return "approve"

    res = run(
        "build a todo app",
        checkpoints=True,
        checkpoint_responder=approve,
        thread_id="ck-approve-all",
    )
    assert order == ["product", "design", "release"]  # paused at all three, in order
    assert res.paused_at is None  # ran to completion
    assert res.passed is True
    assert res.open_pr_idempotency_key is not None
    assert res.gate_statuses.get("test") is GateStatus.PASS


# ---- end-to-end: reject halts (no PR) ---------------------------------------


def test_reject_at_product_halts_no_pr() -> None:
    """A reject at the product boundary halts the run before any gate -> no PR."""
    res = run(
        "build a todo app",
        checkpoints=["product"],
        checkpoint_responder=lambda _p: "reject",
        thread_id="ck-reject-product",
    )
    assert res.passed is False
    assert res.open_pr_idempotency_key is None
    # The run halted before the spec gate, so no terminal verdicts landed.
    assert res.gate_statuses == {}


def test_reject_at_release_halts_even_though_gates_pass() -> None:
    """The release checkpoint sits AFTER the Build+Test gate. A human reject there
    HALTS the run even though the gates ALL PASS — the checkpoint can SUBTRACT a
    release. No PR is opened."""
    res = run(
        "build a todo app",
        checkpoints=["release"],
        checkpoint_responder=lambda _p: "reject",
        thread_id="ck-reject-release",
    )
    # The gates DID pass (the checkpoint did not — could not — change them).
    assert res.gate_statuses.get("spec") is GateStatus.PASS
    assert res.gate_statuses.get("build") is GateStatus.PASS
    assert res.gate_statuses.get("test") is GateStatus.PASS
    # ...yet the human reject halted the release: NO PR.
    assert res.passed is False
    assert res.open_pr_idempotency_key is None


# ---- HARD TRUST: an approve can NEVER turn a gate FAIL into a release --------


def test_checkpoint_approve_cannot_release_a_failing_gate() -> None:
    """THE trust invariant of [10]: a build whose Test gate FAILs does NOT release
    even when the human APPROVES every checkpoint. The gates remain the sole trust
    authority — an approve only lets the run proceed to the (unchanged) gate
    boundary; it can never make a FAILing gate pass."""
    res = run(
        "build a todo app",
        checkpoints=True,
        checkpoint_responder=lambda _p: "approve",  # approve EVERYTHING
        diff_body=_ERROR_DIFF,  # Test gate ERRORs (a non-PASS verdict)
        max_fix_attempts=1,
        thread_id="ck-fail-approve",
    )
    # The Test gate did not PASS...
    assert res.gate_statuses.get("test") is not GateStatus.PASS
    # ...so despite the human approving every checkpoint, NOTHING released.
    assert res.passed is False
    assert res.open_pr_idempotency_key is None


def test_gated_checkpoint_approve_cannot_release_a_failing_gate() -> None:
    """Same trust invariant in GATED mode: a Test-gate FAIL is not released even
    with approve-all checkpoints (the Release sign-off + the gates still gate)."""
    res = run(
        "build a todo app",
        mode="gated",
        checkpoints=True,
        checkpoint_responder=lambda _p: "approve",
        diff_body=_ERROR_DIFF,
        max_fix_attempts=1,
        thread_id="ck-gated-fail-approve",
    )
    assert res.gate_statuses.get("test") is not GateStatus.PASS
    assert res.passed is False
    assert res.open_pr_idempotency_key is None


# ---- default OFF: a non-checkpoint run is unchanged -------------------------


def test_default_run_has_no_checkpoints_and_opens_pr() -> None:
    """The default (checkpoints OFF) run never pauses and behaves exactly as today
    — a clean PASS opens a PR, with no checkpoint metadata."""
    res = run("build a todo app", thread_id="ck-default")
    assert res.checkpoints == ()
    assert res.paused_at is None
    assert res.passed is True
    assert res.open_pr_idempotency_key is not None


# ---- structural unit tests: the trust posture is structural -----------------


def test_checkpoint_blocks_release_only_subtracts() -> None:
    """``checkpoint_blocks_release`` blocks ONLY on a non-proceeding RELEASE
    decision; it never blocks on approve / other boundaries / no decision. It can
    only SUBTRACT a release, never add one."""
    # No decisions -> does not block (checkpoints off / boundary not reached).
    assert checkpoint_blocks_release({}) is False
    # A RELEASE approve -> does not block (gates still decide independently).
    approve: FactoryState = {
        "checkpoint_decisions": [_decision(CheckpointBoundary.RELEASE, CheckpointVerdict.APPROVE)]
    }
    assert checkpoint_blocks_release(approve) is False
    # A RELEASE reject -> blocks.
    reject: FactoryState = {
        "checkpoint_decisions": [_decision(CheckpointBoundary.RELEASE, CheckpointVerdict.REJECT)]
    }
    assert checkpoint_blocks_release(reject) is True
    # A reject at a NON-release boundary does not block the release guard (that
    # boundary's own router already halted the run earlier).
    other: FactoryState = {
        "checkpoint_decisions": [_decision(CheckpointBoundary.PRODUCT, CheckpointVerdict.REJECT)]
    }
    assert checkpoint_blocks_release(other) is False


def test_checkpoint_decision_is_not_a_gate_channel() -> None:
    """A ``CheckpointDecision`` rides its OWN advisory channel folded by a plain
    union reducer — it carries no gate verdict and is keyed only by content
    identity, so it can never enter the trust-filtered ``gates`` channel."""
    d1 = _decision(CheckpointBoundary.RELEASE, CheckpointVerdict.APPROVE)
    d2 = _decision(CheckpointBoundary.RELEASE, CheckpointVerdict.REJECT)
    # Distinct decisions coexist; a re-fold of the same record coalesces.
    merged = union_checkpoint_decisions([d1], [d2, d1])
    assert len(merged) == 2
    # proceeds() is purely the human verdict; it has no gate/verdict surface.
    assert d1.proceeds() is True
    assert d2.proceeds() is False


def test_inspection_payload_is_read_only_advisory() -> None:
    """The inspection payload is a pure read of state (no bodies, advisory only):
    it surfaces the stage artifacts, gates-so-far, and the [11] journal."""
    state: FactoryState = {"phase": "product", "route": "greenfield"}
    payload = inspection_payload(
        state, boundary=CheckpointBoundary.PRODUCT, state_sha="sha-x"
    )
    assert payload["boundary"] == "product"
    assert payload["state_sha"] == "sha-x"
    assert payload["artifacts"] == [] and payload["gates"] == []
    assert payload["agent_journal"] == []
    assert "gate" in str(payload["note"]).lower()  # documents the trust posture
    assert "org" not in payload  # no resolver / no OwnershipMap -> no org section


def test_inspection_payload_surfaces_org_chart_when_resolvable() -> None:
    """Wave E: with a resolver + an OwnershipMap on state, the payload carries an
    advisory org chart — present areas, their Tech-Lead domains, and each domain's
    units (derived from Wave A's leads_by_area/area_of/units_of)."""
    from foundry.schemas.architecture import OwnershipMap
    from foundry.schemas.base import Artifact

    ownership = OwnershipMap(
        owners={
            "web/Nav.tsx": "ui_team",  # frontend
            "warehouse/etl.py": "data_team",  # data
            "services/a.py": "svc",  # backend
        },
        seq=Seq("run", 1, 1, "principal_architect", 0),
    )
    handle = Artifact(
        kind="ownership_map",
        uri="cas://ownership/x",
        produced_by="principal_architect",
        seq=Seq("run", 1, 1, "principal_architect", 0),
    )
    state: FactoryState = {"phase": "design", "artifacts": [handle]}

    payload = inspection_payload(
        state,
        boundary=CheckpointBoundary.DESIGN,
        state_sha="sha-y",
        resolve=lambda _h: ownership,
    )

    org = payload["org"]
    assert isinstance(org, dict)
    areas = {a["area"]: a["leads"] for a in org["areas"]}
    assert areas == {"frontend": ["ui_team"], "backend": ["svc"], "data": ["data_team"]}
    domains = {d["owner"]: d for d in org["domains"]}
    assert domains["data_team"]["area"] == "data"
    assert domains["svc"]["units"] == ["services/a.py"]


def test_inspection_payload_org_fail_soft_on_resolver_error() -> None:
    """A telemetry read must NEVER break a pause: a resolver that raises (or a
    missing/typed-wrong body) just omits the org section."""
    from foundry.schemas.base import Artifact

    def _boom(_h: Any) -> Any:
        raise RuntimeError("cas down")

    handle = Artifact(
        kind="ownership_map",
        uri="cas://ownership/x",
        produced_by="principal_architect",
        seq=Seq("run", 1, 1, "principal_architect", 0),
    )
    state: FactoryState = {"phase": "design", "artifacts": [handle]}
    payload = inspection_payload(
        state, boundary=CheckpointBoundary.DESIGN, state_sha="z", resolve=_boom
    )
    assert "org" not in payload  # fail-soft, no crash


def test_unknown_checkpoint_boundary_is_rejected() -> None:
    """A typo'd boundary name is fail-closed (ValueError), never a silent no-op."""
    import pytest

    from foundry.graph.supervisor import normalize_checkpoints

    assert normalize_checkpoints(None) == frozenset()
    assert normalize_checkpoints(True) == frozenset(CHECKPOINT_BOUNDARIES)
    with pytest.raises(ValueError, match="unknown checkpoint boundary"):
        normalize_checkpoints(["product", "nope"])
