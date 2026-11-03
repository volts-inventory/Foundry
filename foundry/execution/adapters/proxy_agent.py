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

import hashlib
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass
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
    '  {"action": "read_file", "path": "<rel>", "start_line": 1, "end_line": 200}'
    "   # line range optional (1-based, inclusive); omit it to read the whole file\n"
    '  {"action": "search", "pattern": "<regex>", "path": "<optional rel dir>"}\n'
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
            "enum": ["write_file", "read_file", "delete_file", "run", "search", "done"],
        },
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
        "pattern": {"type": "string"},
        "content": {"type": "string"},
        "cmd": {"type": "string"},
        "summary": {"type": "string"},
        "triage": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action"],
    "additionalProperties": False,
}

_OBS_CAP = 4_000  # chars of run/search output fed back per turn (context discipline)
# Larger than _OBS_CAP so a typical source file fits in one read: a small read cap
# is what drove the model to re-read one file in many slices.
_READ_CAP = 12_000
_OBS_KEEP = 3  # observations kept verbatim after an elision pass (arXiv 2508.21433)
_OBS_MAX = 6  # verbatim observations allowed to pile up before the next pass
_REPEAT_WARN = 3
_REPEAT_STOP = 5
# Reads of one unchanged file (any line ranges) before a nudge / a stop.
_THRASH_WARN = 4
_THRASH_STOP = 6
# Chars of unchanged-file reads kept verbatim past masking; over it, the oldest
# are elided down to half, so the cap doesn't move the cache prefix every turn.
_PIN_CAP = 30_000
_SEARCH_MAX_HITS = 50
_SEARCH_MAX_FILE = 1_000_000
# Lines this long are skipped: the model's regex runs in-process, and catastrophic
# backtracking cost grows with line length.
_SEARCH_MAX_LINE = 2_000
_SEARCH_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
)
# Dirs whose churn is a by-product of re-running the same command (test/lint
# caches, bytecode) — a change there must not reset the repeat detector.
_TREE_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}
)


@dataclass(frozen=True)
class _Read:
    path: str  # worktree-relative posix
    lo: int
    hi: int  # last line actually shown
    stamp: tuple[int, int]  # (size, mtime_ns) when read


@dataclass(frozen=True)
class _Kept:
    idx: int  # message index
    what: str
    size: int
    read: _Read | None


def _contained(worktree: Path, rel: str) -> Path:
    """Fail-closed containment for model-named paths (same posture as the
    integration boundary, D-R1-03)."""
    pure = PurePosixPath(rel)
    root = worktree.resolve()
    if not rel or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"path {rel!r} escapes the worktree")
    target = (root / pure).resolve()
    if not target.is_relative_to(root):  # not a string prefix: /wt2 is not under /wt
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
        verbatim: list[_Kept] = []  # still-verbatim observations
        # action fingerprint -> (consecutive-unchanged count, tree state after it ran)
        repeats: dict[tuple[str, ...], tuple[int, str]] = {}
        # path -> (reads since it last changed, its stamp at the last read)
        reads: dict[str, tuple[int, tuple[int, int]]] = {}
        tree = _tree_state(worktree)
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
            fp = _fingerprint(act)
            count, tree_then = repeats.get(fp, (0, tree))
            # Any tree change since the last identical action (a write, sed -i, a
            # formatter, pip install) makes re-running it legitimate again.
            count = count + 1 if tree_then == tree else 1
            if count >= _REPEAT_STOP:
                triage_lines.append(
                    f"stopped early: identical action repeated {_REPEAT_STOP} times "
                    f"({_summarize(act)}) — partial work recorded"
                )
                break
            key = _file_key(worktree, str(act.get("path", ""))) if action == "read_file" else None
            if key is not None:
                rel, stamp = key
                n, stamp_then = reads.get(rel, (0, stamp))
                n = n + 1 if stamp_then == stamp else 1
                if n >= _THRASH_STOP:
                    triage_lines.append(
                        f"stopped early: read {rel} {_THRASH_STOP} times without changing "
                        "it — partial work recorded"
                    )
                    break
                reads[rel] = (n, stamp)
            read: _Read | None = None
            try:
                if action == "read_file":
                    observation, read = _read_file(worktree, str(act.get("path", "")), act)
                else:
                    observation = self._execute(act, sandbox, worktree)
            except (ValueError, OSError) as exc:
                observation = f"ERROR: {exc}"
            tree = _tree_state(worktree)
            repeats[fp] = (count, tree)
            if count >= _REPEAT_WARN:
                observation = (
                    f"WARNING: you have issued this exact action {count} times in a row "
                    "with no change to the worktree in between. Try something different "
                    "or declare done.\n" + observation
                )
            if key is not None and reads[key[0]][0] >= _THRASH_WARN:
                observation = (
                    f"NOTE: you have read {key[0]} {reads[key[0]][0]} times without changing "
                    "it — its content is above; make the edit or finish.\n" + observation
                )
            content = f"OBSERVATION:\n{observation}"
            messages.append({"role": "user", "content": content})
            verbatim.append(_Kept(len(messages) - 1, _summarize(act), len(content), read))
            pinned = _pinned(verbatim, worktree)
            pinned_size = sum(k.size for k in verbatim if k.idx in pinned)
            if len(verbatim) - len(pinned) > _OBS_MAX or pinned_size > _PIN_CAP:
                # Elide in batches, not one per turn: every rewrite invalidates the
                # prompt-prefix cache from that message on, so batching moves the
                # prefix once every 4 turns (3 kept + 4 new).
                # A placeholder is never rewritten again.
                keep = {k.idx for k in verbatim[-_OBS_KEEP:]}
                budget = _PIN_CAP if pinned_size <= _PIN_CAP else _PIN_CAP // 2
                budget -= sum(k.size for k in verbatim[-_OBS_KEEP:] if k.idx in pinned)
                for k in reversed(verbatim):
                    if k.idx not in pinned or k.idx in keep:
                        continue
                    if k.size > budget:
                        break  # oldest go first: no older read slips into the gap
                    keep.add(k.idx)
                    budget -= k.size
                for k in verbatim:
                    if k.idx not in keep:
                        messages[k.idx] = {
                            "role": "user",
                            "content": f"OBSERVATION: [output of {k.what} elided — re-run or "
                            "re-read if needed]",
                        }
                verbatim = [k for k in verbatim if k.idx in keep]
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
        notes += [
            f"non-UTF-8 (binary) file skipped from snapshot slice: {p}" for p in skipped_binary
        ]
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
            content = str(act.get("content", ""))
            target.write_text(content, encoding="utf-8")
            obs = f"wrote {act.get('path')} ({len(content)} chars)"
            if target.suffix == ".py":
                obs += _lint_python(content, str(act.get("path")))
            return obs
        if action == "delete_file":
            target = _contained(worktree, str(act.get("path", "")))
            if target.is_file():
                target.unlink()
                return f"deleted {act.get('path')}"
            return f"{act.get('path')} did not exist"
        if action == "search":
            return _search(worktree, str(act.get("pattern", "")), str(act.get("path") or ""))
        if action == "run":
            cmd = str(act.get("cmd", "")).strip()
            if not cmd:
                return "ERROR: empty cmd"
            result = sandbox.run(cmd, timeout_s=self._run_timeout_s)
            out = result.stdout + "\n" + result.stderr
            tail = out[-_OBS_CAP:]
            if result.timed_out:
                status = "TIMED OUT (hard-killed)"
            elif result.returncode != 0:
                status = f"FAILED: exit {result.returncode}"
            else:
                status = "exit 0"
            if len(out) > _OBS_CAP:
                status += f" (output truncated; last {_OBS_CAP} chars shown)"
            return f"{status}\n{tail}"
        return f"ERROR: unknown action {action!r}"


def _summarize(act: dict[str, Any]) -> str:
    action = str(act.get("action"))
    if action == "run":
        return f"run {str(act.get('cmd', ''))[:80]!r}"
    if action == "search":
        return f"search {str(act.get('pattern', ''))[:80]!r} in {act.get('path') or '.'}"
    if action == "read_file" and (act.get("start_line") or act.get("end_line")):
        lo, hi = act.get("start_line") or 1, act.get("end_line") or "end"
        return f"read_file {act.get('path')} lines {lo}-{hi}"
    return f"{action} {act.get('path')}"


def _fingerprint(act: dict[str, Any]) -> tuple[str, ...]:
    content = hashlib.sha256(str(act.get("content", "")).encode("utf-8")).hexdigest()
    keys = ("action", "path", "cmd", "pattern", "start_line", "end_line")
    return (*(str(act.get(k)) for k in keys), content)


def _file_key(worktree: Path, rel: str) -> tuple[str, tuple[int, int]] | None:
    """(worktree-relative path, (size, mtime_ns)) of a regular file, else None —
    the same cheap stamp _tree_state uses."""
    try:
        target = _contained(worktree, rel)
        st = target.stat()
    except (ValueError, OSError):
        return None
    if not target.is_file():
        return None
    return target.relative_to(worktree.resolve()).as_posix(), (st.st_size, st.st_mtime_ns)


def _pinned(kept: list[_Kept], worktree: Path) -> set[int]:
    """Message indexes of reads still showing a file's current content and not
    covered by a newer read of the same file — masking keeps these verbatim."""
    pinned: set[int] = set()
    stamps: dict[str, tuple[int, int] | None] = {}
    covered: dict[str, list[tuple[int, int]]] = {}
    for k in reversed(kept):
        r = k.read
        if r is None:
            continue
        if r.path not in stamps:
            key = _file_key(worktree, r.path)
            stamps[r.path] = key[1] if key else None
        if stamps[r.path] != r.stamp:
            continue
        ranges = covered.setdefault(r.path, [])
        if any(lo <= r.lo and r.hi <= hi for lo, hi in ranges):
            continue
        ranges.append((r.lo, r.hi))
        pinned.add(k.idx)
    return pinned


def _tree_state(worktree: Path) -> str:
    """Cheap worktree fingerprint: (path, size, mtime) of every file, no reads."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(worktree):
        dirnames[:] = sorted(d for d in dirnames if d not in _TREE_SKIP_DIRS)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            h.update(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _lint_python(content: str, rel: str) -> str:
    """Best-effort compile check; never raises (the host Python may not match the
    target interpreter, so it is reported as a likely error for the model to judge)."""
    host = f"{sys.version_info.major}.{sys.version_info.minor}"
    note = f"(host Python {host} compile check; the project's Python may differ)"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(content, rel, "exec")
    except SyntaxError as exc:
        return f"\nLIKELY SYNTAX ERROR line {exc.lineno}: {exc.msg} {note}"
    except Exception as exc:
        return f"\nLIKELY SYNTAX ERROR: compile failed with {type(exc).__name__}: {exc} {note}"
    return ""


def _read_file(worktree: Path, rel: str, act: dict[str, Any]) -> tuple[str, _Read | None]:
    target = _contained(worktree, rel)
    if not target.is_file():
        return f"{rel} does not exist", None
    key = _file_key(worktree, rel)
    text = target.read_text(encoding="utf-8", errors="replace")
    obs, lo, hi = _read_range(rel, text, act.get("start_line"), act.get("end_line"))
    if key is None or hi < lo:
        return obs, None
    return obs, _Read(key[0], lo, hi, key[1])


def _read_range(rel: str, text: str, start_line: Any, end_line: Any) -> tuple[str, int, int]:
    """(observation, first line shown, last line shown)."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = min(total, int(end_line or total))
    if start > total:
        return f"{rel} has only {total} lines", start, start - 1
    out: list[str] = []
    size = 0
    last = start - 1
    for n in range(start, end + 1):
        line = lines[n - 1]
        if size + len(line) > _READ_CAP:
            if not out:  # a single over-long line: show what fits
                out.append(line[:_READ_CAP])
                last = n
            break
        out.append(line)
        size += len(line)
        last = n
    header = f"{rel} (lines {start}-{last} of {total})"
    if last < end:
        rest = f"start_line={last + 1}" + (f", end_line={end}" if end < total else "")
        header += (
            f" — truncated at line {last} of {total} (a read shows up to {_READ_CAP} chars); "
            f"get the rest in ONE call: read_file with {rest} — don't re-read in smaller slices"
        )
    return header + "\n" + "".join(out), start, last


def _search(worktree: Path, pattern: str, rel: str) -> str:
    """Pure-Python grep over the worktree (no shell); skips VCS/env/build dirs,
    binaries, files over 1 MB, and over-long lines."""
    if not pattern:
        return "ERROR: empty pattern"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: bad regex: {exc}"
    root = worktree.resolve()
    base = _contained(worktree, rel) if rel else root
    if not base.is_dir():
        return f"{rel} is not a directory"
    hits: list[str] = []
    size = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            try:
                if not path.resolve().is_relative_to(root):
                    continue  # symlink pointing out of the cage
                if path.stat().st_size > _SEARCH_MAX_FILE:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            shown = path.relative_to(root).as_posix()
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if len(line) <= _SEARCH_MAX_LINE and rx.search(line):
                    hit = f"{shown}:{n}: {line.strip()[:200]}"
                    size += len(hit) + 1
                    if len(hits) >= _SEARCH_MAX_HITS or size > _OBS_CAP:
                        return "\n".join(hits) + f"\n[results capped at {len(hits)} matches]"
                    hits.append(hit)
    return "\n".join(hits) if hits else f"no matches for {pattern!r}"


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
