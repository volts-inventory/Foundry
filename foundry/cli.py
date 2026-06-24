"""Foundry CLI — run a one-line request end-to-end through the factory graph.

(SPEC.md "SMOKE-TEST CORE PATH"; implementation-plan §0.3, §7; T-M1-15)

This is the Phase-1 smoke-test entry point: from a single natural-language
request it assembles the FULL dev wiring with the test doubles (NO live API key,
NO ``claude -p`` binary, NO network), compiles the factory graph, and runs the
SPEC core path::

    intake -> product -> architecture -> [spec gate] -> implementation
           -> quality -> [build+test gates] -> emit (PR-equivalent action)

end to end, returning the emitted reviewed-artifact handle, the single ``open_pr``
:class:`ExternalAction` (its idempotency key + payload uri), and the attested
gate statuses.

The dev wiring (LEARNINGS D1-D6, AS1-AS5, FG1-3, GW1-3, SUP1-7) — mirrors the
proven assembly in ``tests/test_factory_graph.py`` / ``tests/test_supervisor.py``:

* an in-memory :class:`ArtifactStore` (``rt.cas``) — the real content-addressed
  body store written on emit (AS1-AS5);
* an :class:`AgentRuntime` whose model egress is a real
  :class:`ModelProviderProxy` backed by a :class:`MockTransport` (deterministic,
  no live API key, L6/D1) + a dev :class:`ZoneBSigner`, and whose coding
  substrate is a :class:`StubAdapter` (no ``claude -p``, D3);
* a real :class:`TwoZoneGateRunner` (dev signer + :class:`GlobalRecords` +
  scanner/lineage revs) — the SOLE minter of signed verdicts (L5/GW2);
* a :class:`DevGateWriteVerifier` installed on the ``merge_gates`` contextvar so
  the REAL reducer admits the runner-attested verdicts (D4) — its token is reset
  in a ``finally`` so it never leaks;
* a dev :class:`FoundryCheckpointer` so the run is durable + resumable;
* the materialization seams ``resolve`` / ``resolve_blob`` (GW1a/D-R1-02). The
  StubAdapter authors a deterministic no-op blob (not a passing test suite), so
  to drive the SPEC core path to a clean Spec/Build/Test PASS the smoke run uses
  the SAME KIND-dispatching synthetic resolver the end-to-end tests use (SUP7):
  it synthesizes a faithful body per ``handle.kind`` and a passing unified-diff
  blob. The real :meth:`ArtifactStore.resolve` / :meth:`ArtifactStore.resolve_blob`
  views are wired as the default fallthrough so a future adapter that authors a
  real passing suite needs no CLI change.

ALL model egress stays behind the proxy (L6) — this module never imports
``anthropic`` and never touches GitHub / the network: the PR is expressed ONLY
as an :class:`ExternalAction` on the ``actions`` channel (FG2); the outbox relay
is the sole component that ever touches the outside world.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from foundry.agents import AgentRuntime
from foundry.docs import write_bundle
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import build_factory_graph, terminal_gates_pass
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import FrozenModel
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.docs import DocumentationBundle
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact, ExternalAction
    from foundry.state.factory_state import FactoryState

__all__ = ["RunResult", "main", "run"]

# Dev trust-root material (HMAC keys stand in for the production roots, §0.3).
_KEY = b"dev-zone-b-cli-key-0123456789-abcdef"
_KEY_ID = "dev-key-cli"
_SCANNER_REV = "scanner-rev-cli-1"
_LINEAGE_REV = "lineage-rev-cli-1"

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

_INTENT_PAYLOAD: dict[str, Any] = {
    "intent": "Run the one-line request through the factory.",
    "assumptions": ["single user"],
    "unknowns": [],
    "success_criteria": ["the requested behavior works"],
    "must_haves": ["core feature"],
    "nice_to_haves": [],
}

_PRD_PAYLOAD: dict[str, Any] = {
    "title": "Requested service",
    "prd_body": "Deliver the requested behavior.",
    "required_capabilities": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "text": "Given the service, When invoked, Then it responds.",
            "criticality": "normal",
            "oracle_hint": "call it; observe the response",
            "security_abuse": False,
        },
        {
            "id": "AC-002",
            "text": "Given an unauthenticated request, When it hits the API, Then 401.",
            "criticality": "high-risk",
            "oracle_hint": "no token -> 401; negative oracle",
            "security_abuse": True,
        },
    ],
}

_CRITIQUE_PAYLOAD: dict[str, Any] = {"missing": [], "security_abuse_omissions": []}

_SPRINT_PAYLOAD: dict[str, Any] = {
    "milestones": ["M1"],
    "usd_cap_microusd": 30_000_000,
    "risk_register_body": "none",
    "done_definition": "all criteria green",
}

_DESIGN_PAYLOAD: dict[str, Any] = {
    "title": "Requested service",
    "design_body": "api -> store",
    "components": ["api"],
    "adrs": [],
    "interfaces": [],
    "ownership": {"app.py": "owner-a"},  # one unit -> one task -> one diff
    "pinned_constraints": [],
}

_DECOMPOSITION_PAYLOAD: dict[str, Any] = {
    "tasks": [{"unit": "app.py", "title": "build app", "criterion_ids": ["AC-001"]}]
}

_QA_PAYLOAD: dict[str, Any] = {
    "tests": [{"test_id": "t-1", "criterion_ids": ["AC-001", "AC-002"]}],
    "quarantined": [],
}

_SDET_PAYLOAD: dict[str, Any] = {
    "scenarios": [{"scenario_id": "e2e-1", "criterion_ids": ["AC-001"]}],
    "quarantined": [],
}

_RUNBOOK_PAYLOAD: dict[str, Any] = {
    "overview": "Operational guide for deploying and running the service.",
    "prerequisites": ["provisioned runtime", "configured secrets"],
    "deploy_steps": ["build artifact", "run gates", "promote to prod"],
    "rollback_steps": ["re-point to previous artifact digest", "verify health"],
    "health_checks": ["GET /healthz returns 200"],
    "monitoring": ["error rate", "p99 latency"],
    "oncall_escalation": ["page primary on-call after 5m of failing health checks"],
}
_KNOWN_ISSUES_PAYLOAD: dict[str, Any] = {
    "issues": [
        {
            "summary": "Dev-grade in-memory persistence",
            "impact": "State is lost on restart",
            "workaround": "Wire a durable store before production",
            "severity": "medium",
        }
    ],
    "limitations": ["single-region deployment assumed"],
}
_UPGRADE_PAYLOAD: dict[str, Any] = {
    "from_version": "0.0.0",
    "to_version": "0.1.0",
    "breaking_changes": [],
    "migration_steps": ["no migration required for the initial release"],
    "rollback_notes": "Re-deploy the previous artifact digest.",
}
_PERF_COST_PAYLOAD: dict[str, Any] = {
    "summary": "Build completed within the configured budget.",
    "observations": ["most tokens were spent in the architecture and quality stages"],
    "recommendations": ["enable prompt caching on the stable SOP prefixes"],
}

_SCRIPTS: dict[str, str] = {
    "Stakeholder / Client Proxy": json.dumps(_INTENT_PAYLOAD),
    "Product Manager": json.dumps(_PRD_PAYLOAD),
    "Criteria-Completeness Critic": json.dumps(_CRITIQUE_PAYLOAD),
    "Engineering / Program Manager": json.dumps(_SPRINT_PAYLOAD),
    "Principal Architect": json.dumps(_DESIGN_PAYLOAD),
    "Senior Engineer": json.dumps(_DECOMPOSITION_PAYLOAD),
    "QA Engineer": json.dumps(_QA_PAYLOAD),
    "SDET": json.dumps(_SDET_PAYLOAD),
    # Technical Writer (foundry/docs) authored docs.
    "OPERATIONAL RUNBOOK": json.dumps(_RUNBOOK_PAYLOAD),
    "KNOWN-ISSUES": json.dumps(_KNOWN_ISSUES_PAYLOAD),
    "UPGRADE / MIGRATION GUIDE": json.dumps(_UPGRADE_PAYLOAD),
    "PERFORMANCE & COST": json.dumps(_PERF_COST_PAYLOAD),
}


# ---- in-memory materialization doubles (SUP7) -------------------------------


def _synthetic_resolve(store: ArtifactStore) -> ArtifactResolver:
    """A KIND-dispatching typed resolver that falls through to the real store.

    The architecture/product agents mint content-addressed HANDLES mid-run whose
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
            return OwnershipMap(owners={"app.py": "owner-a"}, seq=handle.seq)
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


# ---- final source-tree materialization (on a passing build) -----------------

# The gate-input file the integrated tree carries for the Spec gate; it is NOT a
# source file the user asked the factory to produce, so it is excluded from the
# emitted output tree.
_GATE_INPUT_FILES = frozenset({"criteria.json"})


def _write_output_tree(
    final_state: FactoryState,
    output_dir: str,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> str:
    """Materialize the FINAL accepted integrated tree and write the source files.

    Reuses :func:`graph.gate_wiring.materialize_workdir` over the reviewed
    FeatureDiff handles + the same ``resolve`` / ``resolve_blob`` seams the run
    wired (so the bytes written are EXACTLY what the terminal gates ran against,
    never a parallel re-render). The materialization happens in a throwaway temp
    dir; the real source files (everything except the Spec gate's ``criteria.json``
    input) are then copied into ``output_dir`` on disk. Returns the resolved
    absolute path written.

    ``output_dir`` is operator-supplied, but the per-file relative paths come
    from agent-influenced diff bodies; ``materialize_workdir`` already rejects an
    absolute/``..`` diff path fail-closed (D-R1-03), and we re-check each copied
    relative path against the destination root (mirrors
    :func:`docs.assemble.write_bundle`) so a write can never escape ``output_dir``.
    """
    from foundry.graph.gate_wiring import materialize_workdir

    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="foundry-cli-out-") as staging:
        stage_root = Path(staging)
        materialize_workdir(
            final_state, stage_root, resolve=resolve, resolve_blob=resolve_blob
        )
        for src in sorted(stage_root.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(stage_root)
            if rel.name in _GATE_INPUT_FILES and len(rel.parts) == 1:
                continue
            # Defense-in-depth (mirrors docs/assemble.write_bundle): reject an
            # absolute or '..' relative path before writing under out_root.
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe source path {str(rel)!r}")
            target = (out_root / rel).resolve()
            if not target.is_relative_to(out_root):
                raise ValueError(f"source path escapes output dir: {str(rel)!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
    return str(out_root)


# ---- result ----------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """The outcome of a smoke run — what the CLI reports.

    Holds only content-addressed handles / identifiers + the attested gate
    statuses; carries no bodies (L4) and makes no external effect.
    """

    request: str
    thread_id: str
    passed: bool
    phase: str
    artifact_uri: str | None
    """The reviewed FeatureDiff handle uri the PR opens against (the PR subject)."""
    open_pr_idempotency_key: str | None
    open_pr_payload_uri: str | None
    gate_statuses: dict[str, GateStatus]
    """Newest-by-name attested status per gate (spec / build / test / review...)."""
    doc_root: str | None = None
    """Where the per-build documentation set was written (when ``docs_dir`` set)."""
    output_root: str | None = None
    """Where the final accepted source tree was written (when ``output_dir`` is
    set AND the build passed); ``None`` on a halted run (no PASS, no write)."""

    def summary(self) -> str:
        """A concise human-readable report of the run (printed by ``main``)."""
        lines = [
            f"request:   {self.request!r}",
            f"thread_id: {self.thread_id}",
            f"phase:     {self.phase}",
            f"result:    {'PASS' if self.passed else 'HALTED (no PR)'}",
            "gates:",
        ]
        for name in sorted(self.gate_statuses):
            lines.append(f"  - {name}: {self.gate_statuses[name].value}")
        if self.passed:
            lines.append("reviewed artifact (PR subject):")
            lines.append(f"  {self.artifact_uri}")
            lines.append("open_pr action:")
            lines.append(f"  idempotency_key: {self.open_pr_idempotency_key}")
            lines.append(f"  payload_uri:     {self.open_pr_payload_uri}")
        else:
            lines.append("no open_pr action emitted (gates did not all PASS).")
        if self.output_root:
            lines.append(f"source tree written to: {self.output_root}")
        if self.doc_root:
            lines.append(f"documentation set written to: {self.doc_root}")
        return "\n".join(lines)


# ---- the smoke run ----------------------------------------------------------


def run(
    request: str,
    *,
    thread_id: str = "cli-run",
    resolve: ArtifactResolver | None = None,
    resolve_blob: BlobResolver | None = None,
    diff_body: str = _PASSING_DIFF,
    max_fix_attempts: int = 3,
    docs_dir: str | None = None,
    output_dir: str | None = None,
) -> RunResult:
    """Run ``request`` end-to-end through the compiled factory graph (smoke path).

    Assembles the full dev wiring with the test doubles (NO live API key, NO
    ``claude -p`` binary, NO network), compiles :func:`build_factory_graph`,
    invokes it with ``{"request": request}`` + a ``thread_id`` config, and
    returns a :class:`RunResult` reporting the reviewed artifact handle, the
    single ``open_pr`` :class:`ExternalAction`, and the attested gate statuses.

    Parameters
    ----------
    request:
        The one-line natural-language request (e.g. ``"build a URL shortener"``).
    thread_id:
        The run identity (LangGraph ``thread_id`` / ``Seq.run_id``); also seeds
        the deterministic idempotency keys (verdict nonce, PR key).
    resolve / resolve_blob:
        Materialization seams (GW1a/D-R1-02). Default to the proven synthetic
        SUP7 doubles (which fall through to the real ``rt.cas`` views) so the
        SPEC core path reaches a clean Spec/Build/Test PASS with the StubAdapter.
    diff_body:
        The unified-diff body the synthetic ``resolve_blob`` returns for the
        FeatureDiff (default: a passing addition; pass an error/failing body to
        exercise the halted-run path).
    max_fix_attempts:
        Convergence bound for the gate fix-loop.
    docs_dir:
        When set, write the company-shaped documentation set + BUILD-MANIFEST to
        this directory (best-effort; surfaced on ``RunResult.doc_root``).
    output_dir:
        When set AND the build PASSES (terminal gates pass), materialize the
        final accepted integrated source tree to this directory on disk; the
        written path is surfaced on ``RunResult.output_root``. On a halted run
        nothing is written (no accepted tree to emit).
    """
    store = ArtifactStore()
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(_KEY, _KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=store)

    records = GlobalRecords()
    runner = TwoZoneGateRunner(ZoneBSigner(_KEY, _KEY_ID), records, _SCANNER_REV, _LINEAGE_REV)

    resolve = resolve or _synthetic_resolve(store)
    resolve_blob = resolve_blob or _synthetic_resolve_blob(store, diff_body)

    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "verdict_nonce": f"N-{thread_id}",
            "subject_artifact_uri": _DIFF_BODY_URI,
            "state_sha": "integrated-sha",
        },
        "metadata": {"langgraph_step": 1},
        "recursion_limit": 200,
    }

    # The terminal ``docs`` graph node assembles the company-shaped documentation
    # set + BUILD-MANIFEST IN-GRAPH (task [3]): the Technical Writer's model spend
    # is budget-metered through the SAME proxy and the authored doc artifacts land
    # on the checkpointed ``artifacts`` channel. The node appends the assembled
    # bundle to this in-process sink so the CLI can WRITE the already-assembled
    # tree to disk without re-authoring (no second round of model spend). The sink
    # is only wired when ``--docs DIR`` was requested.
    doc_sink: list[DocumentationBundle] = []

    # Install the dev gate-write verifier so the REAL merge_gates admits the
    # runner-attested verdicts (D4); reset its token in finally so it never leaks.
    token = set_gate_write_verifier(DevGateWriteVerifier(_KEY, _KEY_ID, records))
    ckpt = FoundryCheckpointer()
    with tempfile.TemporaryDirectory(prefix="foundry-cli-src-") as source_dir:
        try:
            app = build_factory_graph(
                rt,
                runner=runner,
                resolve=resolve,
                resolve_blob=resolve_blob,
                source_dir=source_dir,
                checkpointer=ckpt.saver,
                max_fix_attempts=max_fix_attempts,
                doc_sink=doc_sink if docs_dir is not None else None,
            )
            final = app.invoke({"request": request}, config)
        finally:
            reset_gate_write_verifier(token)
            ckpt.close()

    final_state = cast("FactoryState", final)
    result = _result(request, thread_id, final_state)
    if output_dir is not None and result.passed:
        # On a PASSING build, materialize the FINAL accepted integrated tree
        # (reusing the SAME materialize_workdir + resolve/resolve_blob seams the
        # terminal gates ran against) and write the real source files to disk.
        # Halted runs write nothing — there is no accepted tree to emit.
        output_root = _write_output_tree(
            final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob
        )
        result = replace(result, output_root=output_root)
    if docs_dir is not None and doc_sink:
        # AUTHORING + the manifest already happened IN-GRAPH (budget-metered,
        # checkpointed). The CLI only WRITES the already-assembled bundle to disk.
        # The blanket ``except Exception`` is GONE: the bundle was assembled
        # fail-safe inside the node, so a missing bundle means docs were skipped
        # (halted run) or the node swallowed an authoring error — both leave
        # ``doc_sink`` empty and we simply don't write. A FAILURE here is now only
        # a disk-write fault, which we let surface (a narrow guard) rather than
        # re-swallow wiring bugs the assembly deliberately would have raised.
        write_bundle(doc_sink[-1], docs_dir)
        result = replace(result, doc_root=docs_dir)
    return result


def _result(request: str, thread_id: str, final: FactoryState) -> RunResult:
    """Distill the terminal state into a :class:`RunResult` (no bodies, L4)."""
    statuses: dict[str, GateStatus] = {}
    for g in final.get("gates") or ():
        prev = statuses.get(g.name)
        # newest-by-name (the verdict that gates the release; mirrors emit guard)
        if prev is None:
            statuses[g.name] = g.status

    open_prs: list[ExternalAction] = [
        a for a in (final.get("actions") or ()) if a.kind == "open_pr"
    ]
    pr = open_prs[0] if open_prs else None
    return RunResult(
        request=request,
        thread_id=thread_id,
        passed=terminal_gates_pass(final),
        phase=str(final.get("phase") or "unknown"),
        artifact_uri=pr.payload_uri if pr is not None else None,
        open_pr_idempotency_key=pr.idempotency_key if pr is not None else None,
        open_pr_payload_uri=pr.payload_uri if pr is not None else None,
        gate_statuses=statuses,
    )


# ---- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m foundry.cli "build a ..."`` — run the smoke path, print a report.

    Returns process exit code 0 on a clean Spec/Build/Test PASS (a PR action was
    emitted), 1 on a halted/failed run, 2 on a usage error.
    """
    args = sys.argv[1:] if argv is None else argv
    docs_dir: str | None = None
    if "--docs" in args:
        i = args.index("--docs")
        if i + 1 >= len(args):
            print("error: --docs requires a directory argument", file=sys.stderr)
            return 2
        docs_dir = args[i + 1]
        args = args[:i] + args[i + 2 :]
    output_dir: str | None = None
    if "--out" in args:
        i = args.index("--out")
        if i + 1 >= len(args):
            print("error: --out requires a directory argument", file=sys.stderr)
            return 2
        output_dir = args[i + 1]
        args = args[:i] + args[i + 2 :]
    if not args:
        print(
            'usage: python -m foundry.cli [--docs DIR] [--out DIR] "<one-line request>"',
            file=sys.stderr,
        )
        return 2
    request = " ".join(args).strip()
    if not request:
        print("error: request must be non-empty", file=sys.stderr)
        return 2

    result = run(request, docs_dir=docs_dir, output_dir=output_dir)
    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
