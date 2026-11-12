"""The scoped adapter: a ticket's run cut back to its write scope and checked."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.agents._runtime import AgentRuntime
from foundry.agents.implementation.junior_engineer import _mirror_diff_blob
from foundry.agents.implementation.stubs import is_stub
from foundry.agents.implementation.tickets.constants import NODE_ID
from foundry.agents.implementation.tickets.contracts import (
    ContractDriftError,
    SignatureChangeError,
    _check_contract_names,
    _check_stub_signatures,
    _flag_contract_duplicates,
    _written_sources,
)
from foundry.agents.implementation.tickets.scope import trim_to_write_scope
from foundry.execution.adapter_contract import AdapterResult, TaskPacket
from foundry.execution.adapters.tiered import CHECK_SCORE_TRIAGE, session_stop_reason
from foundry.schemas.implementation import FeatureDiff
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from foundry.execution.sandbox import Sandbox
    from foundry.gates.self_check import CheckSpec


class _ScopedAdapter:
    """``rt.adapter`` + in-worker write-scope trim + diff-blob mirror into ``rt.cas``
    (both need the sandbox, which the pool destroys right after ``run``)."""

    def __init__(
        self,
        rt: AgentRuntime,
        scopes: Mapping[str, tuple[str, ...]],
        contracts: Mapping[str, str] | None = None,
        names: Mapping[str, tuple[Mapping[str, Sequence[str]], bool]] | None = None,
        stubs: Mapping[str, tuple[Mapping[str, str], bool]] | None = None,
        skeleton: Mapping[str, str] | None = None,
        checks: Mapping[str, CheckSpec] | None = None,
        shared: Mapping[str, frozenset[str]] | None = None,
        carried: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        assert rt.adapter is not None
        self._rt = rt
        self._scopes = scopes
        # Task id -> its session's gate check (read by the proxy-agent loop); held by
        # reference, so the node can add a ticket's before it submits it.
        self._checks = checks if checks is not None else {}
        self._contracts = dict(contracts or {})
        # Task id -> (planned contract -> the names it declares, fail the attempt?).
        self._names = dict(names or {})
        # Task id -> (its typed stub files: path -> stub text, fail the attempt?).
        self._stubs = dict(stubs or {})
        self._skeleton = dict(skeleton or {})
        # Task id -> the stubbed top-level names another ticket depends on.
        self._shared = dict(shared or {})
        # Task id -> the failed attempt's files its sandbox was seeded with.
        self._carried = dict(carried or {})
        # Task id -> the files a failed attempt wrote, for its retry to start from.
        self.carry: dict[str, dict[str, str]] = {}
        # Task id -> each carryable attempt this pass (an escalation re-runs one in
        # the same pass): its failing-line count at its last check, and its files.
        self.attempts: dict[str, list[tuple[int | None, dict[str, str]]]] = {}
        self.supports_turn_snapshots = rt.adapter.supports_turn_snapshots
        # Task id -> why its session counted as empty. The work pool records a
        # worker's exception only as text, so the type is kept here.
        self.empty: dict[str, str] = {}

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        tid = str(packet.task.get("id"))
        from foundry.gates.self_check import check_scope

        with check_scope(self._checks.get(tid)):
            result = self._rt.adapter.run(packet, sandbox)  # type: ignore[union-attr]
        if tid in self._carried:
            result = _with_carried(result, sandbox, self._carried[tid])
        files = self._scopes.get(tid, ())
        result = trim_to_write_scope(result, sandbox, files, tid)
        try:
            _require_scope_written(result, sandbox, files, tid, self._skeleton)
        except EmptyTicketError as exc:
            self.empty[tid] = str(exc)
            raise
        try:
            if tid in self._names:
                required, strict = self._names[tid]
                result = _check_contract_names(result, sandbox, files, tid, required, strict=strict)
            if tid in self._stubs:
                stubs, strict = self._stubs[tid]
                result = _check_stub_signatures(
                    result, sandbox, tid, stubs, strict=strict, shared=self._shared.get(tid)
                )
        except ContractDriftError as exc:
            # One named violation in otherwise kept work, or a session cut off mid-way:
            # the retry fixes it on these files instead of rewriting them from the stubs.
            if isinstance(exc, SignatureChangeError) or session_stop_reason(result.triage):
                written = _written_sources(result, sandbox, tid)
                if kept := {p: t for p, t in written.items() if isinstance(t, str)}:
                    self.carry[tid] = kept
                    self.attempts.setdefault(tid, []).append((check_score(result.triage), kept))
            raise
        result = _flag_contract_duplicates(result, sandbox, self._contracts, tid)
        _mirror_diff_blob(self._rt, sandbox, result)
        return result


def _with_carried(
    result: AdapterResult, sandbox: Sandbox, carried: Mapping[str, str]
) -> AdapterResult:
    """The session's diff plus every carried file it left untouched: the adapter
    diffs against the sandbox it was handed, which already held them, while the
    integrated tree does not."""
    try:
        body = json.loads(sandbox.get_blob(result.diff_uri))
    except ValueError:
        # A unified-diff adapter (stub, raw_react) writes diff text or "": there is no
        # snapshot to fold the carried files into.
        return result
    if not (isinstance(body, dict) and body.get("kind") == "full-file-snapshot"):
        return result
    files: dict[str, str] = dict(body.get("files") or {})
    gone = set(body.get("deleted") or ())
    worktree = Path(sandbox.worktree)
    for rel in carried:
        if rel not in files and rel not in gone and (worktree / rel).is_file():
            files[rel] = (worktree / rel).read_text(encoding="utf-8")
    payload = json.dumps({**body, "files": files}, sort_keys=True, ensure_ascii=False)
    return result.model_copy(update={"diff_uri": sandbox.put_blob(payload.encode("utf-8"))})


def check_score(triage: Sequence[object]) -> int | None:
    """A session's own failing-line count at its last gate check, else None (no
    check judged its final tree, or the adapter has none)."""
    for t in triage:
        if getattr(t, "name", None) == CHECK_SCORE_TRIAGE:
            try:
                return int(str(getattr(t, "explanation", "")))
            except ValueError:
                return None
    return None


def best_attempt(
    attempts: Sequence[tuple[int | None, str]],
) -> tuple[int | None, str] | None:
    """The attempt to carry, oldest first in ``attempts``: the fewest failing lines,
    an unscored one ranked last, the newer on a tie."""
    if not attempts:
        return None
    _, best = min(
        enumerate(attempts),
        key=lambda ia: (ia[1][0] if ia[1][0] is not None else float("inf"), -ia[0]),
    )
    return best


class EmptyTicketError(RuntimeError):
    """A ticket's session ended without any of its ``files`` in the tree."""


def _require_scope_written(
    result: AdapterResult,
    sandbox: Sandbox,
    files: Sequence[str],
    task_id: str,
    skeleton: Mapping[str, str] | None = None,
) -> None:
    """Fail a ticket whose diff touches none of its ``files`` while none of them
    exists yet: recording that empty diff as built would unblock its dependents on
    a tree without the work (a session that ran out of turns before writing). It
    becomes an ordinary failed attempt — retried, then skipped. A typed-skeleton
    file whose every body is still a stub counts as unwritten; an empty package
    marker the skeleton wrote is not the ticket's work and is not counted. An
    unrestricted scope or an unparseable body is left to integration."""
    from foundry.graph.gate_wiring import MaterializationError, _diff_ops

    if not files:
        return
    probe = FeatureDiff(
        diff_uri=result.diff_uri,
        task_id=task_id,
        branch=f"task/{task_id}",
        base_tree_sha="",
        seq=Seq("scope", 0, 0, NODE_ID, 0),
    )
    try:
        ops = _diff_ops(probe, sandbox.get_blob)
    except MaterializationError:
        return
    if set(ops.writes) | set(ops.deletes) | set(ops.patches):
        return
    worktree = Path(sandbox.worktree)
    skeleton = skeleton or {}
    counted = [f for f in files if skeleton.get(f) != ""]
    if not counted:
        return

    def written(f: str) -> bool:
        path = worktree / f.rstrip("/")
        if f not in skeleton or not path.is_file():
            return path.exists()
        try:
            return not is_stub(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return True

    if any(written(f) for f in counted):
        return
    report = "; ".join(t.explanation for t in result.triage)
    raise EmptyTicketError(
        f"the session wrote none of the ticket's files ({', '.join(files)}) "
        f"after {result.turns_used} turn(s)"
        + (f"; adapter report: {report[:800]}" if report else "")
    )
