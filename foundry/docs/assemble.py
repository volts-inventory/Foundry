"""Assemble a finished build's documentation set — the company-shaped deliverable.

One entry point, :func:`assemble_documentation`, takes a terminal
:class:`FactoryState` plus the :class:`AgentRuntime` that ran it and produces a
:class:`DocumentationBundle`:

1. resolve the upstream typed bodies from the content-addressed store and render
   them (PRD, requirements, design + ADRs + interfaces + ownership, test plan /
   report / e2e);
2. author the docs no upstream stage produces — runbook, known-issues, upgrade
   guide, perf/cost report — via the Technical Writer (through the proxy, so the
   backend is config-swappable), and emit them as content-addressed artifacts;
3. build the :class:`BuildManifest` (grounded in state + the proxy manifest);
4. render a top-level README index and the BUILD-MANIFEST (json + md).

Optionally write the whole tree to disk. The flow is unchanged whether the
backend is the Anthropic SDK, ``claude -p``, ``codex`` or Ollama-Gemma — the
writer's model calls go through the same proxy seam.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, MutableSequence, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents._common import emit_artifact
from foundry.docs import render
from foundry.docs.manifest import BuildManifest, DocRef, build_manifest, render_manifest_md
from foundry.docs.render import RenderedDoc
from foundry.docs.writers import TechnicalWriter
from foundry.schemas.architecture import ADR, InterfaceContract, OwnershipMap, SystemDesign
from foundry.schemas.base import Artifact
from foundry.schemas.docs import ProseBody
from foundry.schemas.product import PRD, AcceptanceCriterion
from foundry.schemas.quality import E2EReport, TestPlan, TestReport

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents._runtime import AgentRuntime
    from foundry.schemas.base import FrozenModel
    from foundry.state.factory_state import FactoryState

__all__ = [
    "DocumentationBundle",
    "assemble_documentation",
    "make_documentation_node",
    "write_bundle",
]


@dataclass(frozen=True, slots=True)
class DocumentationBundle:
    """Everything produced for a build's documentation."""

    docs: tuple[RenderedDoc, ...]  # every rendered file (incl. README + manifest)
    manifest: BuildManifest
    doc_artifacts: tuple[Artifact, ...]  # the authored-doc handles (for the artifacts channel)


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _resolve_bodies(
    artifacts: tuple[Artifact, ...], rt: AgentRuntime
) -> list[FrozenModel]:
    """Reconstitute the typed bodies behind the handles, tolerantly.

    Uses the run's content-addressed store (``rt.cas``); a handle whose body is
    not stored (e.g. a synthetic test handle) is skipped rather than failing the
    whole doc set — documentation is best-effort."""
    if rt.cas is None:
        return []
    bodies: list[FrozenModel] = []
    for handle in artifacts:
        try:
            bodies.append(rt.cas.get_body(handle.uri))
        except Exception:
            continue
    return bodies


def _resolve_prose(uri: str, rt: AgentRuntime) -> str | None:
    """Resolve a narrative ``ProseBody`` behind a ``*_uri`` handle, tolerantly.

    The PM/architect persist the PRD/design/ADR prose as a ``ProseBody`` keyed on
    its ``cas_uri(text)`` handle (task [2]); this reconstitutes the text so the
    renderers can inline it. Returns ``None`` when there is no store, the handle
    was never persisted (a synthetic / pre-task-[2] run), or the body is not a
    ``ProseBody`` — the renderer then falls back to the dead-handle note."""
    if rt.cas is None or not uri:
        return None
    try:
        body = rt.cas.get_body(uri)
    except Exception:
        return None
    return body.text if isinstance(body, ProseBody) else None


def _first(bodies: list[FrozenModel], kind: type) -> Any:
    return next((b for b in bodies if isinstance(b, kind)), None)


def _all(bodies: list[FrozenModel], kind: type) -> tuple[Any, ...]:
    return tuple(b for b in bodies if isinstance(b, kind))


def _gate_statuses(state: FactoryState) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for g in state.get("gates") or ():
        statuses.setdefault(g.name, g.status.value)
    return statuses


def _budget_spend(state: FactoryState) -> int:
    budget = state.get("budget")
    spend = getattr(budget, "spend_microusd", 0)
    return int(spend) if isinstance(spend, int) and not isinstance(spend, bool) else 0


def assemble_documentation(
    *,
    request: str,
    thread_id: str,
    state: FactoryState,
    rt: AgentRuntime,
    passed: bool,
    phase: str,
    tree_sha: str = "",
    clock: Callable[[], str] | None = None,
    write_to: str | Path | None = None,
) -> DocumentationBundle:
    """Render + author the full per-build documentation set and manifest."""
    artifacts = tuple(state.get("artifacts") or ())
    bodies = _resolve_bodies(artifacts, rt)

    prd = _first(bodies, PRD)
    criteria: tuple[AcceptanceCriterion, ...] = _all(bodies, AcceptanceCriterion)
    design = _first(bodies, SystemDesign)
    adrs = _all(bodies, ADR)
    interfaces = _all(bodies, InterfaceContract)
    ownership = _first(bodies, OwnershipMap)
    test_plan = _first(bodies, TestPlan)
    test_report = _first(bodies, TestReport)
    e2e = _first(bodies, E2EReport)

    title = (
        (prd.title if prd else "")
        or (design.title if design else "")
        or request
        or "Service"
    )

    # ---- render upstream artifacts (inline resolved prose where persisted) ----
    rendered: list[RenderedDoc] = []
    if prd:
        rendered.append(render.render_prd(prd, criteria, prose=_resolve_prose(prd.prd_uri, rt)))
    if criteria:
        rendered.append(render.render_acceptance(criteria))
    if design:
        rendered.append(
            render.render_design(design, prose=_resolve_prose(design.design_uri, rt))
        )
    if adrs:
        adr_prose = {
            a.id: text
            for a in adrs
            if (text := _resolve_prose(a.body_uri, rt)) is not None
        }
        rendered.append(render.render_adrs(adrs, prose=adr_prose))
    if interfaces:
        rendered.append(render.render_interfaces(interfaces))
    if ownership:
        rendered.append(render.render_ownership(ownership))
    if test_plan:
        rendered.append(render.render_test_plan(test_plan))
    if test_report:
        rendered.append(render.render_test_report(test_report))
    if e2e:
        rendered.append(render.render_e2e_report(e2e))

    # ---- author the missing doc types (via the proxy) ----
    writer = TechnicalWriter(rt, run_id=thread_id)
    context: dict[str, Any] = {
        "title": title,
        "request": request,
        "components": list(design.components) if design else [],
        "criteria": [{"id": c.id, "text": c.text} for c in criteria],
        "gate_statuses": _gate_statuses(state),
        "phase": phase,
        "passed": passed,
    }
    usage = _proxy_usage(rt)
    runbook = writer.runbook(title, context)
    known_issues = writer.known_issues(title, context)
    upgrade = writer.upgrade_guide(title, context)
    perf_cost = writer.perf_cost(
        title, context, usage={**usage, "estimated_cost_microusd": _budget_spend(state)}
    )
    rendered += [
        render.render_runbook(runbook),
        render.render_known_issues(known_issues),
        render.render_upgrade(upgrade),
        render.render_perf_cost(perf_cost),
    ]

    doc_artifacts = tuple(
        emit_artifact(body, kind, "tech_writer", body.seq, store=rt.cas)
        for body, kind in (
            (runbook, "runbook"),
            (known_issues, "known_issues"),
            (upgrade, "upgrade_guide"),
            (perf_cost, "perf_cost_report"),
        )
    )

    # ---- real project README (repo root) ----
    readme = _render_readme(
        title, request, rendered,
        prd_prose=_resolve_prose(prd.prd_uri, rt) if prd else None,
        criteria=criteria,
        components=list(design.components) if design else [],
    )
    rendered.insert(0, readme)

    # ---- manifest (over content + authored docs, incl. README) ----
    generated_at = (clock or _now)()
    doc_refs = tuple(DocRef(path=d.path, title=d.title, sha256=_sha(d.body)) for d in rendered)
    manifest = build_manifest(
        request=request,
        thread_id=thread_id,
        generated_at=generated_at,
        passed=passed,
        phase=phase,
        tree_sha=tree_sha,
        artifacts=artifacts + doc_artifacts,
        gates=tuple(state.get("gates") or ()),
        docs=doc_refs,
        proxy_manifest=rt.proxy.manifest,
        drift_events=len(rt.proxy.drift_events),
        estimated_cost_microusd=_budget_spend(state),
    )
    manifest_md = RenderedDoc("BUILD-MANIFEST.md", "Build Manifest", render_manifest_md(manifest))
    manifest_json = RenderedDoc(
        "BUILD-MANIFEST.json", "Build Manifest (JSON)", manifest.model_dump_json(indent=2) + "\n"
    )
    all_docs = (*rendered, manifest_md, manifest_json)

    bundle = DocumentationBundle(docs=all_docs, manifest=manifest, doc_artifacts=doc_artifacts)
    if write_to is not None:
        write_bundle(bundle, write_to)
    return bundle


def make_documentation_node(
    rt: AgentRuntime,
    *,
    sink: MutableSequence[DocumentationBundle] | None = None,
) -> Callable[[FactoryState, RunnableConfig], dict[str, Any]]:
    """Build the TERMINAL documentation node for the factory graph (task [3]).

    The node runs :func:`assemble_documentation` against the IN-GRAPH terminal
    state + ``rt``, so the Technical Writer's model calls go through the SAME
    proxy/budget seam as every other node (budget-metered, observed on the proxy
    manifest) and the authored doc artifacts are returned on the ``artifacts``
    channel — checkpointed + outbox-governed like every other artifact. This
    replaces the old out-of-graph ``assemble_documentation`` call the CLI made
    AFTER ``app.invoke`` (whose writer spend escaped the ledger and whose doc
    handles never entered the checkpointed channel).

    BEST-EFFORT (governance: a docs failure must NEVER fail the build / block the
    PR — the build's correctness is decided by the gates, not by whether the
    writer reached the model). Any exception during assembly is swallowed with a
    warning, and the node returns an empty delta — the emit guard / gate verdicts
    are untouched. Mirrors the fail-safe guard the CLI historically wrapped this
    call in.

    ``sink`` (optional, in-process) receives the assembled
    :class:`DocumentationBundle` so an in-process caller (the CLI) can write the
    ALREADY-assembled tree to disk without re-authoring (no second round of model
    spend). It is a plain in-memory holder — NOT a state channel — so it never
    rides the checkpoint; the durable record of the authored docs is the doc
    artifacts emitted onto the ``artifacts`` channel.
    """

    def _node(state: FactoryState, config: RunnableConfig) -> dict[str, Any]:
        # Lazy import to avoid a graph<->factory_graph import cycle at module top.
        from foundry.graph.factory_graph import terminal_gates_pass

        try:
            bundle = assemble_documentation(
                request=str(state.get("request") or ""),
                thread_id=str(config["configurable"]["thread_id"]),
                state=state,
                rt=rt,
                passed=terminal_gates_pass(state),
                phase=str(state.get("phase") or "unknown"),
            )
        except Exception as exc:
            print(f"warning: documentation assembly failed: {exc}", file=sys.stderr)
            return {}
        if sink is not None:
            sink.append(bundle)
        # The authored doc handles ride the artifacts channel (checkpointed); they
        # fold through merge_by_uri alongside the upstream artifacts.
        return {"artifacts": list(bundle.doc_artifacts)}

    return _node


def _proxy_usage(rt: AgentRuntime) -> dict[str, int]:
    tin = tout = 0
    manifest = rt.proxy.manifest
    for rec in manifest:
        u = rec.get("usage") or {}
        tin += int(u.get("input_tokens", 0))
        tout += int(u.get("output_tokens", 0))
    return {"total_tokens_in": tin, "total_tokens_out": tout, "model_calls": len(manifest)}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _render_readme(
    title: str,
    request: str,
    docs: list[RenderedDoc],
    *,
    prd_prose: str | None,
    criteria: Sequence[Any],
    components: Sequence[str],
) -> RenderedDoc:
    """A real, GitHub-ready project README at the repo root: what it is, how to
    run + test it, its features, and links to the full docs/ set."""
    # A concise description: the PRD's first paragraph, else the request.
    desc = ""
    if prd_prose:
        desc = prd_prose.strip().split("\n\n", 1)[0].strip()
    desc = desc or f"{title}. Built from the request: {request}"

    lines = [f"# {title}", "", desc, ""]

    feats = [
        c.text for c in criteria
        if isinstance(getattr(c, "text", None), str) and not getattr(c, "security_abuse", False)
    ]
    if feats:
        lines += ["## Features", ""]
        lines += [f"- {t}" for t in feats[:8]]
        lines.append("")

    if components:
        lines += ["## Components", "", ", ".join(map(str, components)), ""]

    lines += [
        "## Getting started",
        "",
        "```sh",
        "# Install dependencies (if the project declares any):",
        "pip install -r requirements.txt",
        "",
        "# Run the tests:",
        "pytest",
        "```",
        "",
    ]

    doc_links = [f"- [{d.title}]({d.path})" for d in docs if d.path.startswith("docs/")]
    if doc_links:
        lines += ["## Documentation", "", "Full design + ops docs under [`docs/`](docs/):", ""]
        lines += doc_links
        lines += ["- [Build Manifest](BUILD-MANIFEST.md)", ""]

    lines.append(f"_Generated by Foundry from the request: {request!r}._")
    return RenderedDoc("README.md", title, "\n".join(lines) + "\n")


def write_bundle(bundle: DocumentationBundle, root: str | Path) -> Path:
    """Write every rendered doc under ``root`` (created if absent). Returns root."""
    root_path = Path(root).resolve()
    for doc in bundle.docs:
        rel = Path(doc.path)
        # Doc paths are fixed repo-relative literals; defend against an absolute
        # or ``..`` path ever escaping the requested root.
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe document path {doc.path!r}")
        target = (root_path / rel).resolve()
        if not target.is_relative_to(root_path):
            raise ValueError(f"document path escapes root: {doc.path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(doc.body, encoding="utf-8")
    return root_path
