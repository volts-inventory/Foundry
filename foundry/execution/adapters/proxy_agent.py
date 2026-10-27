"""Proxy-mediated agentic adapter — the trust-preserving inner loop. (ROADMAP 1c)

The ``claude-code`` adapter gets its agency from the Claude Code CLI, which
moves the credential locus into the sandbox (the documented trade). THIS
adapter provides a multi-turn agentic inner loop with the ORIGINAL trust story
intact: every model call routes through the §2b mediating
:class:`~foundry.proxy.model_provider_proxy.ModelProviderProxy` — the proxy
stays the sole credential holder and the lineage observer — and works over ANY
configured backend (Anthropic API, OpenAI, the CLIs-as-completion).

The loop is a schema-constrained ReAct cycle: each turn the model returns ONE
action as JSON — ``write_file`` / ``delete_file`` / ``read_file`` / ``run`` /
``done`` — which the adapter executes against the :class:`Sandbox` (writes
stay inside the cage; ``run`` rides ``Sandbox.run``'s scrubbed env + hard-kill)
and feeds the observation back. Bounded by ``packet.turn_budget``; the result
is the same authored-tree ``full-file-snapshot`` contract the claude-code
adapter emits, so integration is identical.

The adapter holds NO credentials and declares NO sandbox grant — the scrubbed
cage is unchanged. ``complete_fn(messages, schema) -> (text, parsed)`` is the
injected proxy seam (a closure over ``proxy.complete`` in production; a fake in
tests), mirroring raw_react's ``complete_fn`` discipline.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
    assemble_prompt,
    packet_tier_scope,
)
from foundry.execution.adapters.claude_code import changed_files, walk_worktree
from foundry.execution.sandbox import Sandbox
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    # (messages, schema) -> (raw_text, parsed_json_or_None). In production a
    # closure over ModelProviderProxy.complete (the only credential holder).
    AgentCompleteFn = Callable[
        [Sequence[dict[str, Any]], dict[str, Any]], tuple[str, dict[str, Any] | None]
    ]

__all__ = ["ACTION_SCHEMA", "ProxyAgentAdapter"]

_EPILOGUE = (
    "You are an agentic engineer working in a disposable sandbox worktree. Each "
    "turn, reply with EXACTLY ONE action as JSON conforming to the given schema:\n"
    '  {"action": "write_file", "path": "<rel>", "content": "<full file>"}\n'
    '  {"action": "read_file", "path": "<rel>"}\n'
    '  {"action": "delete_file", "path": "<rel>"}\n'
    '  {"action": "run", "cmd": "<shell command>"}   # build/test inside the sandbox\n'
    '  {"action": "done", "summary": "<what you built>", "triage": ["<risk>", ...]}\n'
    "Write real, complete code (no placeholders). Run the build/tests and fix "
    "failures before declaring done."
)

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["write_file", "read_file", "delete_file", "run", "done"],
        },
        "path": {"type": "string"},
        "content": {"type": "string"},
        "cmd": {"type": "string"},
        "summary": {"type": "string"},
        "triage": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action"],
    "additionalProperties": False,
}

_OBS_CAP = 4_000  # chars of run/read output fed back per turn (context discipline)


def _contained(worktree: Path, rel: str) -> Path:
    """Fail-closed containment for model-named paths (same posture as the
    integration boundary, D-R1-03)."""
    pure = PurePosixPath(rel)
    root = worktree.resolve()
    if not rel or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"path {rel!r} escapes the worktree")
    target = (root / pure).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"path {rel!r} escapes the worktree")
    return target


class ProxyAgentAdapter:
    """Schema-constrained agentic loop over the mediating proxy."""

    supports_turn_snapshots: bool = True
    name = "proxy-agent"
    # No sandbox credential grant: the proxy outside the cage is the only egress.
    credential_grant: tuple[str, ...] = ()
    home_passthrough: bool = False

    def __init__(
        self,
        complete_fn: AgentCompleteFn,
        *,
        run_id: str = "dev",
        node_id: str = "proxy-agent",
        run_timeout_s: float = 300.0,
        self_reported_model: str | None = None,
    ) -> None:
        self._complete = complete_fn
        self._run_id = run_id
        self._node_id = node_id
        self._run_timeout_s = run_timeout_s
        self._self_reported_model = self_reported_model

    def available(self) -> bool:
        return True  # nothing but the injected proxy seam is required

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        with packet_tier_scope(packet):  # the completion closure picks the packet's model tier
            return self._run(packet, sandbox)

    def _run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        worktree = Path(sandbox.worktree)
        before = walk_worktree(worktree)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": assemble_prompt(packet, epilogue=_EPILOGUE)}
        ]
        summary = ""
        triage_lines: list[str] = []
        turns = 0
        for _ in range(max(1, packet.turn_budget)):
            turns += 1
            text, parsed = self._complete(messages, ACTION_SCHEMA)
            act = parsed if isinstance(parsed, dict) else _lenient_action(text)
            messages.append({"role": "assistant", "content": text})
            if act is None:
                messages.append(
                    {"role": "user", "content": "Reply with ONE valid action JSON only."}
                )
                continue
            action = act.get("action")
            if action == "done":
                summary = str(act.get("summary", ""))
                triage_lines = [str(t) for t in act.get("triage", []) if str(t).strip()]
                break
            try:
                observation = self._execute(act, sandbox, worktree)
            except (ValueError, OSError) as exc:
                observation = f"ERROR: {exc}"
            messages.append({"role": "user", "content": f"OBSERVATION:\n{observation}"})
        else:
            triage_lines.append("turn budget exhausted before 'done' — partial work recorded")

        after = walk_worktree(worktree)
        authored, deleted, skipped_binary = changed_files(before, after)
        payload: dict[str, Any] = {"kind": "full-file-snapshot", "files": authored}
        if deleted:
            payload["deleted"] = deleted
        diff_uri = sandbox.put_blob(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )

        notes = triage_lines or [summary.strip()[:500] or "run completed with no summary"]
        notes += [f"non-UTF-8 (binary) file skipped from snapshot slice: {p}" for p in skipped_binary]
        run_against = f"task/{packet.task.get('id', 'unknown')}"
        triage = tuple(
            GateTriage(
                name="adapter-report",
                run_against=run_against,
                explanation=note,
                produced_by=self.name,
                seq=Seq(self._run_id, 0, 0, self._node_id, i),
            )
            for i, note in enumerate(notes)
        )
        return AdapterResult(
            diff_uri=diff_uri,
            triage=triage,
            adapter_self_reported_model=self._self_reported_model,
            turns_used=turns,
            snapshot_digest=sandbox.snapshot(),
        )

    def _execute(self, act: dict[str, Any], sandbox: Sandbox, worktree: Path) -> str:
        action = act.get("action")
        if action == "write_file":
            target = _contained(worktree, str(act.get("path", "")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(act.get("content", "")), encoding="utf-8")
            return f"wrote {act.get('path')} ({len(str(act.get('content', '')))} chars)"
        if action == "delete_file":
            target = _contained(worktree, str(act.get("path", "")))
            if target.is_file():
                target.unlink()
                return f"deleted {act.get('path')}"
            return f"{act.get('path')} did not exist"
        if action == "read_file":
            target = _contained(worktree, str(act.get("path", "")))
            if not target.is_file():
                return f"{act.get('path')} does not exist"
            return target.read_text(encoding="utf-8", errors="replace")[:_OBS_CAP]
        if action == "run":
            cmd = str(act.get("cmd", "")).strip()
            if not cmd:
                return "ERROR: empty cmd"
            result = sandbox.run(cmd, timeout_s=self._run_timeout_s)
            tail = (result.stdout + "\n" + result.stderr)[-_OBS_CAP:]
            status = "TIMED OUT (hard-killed)" if result.timed_out else f"exit {result.returncode}"
            return f"{status}\n{tail}"
        return f"ERROR: unknown action {action!r}"


def _lenient_action(text: str) -> dict[str, Any] | None:
    """Outermost ``{...}`` span parse for backends without a grammar channel."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return None
    return None
