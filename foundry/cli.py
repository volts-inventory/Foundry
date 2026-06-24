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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langgraph.types import Command

from foundry.agents import AgentRuntime
from foundry.docs import write_bundle
from foundry.durability.artifact_store import ArtifactStore
from foundry.durability.checkpointer import FoundryCheckpointer
from foundry.durability.persistence import (
    DurableBackends,
    durable_backends,
    state_dir_from_env,
)
from foundry.execution.adapters.stub import StubAdapter
from foundry.gates import DevGateWriteVerifier, TwoZoneGateRunner, ZoneBSigner
from foundry.graph.factory_graph import build_factory_graph, terminal_gates_pass
from foundry.graph.supervisor import (
    MODE_ASSISTANT,
    MODE_AUTONOMOUS,
    MODE_GATED,
    normalize_checkpoints,
)
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import OwnershipMap, SystemDesign
from foundry.schemas.base import FrozenModel
from foundry.schemas.checkpoint import CHECKPOINT_BOUNDARIES
from foundry.schemas.gates import GateStatus
from foundry.schemas.implementation import FeatureDiff
from foundry.schemas.product import AcceptanceCriterion, Criticality
from foundry.state.global_records import GlobalRecords
from foundry.state.reducers import reset_gate_write_verifier, set_gate_write_verifier

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.docs import DocumentationBundle
    from foundry.domains.contract import DomainPackContract
    from foundry.domains.registry import PackRegistry
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.schemas.base import Artifact, ExternalAction
    from foundry.schemas.checkpoint import CheckpointBoundary
    from foundry.state.factory_state import FactoryState

# A programmatic checkpoint responder (plan [10]): given the READ-ONLY inspection
# payload surfaced at a pause, return the human decision (a verdict string, or a
# mapping with ``verdict`` + optional ``steered_by``/``note``). Programmatic by
# design — NO interactive stdin — so the checkpoint flow is fully testable.
CheckpointResponder = Callable[[dict[str, Any]], "str | dict[str, Any]"]

__all__ = ["MODES", "CheckpointResponder", "RunResult", "main", "run"]

# Dev trust-root material (HMAC keys stand in for the production roots, §0.3).
_KEY = b"dev-zone-b-cli-key-0123456789-abcdef"
_KEY_ID = "dev-key-cli"
_SCANNER_REV = "scanner-rev-cli-1"
_LINEAGE_REV = "lineage-rev-cli-1"

# The fixed diff_uri the synthetic FeatureDiff body points at; resolve_blob
# returns the passing unified-diff body keyed on it (SUP7).
_DIFF_BODY_URI = "cas://cli/diff-body"

# The dev-grade trusted-builder identity + deploy target the AUTONOMOUS trust-root
# chain (build → admit → deploy) wires hands-off. They stand in for the production
# KMS/OIDC builder + deploy target (plan §0.3); the builder shares the run's zone-B
# key so admission re-verifies the build signature under the SAME root.
_BUILDER_ID = "dev-trusted-builder-cli"
_DEPLOY_TARGET = "prod"

# The release modes the CLI exposes (the supervisor mode constants; assistant is the
# DEFAULT — byte-identical to the historical request-only run).
MODES = (MODE_ASSISTANT, MODE_GATED, MODE_AUTONOMOUS)

# The first-party domain packs selectable via ``--packs`` (T-M5-08). Mapped by the
# stable pack NAME (``pack.name``); a NEW pack is added here AND via register() — but
# the graph wiring stays open/closed (it reads the COMPOSED policy generically).
_PACK_FACTORIES: dict[str, Callable[[], DomainPackContract]] = {}


def _pack_factories() -> dict[str, Callable[[], DomainPackContract]]:
    """The lazily-built ``name -> pack-factory`` map for ``--packs`` selection.

    Imported lazily (the pack modules pull in the domain contract) so a plain
    ``--mode assistant`` run never imports the domain layer (back-compat: no new
    import cost / no new surface on the default path).
    """
    if not _PACK_FACTORIES:
        from foundry.domains.packs.frontend import FrontendPack
        from foundry.domains.packs.ml_data import MLDataPack
        from foundry.domains.packs.regulated import RegulatedPack

        for cls in (RegulatedPack, MLDataPack, FrontendPack):
            _PACK_FACTORIES[cls().name] = cls
    return _PACK_FACTORIES


def _build_pack_registry(packs: list[str]) -> tuple[PackRegistry, frozenset[str]]:
    """Build a :class:`PackRegistry` from selected pack NAMES + their capabilities.

    Returns the registry (with each named pack registered) and the UNION of the
    selected packs' declared capabilities. The capabilities are seeded onto the
    request's ``required_capabilities`` channel so the COMPOSED policy actually
    engages (a pack's veto / approver-of-record / HITL fires off the request
    capabilities, not merely off registration — mirrors the supervisor-pack
    tests). Raises :class:`ValueError` for an unknown pack name (fail-closed; a
    typo never silently engages nothing)."""
    from foundry.domains.registry import PackRegistry

    factories = _pack_factories()
    registry = PackRegistry()
    capabilities: set[str] = set()
    for name in packs:
        factory = factories.get(name)
        if factory is None:
            raise ValueError(
                f"unknown pack {name!r}; available: {', '.join(sorted(factories))}"
            )
        pack = factory()
        registry.register(pack)
        capabilities.update(pack.capabilities)
    return registry, frozenset(capabilities)


def _enforce_mode_veto(
    rt: AgentRuntime,
    config: RunnableConfig,
    registry: PackRegistry,
    capabilities: frozenset[str],
    *,
    requested_mode: str,
) -> None:
    """Fail-closed: refuse to run a mode an engaged domain pack VETOES (T-M5-08).

    The composed ``mode_tier`` policy is a PURE function of the request's
    capabilities; :func:`resolve_effective_mode_tier` resolves the EFFECTIVE mode
    after the veto. If the requested mode is no longer reachable (e.g. a regulated
    pack vetoes hands-off Autonomous, degrading it to Gated), we RAISE rather than
    silently run the (now-vetoed) mode — the operator must re-issue with the
    allowed mode. This mirrors the supervisor's at-triage degrade, surfaced as an
    explicit CLI error so the trust posture the operator believes they ran is the
    posture that actually ran."""
    from foundry.domains.governance_hooks import resolve_effective_mode_tier

    request_view = {"required_capabilities": sorted(capabilities)}
    policy = registry.compose(request_view)
    eff = resolve_effective_mode_tier(
        policy.mode_tier,
        requested_mode=requested_mode,
        request=request_view,
        capabilities=sorted(capabilities),
        seq=rt.seq(config, "cli", "pack:mode_veto"),
    )
    if requested_mode not in eff.reachable_modes:
        raise ValueError(
            f"mode {requested_mode!r} is VETOED by the engaged pack(s) "
            f"{', '.join(p.name for p in registry.packs())}; the strictest "
            f"reachable mode is {eff.effective_mode!r} "
            f"(re-run with --mode {eff.effective_mode})"
        )

# Spec ingestion (task [8]). A whole-run cap on the design-spec text seeded into
# state: bounds checkpoint size + token cost on a large doc / directory; the
# product nodes truncate again for their prompts. Read as UTF-8, errors replaced
# (a spec is plain input data, never executed — no new trust surface).
_SPEC_MAX_BYTES = 512 * 1024
_SPEC_SUFFIXES = (".md", ".txt")

# A unified-diff ADDITION blob carrying a passing test, so the integrated tree
# the gates run against yields Build PASS (compiles) + Test PASS (pytest green).
_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
)

# The Gated/Autonomous spine ADDS the assurance phase (Security/Audit/supply-chain
# on the integrated tree). The Audit gate reads a lockfile inventory (AW2), so the
# assurance-mode default diff carries a ``uv.lock`` alongside the passing test (no
# hardcoded secret → Security PASS). Used as the run() default ONLY for
# gated/autonomous (assistant stays byte-identical to the Phase-1 _PASSING_DIFF).
_ASSURANCE_PASSING_DIFF = (
    "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+VALUE = 42\n"
    "--- /dev/null\n+++ b/test_app.py\n@@ -0,0 +1,2 @@\n"
    "+def test_ok():\n    assert 1 + 1 == 2\n"
    '--- /dev/null\n+++ b/uv.lock\n@@ -0,0 +1,2 @@\n+name = "pkg"\n    license = "MIT"\n'
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

# The assurance-phase agents (Gated/Autonomous spine only). Mirrors the gated /
# autonomous end-to-end test fixtures: a clean STRIDE model, a clean SBOM, and a
# clean supply-chain review → Security/Audit/supply_chain PASS on the integrated
# tree. Unused on the Assistant path (the assurance subgraph is never mounted).
_APPSEC_PAYLOAD: dict[str, Any] = {
    "threat_model_body": "STRIDE model",
    "oracle_bindings": {"S1": "test_auth"},
    "residual_item_ids": [],
    "severity": "low",
    "findings": [],
}
_AUDIT_PAYLOAD: dict[str, Any] = {"sbom_body": "sbom", "severity": "low", "findings": []}
_SUPPLY_CHAIN_PAYLOAD: dict[str, Any] = {
    "report_body": "supply-chain review",
    "policy_violations": [],
    "trust_root_touched": False,
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
    # Assurance phase (Gated / Autonomous spine).
    "You are the Security / AppSec Engineer": json.dumps(_APPSEC_PAYLOAD),
    "You are the Audit / Compliance agent": json.dumps(_AUDIT_PAYLOAD),
    "You are the Supply-Chain Reviewer": json.dumps(_SUPPLY_CHAIN_PAYLOAD),
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


# ---- design-spec ingestion (task [8]) ---------------------------------------


def _read_spec_path(path: str) -> str:
    """Read a design-spec FILE or DIRECTORY of ``.md``/``.txt`` docs to text.

    A directory is read recursively in sorted (deterministic) path order; only
    ``.md``/``.txt`` files are included. Each file's text is prefixed with a
    ``# === <relative-path> ===`` marker so a multi-file spec stays legible.
    Raises :class:`FileNotFoundError` for a missing path and :class:`ValueError`
    when a directory contains no spec files (a clean, actionable error).

    The spec is INPUT DATA only: it is read as UTF-8 (decode errors replaced) and
    never executed — no new egress, no new trust surface. The combined text is
    capped at :data:`_SPEC_MAX_BYTES` to bound checkpoint size / token cost.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"spec path not found: {path}")
    if p.is_dir():
        files = sorted(
            f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in _SPEC_SUFFIXES
        )
        if not files:
            raise ValueError(
                f"spec directory has no {'/'.join(_SPEC_SUFFIXES)} files: {path}"
            )
        parts = [
            f"# === {f.relative_to(p)} ===\n{f.read_text(encoding='utf-8', errors='replace')}"
            for f in files
        ]
        return "\n\n".join(parts)
    return p.read_text(encoding="utf-8", errors="replace")


def _is_existing_path(value: str) -> bool:
    """True iff ``value`` names an existing path. Guards ``Path.exists()`` against
    an ``OSError`` (e.g. a long literal-spec string exceeding the OS name limit),
    so literal spec text never crashes the path probe. An empty/whitespace string
    is NEVER a path (``Path("").exists()`` resolves to cwd) — else an explicit
    ``spec=""`` would silently ingest the entire working directory."""
    if not value.strip():
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _load_spec(spec: str | list[str] | None) -> str:
    """Resolve the ``spec`` argument to the design-spec text seeded into state.

    ``spec`` is additive (task [8]) and may be:

    * ``None`` — no spec (request-only run, unchanged behavior);
    * a single string that names an existing file/dir — its text is read;
    * a single string that is NOT a path — treated as literal spec text;
    * a list of file/dir paths — each read and concatenated (sorted by path
      marker, deterministic).

    The combined text is capped at :data:`_SPEC_MAX_BYTES`.
    """
    if spec is None:
        return ""
    if isinstance(spec, str):
        if not spec.strip():
            return ""  # empty/blank literal == no spec (never probed as a path)
        text = _read_spec_path(spec) if _is_existing_path(spec) else spec
    else:
        text = "\n\n".join(_read_spec_path(s) for s in sorted(spec))
    encoded = text.encode("utf-8")
    if len(encoded) > _SPEC_MAX_BYTES:
        # Cap on ENCODED BYTES (deterministic, truly bounds checkpoint size even
        # for multibyte specs); decode back, dropping a partial trailing char.
        # The product nodes truncate again for their prompts.
        text = encoded[:_SPEC_MAX_BYTES].decode("utf-8", errors="ignore")
    return text


# ---- per-mode trust posture (lifecycle.md §4 deployment-trust modes) --------
# A one-line human summary of the trust root each mode engages, surfaced in the
# run summary + --help so the operator sees WHAT authority releases the artifact.
_TRUST_POSTURE: dict[str, str] = {
    MODE_ASSISTANT: (
        "advisory review, no trust root — produces a bare open_pr a human reviews "
        "+ merges via normal CI (the reviewer's verdict is ADVISORY)"
    ),
    MODE_GATED: (
        "full gate set + human sign-off — a release candidate behind Spec/Build/Test "
        "+ the tier-scaled assurance family, carrying a WORM evidence pack, gated on "
        "a recorded Design + Release sign-off"
    ),
    MODE_AUTONOMOUS: (
        "full trust root — hands-off build -> admit -> deploy; deploy-time admission "
        "is the SOLE release gate (no human sign-off)"
    ),
}


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
    mode: str = MODE_ASSISTANT
    """The release mode the run was driven in (assistant | gated | autonomous)."""
    packs: tuple[str, ...] = ()
    """The domain packs engaged (by name) — empty when no ``--packs`` were given."""
    artifact_uri: str | None = None
    """The reviewed FeatureDiff handle uri the PR opens against (the PR subject)."""
    open_pr_idempotency_key: str | None = None
    open_pr_payload_uri: str | None = None
    gate_statuses: dict[str, GateStatus] = field(default_factory=dict)
    """Newest-by-name attested status per gate (spec / build / test / review...)."""
    doc_root: str | None = None
    """Where the per-build documentation set was written (when ``docs_dir`` set)."""
    output_root: str | None = None
    """Where the final accepted source tree was written (when ``output_dir`` is
    set AND the build passed); ``None`` on a halted run (no PASS, no write)."""
    checkpoints: tuple[str, ...] = ()
    """The checkpoint stage boundaries the run was driven with (plan [10]); empty
    when checkpoints were OFF (the default)."""
    paused_at: str | None = None
    """The stage boundary the run is PAUSED at awaiting a human decision (plan
    [10]) — set only when checkpoints are on and no responder drove past the pause.
    ``None`` means the run completed (or was never checkpointed)."""

    def summary(self) -> str:
        """A concise human-readable report of the run (printed by ``main``)."""
        lines = [
            f"request:   {self.request!r}",
            f"thread_id: {self.thread_id}",
            f"mode:      {self.mode}",
            f"  trust:   {_TRUST_POSTURE.get(self.mode, 'unknown')}",
        ]
        if self.packs:
            lines.append(f"packs:     {', '.join(self.packs)}")
        if self.checkpoints:
            lines.append(f"checkpoints: {', '.join(self.checkpoints)}")
        if self.paused_at is not None:
            lines.append(
                f"PAUSED at the {self.paused_at!r} checkpoint awaiting a human "
                "decision (resume with a CheckpointDecision)."
            )
        lines += [
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


# ---- checkpoint pause/resume driver (plan [10]) -----------------------------


def _pending_interrupt(result: Any) -> Any:
    """The pending LangGraph ``Interrupt`` on a paused invoke result, or ``None``.

    A checkpointed run that hits a pause returns a dict carrying ``__interrupt__``
    (the active interrupts); we read the first one's ``value`` (our read-only
    inspection payload) to drive the responder. ``None`` means the run completed."""
    if isinstance(result, dict):
        interrupts = result.get("__interrupt__")
        if interrupts:
            return interrupts[0]
    return None


def _invoke_with_checkpoints(
    app: Any,
    seed: FactoryState,
    config: RunnableConfig,
    *,
    checkpointed: bool,
    responder: CheckpointResponder | None,
) -> tuple[FactoryState, str | None]:
    """Drive the compiled graph, pausing/resuming at the checkpoint boundaries.

    Non-checkpointed runs (the default) invoke ONCE — byte-identical to today.
    A checkpointed run invokes, and while the result carries a pending
    ``__interrupt__`` it asks the ``responder`` for a decision and RESUMES via
    ``Command(resume=...)`` (the run is durable on the checkpointer). With NO
    responder, it stops at the first pause and returns ``(state, boundary)`` so the
    caller can resume out-of-band. The bound on the resume loop is the number of
    checkpoint boundaries (a responder that always rejects halts immediately)."""
    result = app.invoke(seed, config)
    if not checkpointed:
        # Byte-identical to the historical single-invoke path: the invoke return
        # IS the terminal channel state (no interrupts on a non-checkpointed run).
        return cast("FactoryState", result), None

    # Bound the loop: at most one pause per stage boundary (3) + slack.
    for _ in range(len(CHECKPOINT_BOUNDARIES) + 1):
        interrupt = _pending_interrupt(result)
        if interrupt is None:
            break
        payload = dict(interrupt.value) if isinstance(interrupt.value, dict) else {}
        if responder is None:
            # No responder: surface the pause and stop (caller resumes out-of-band).
            boundary = str(payload.get("boundary") or "")
            return cast("FactoryState", app.get_state(config).values), boundary or None
        decision = responder(payload)
        result = app.invoke(Command(resume=decision), config)
    # The authoritative terminal state is the checkpointed channel view.
    return cast("FactoryState", app.get_state(config).values), None


# ---- the smoke run ----------------------------------------------------------


def run(
    request: str,
    *,
    mode: str = MODE_ASSISTANT,
    packs: list[str] | None = None,
    spec: str | list[str] | None = None,
    thread_id: str = "cli-run",
    resolve: ArtifactResolver | None = None,
    resolve_blob: BlobResolver | None = None,
    diff_body: str | None = None,
    max_fix_attempts: int = 3,
    docs_dir: str | None = None,
    output_dir: str | None = None,
    checkpoints: bool | list[str | CheckpointBoundary] | None = None,
    checkpoint_responder: CheckpointResponder | None = None,
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
    mode:
        The deployment-trust mode (lifecycle.md §4; default ``"assistant"`` — the
        run is byte-identical to the historical request-only run). ``"gated"``
        threads the Phase-2 supervisor (tiering + Design/Release sign-off +
        assurance) and the evidence-pack-carrying, release-gated emit; the
        reviewer's verdict is advisory in Assistant — the deterministic gates own
        the release bit. ``"autonomous"`` (Phase-4) threads the SAME assurance
        spine and additionally constructs the dev-grade TRUST ROOT (a hermetic
        :class:`TrustedBuilder` + the fail-closed :class:`AdmissionController` +
        the least-privileged :class:`Deployer` + a :class:`DevGateWriteVerifier`,
        all sharing this run's zone-B key + cross-fork records), so the emit is
        the hands-off build → admit → deploy chain (admission is the SOLE release
        gate). Unknown modes are rejected fail-closed.
    packs:
        OPTIONAL domain packs to engage (by NAME — ``regulated`` / ``ml_data`` /
        ``frontend``; T-M5-08). The selected packs are registered in a
        :class:`PackRegistry` (passed to the graph) AND their declared
        capabilities are seeded onto ``required_capabilities`` so the composed
        policy actually engages at triage + the release boundary. A pack can only
        TIGHTEN the release; crucially it may VETO an unsafe mode (a regulated pack
        makes hands-off Autonomous UNREACHABLE / forces a human approver-of-record).
        The veto is enforced FAIL-CLOSED up front: if the requested mode is vetoed
        the run raises a clear error rather than silently running the vetoed mode.
    spec:
        OPTIONAL full design-spec document (task [8]). ADDITIVE to ``request``: a
        short request summarizes intent while the spec provides the detail. May be
        a path to a ``.md``/``.txt`` FILE, a path to a DIRECTORY of such files
        (read recursively, sorted), a list of paths, or literal spec text. The
        resolved text is seeded onto the ``spec`` state channel, where the
        Stakeholder/Client Proxy and Product Manager GROUND the IntentBrief/PRD in
        it. It is plain input data — it INFORMS the criteria but never bypasses the
        acceptance-criteria derivation or any signed gate.
    thread_id:
        The run identity (LangGraph ``thread_id`` / ``Seq.run_id``); also seeds
        the deterministic idempotency keys (verdict nonce, PR key).
    resolve / resolve_blob:
        Materialization seams (GW1a/D-R1-02). Default to the proven synthetic
        SUP7 doubles (which fall through to the real ``rt.cas`` views) so the
        SPEC core path reaches a clean Spec/Build/Test PASS with the StubAdapter.
    diff_body:
        The unified-diff body the synthetic ``resolve_blob`` returns for the
        FeatureDiff. ``None`` (the default) selects a passing addition: the
        Phase-1 ``_PASSING_DIFF`` for Assistant (byte-identical to today) or the
        lockfile-bearing ``_ASSURANCE_PASSING_DIFF`` for Gated/Autonomous (the
        assurance Audit gate reads a lockfile inventory). Pass an explicit
        error/failing body to exercise the halted-run path.
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
    checkpoints:
        OPTIONAL checkpoint/HITL mode (plan [10]). ``None``/``False`` (the
        DEFAULT) runs straight through with NO pauses (byte-identical to today).
        ``True`` pauses at all three stage boundaries (after product/spec, after
        architecture/design, after implementation+quality / before release); a
        list of boundary names pauses at exactly those. The run is resumable on
        the durable checkpointer. **Trust:** a checkpoint approve only lets the run
        proceed to the (unchanged) gate boundary — it can NEVER turn a gate FAIL
        into a release; a release reject HALTS even on a gate PASS.
    checkpoint_responder:
        OPTIONAL programmatic responder driving the pauses (plan [10]; NO
        interactive stdin, so the flow is fully testable). Called with the
        READ-ONLY inspection payload at each pause; returns the human decision (a
        verdict string ``"approve"``/``"reject"``/``"needs_changes"``, or a mapping
        with ``verdict`` + optional ``steered_by``/``note`` recording steering
        authorship). When ``checkpoints`` are on but NO responder is supplied, the
        run PAUSES at the first boundary and returns with ``RunResult.paused_at``
        set (the caller resumes out-of-band on the same ``thread_id``).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose one of {', '.join(MODES)}")
    # Resolve the checkpoint boundary set up front (fail-closed on a typo'd name).
    ckpt_set = normalize_checkpoints(checkpoints)
    ckpt_names = tuple(sorted(b.value for b in ckpt_set))
    # The default passing diff is mode-specific: Assistant uses the Phase-1
    # _PASSING_DIFF (byte-identical to today); Gated/Autonomous use the
    # lockfile-bearing variant the assurance Audit gate needs. An explicit
    # diff_body (e.g. a failing body) always wins.
    if diff_body is None:
        diff_body = _PASSING_DIFF if mode == MODE_ASSISTANT else _ASSURANCE_PASSING_DIFF
    spec_text = _load_spec(spec)
    # Single-host durability seam (durability/persistence.py): when
    # FOUNDRY_STATE_DIR is SET, the checkpointer (+ its outbox/WORM audit),
    # the GlobalRecords trust ledgers and the CAS are all file-backed under that
    # dir so the run survives a process restart. UNSET (the DEFAULT) ⇒ in-memory
    # everywhere — byte-identical to the historical request-only run.
    state_dir = state_dir_from_env()
    durable: DurableBackends | None = (
        durable_backends(state_dir) if state_dir is not None else None
    )
    if durable is not None:
        # FilesystemArtifactStore is a documented structural drop-in for the
        # in-memory ArtifactStore (identical put_body/.../resolve/resolve_blob
        # seams, pinned by tests/test_fs_artifact_store.py); cast so the rest of
        # the assembly reads it as the same cas type with no call-site change.
        store: ArtifactStore = cast("ArtifactStore", durable.cas)
        records = durable.global_records
        ckpt = durable.checkpointer
    else:
        store = ArtifactStore()
        records = GlobalRecords()
        ckpt = FoundryCheckpointer()
    tx = MockTransport.scripted(dict(_SCRIPTS), default_verdict="approve")
    proxy = ModelProviderProxy(tx, signer=ZoneBSigner(_KEY, _KEY_ID))
    rt = AgentRuntime(proxy=proxy, adapter=StubAdapter(), cas=store)

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

    # -- domain packs (T-M5-08) -----------------------------------------------
    # Build the registry from the selected pack NAMES + collect their capabilities
    # (seeded onto required_capabilities so the composed policy engages). Then
    # ENFORCE the mode veto fail-closed BEFORE running: a regulated pack vetoes
    # hands-off Autonomous (and auto-approve Assistant) — we refuse rather than
    # silently downgrade, so the operator never believes a vetoed mode ran.
    pack_names = tuple(packs or ())
    pack_registry: PackRegistry | None = None
    pack_capabilities: frozenset[str] = frozenset()
    if pack_names:
        pack_registry, pack_capabilities = _build_pack_registry(list(pack_names))
        _enforce_mode_veto(rt, config, pack_registry, pack_capabilities, requested_mode=mode)

    # -- trust-root chain for AUTONOMOUS (lifecycle.md §4) --------------------
    # Construct the SAME dev-grade trust-root components the autonomous tests wire,
    # all sharing THIS run's zone-B key + cross-fork records (so admission
    # re-verifies the runner-attested gate-state + reads the disqualification
    # latch). For gated/assistant these stay None (ignored by build_factory_graph).
    builder = admission = deployer = gate_verifier = None
    if mode == MODE_AUTONOMOUS:
        from foundry.trust_root import AdmissionController, Deployer, TrustedBuilder

        builder = TrustedBuilder(ZoneBSigner(_KEY, _KEY_ID), builder_id=_BUILDER_ID)
        admission = AdmissionController(builder_id=_BUILDER_ID)
        deployer = Deployer(target=_DEPLOY_TARGET)
        gate_verifier = DevGateWriteVerifier(_KEY, _KEY_ID, records)

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
                mode=mode,
                builder=builder,
                admission=admission,
                deployer=deployer,
                gate_verifier=gate_verifier,
                pack_registry=pack_registry,
                doc_sink=doc_sink if docs_dir is not None else None,
                checkpoints=sorted(ckpt_set) if ckpt_set else None,
            )
            # `spec` is additive and seeded ONLY when provided (request-only runs
            # keep the historical {"request": ...} seed exactly). The product
            # nodes read it off the `spec` channel to ground the IntentBrief/PRD.
            seed: FactoryState = {"request": request}
            if spec_text:
                seed["spec"] = spec_text
            # Seed the selected packs' capabilities so the composed policy engages
            # at triage + the release boundary (a pack's veto / approver-of-record
            # / HITL fires off required_capabilities). Default run: never set.
            if pack_capabilities:
                seed["required_capabilities"] = sorted(pack_capabilities)
            final, paused_at = _invoke_with_checkpoints(
                app,
                seed,
                config,
                checkpointed=bool(ckpt_set),
                responder=checkpoint_responder,
            )
        finally:
            reset_gate_write_verifier(token)
            # Release the SQLite handles: durable mode closes the checkpointer +
            # the global-records connection together; otherwise just the ckpt.
            if durable is not None:
                durable.close()
            else:
                ckpt.close()

    final_state = final
    result = _result(
        request,
        thread_id,
        final_state,
        mode=mode,
        packs=pack_names,
        checkpoints=ckpt_names,
        paused_at=paused_at,
    )
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


def _result(
    request: str,
    thread_id: str,
    final: FactoryState,
    *,
    mode: str = MODE_ASSISTANT,
    packs: tuple[str, ...] = (),
    checkpoints: tuple[str, ...] = (),
    paused_at: str | None = None,
) -> RunResult:
    """Distill the terminal state into a :class:`RunResult` (no bodies, L4).

    The release action is mode-specific: Assistant/Gated emit an ``open_pr``
    (Gated's payload is the evidence-pack handle), Autonomous emits a ``deploy``
    (payload is the admitting decision handle). We surface whichever was emitted
    on the same ``open_pr_*`` fields so the summary reports the release uniformly.
    """
    statuses: dict[str, GateStatus] = {}
    for g in final.get("gates") or ():
        prev = statuses.get(g.name)
        # newest-by-name (the verdict that gates the release; mirrors emit guard)
        if prev is None:
            statuses[g.name] = g.status

    # The mode-specific release action: open_pr (assistant/gated) or deploy
    # (autonomous). At most one is emitted on a released run (fail-closed otherwise).
    release: ExternalAction | None = next(
        (a for a in (final.get("actions") or ()) if a.kind in ("open_pr", "deploy")),
        None,
    )
    # A run "passed" only if a release action was actually emitted (gates PASS AND
    # not halted by a checkpoint reject / pause). On a non-checkpoint PASS a
    # release is always emitted, so this is byte-identical to the prior
    # ``terminal_gates_pass`` semantics; a checkpoint reject (gates PASS but no
    # PR) or a paused run reports ``passed=False`` — the gates remain the trust
    # authority, the checkpoint only subtracts the release.
    passed = terminal_gates_pass(final) and release is not None
    return RunResult(
        request=request,
        thread_id=thread_id,
        mode=mode,
        packs=packs,
        passed=passed,
        phase=str(final.get("phase") or "unknown"),
        artifact_uri=release.payload_uri if release is not None else None,
        open_pr_idempotency_key=release.idempotency_key if release is not None else None,
        open_pr_payload_uri=release.payload_uri if release is not None else None,
        gate_statuses=statuses,
        checkpoints=checkpoints,
        paused_at=paused_at,
    )


# ---- the evolution route (task [12]) ----------------------------------------


def _read_overlay(overlay_dir: str) -> dict[str, str | None]:
    """Read an OVERLAY directory into a change set (relpath -> new content).

    The overlay's files are the added/modified files of the maintained change;
    they are laid over the prior tree by :func:`foundry.lifecycle.evolve.evolve`.
    Read as UTF-8 (errors replaced) — plain input data, never executed. (Deletes
    are expressed via the programmatic ``evolve(changes={rel: None})`` API; the
    shell affordance overlays added/modified files, the common evolution shape.)
    """
    root = Path(overlay_dir)
    if not root.is_dir():
        raise ValueError(f"overlay path is not a directory: {overlay_dir}")
    changes: dict[str, str | None] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            changes[p.relative_to(root).as_posix()] = p.read_text(
                encoding="utf-8", errors="replace"
            )
    if not changes:
        raise ValueError(f"overlay directory is empty: {overlay_dir}")
    return changes


def _evolve_main(args: list[str]) -> int:
    """``foundry evolve <prior repo> "<change>" --apply OVERLAY`` (task [12]).

    Re-enters an EXISTING released codebase as a MAINTAINED change: re-verify
    change-proportionally, re-gate via the SAME runner, re-admit fail-closed, link
    provenance to the prior version. The maintained change is RELEASED only when
    it RE-PASSES the gates + RE-ADMITS (a failing re-gate ⇒ NOT released — no PR /
    deploy). ``--apply OVERLAY`` supplies the changed files (overlaid on the prior
    tree); ``--guardian-approve`` records the HITL decision for a protected
    criterion/oracle/gate edit (which otherwise BLOCKS release, deterministic HITL).

    Returns 0 iff the maintained change was RELEASED, 1 if NOT released
    (fail-closed), 2 on a usage error.
    """
    from foundry.lifecycle.evolve import evolve

    guardian_approval = False
    if "--guardian-approve" in args:
        args = [a for a in args if a != "--guardian-approve"]
        guardian_approval = True
    overlay_dir: str | None = None
    if "--apply" in args:
        i = args.index("--apply")
        if i + 1 >= len(args):
            print("error: --apply requires a directory argument", file=sys.stderr)
            return 2
        overlay_dir = args[i + 1]
        args = args[:i] + args[i + 2 :]
    if len(args) < 2:
        print(
            'usage: python -m foundry.cli evolve <prior-repo-dir> "<change>" '
            "--apply OVERLAY_DIR [--guardian-approve]\n"
            "  re-enter an EXISTING released repo as a maintained change: re-verify\n"
            "  change-proportionally, re-gate (same runner), re-admit (fail-closed),\n"
            "  link provenance to the prior version. A failing re-gate => NOT released.\n"
            "  --apply OVERLAY_DIR: the changed/added files overlaid on the prior tree\n"
            "  --guardian-approve: record the HITL decision for a protected\n"
            "    criterion/oracle/gate edit (otherwise it BLOCKS release)",
            file=sys.stderr,
        )
        return 2
    prior_dir = args[0]
    change = " ".join(args[1:]).strip()
    if not change:
        print("error: change request must be non-empty", file=sys.stderr)
        return 2
    if overlay_dir is None:
        print("error: evolve requires --apply OVERLAY_DIR", file=sys.stderr)
        return 2
    if not Path(prior_dir).is_dir():
        print(f"error: prior repo not found: {prior_dir}", file=sys.stderr)
        return 2
    try:
        changes = _read_overlay(overlay_dir)
        result = evolve(
            prior_dir, change, changes=changes, guardian_approval=guardian_approval
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.summary())
    return 0 if result.released else 1


# ---- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m foundry.cli "build a ..."`` — run the smoke path, print a report.

    Returns process exit code 0 on a clean Spec/Build/Test PASS (a PR action was
    emitted), 1 on a halted/failed run, 2 on a usage error.
    """
    args = sys.argv[1:] if argv is None else argv
    # The evolution route (task [12]): `foundry evolve <prior repo> "<change>"`
    # re-enters an EXISTING released codebase as a maintained change (re-gate +
    # re-admit, fail-closed). Dispatched as a leading subcommand so the greenfield
    # path stays byte-identical (the default — no subcommand — is unchanged).
    if args and args[0] == "evolve":
        return _evolve_main(args[1:])
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
    # --mode {assistant|gated|autonomous} (default assistant; task [9]). The
    # default keeps the run byte-identical to the historical request-only path.
    mode = MODE_ASSISTANT
    if "--mode" in args:
        i = args.index("--mode")
        if i + 1 >= len(args):
            print("error: --mode requires a value", file=sys.stderr)
            return 2
        mode = args[i + 1]
        args = args[:i] + args[i + 2 :]
        if mode not in MODES:
            print(
                f"error: unknown --mode {mode!r}; choose one of {', '.join(MODES)}",
                file=sys.stderr,
            )
            return 2
    # --packs name1,name2 (task [9]): the domain packs to engage (by name). A pack
    # may VETO the requested mode (enforced fail-closed in run()).
    packs: list[str] = []
    if "--packs" in args:
        i = args.index("--packs")
        if i + 1 >= len(args):
            print("error: --packs requires a comma-separated list", file=sys.stderr)
            return 2
        packs = [p.strip() for p in args[i + 1].split(",") if p.strip()]
        args = args[:i] + args[i + 2 :]
    # --spec FILE|DIR (task [8]): may REPEAT to ingest several spec files/dirs.
    spec_paths: list[str] = []
    while "--spec" in args:
        i = args.index("--spec")
        if i + 1 >= len(args):
            print("error: --spec requires a file or directory argument", file=sys.stderr)
            return 2
        spec_paths.append(args[i + 1])
        args = args[:i] + args[i + 2 :]
    # --checkpoints [all|name1,name2] (plan [10]): pause at the stage boundaries
    # for human verification. ``--checkpoints`` alone (or ``all``) enables all
    # three; a comma list selects specific boundaries. From the shell the CLI uses
    # an APPROVE-ALL responder (a human partner would inspect + decide via the
    # ``run(checkpoint_responder=...)`` API — programmatic, no interactive stdin);
    # an approve still cannot turn a gate FAIL into a release (the gates decide).
    checkpoints: bool | list[str] | None = None
    if "--checkpoints" in args:
        i = args.index("--checkpoints")
        # An optional value follows ONLY if the next token isn't the request /
        # another flag (heuristic: contains no spaces and isn't the last arg or a
        # known sub-token). We accept "all" or a comma list; otherwise enable all.
        nxt = args[i + 1] if i + 1 < len(args) else None
        if nxt is not None and (nxt == "all" or ("," in nxt) or nxt in {
            b.value for b in CHECKPOINT_BOUNDARIES
        }):
            checkpoints = True if nxt == "all" else [s.strip() for s in nxt.split(",") if s.strip()]
            args = args[:i] + args[i + 2 :]
        else:
            checkpoints = True
            args = args[:i] + args[i + 1 :]
    if not args:
        print(
            "usage: python -m foundry.cli "
            "[--mode {assistant|gated|autonomous}] [--packs name1,name2] "
            "[--checkpoints [all|product,design,release]] "
            "[--docs DIR] [--out DIR] [--spec FILE|DIR ...] "
            '"<one-line request>"\n'
            "  modes (lifecycle.md §4 deployment-trust):\n"
            f"    assistant  {_TRUST_POSTURE[MODE_ASSISTANT]}\n"
            f"    gated      {_TRUST_POSTURE[MODE_GATED]}\n"
            f"    autonomous {_TRUST_POSTURE[MODE_AUTONOMOUS]}\n"
            "  packs: regulated | ml_data | frontend "
            "(a pack may VETO an unsafe mode — fail-closed)\n"
            "  checkpoints (plan [10]): pause at stage boundaries for human "
            "verification; an approve can NEVER turn a gate FAIL into a release",
            file=sys.stderr,
        )
        return 2
    request = " ".join(args).strip()
    if not request:
        print("error: request must be non-empty", file=sys.stderr)
        return 2

    # Resolve --spec paths to text HERE so a missing/empty path is reported as a
    # clean usage error (exit 2) and a deep ValueError from the run itself is not
    # masked. The resolved text is handed to run() (which seeds it onto `spec`).
    try:
        spec_text = _load_spec(spec_paths) if spec_paths else None
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # A shell --checkpoints run uses an APPROVE-ALL responder so it completes
    # without interactive stdin (a human partner inspects + decides via the
    # run(checkpoint_responder=...) API). The approve only proceeds to the gates;
    # it can never make a FAILing gate release.
    responder: CheckpointResponder | None = (
        (lambda _payload: "approve") if checkpoints else None
    )

    # An unknown pack name OR a pack VETO of the requested mode is a fail-closed
    # usage error (exit 2) — we never silently run a vetoed/empty configuration.
    try:
        result = run(
            request,
            mode=mode,
            packs=packs or None,
            spec=spec_text,
            docs_dir=docs_dir,
            output_dir=output_dir,
            checkpoints=checkpoints,
            checkpoint_responder=responder,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
