"""The mock backend's scripted payloads and the in-memory materialization doubles
(SUP7) that drive the keyless smoke path to a deterministic PASS."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from foundry.durability.artifact_store import ArtifactStore
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import FrozenModel
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality

if TYPE_CHECKING:
    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact


# The fixed diff_uri the synthetic FeatureDiff body points at; resolve_blob
# returns the passing unified-diff body keyed on it (SUP7).
_DIFF_BODY_URI = "cas://cli/diff-body"


# A unified-diff ADDITION blob carrying a passing test, so the integrated tree
# the gates run against yields Build PASS (compiles) + Test PASS (pytest green).
_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
)


# ---- scripted per-node payloads (free-text completions, SOP order) ----------
# Substring-keyed on each agent's SOP marker (count-independent, so a fix-loop
# can re-run the implementation+quality phases without exhausting a queue; SUP7).

# The mock backend must script EVERY node that can make a model call: an unscripted
# node gets `default_text=""`, which fails structured-output parsing and dies as a
# ProxyUnavailable after the retries. STANDARD keeps the smoke path's scope
# unchanged; `--scope demo` still exercises the capped path deterministically.
# The planner's ONE call authors the whole product + architecture plan.
_PLAN_PAYLOAD: dict[str, Any] = {
    "scope": {
        "tier": "standard",
        "rationale": "the smoke request is treated as a full-scope build",
        "signals": [],
    },
    "intent": {
        "intent": "Run the one-line request through the factory.",
        "assumptions": ["single user"],
        "unknowns": [],
        "success_criteria": [{"id": "SC-1", "text": "the requested behavior works"}],
        "must_haves": [{"id": "MH-1", "text": "core feature"}],
        "nice_to_haves": [],
        "deliverable": "cli",
    },
    "prd": {
        "title": "Requested service",
        "body": "Deliver the requested behavior.",
        "required_capabilities": [],
    },
    "criteria_review": {"missing": [], "security_abuse_omissions": []},
    "sprint_plan": {
        "milestones": ["M1"],
        "usd_cap_microusd": 30_000_000,
        "risk_register": "none",
        "done_definition": "all criteria green",
    },
    "design": {"title": "Requested service", "body": "api -> store", "components": ["api"]},
    "adrs": [],
    # Python without a bdd command: the gates keep the pinned pytest-bdd harness.
    "stack": {
        "language": "python",
        "framework": "",
        "package_manager": "pip",
        "install": "",
        "build": "",
        "typecheck": "",
        "test": "pytest",
        "bdd": "",
        "run": "python3 -c 'import app'",
        "notes": "",
        "contracts_file": "",
        "harness_files": [],
    },
    "interfaces": [],
    # A small build: one epic, its tickets inline (no per-epic calls).
    "epics": [
        {
            "id": "E1",
            "title": "Requested service",
            "summary": "The whole requested behavior.",
            "specialty_mix": ["backend", "test"],
            "depends_on": [],
            "provides": [],
            "consumes": [],
        },
    ],
    # One feature ticket (also the entrypoint, binding its own scenarios) after a
    # trivial test-fixtures ticket.
    "tickets": [
        {
            "id": "T-001",
            "title": "Serve the requested behavior",
            "description": "The service answers callers and rejects anonymous ones.",
            "specialty": "backend",
            "difficulty": "standard",
            "depends_on": ["T-002"],
            "files": ["app.py", "tests/step_defs/test_t_001.py"],
            "implements": [],
            "consumes": [],
            "entrypoint": True,
            "scenarios": [
                {
                    "id": "AC-001",
                    "given": ["the service is running"],
                    "when": ["it is invoked"],
                    "then": ["it responds"],
                    "criticality": "normal",
                    "security_abuse": False,
                    "covers": ["MH-1", "SC-1"],
                },
                {
                    "id": "AC-002",
                    "given": ["an unauthenticated request"],
                    "when": ["it hits the API"],
                    "then": ["the response is 401"],
                    "criticality": "high-risk",
                    "security_abuse": True,
                    "covers": [],
                },
            ],
        },
        {
            "id": "T-002",
            "title": "Shared test fixtures",
            "specialty": "test",
            "difficulty": "trivial",
            "depends_on": [],
            "files": ["tests/conftest.py"],
            "implements": [],
            "consumes": [],
            "entrypoint": False,
            "scenarios": [],
        },
    ],
    "pinned_constraints": [],
}


_SCRIPTS: dict[str, str] = {
    "You are the Planner": json.dumps(_PLAN_PAYLOAD),
}


# ---- in-memory materialization doubles (SUP7) -------------------------------


def _synthetic_resolve(store: ArtifactStore) -> ArtifactResolver:
    """A KIND-dispatching typed resolver that falls through to the real store.

    The planner mints content-addressed HANDLES mid-run whose
    uri encodes an epoch-dependent ``content_identity()`` (so it can't be
    pre-keyed) and, with the StubAdapter, no faithful passing-test body is ever
    authored. So for the handle kinds the SPEC core path consumes we synthesize a
    faithful body (SUP7); anything else falls through to the REAL store
    (``rt.cas``) so a future real-suite adapter needs no CLI change.
    """

    def _resolve(handle: Artifact) -> FrozenModel:
        if handle.kind in {SystemDesign.__name__, "system_design"}:
            return SystemDesign(
                title="Requested service",
                design_uri="cas://design/body",
                components=("api",),
                interface_ids=(),
                seq=handle.seq,
            )
        if handle.kind == "ownership_map":
            return OwnershipMap(
                owners={"app.py": "owner-a"},
                unit_criteria={"app.py": ("AC-001",)},
                seq=handle.seq,
            )
        if handle.kind == "acceptance_criterion":
            cid = f"AC-{handle.seq.local:03d}-{handle.seq.emission_epoch}"
            return AcceptanceCriterion(
                id=cid,
                text=f"Given/When/Then for {cid}",
                criticality=Criticality.HIGH_RISK,
                oracle_hint=f"oracle for {cid}",
                security_abuse=True,  # guarantees Spec-gate security coverage
                seq=handle.seq,
            )
        if handle.kind == "prd":
            # The criteria above get synthetic ids the PRD never lists: an empty
            # list keeps the gates from filtering them all out as dropped.
            prd = store.resolve(handle)
            return prd.model_copy(update={"criterion_ids": ()})
        if handle.kind == "feature_diff":
            return FeatureDiff(
                diff_uri=_DIFF_BODY_URI,
                task_id=handle.task_id or "T1",
                branch=f"task/{handle.task_id or 'T1'}",
                base_tree_sha="base-sha",
                seq=handle.seq,
            )
        # Fall through to the REAL store for any other body kind.
        return store.resolve(handle)

    return _resolve


def _synthetic_resolve_blob(store: ArtifactStore, diff_body: str) -> BlobResolver:
    """Return the passing unified-diff body for the synthetic FeatureDiff uri.

    Mirrors ``sandbox.get_blob``: keyed on the ``cas://`` uri. The fixed
    synthetic FeatureDiff diff_uri (``_DIFF_BODY_URI``) returns ``diff_body``;
    anything else falls through to the REAL store (``rt.cas.resolve_blob``).
    """

    def _resolve_blob(uri: str) -> bytes:
        if uri == _DIFF_BODY_URI:
            return diff_body.encode("utf-8")
        return store.resolve_blob(uri)

    return _resolve_blob


def _materialization_seam(
    real_artifacts: bool, *, store: ArtifactStore, diff_body: str
) -> tuple[ArtifactResolver, BlobResolver]:
    """The (resolve, resolve_blob) pair for the run (Phase-1 2b).

    Real backend ⇒ resolve straight off the real CAS (the agents' authored
    designs/criteria + the adapter's real FeatureDiff are the bytes the gates
    run). Mock ⇒ the synthetic SUP7 doubles that guarantee the deterministic
    Spec/Build/Test PASS with the StubAdapter (byte-identical smoke)."""
    if real_artifacts:
        return store.resolve, store.resolve_blob
    return _synthetic_resolve(store), _synthetic_resolve_blob(store, diff_body)


def _judgment_anchors(real_artifacts: bool) -> dict[str, str]:
    """The pre-build judgment-binding LABELS pinned on the run config (Phase-1 2c).

    Mock ⇒ pin the synthetic subject + tree label the synthetic resolver
    produces (the reviewer's advisory verdict binds them; byte-identical smoke).
    Real backend ⇒ pin NOTHING: the reviewer then binds the REAL newest
    FeatureDiff uri (state fallback) + the REAL integrated tree_sha (read off the
    attested gate verdicts). The gates bind the real verdict-carried
    ``run_against``, never these labels, so unpinning is safe."""
    if real_artifacts:
        return {}
    return {"subject_artifact_uri": _DIFF_BODY_URI, "state_sha": "integrated-sha"}
