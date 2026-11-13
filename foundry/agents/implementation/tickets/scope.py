"""Write scope: what a ticket (or the fixer) may write, and cutting a diff back to it."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.agents.implementation.junior_engineer import FIXER_NODE_ID
from foundry.agents.implementation.tickets.constants import (
    CONTRACT_CHANGE_TRIAGE,
    FIX_SCOPE_TRIAGE,
    NODE_ID,
    SCOPE_TRIAGE,
)
from foundry.execution.adapter_contract import AdapterResult
from foundry.schemas.gates import GateTriage
from foundry.schemas.implementation import FeatureDiff
from foundry.state.seq import Seq
from foundry.state.tree_sha import is_cache_path

if TYPE_CHECKING:
    from foundry.execution.sandbox import Sandbox
    from foundry.graph.gate_wiring import BlobResolver, _DiffOps


def _is_gate_input(path: str) -> bool:
    """Criteria and every ``.feature`` file: the gate writes features/<ticket>.feature
    and executes only those, so an agent's own feature file anywhere (a copy under
    tests/features/) would only be a scenario binding the gate never runs."""
    from foundry.graph.gate_wiring import GATE_INPUT_FILES

    return path in GATE_INPUT_FILES or path.endswith(".feature")


# A manifest's lockfiles: written by the package manager whenever the manifest's
# owner installs, so they belong to that ticket's scope (dropping one leaves a
# lockfile-strict install like `npm ci` nothing to install from).
_LOCKFILES: dict[str, tuple[str, ...]] = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"),
    "Cargo.toml": ("Cargo.lock",),
    "go.mod": ("go.sum",),
    "pyproject.toml": ("uv.lock", "poetry.lock"),
}


def in_write_scope(path: str, files: Sequence[str]) -> bool:
    """May a ticket with write scope ``files`` write ``path``? Gate inputs (criteria,
    feature files) and tool caches never; an entry ending in ``/`` admits its
    subtree, a package manifest its lockfiles; an empty scope admits everything else
    (the ticket declared none)."""
    if _is_gate_input(path) or is_cache_path(path):
        return False
    if not files:
        return True
    return any(
        path == f or (f.endswith("/") and path.startswith(f)) or _is_lockfile_of(path, f)
        for f in files
    )


def _is_lockfile_of(path: str, manifest: str) -> bool:
    m_dir, _, m_name = manifest.rpartition("/")
    p_dir, _, p_name = path.rpartition("/")
    return m_dir == p_dir and p_name in _LOCKFILES.get(m_name, ())


def _result_ops(result: AdapterResult, get_blob: BlobResolver, task_id: str) -> _DiffOps | None:
    """The adapter diff's ops, or None for an unparseable body (integration fails it)."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops

    probe = FeatureDiff(
        diff_uri=result.diff_uri,
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="",
        seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        return _diff_ops(probe, get_blob)
    except MaterializationError:
        return None


def note_fix_writes(
    result: AdapterResult,
    sandbox: Sandbox,
    task_id: str,
    *,
    focus: Sequence[str],
    contracts: Sequence[str],
) -> AdapterResult:
    """Triage the fixer's kept writes: one note for the paths outside ``focus`` (the
    built tickets' files; empty = no focus, nothing is outside it) and one per
    contract file it changed. Notes only — the writes stay in the diff, all but
    tool caches (``.hypothesis/``, ``__pycache__/``...), which are cut from it."""
    ops = _result_ops(result, sandbox.get_blob, task_id)
    if ops is None:
        return result
    touched = {p for p in (*ops.writes, *ops.deletes, *ops.patches) if not is_cache_path(p)}
    if len(touched) < len(set(ops.writes) | set(ops.deletes) | set(ops.patches)):
        result, _ = _trimmed(result, sandbox, ops, lambda p: not is_cache_path(p))
    outside = sorted(p for p in touched if focus and not in_write_scope(p, focus))
    notes: list[GateTriage] = []
    if outside:
        notes.append(
            GateTriage(
                name=FIX_SCOPE_TRIAGE,
                run_against=f"task/{task_id}",
                explanation="the fixer wrote outside the built tickets' files (kept): "
                + ", ".join(outside),
                produced_by=FIXER_NODE_ID,
                seq=Seq("scope", 0, 0, FIXER_NODE_ID, len(result.triage)),
            )
        )
    for path in sorted(touched & set(contracts)):
        how = "deleted" if path in ops.deletes else "changed"
        notes.append(
            GateTriage(
                name=f"{CONTRACT_CHANGE_TRIAGE}:{path}",
                run_against=f"task/{task_id}",
                explanation=f"the fixer {how} the contract file {path}; this version is now "
                "the agreed one every later ticket builds against",
                produced_by=FIXER_NODE_ID,
                seq=Seq("scope", 0, 0, FIXER_NODE_ID, len(result.triage) + len(notes)),
            )
        )
    if not notes:
        return result
    return result.model_copy(update={"triage": (*result.triage, *notes)})


def trim_to_write_scope(
    result: AdapterResult, sandbox: Sandbox, files: Sequence[str], task_id: str
) -> AdapterResult:
    """Cut the adapter's diff back to ``files``; record the dropped paths as triage.

    Called in-worker while the sandbox is alive. An unparseable body is returned
    as-is (integration fails it closed). Modification hunks are applied against the
    sandbox's base file so the trimmed body is a self-contained full-file snapshot."""
    ops = _result_ops(result, sandbox.get_blob, task_id)
    if ops is None:
        return result
    touched = set(ops.writes) | set(ops.deletes) | set(ops.patches)
    if all(in_write_scope(p, files) for p in touched):
        return result
    result, failed = _trimmed(result, sandbox, ops, lambda p: in_write_scope(p, files))
    # Tool caches are cut without a note: no session authored them.
    dropped = sorted(
        {p for p in touched if not in_write_scope(p, files) and not is_cache_path(p)} | failed
    )
    if not dropped:
        return result
    note = GateTriage(
        name=SCOPE_TRIAGE,
        run_against=f"task/{task_id}",
        explanation="dropped writes outside the ticket's files: " + ", ".join(dropped),
        produced_by=NODE_ID,
        seq=Seq("scope", 0, 0, NODE_ID, len(result.triage)),
    )
    return result.model_copy(update={"triage": (*result.triage, note)})


def _trimmed(
    result: AdapterResult, sandbox: Sandbox, ops: _DiffOps, keep: Callable[[str], bool]
) -> tuple[AdapterResult, set[str]]:
    """The result with its diff cut to the paths ``keep`` admits, as a full-file
    snapshot (modification hunks applied against the sandbox's base file), and the
    kept patched paths whose hunks did not apply (dropped too)."""
    from foundry.graph.gate_wiring import MaterializationError, _apply_hunks

    kept: dict[str, str] = {p: c for p, c in ops.writes.items() if keep(p)}
    failed: set[str] = set()
    worktree = Path(sandbox.worktree)
    for rel, hunks in ops.patches.items():
        if not keep(rel):
            continue
        base = worktree / rel
        original = base.read_text(encoding="utf-8") if base.is_file() else ""
        try:
            kept[rel] = _apply_hunks(original, hunks, diff_uri=result.diff_uri, rel=rel)
        except MaterializationError:
            failed.add(rel)
    payload: dict[str, Any] = {"kind": "full-file-snapshot", "files": kept}
    deleted = [p for p in ops.deletes if keep(p)]
    if deleted:
        payload["deleted"] = deleted
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return result.model_copy(update={"diff_uri": sandbox.put_blob(blob)}), failed
