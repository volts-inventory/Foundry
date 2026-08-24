"""Per-build documentation set (foundry/docs) — renderers, writer, manifest, assembly.

Covers: deterministic markdown rendering from typed bodies; the Technical Writer
authoring the four missing doc types through the proxy (and degrading to a safe
empty doc on a transport failure); the grounded BuildManifest; and end-to-end
assembly + on-disk write over an in-memory ArtifactStore.
"""

from __future__ import annotations

from pathlib import Path

from foundry.agents._common import emit_artifact
from foundry.agents._runtime import AgentRuntime
from foundry.docs import assemble_documentation, write_bundle
from foundry.docs.manifest import DocRef, build_manifest, render_manifest_md
from foundry.docs.render import (
    render_known_issues,
    render_perf_cost,
    render_prd,
    render_runbook,
)
from foundry.docs.writers import TechnicalWriter
from foundry.durability.artifact_store import ArtifactStore
from foundry.proxy import ModelProviderProxy
from foundry.proxy.test_doubles import MockTransport
from foundry.schemas.architecture import SystemDesign
from foundry.schemas.docs import KnownIssue, KnownIssues, PerfCostReport, Runbook
from foundry.schemas.gates import GateResult, GateStatus, VerdictClass
from foundry.schemas.product import PRD, AcceptanceCriterion, Criticality
from foundry.state.seq import Seq

SEQ = Seq("r", 1, 1, "n", 0)


def _rt(transport: MockTransport | None = None) -> AgentRuntime:
    tx = transport or MockTransport(default_text="{}")
    return AgentRuntime(proxy=ModelProviderProxy(tx), cas=ArtifactStore())


# ---- pure renderers ---------------------------------------------------------


def test_render_prd_table_lists_criteria() -> None:
    prd = PRD(title="Shortener", prd_uri="cas://prd/x", criterion_ids=("AC-1",), seq=SEQ)
    criteria = (
        AcceptanceCriterion(
            id="AC-1", text="Given X When Y Then Z", criticality=Criticality.HIGH_RISK, seq=SEQ
        ),
    )
    doc = render_prd(prd, criteria)
    assert doc.path == "docs/PRD.md"
    assert "Shortener" in doc.body
    assert "AC-1" in doc.body and "high-risk" in doc.body


def test_render_runbook_sections() -> None:
    rb = Runbook(title="Svc", deploy_steps=("build", "ship"), rollback_steps=("revert",), seq=SEQ)
    body = render_runbook(rb).body
    assert "## Deploy" in body and "- build" in body
    assert "## Rollback" in body and "- revert" in body


def test_render_known_issues_table_and_limitations() -> None:
    ki = KnownIssues(
        title="Known",
        issues=(KnownIssue(summary="leak", impact="oom", severity="high"),),
        limitations=("single region",),
        seq=SEQ,
    )
    body = render_known_issues(ki).body
    assert "leak" in body and "high" in body
    assert "single region" in body


def test_render_perf_cost_uses_grounded_numbers() -> None:
    pc = PerfCostReport(
        title="Svc", total_tokens_in=1000, total_tokens_out=200, model_calls=5,
        estimated_cost_microusd=1_500_000, observations=("hot path: arch",), seq=SEQ,
    )
    body = render_perf_cost(pc).body
    assert "1,000" in body and "200" in body
    assert "$1.5000" in body  # 1_500_000 µUSD


# ---- Technical Writer -------------------------------------------------------


def test_writer_authors_runbook_from_proxy() -> None:
    payload = (
        '{"overview": "ops guide", "deploy_steps": ["a", "b"], '
        '"rollback_steps": ["c"], "health_checks": ["/healthz"]}'
    )
    tx = MockTransport.scripted({"OPERATIONAL RUNBOOK": payload})
    rb = TechnicalWriter(_rt(tx), run_id="r").runbook("Svc", {"title": "Svc"})
    assert rb.overview == "ops guide"
    assert rb.deploy_steps == ("a", "b")
    assert rb.rollback_steps == ("c",)


def test_writer_perf_cost_numbers_come_from_usage_not_model() -> None:
    # The model authors prose; the node injects the grounded numbers.
    tx = MockTransport.scripted({"PERFORMANCE & COST": '{"summary": "ok", "observations": ["x"]}'})
    writer = TechnicalWriter(_rt(tx), run_id="r")
    pc = writer.perf_cost(
        "Svc", {"title": "Svc"},
        usage={"total_tokens_in": 42, "total_tokens_out": 7, "model_calls": 3,
               "estimated_cost_microusd": 99},
    )
    assert pc.summary == "ok" and pc.observations == ("x",)
    assert pc.total_tokens_in == 42 and pc.estimated_cost_microusd == 99


def test_writer_degrades_to_empty_doc_on_transport_failure() -> None:
    class _Boom:
        def send(self, request: dict) -> object:
            raise RuntimeError("backend down")

    rt = AgentRuntime(proxy=ModelProviderProxy(_Boom()), cas=ArtifactStore())
    ki = TechnicalWriter(rt, run_id="r").known_issues("Svc", {"title": "Svc"})
    # Best-effort: no crash, an empty-but-valid doc body.
    assert isinstance(ki, KnownIssues) and ki.issues == () and ki.limitations == ()


# ---- manifest ---------------------------------------------------------------


def test_build_manifest_aggregates_usage_and_indexes() -> None:
    store = ArtifactStore()
    art = emit_artifact(
        PRD(title="S", prd_uri="cas://p", seq=SEQ), "prd", "product_manager", SEQ, store=store
    )
    proxy_manifest = [
        {"tier": "T1", "node_id": "product_manager",
         "served_model": "claude-opus-4-8", "served_family": ["anthropic-claude"],
         "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 50}},
        {"tier": "T2", "node_id": "reviewer_critic",
         "served_model": "claude-sonnet-4-6", "served_family": ["anthropic-claude"],
         "usage": {"input_tokens": 20, "output_tokens": 5}},
    ]
    m = build_manifest(
        request="build it", thread_id="r", generated_at="2026-01-01T00:00:00+00:00",
        passed=True, phase="done", artifacts=[art], gates=[],
        docs=[DocRef(path="README.md", title="R", sha256="a" * 64)],
        proxy_manifest=proxy_manifest, drift_events=0,
    )
    assert m.model_usage.model_calls == 2
    assert m.model_usage.total_tokens_in == 120
    assert m.model_usage.calls_by_tier == {"T1": 1, "T2": 1}
    assert m.model_usage.served_models == ("claude-opus-4-8", "claude-sonnet-4-6")
    # Per-node token breakdown (the "who's the hog" view), highest first.
    assert m.model_usage.tokens_by_node == {"product_manager": 110, "reviewer_critic": 25}
    assert next(iter(m.model_usage.tokens_by_node)) == "product_manager"  # sorted desc
    assert any(a.kind == "prd" for a in m.artifacts)
    md = render_manifest_md(m)
    assert "Build Manifest" in md and "claude-opus-4-8" in md
    assert "Tokens by node" in md and "product_manager" in md


def test_manifest_attested_counts_judgment_binding() -> None:
    """A bound judgment verdict (Review) carries ``judgment_binding`` (a proxy
    stamp), not ``runner_attestation`` (a zone-B deterministic signature). The
    manifest must report BOTH binding kinds as attested (task [3])."""
    det = GateResult(
        name="build",
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.EXECUTION_DETERMINISTIC,
        runner_attestation="zoneB-sig",
        run_against="tree-sha",
        seq=SEQ,
    )
    judged = GateResult(
        name="review",
        status=GateStatus.PASS,
        decision_deterministic=False,
        execution_deterministic=False,
        verdict_class=VerdictClass.PERCEPTUAL,
        judgment_binding="proxy-binding",  # bound, but NO runner_attestation
        run_against="tree-sha",
        seq=SEQ,
    )
    unbound = GateResult(
        name="audit",
        status=GateStatus.PASS,
        decision_deterministic=True,
        execution_deterministic=True,
        verdict_class=VerdictClass.CONFIG_EVALUABLE,
        run_against="tree-sha",
        seq=SEQ,  # neither binding -> not attested
    )
    m = build_manifest(
        request="x", thread_id="r", generated_at="t", passed=True, phase="done",
        artifacts=[], gates=[det, judged, unbound],
        docs=[DocRef(path="README.md", title="R", sha256="a" * 64)],
    )
    by_name = {g.name: g.attested for g in m.gates}
    assert by_name["build"] is True  # runner_attestation
    assert by_name["review"] is True  # judgment_binding (previously showed no)
    assert by_name["audit"] is False  # neither binding


# ---- end-to-end assembly ----------------------------------------------------


def _seed_state(store: ArtifactStore) -> dict:
    """A minimal terminal-state-shaped dict with real bodies in the store."""
    prd = emit_artifact(
        PRD(title="Shortener", prd_uri="cas://p", criterion_ids=("AC-1",), seq=SEQ),
        "prd", "product_manager", SEQ, store=store,
    )
    crit = emit_artifact(
        AcceptanceCriterion(id="AC-1", text="G/W/T", seq=SEQ),
        "acceptance_criterion", "product_manager", SEQ, store=store,
    )
    design = emit_artifact(
        SystemDesign(title="Shortener", design_uri="cas://d", components=("api",), seq=SEQ),
        "system_design", "principal_architect", SEQ, store=store,
    )
    return {"artifacts": [prd, crit, design], "gates": []}


def test_assemble_documentation_writes_full_tree(tmp_path: Path) -> None:
    tx = MockTransport.scripted(
        {"OPERATIONAL RUNBOOK": '{"overview": "run it"}'}, default_text="{}"
    )
    store = ArtifactStore()
    rt = AgentRuntime(proxy=ModelProviderProxy(tx), cas=store)
    state = _seed_state(store)

    bundle = assemble_documentation(
        request="build a shortener", thread_id="r", state=state, rt=rt,
        passed=True, phase="done", clock=lambda: "2026-01-01T00:00:00+00:00",
        write_to=tmp_path,
    )

    # Rendered upstream + authored docs + README + manifest present.
    paths = {d.path for d in bundle.docs}
    assert {"README.md", "docs/PRD.md", "docs/architecture/DESIGN.md", "docs/ops/RUNBOOK.md",
            "docs/KNOWN-ISSUES.md", "docs/UPGRADE.md", "docs/PERF-COST.md",
            "BUILD-MANIFEST.md", "BUILD-MANIFEST.json"} <= paths

    # Authored docs were emitted as content-addressed artifacts.
    assert {a.kind for a in bundle.doc_artifacts} == {
        "runbook", "known_issues", "upgrade_guide", "perf_cost_report"
    }

    # The manifest is grounded in the real run.
    assert bundle.manifest.passed is True
    assert bundle.manifest.generated_at == "2026-01-01T00:00:00+00:00"
    assert any(a.kind == "prd" for a in bundle.manifest.artifacts)

    # Files actually written to disk.
    assert (tmp_path / "README.md").is_file()
    assert "run it" in (tmp_path / "docs/ops/RUNBOOK.md").read_text()
    assert (tmp_path / "BUILD-MANIFEST.json").read_text().strip().startswith("{")


def test_write_bundle_is_idempotent(tmp_path: Path) -> None:
    store = ArtifactStore()
    rt = _rt()
    rt = AgentRuntime(proxy=rt.proxy, cas=store)
    state = _seed_state(store)
    bundle = assemble_documentation(
        request="x", thread_id="r", state=state, rt=rt, passed=False, phase="halted",
        clock=lambda: "t",
    )
    write_bundle(bundle, tmp_path)
    write_bundle(bundle, tmp_path)  # second write overwrites, no error
    assert (tmp_path / "README.md").read_text() == bundle.docs[0].body
