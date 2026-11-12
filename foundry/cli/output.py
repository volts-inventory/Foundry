"""Writing the generated tree: the source tree on a passing build, and the
held DRAFT tree plus DRAFT-STATUS on a halt or stop."""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from foundry.cli import result
from foundry.graph.gate_wiring import GATE_INPUT_FILES
from foundry.schemas.gates import FixPass, GateResult, GateStatus

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from foundry.agents.implementation.senior_engineer import ArtifactResolver
    from foundry.graph.gate_wiring import BlobResolver
    from foundry.state.factory_state import FactoryState


# ---- final source-tree materialization (on a passing build) -----------------


def _write_output_tree(
    final_state: FactoryState,
    output_dir: str,
    *,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    best: FixPass | None = None,
) -> str:
    """Materialize the FINAL accepted integrated tree and write the source files.
    With ``best`` (a halted run's draft), write that fix pass's tree instead.

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
    ``materialize_workdir``) so a write can never escape ``output_dir``.
    """
    from foundry.graph.gate_wiring import materialize_best, materialize_workdir

    out_root = Path(output_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="foundry-cli-out-") as staging:
        stage_root = Path(staging)
        if best is not None:
            materialize_best(
                final_state, stage_root, best, resolve=resolve, resolve_blob=resolve_blob
            )
        else:
            materialize_workdir(final_state, stage_root, resolve=resolve, resolve_blob=resolve_blob)
        for src in sorted(stage_root.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(stage_root)
            if rel.name in GATE_INPUT_FILES and len(rel.parts) == 1:
                continue
            # Defense-in-depth: reject an
            # absolute or '..' relative path before writing under out_root.
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe source path {str(rel)!r}")
            target = (out_root / rel).resolve()
            if not target.is_relative_to(out_root):
                raise ValueError(f"source path escapes output dir: {str(rel)!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
    return str(out_root)


def _draft_best(final_state: FactoryState, resolve: ArtifactResolver) -> Any:
    from foundry.graph.gate_wiring import draft_best

    try:
        return draft_best(final_state, resolve)
    except Exception:
        return None


def _draft_status(
    final_state: FactoryState,
    *,
    gate_findings: Mapping[str, tuple[str, ...]],
    best: Any = None,
    stopped: str | None = None,
) -> str:
    """The ``DRAFT-STATUS.md`` text of a held run: the gate verdicts on ``best``
    (:func:`~foundry.graph.gate_wiring.draft_best`'s tree; None = the newest
    verdicts) and what's still failing."""
    gates = final_state.get("gates") or ()
    statuses = {g.name: g.status for g in gates}
    if best is not None:
        newest: dict[str, GateResult] = {}
        for g in gates:
            if g.run_against == best.tree and (g.name not in newest or g.seq > newest[g.name].seq):
                newest[g.name] = g
        statuses = {name: g.status for name, g in newest.items()}
        gate_findings = {name: tuple(g.findings) for name, g in newest.items()}
    lines = [
        "# DRAFT — held at the gate (NOT shipped)\n\n",
        "This build did not pass every gate, so it was **not admitted/released**. The\n",
        "source below is the best attempt — fix what's failing, then re-run.\n\n",
    ]
    if stopped:
        lines.append(f"The run stopped before its gates finished: {stopped}\n\n")
    lines.append("## Gate verdicts\n\n")
    for name, status in statuses.items():
        mark = "✅" if status is GateStatus.PASS else "❌"
        lines.append(f"- {mark} **{name}**: {str(status).split('.')[-1]}\n")
    failing = {
        name: gate_findings.get(name, ())
        for name, status in statuses.items()
        if status is not GateStatus.PASS
    }
    if any(failing.values()):
        lines.append("\n## What's still failing\n")
        for name, findings in failing.items():
            if findings:
                body = "\n".join(findings)
                lines.append(f"\n### {name}\n\n```\n{body}\n```\n")
    return "".join(lines)


def _write_draft_on_halt(
    final_state: FactoryState,
    output_dir: str,
    *,
    gate_findings: dict[str, tuple[str, ...]],
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
    stopped: str | None = None,
) -> tuple[str | None, str]:
    """Write a best-effort DRAFT on a HALTED run: the best tree of the fix loop the
    run ended in (:func:`~foundry.graph.gate_wiring.draft_best`), else the latest
    attempt's source tree, plus a ``DRAFT-STATUS.md`` naming what's still failing on
    it — so a held build hands back a usable starting point, never empty hands.
    Best-effort: a tree that can't even be materialized (e.g. an unparseable diff) is
    skipped (root ``None``), never crashes the run. Clearly marked NOT shipped —
    the gates rejected the tree. Returns the draft root and the status text."""
    best = _draft_best(final_state, resolve)
    draft_root: str | None = None
    try:
        draft_root = _write_output_tree(
            final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob, best=best
        )
    except Exception:
        if best is not None:
            best = None
            with contextlib.suppress(Exception):
                draft_root = _write_output_tree(
                    final_state, output_dir, resolve=resolve, resolve_blob=resolve_blob
                )
    text = _draft_status(final_state, gate_findings=gate_findings, best=best, stopped=stopped)
    if draft_root is not None:
        (Path(draft_root) / "DRAFT-STATUS.md").write_text(text, encoding="utf-8")
    return draft_root, text


def _has_draft_tree(state: FactoryState) -> bool:
    return any(a.kind == "feature_diff" for a in state.get("artifacts") or ())


def _latest_values(app: Any, config: RunnableConfig) -> FactoryState:
    """The newest checkpointed state that carries artifacts: the deepest in-flight
    stage subgraph's, since a stage holds its work until it returns."""
    snap = app.get_state(config, subgraphs=True)
    best = dict(snap.values or {})
    while True:
        inner = next((t.state for t in snap.tasks if hasattr(t.state, "values")), None)
        if inner is None:
            return cast("FactoryState", best)
        snap = inner
        if (snap.values or {}).get("artifacts"):
            best = dict(snap.values)


def _draft_on_stop(
    app: Any,
    config: RunnableConfig,
    exc: BaseException,
    *,
    request: str,
    thread_id: str,
    output_dir: str,
    resolve: ArtifactResolver,
    resolve_blob: BlobResolver,
) -> None:
    """Write the stopped run's latest tree as a DRAFT and note where on ``exc``.
    Best-effort: it runs while ``exc`` propagates and must never replace it."""
    try:
        state = _latest_values(app, config)
        if not _has_draft_tree(state):
            exc.add_note("no DRAFT written: the run stopped before any code was built")
            return
        draft, _ = _write_draft_on_halt(
            state,
            output_dir,
            gate_findings=result._result(request, thread_id, state).gate_findings,
            resolve=resolve,
            resolve_blob=resolve_blob,
            stopped=str(exc).splitlines()[0][:300],
        )
    except Exception as err:
        exc.add_note(f"no DRAFT written: {err!r}")
        return
    if draft is None:
        exc.add_note("no DRAFT written: neither the best nor the latest tree materialized")
    else:
        exc.add_note(f"DRAFT written to: {draft}  (held — see DRAFT-STATUS.md)")
