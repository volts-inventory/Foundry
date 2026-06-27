"""Claude Code adapter — the default substrate (`claude -p`, plan §3.4).

Runs the Claude Code CLI non-interactively inside the sandbox (worktree cwd,
scrubbed env, hard wall-clock kill). The CLI's own credential/network access
is mediated in production by the §2b proxy; here the sandbox passes at most
`FOUNDRY_PROXY_ENDPOINT` and nothing else.

Self-reported model: the CLI's JSON output names a model; we carry it in
`adapter_self_reported_model` but it is IGNORED for lineage — served family
comes from proxy observation only (arch §2b).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from foundry.execution.adapter_contract import (
    AdapterResult,
    AdapterUnavailableError,
    TaskPacket,
    assemble_prompt,
)
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq
from foundry.state.tree_sha import default_exclude

__all__ = ["ClaudeCodeAdapter"]

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_lenient(text: str) -> dict[str, Any]:
    """Best-effort parse of CLI stdout: strict JSON, else the outermost
    {...} span, else an empty dict (the raw text is still triaged)."""
    for candidate in (text, *(m.group(0) for m in [_JSON_OBJECT_RE.search(text)] if m)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


class ClaudeCodeAdapter:
    """`claude -p "<prompt>" --output-format json` inside the sandbox."""

    supports_turn_snapshots: bool = True
    name = "claude-code"

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        run_id: str = "dev",
        node_id: str = "claude-code",
        timeout_s: float | None = None,
        permission_mode: str | None = None,
    ) -> None:
        self._bin = claude_bin
        self._run_id = run_id
        self._node_id = node_id
        self._timeout_s = timeout_s
        # `claude -p` runs HEADLESS with no interactive approval, so file writes
        # are DENIED by default ("write permission wasn't granted") and the agent
        # produces NO code. The adapter runs in a throwaway sandbox worktree, so
        # auto-accepting edits there is safe + necessary. `acceptEdits` works as
        # root AND non-root; `bypassPermissions` is broader (Bash too) but the CLI
        # REFUSES it under root. Override via FOUNDRY_CLAUDE_PERMISSION_MODE.
        self._permission_mode = (
            permission_mode
            or os.environ.get("FOUNDRY_CLAUDE_PERMISSION_MODE", "").strip()
            or "acceptEdits"
        )

    def available(self) -> bool:
        """True iff the `claude` binary is on PATH here."""
        return shutil.which(self._bin) is not None

    def build_prompt(self, packet: TaskPacket) -> str:
        """Pinned constraints VERBATIM first, then task, then spans."""
        return assemble_prompt(
            packet,
            epilogue=(
                "Make the required changes in the current working directory. "
                "Do not push, publish, or touch anything outside this worktree. "
                # The Audit gate (autonomous/gated) grounds a dependency inventory
                # from a lockfile and fails CLOSED if none exists, so the build must
                # DECLARE its dependencies — even when there are none.
                "For a Python project, ALWAYS include a `requirements.txt` pinning "
                "every third-party dependency you use (create an EMPTY "
                "`requirements.txt` if the code uses only the standard library) so "
                "the dependency inventory is auditable."
            ),
        )

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        if not self.available():
            raise AdapterUnavailableError(f"{self._bin!r} binary not found on PATH")

        proc = sandbox.run(
            [
                self._bin, "-p", self.build_prompt(packet),
                "--output-format", "json",
                "--permission-mode", self._permission_mode,
            ],
            timeout_s=self._timeout_s,
        )
        parsed = _parse_json_lenient(proc.stdout)

        diff_text = self._capture_diff(sandbox)
        diff_uri = sandbox.put_blob(diff_text.encode("utf-8"))
        test_uris = self._capture_test_sources(sandbox)
        snapshot_digest = sandbox.snapshot()

        result_text = str(parsed.get("result") or proc.stdout or proc.stderr)[:2000]
        if proc.timed_out:
            result_text = f"[HARD-KILLED at wall-clock cap] {result_text}"
        run_against = f"task/{packet.task.get('id', 'unknown')}"
        triage = (
            GateTriage(
                name="adapter-report",
                run_against=run_against,
                explanation=result_text or "(no output)",
                produced_by=self.name,
                seq=Seq(self._run_id, 0, 0, self._node_id, 0),
            ),
        )

        turns = parsed.get("num_turns")
        return AdapterResult(
            diff_uri=diff_uri,
            test_source_uris=test_uris,
            triage=triage,
            adapter_self_reported_model=_as_optional_str(parsed.get("model")),
            turns_used=turns if isinstance(turns, int) and turns > 0 else 1,
            snapshot_digest=snapshot_digest,
        )

    # -- diff capture ----------------------------------------------------------

    def _capture_diff(self, sandbox: Sandbox) -> str:
        """`git diff` over the worktree, or a full-file snapshot manifest when
        the worktree is not a git repo (or git fails)."""
        if (sandbox.worktree / ".git").exists():
            # intent-to-add so newly created files show up in `git diff`
            sandbox.run(["git", "add", "-N", "."])
            proc = sandbox.run(["git", "diff"])
            if proc.ok:
                return proc.stdout
        return self._full_file_snapshot(sandbox.worktree)

    @staticmethod
    def _full_file_snapshot(worktree: Path) -> str:
        files: dict[str, str] = {}
        for path in sorted(worktree.rglob("*")):
            rel = path.relative_to(worktree).as_posix()
            if path.is_file() and not default_exclude(rel):
                files[rel] = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps(
            {"kind": "full-file-snapshot", "files": files}, sort_keys=True, ensure_ascii=False
        )

    def _capture_test_sources(self, sandbox: Sandbox) -> tuple[str, ...]:
        """Content-addressed handles for changed files that look like tests."""
        if not (sandbox.worktree / ".git").exists():
            return ()
        proc = sandbox.run(["git", "diff", "--name-only"])
        if not proc.ok:
            return ()
        uris: list[str] = []
        for rel in sorted(filter(None, proc.stdout.splitlines())):
            basename = rel.rsplit("/", 1)[-1]
            path = sandbox.worktree / rel
            if (basename.startswith("test_") or basename.endswith("_test.py")) and path.is_file():
                uris.append(sandbox.put_blob(path.read_bytes()))
        return tuple(uris)


def _as_optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
