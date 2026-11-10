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

Opt-in native tool calls (``tool_complete_fn``, wired by ``run(native_tools=)``,
else FOUNDRY_NATIVE_TOOLS): the same actions are offered as tools (:data:`TOOLS`), one model turn
may request several, and each runs through exactly the guards a JSON action
does. A backend without a tool channel raises ``ToolsUnsupported`` and the
adapter drops to JSON actions for that packet tier (its transport + model) from
then on; other tiers keep their tools.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import warnings
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from foundry import progress
from foundry.execution.adapter_contract import (
    AdapterResult,
    TaskPacket,
    assemble_prompt,
    current_packet_tier,
    packet_tier_scope,
)
from foundry.execution.adapters.claude_code import (
    TreeTooLargeError,
    changed_files,
    walk_worktree,
)
from foundry.execution.adapters.tiered import DIGEST_CAP, DIGEST_TRIAGE, STOPPED_DIGEST
from foundry.execution.sandbox import Sandbox
from foundry.proxy.model_provider_proxy import ToolsUnsupported
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from foundry.gates.self_check import SessionCheck

    # (messages, schema) -> (raw_text, parsed_json_or_None). In production a
    # closure over ModelProviderProxy.complete (the only credential holder).
    AgentCompleteFn = Callable[
        [Sequence[dict[str, Any]], dict[str, Any]], tuple[str, dict[str, Any] | None]
    ]
    # (messages, tools) -> (text, tool_calls); each call {"id", "name", "input"}
    # (+ "error"). Raises ToolsUnsupported when the backend has no tool channel.
    AgentToolCompleteFn = Callable[
        [Sequence[dict[str, Any]], Sequence[dict[str, Any]]],
        tuple[str, Sequence[dict[str, Any]]],
    ]

__all__ = ["ACTION_SCHEMA", "TOOLS", "ProxyAgentAdapter"]

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

# A session that can run the gates (``check``) offers it as one more action.
_CHECK_SCHEMA: dict[str, Any] = {
    **ACTION_SCHEMA,
    "properties": {
        **ACTION_SCHEMA["properties"],
        "action": {
            "type": "string",
            "enum": [*ACTION_SCHEMA["properties"]["action"]["enum"], "check"],
        },
    },
}
_CHECK_LINE = (
    '  {"action": "check"}   # run the gates on a gate-shaped copy of this tree '
    "(build + your scenarios)\n"
)
_CHECK_CLOSE = (
    "Write real, complete code (no placeholders). Call check after your edits "
    "and fix what it reports as yours: done is refused while it is red."
)
_CHECK_EPILOGUE = (
    _EPILOGUE[: _EPILOGUE.index('  {"action": "done"')]
    + _CHECK_LINE
    + _EPILOGUE[_EPILOGUE.index('  {"action": "done"') : _EPILOGUE.index("Write real")]
    + _CHECK_CLOSE
)


def _with_reason(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with the optional ``reason`` a fenced fixer's write outside its
    target carries (``fix_files``)."""
    return {**schema, "properties": {**schema["properties"], "reason": {"type": "string"}}}


_FENCED_SCHEMAS = {id(s): _with_reason(s) for s in (ACTION_SCHEMA, _CHECK_SCHEMA)}

_MAX_CALLS_PER_TURN = 8

_NATIVE_EPILOGUE = (
    "You are an agentic engineer working in a disposable sandbox worktree. Work by "
    "calling the provided tools: read_file, search, write_file (the full file), "
    "delete_file, run (build/test inside the sandbox), and done when finished. You "
    f"may call several tools in one turn (at most {_MAX_CALLS_PER_TURN}); they run "
    "in order. Write real, complete code (no placeholders). Run the build/tests and "
    "fix failures before calling done."
)
_NATIVE_CHECK_EPILOGUE = (
    "You are an agentic engineer working in a disposable sandbox worktree. Work by "
    "calling the provided tools: read_file, search, write_file (the full file), "
    "delete_file, run (a command inside the sandbox), check (the gates themselves on "
    "this tree), and done when finished. You "
    f"may call several tools in one turn (at most {_MAX_CALLS_PER_TURN}); they run "
    f"in order. {_CHECK_CLOSE}"
)
_NUDGE = "Call a tool to make progress, or call done when the task is finished."


def _tool(name: str, description: str, required: tuple[str, ...], **props: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": props, "required": list(required)},
    }


_STR = {"type": "string"}
_LINE = {"type": "integer", "minimum": 1}
# Anthropic tool shape; the OpenAI-compatible transports convert it.
TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "read_file",
        "Read a worktree file; start_line/end_line (1-based, inclusive) "
        "are optional: omit them to read the whole file.",
        ("path",),
        path=_STR,
        start_line=_LINE,
        end_line=_LINE,
    ),
    _tool(
        "search",
        "Regex search over the worktree, optionally under a relative dir.",
        ("pattern",),
        pattern=_STR,
        path=_STR,
    ),
    _tool(
        "write_file",
        "Write the FULL content of a worktree file.",
        ("path", "content"),
        path=_STR,
        content=_STR,
    ),
    _tool("delete_file", "Delete a worktree file.", ("path",), path=_STR),
    _tool("run", "Run a shell command (build/tests) inside the sandbox.", ("cmd",), cmd=_STR),
    _tool(
        "done",
        "Declare the task finished.",
        ("summary",),
        summary=_STR,
        triage={"type": "array", "items": _STR},
    ),
)
_CHECK_TOOLS: tuple[dict[str, Any], ...] = (
    *TOOLS[:-1],
    _tool(
        "check",
        "Run the gates (the build and your acceptance scenarios) on a copy of "
        "this tree shaped as the gate sees it: offline, only declared dependencies. "
        "Reports failures that are yours and ones that were failing before you started.",
        (),
    ),
    TOOLS[-1],
)
_REASON = {
    "type": "string",
    "description": "Only for a file outside `fix_files`: why this failure needs it changed.",
}


def _reason_tools(tools: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """``tools`` with write_file/delete_file taking the optional fence ``reason``."""
    return tuple(
        {
            **t,
            "input_schema": {
                **t["input_schema"],
                "properties": {**t["input_schema"]["properties"], "reason": _REASON},
            },
        }
        if t["name"] in ("write_file", "delete_file")
        else t
        for t in tools
    )


_FENCED_TOOLS = {id(t): _reason_tools(t) for t in (TOOLS, _CHECK_TOOLS)}
# Refusals of a red done per session: a failure the session cannot fix by editing
# is reported, not looped on.
_CHECK_REFUSALS = 2
# The adapter-report note of a session whose checks ran out while its own work was
# still red (tiered.session_stop_reason counts it like the turn cap).
# (id of the packet, its opened check) from :meth:`ProxyAgentAdapter.prepared`.
_OPENED: ContextVar[tuple[int, tuple[SessionCheck | None, str | None]] | None] = ContextVar(
    "foundry_opened_check", default=None
)
CHECK_EXHAUSTED = "check budget exhausted with the gate still red"
# The note of a session whose final tree no check judged.
CHECK_UNVERIFIED = "unverified"
# The note of a session whose done was accepted only because its refusals ran out.
CHECK_OVERRIDDEN = "done accepted with the gate still red"
# The note of a session whose done came on its last turn, red, with no turn left to refuse it.
CHECK_RED_LAST_TURN = "done on the last turn with the gate red"
_DIGEST_PATHS = 12
_DIGEST_ACTIONS = 5
_DIGEST_LINES = 4

# The bdd command writes its report to $FOUNDRY_BDD_REPORT; the gate sets it, the
# agent's sandbox does not, so a runner there wrote to "" and the agent never saw
# that the gate would find no report.
_BDD_REPORT_ENV = "FOUNDRY_BDD_REPORT"
_BDD_CMD = re.compile(r"cucumber|godog|\bgo\s+test\b[^|;&]*\bbdd\b", re.IGNORECASE)
_OBS_CAP = 4_000  # chars of run/search output fed back per turn (context discipline)
# Larger than _OBS_CAP so a typical source file fits in one read: a small read cap
# is what drove the model to re-read one file in many slices.
_READ_CAP = 12_000
_OBS_KEEP = 3  # observations kept verbatim after an elision pass (arXiv 2508.21433)
_OBS_MAX = 6  # verbatim observations allowed to pile up before the next pass
_REPEAT_WARN = 3
_REPEAT_STOP = 5
# Reads of one unchanged file before a nudge / a stop. Only a read overlapping
# lines still verbatim in context counts: paging forward, or re-reading what
# masking elided, is legitimate.
_THRASH_WARN = 4
_THRASH_STOP = 6
# Chars of unchanged-file reads kept verbatim past masking, not counting the
# always-kept tail; over it, the oldest are elided down to half, so the cap
# moves the cache prefix once per several reads rather than every turn.
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
# The opening lists the worktree's files: without it a session spends turns on
# find/ls and on reads of files that do not exist.
_LIST_MAX = 200
# With this many turns left the observation says so: a session that never learns
# its budget is cut off mid-change, before it re-runs the checks on its edits.
_LOW_TURNS = 5
_ACTIVITY: dict[str, tuple[str, str | None]] = {
    "write_file": ("writing", "path"),
    "read_file": ("reading", "path"),
    "delete_file": ("deleting", "path"),
    "run": ("running", "cmd"),
    "search": ("searching for", "pattern"),
    "check": ("checking the gates", None),
    "done": ("done:", "summary"),
}
_ACTIVITY_MAX = 72


@dataclass(frozen=True)
class _Read:
    path: str
    lo: int
    hi: int  # last line actually shown
    stamp: tuple[int, int]


@dataclass(frozen=True)
class _Kept:
    idx: int  # message index
    what: str
    size: int
    read: _Read | None
    block: int = -1  # tool_result index within the message; -1 = the whole message

    @property
    def key(self) -> tuple[int, int]:
        return (self.idx, self.block)


@dataclass
class _Loop:
    worktree: Path
    tree: str
    # action fingerprint -> (consecutive-unchanged count, tree state after it ran)
    repeats: dict[tuple[str, ...], tuple[int, str]] = field(default_factory=dict)
    # path -> (thrash count since it last changed, its stamp at the last read)
    reads: dict[str, tuple[int, tuple[int, int]]] = field(default_factory=dict)
    verbatim: list[_Kept] = field(default_factory=list)
    triage: list[str] = field(default_factory=list)
    summary: str = ""
    stopped: bool = False
    budget: int = 0
    left: int = 0  # turns left after the current one
    # The ticket's write scope (packet.task["files"]); None = the packet names none.
    files: tuple[str, ...] | None = None
    # A repair session's starting worktree (walk_worktree), for the no-change done
    # check; None = the packet carries no failures to fix.
    start: dict[str, bytes] | None = None
    done_refused: bool = False
    # Distributions a `run` pip-installed into this sandbox, for the done check.
    installed: list[str] = field(default_factory=list)
    install_refused: bool = False
    # The session's gate check (foundry.gates.self_check); None = no check here.
    checker: SessionCheck | None = None
    origin: dict[str, bytes] = field(default_factory=dict)  # the starting worktree
    check_refusals: int = 0
    done: bool = False
    actions: list[str] = field(default_factory=list)  # _summarize of each action, in order
    last_check: str = ""  # the newest check/run outcome, for the stopped-early digest
    # The per-write mypy's extra flags; None = no type check on write.
    write_check: tuple[str, ...] | None = None
    # A fixer pass's write fence (packet.task["fix_files"]); None = unfenced.
    fence: tuple[str, ...] | None = None
    # One note per file a fenced fixer changed outside its target, with its reason.
    outside: list[str] = field(default_factory=list)


def _contained(worktree: Path, rel: str) -> Path:
    """Fail-closed containment for model-named paths (same posture as the
    integration boundary, D-R1-03)."""
    if not rel:
        # A local model sometimes drops "path" from a full-file write: say so, not
        # "escapes the worktree", so the next turn resends it with the path.
        raise ValueError(
            'no "path" given — nothing done; repeat the action with "path" '
            "set to the worktree-relative file"
        )
    pure = PurePosixPath(rel)
    root = worktree.resolve()
    if pure.is_absolute() or ".." in pure.parts:
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
        tool_complete_fn: AgentToolCompleteFn | None = None,
        activity_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._complete = complete_fn
        self._activity_cb = activity_cb
        self._tool_complete = tool_complete_fn
        # Packet tiers whose backend has no tool channel. Only ever added to, and
        # read once per session: a concurrent session's fallback never changes
        # the mode of one already running.
        self._json_tiers: set[str] = set()
        self._run_id = run_id
        self._node_id = node_id
        self._run_timeout_s = run_timeout_s
        self._self_reported_model = self_reported_model

    def available(self) -> bool:
        return True  # nothing but the injected proxy seam is required

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        with packet_tier_scope(packet):  # the completion closure picks the packet's model tier
            return self._run(packet, sandbox)

    @contextlib.contextmanager
    def prepared(self, packet: TaskPacket) -> Iterator[None]:
        """Run ``packet``'s opening gate check now, for the :meth:`run` inside: the
        tiered adapter enters this before it takes the local model's slot, so the
        model is not held idle while the gates run."""
        token = _OPENED.set((id(packet), _open_check(packet)))
        try:
            yield
        finally:
            _OPENED.reset(token)

    def _run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        worktree = Path(sandbox.worktree)
        before = walk_worktree(worktree)
        tier = current_packet_tier() or ""
        tool_fn = None if tier in self._json_tiers else self._tool_complete
        native = tool_fn is not None
        listing = _listing(before)
        opened = _OPENED.get()
        checker, checked = (
            opened[1] if opened is not None and opened[0] == id(packet) else _open_check(packet)
        )
        if checker is not None and checker.spec.rules:
            packet = _with_rules(packet, checker.spec.rules)
        messages = _opening(packet, native=native, listing=listing, check=checked)
        files = packet.task.get("files")
        fence = packet.task.get("fix_files")
        st = _Loop(
            worktree,
            _tree_state(worktree),
            files=tuple(str(f) for f in files) if isinstance(files, list) else None,
            budget=max(1, packet.turn_budget),
            start=before if packet.prior_failures else None,
            checker=checker,
            origin=before,
            write_check=_write_check_flags(),
            fence=tuple(str(f) for f in fence) if isinstance(fence, list) and fence else None,
        )
        turns = 0
        while turns < st.budget and not st.stopped:
            turns += 1  # one per model call, however many tool calls it makes
            st.left = st.budget - turns
            if not native:
                self._json_turn(messages, st, sandbox)
                continue
            assert tool_fn is not None
            try:
                self._native_turn(tool_fn, messages, st, sandbox)
            except ToolsUnsupported as exc:
                # This tier on JSON actions from now on; the backend never ran a turn.
                progress.log(
                    f"proxy-agent: native tool calls unavailable on tier "
                    f"{tier or '-'} ({exc}); using JSON actions"
                )
                self._json_tiers.add(tier)
                native = False
                messages = _opening(packet, native=False, listing=listing, check=checked)
                st.verbatim = []
                turns -= 1
        if not st.stopped:
            st.triage.append("turn budget exhausted before 'done' — partial work recorded")
        after = walk_worktree(worktree)
        authored, deleted, skipped_binary = changed_files(before, after)
        if (c := st.checker) is not None:
            last = c.cached(authored, deleted)
            if last is not None and last.red and c.left <= 0:
                # Red on the final tree itself: an edit after the last check is unjudged.
                st.triage.append(
                    f"{CHECK_EXHAUSTED} — partial work recorded; its last "
                    "check: " + " | ".join(last.yours[:6])[:1500]
                )
            elif last is not None and last.red and st.done:
                # Not a stop: the wave check judges the tree; the record says why it ended.
                how = (
                    f"{CHECK_OVERRIDDEN} after {st.check_refusals} refused done(s)"
                    if st.check_refusals
                    else CHECK_RED_LAST_TURN
                )
                st.triage.append(f"{how}; its last check: " + " | ".join(last.yours[:6])[:1500])
            elif last is None:
                # Not a stop: the wave check judges the tree as for any session.
                why = (
                    "the gates could not judge them (infra)"
                    if c.unjudged(authored, deleted)
                    else "no check was left to run on them"
                    if c.left <= 0
                    else "the session ended without checking them"
                )
                st.triage.append(
                    f"{CHECK_UNVERIFIED}: the session's last edits were never checked ({why})"
                )
        payload: dict[str, Any] = {"kind": "full-file-snapshot", "files": authored}
        if deleted:
            payload["deleted"] = deleted
        diff_uri = sandbox.put_blob(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )

        summary = st.summary.strip()[:500]
        notes = [*([summary] if summary else []), *st.triage, *st.outside] or [
            "run completed with no summary"
        ]
        notes += [
            f"non-UTF-8 (binary) file skipped from snapshot slice: {p}" for p in skipped_binary
        ]
        # Ended without done, the session has no summary of its own: the digest
        # stands in, under its own name — every adapter-report of a task folds into
        # one record on the state channel (GateTriage.union_key), so the last note
        # would hide it.
        heads = [] if st.done else [(DIGEST_TRIAGE, _digest(st, authored, deleted))]
        run_against = f"task/{packet.task.get('id', 'unknown')}"
        triage = tuple(
            GateTriage(
                name=name,
                run_against=run_against,
                explanation=note,
                produced_by=self.name,
                seq=Seq(self._run_id, 0, 0, self._node_id, i),
            )
            for i, (name, note) in enumerate(
                [*heads, *(("adapter-report", note) for note in notes)]
            )
        )
        return AdapterResult(
            diff_uri=diff_uri,
            triage=triage,
            adapter_self_reported_model=self._self_reported_model,
            turns_used=turns,
            snapshot_digest=sandbox.snapshot(),
        )

    def _json_turn(self, messages: list[dict[str, Any]], st: _Loop, sandbox: Sandbox) -> None:
        schema = ACTION_SCHEMA if st.checker is None else _CHECK_SCHEMA
        if st.fence is not None:
            schema = _FENCED_SCHEMAS[id(schema)]
        text, parsed = self._complete(messages, schema)
        act = parsed if isinstance(parsed, dict) else _lenient_action(text)
        messages.append({"role": "assistant", "content": text})
        if act is None:
            messages.append({"role": "user", "content": "Reply with ONE valid action JSON only."})
            return
        out = self._act(act, st, sandbox)
        if out is None:
            return
        observation, read = out
        content = f"OBSERVATION:\n{observation}{_countdown(st)}"
        messages.append({"role": "user", "content": content})
        st.verbatim.append(_Kept(len(messages) - 1, _summarize(act), len(content), read))
        _mask(messages, st)

    def _native_turn(
        self,
        tool_fn: AgentToolCompleteFn,
        messages: list[dict[str, Any]],
        st: _Loop,
        sandbox: Sandbox,
    ) -> None:
        tools = TOOLS if st.checker is None else _CHECK_TOOLS
        if st.fence is not None:
            tools = _FENCED_TOOLS[id(tools)]
        text, calls = tool_fn(messages, tools)
        if not calls:
            messages.append({"role": "assistant", "content": text or "(no reply)"})
            messages.append({"role": "user", "content": _NUDGE})
            return
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text.strip() else []
        blocks += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("input") or {}}
            for c in calls
        ]
        messages.append({"role": "assistant", "content": blocks})
        idx = len(messages)  # where this turn's tool results land
        results: list[dict[str, Any]] = []
        for i, call in enumerate(calls):
            act: dict[str, Any] | None = None
            read: _Read | None = None
            if i >= _MAX_CALLS_PER_TURN:
                observation = (
                    f"ERROR: not run — at most {_MAX_CALLS_PER_TURN} tool calls per turn; "
                    "call it again next turn if it is still needed"
                )
            elif call.get("error"):
                observation = f"ERROR: {call['error']}"
            else:
                act = {**(call.get("input") or {}), "action": call["name"]}
                out = self._act(act, st, sandbox)
                if out is None:
                    return  # done, or stopped early: no later turn reads the results
                observation, read = out
            results.append(
                {"type": "tool_result", "tool_use_id": call["id"], "content": observation}
            )
            if act is not None:
                st.verbatim.append(
                    _Kept(idx, _summarize(act), len(observation), read, len(results) - 1)
                )
        if note := _countdown(st):
            results[-1] = {**results[-1], "content": f"{results[-1]['content']}{note}"}
        messages.append({"role": "user", "content": results})
        _mask(messages, st)

    def _act(
        self, act: dict[str, Any], st: _Loop, sandbox: Sandbox
    ) -> tuple[str, _Read | None] | None:
        """Run one action through every per-action guard; None ends the loop
        (``done``, or an early stop recorded in ``st.triage``)."""
        worktree = st.worktree
        action = act.get("action")
        if action == "done":
            raw = act.get("triage")
            items = raw if isinstance(raw, list) else [raw] if raw else []
            if refusal := _unchanged_done(st, [str(t) for t in items if str(t).strip()]):
                return refusal, None
            if refusal := _undeclared_done(st):
                return refusal, None
            if refusal := _red_done(st):
                return refusal, None
            st.summary = str(act.get("summary", ""))
            st.triage = [str(t) for t in items if str(t).strip()]
            st.stopped = st.done = True
            self._activity(act)
            return None
        self._activity(act)
        st.actions.append(_summarize(act))
        fp = _fingerprint(act)
        count, tree_then = st.repeats.get(fp, (0, st.tree))
        # Any tree change since the last identical action (a write, sed -i, a
        # formatter, pip install) makes re-running it legitimate again.
        count = count + 1 if tree_then == st.tree else 1
        if count >= _REPEAT_STOP:
            st.triage.append(
                f"stopped early: identical action repeated {_REPEAT_STOP} times "
                f"({_summarize(act)}) — partial work recorded"
            )
            st.stopped = True
            return None
        key = _file_key(worktree, str(act.get("path", ""))) if action == "read_file" else None
        if key is not None:
            rel, stamp = key
            n, stamp_then = st.reads.get(rel, (0, stamp))
            if stamp_then != stamp or n == 0:
                n = 1
            elif _rereads_verbatim(st.verbatim, worktree, rel, stamp, act):
                n += 1
            if n >= _THRASH_STOP:
                st.triage.append(
                    f"stopped early: read {rel} {_THRASH_STOP} times without changing "
                    "it — partial work recorded"
                )
                st.stopped = True
                return None
            st.reads[rel] = (n, stamp)
        read: _Read | None = None
        guard = _fence_snapshot(st) if action == "run" else None
        try:
            if action == "read_file":
                observation, read = _read_file(worktree, str(act.get("path", "")), act)
            elif action == "check":
                observation = _check(st)
            elif fenced := _fenced(st, act):
                observation = fenced
            else:
                observation = self._execute(act, sandbox, worktree, st.files, st.write_check)
        except (ValueError, OSError) as exc:
            observation = f"ERROR: {exc}"
        if guard is not None:
            observation += _revert_fenced_out(st, guard)
        if action == "run":
            observation += _install_notes(st, str(act.get("cmd", "")), observation)
            st.last_check = f"{_summarize(act)}: {_head(observation)}"
        elif action == "check":
            st.last_check = f"check: {_head(observation)}"
        st.tree = _tree_state(worktree)
        st.repeats[fp] = (count, st.tree)
        if count >= _REPEAT_WARN:
            observation = (
                f"WARNING: you have issued this exact action {count} times in a row "
                "with no change to the worktree in between. Try something different "
                "or declare done.\n" + observation
            )
        if key is not None and st.reads[key[0]][0] >= _THRASH_WARN:
            observation = (
                f"NOTE: you have read {key[0]} {st.reads[key[0]][0]} times without changing "
                "it — its content is above; make the edit or finish.\n" + observation
            )
        return observation, read

    def _activity(self, act: dict[str, Any]) -> None:
        """Tell the listener what this action does. Advisory: a listener that raises
        never affects the session."""
        cb = self._activity_cb
        known = _ACTIVITY.get(str(act.get("action")))
        if cb is None or known is None:
            return
        verb, key = known
        detail = " ".join(str(act.get(key) or "").split()) if key else ""
        if len(detail) > _ACTIVITY_MAX:
            detail = detail[: _ACTIVITY_MAX - 1] + "…"
        with contextlib.suppress(Exception):
            cb(f"{verb} {detail}" if detail else verb.rstrip(":"))

    def _execute(
        self,
        act: dict[str, Any],
        sandbox: Sandbox,
        worktree: Path,
        files: tuple[str, ...] | None = None,
        write_check: tuple[str, ...] | None = None,
    ) -> str:
        action = act.get("action")
        if action in ("write_file", "delete_file") and files is not None:
            # The ticket's diff is cut back to its files after the session; refusing
            # here lets the model correct a stray path (contract.py for contracts.py)
            # instead of building on a write that will be discarded.
            from foundry.agents.implementation.tickets import in_write_scope

            raw = str(act.get("path", ""))
            rel = PurePosixPath(raw).as_posix() if raw else raw
            if rel and not in_write_scope(rel, files):
                return (
                    f"ERROR: not written — {rel} is outside this ticket's files, "
                    "and changes there are discarded. Write only: "
                    + (", ".join(files) or "files other than the spec inputs")
                    + "."
                )
        if action == "write_file":
            rel = str(act.get("path", ""))
            target = _contained(worktree, rel)
            content = str(act.get("content", ""))
            # Checked before the write, so a rejected one leaves the file (and the
            # tree fingerprint) exactly as it was: a broken write the model keeps
            # building on costs more turns than one it must redo now.
            error, note = _syntax_check(rel, content)
            if error is not None:
                state = "its previous content is kept" if target.is_file() else "it was not created"
                return (
                    f"ERROR: write rejected — {rel} {error}\nThe file is unchanged "
                    f"({state}). Fix the error and write the full file again."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            obs = f"wrote {rel} ({len(content)} chars){note}"
            if target.suffix == ".py" and write_check is not None:
                obs += _mypy_advisory(sandbox, PurePosixPath(rel).as_posix(), write_check)
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
            extra: dict[str, str] = {}
            report: Path | None = None
            if _BDD_REPORT_ENV not in sandbox.scrubbed_env():
                # Beside the worktree, so the report never lands in the diff.
                report = worktree.parent / "bdd-report.ndjson"
                report.unlink(missing_ok=True)
                extra[_BDD_REPORT_ENV] = str(report)
            result = sandbox.run(cmd, timeout_s=self._run_timeout_s, extra_env=extra)
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
            obs = f"{status}\n{tail}" + _step_hints(out, worktree)
            if (
                report is not None
                and _BDD_CMD.search(cmd)
                and (not report.is_file() or report.stat().st_size == 0)
            ):
                obs += (
                    f"\nNOTE: this run wrote no report to ${_BDD_REPORT_ENV}: the gate "
                    "would see no scenario results. Make the bdd runner write its "
                    f"report to ${_BDD_REPORT_ENV}."
                )
            return obs
        return f"ERROR: unknown action {action!r}"


_FAILING_LINE = re.compile(r"error|fail|assert|traceback|exception", re.IGNORECASE)


def _head(observation: str) -> str:
    """The status line of an observation and its first failing lines (else its
    first lines)."""
    lines = [ln.strip() for ln in observation.splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    failing = [ln for ln in lines[1:] if _FAILING_LINE.search(ln)]
    return " | ".join([lines[0], *(failing or lines[1:])[:_DIGEST_LINES]])


def _digest(st: _Loop, authored: Mapping[str, str], deleted: Sequence[str]) -> str:
    """The summary of a session that ended without ``done`` (turn cap, repeat or
    read-thrash stop): built from the loop's record, not asked of the model."""

    def paths(items: Iterable[str]) -> str:
        names = sorted(items)
        more = f" (+{len(names) - _DIGEST_PATHS} more)" if len(names) > _DIGEST_PATHS else ""
        return ", ".join(names[:_DIGEST_PATHS]) + more if names else "none"

    parts = [
        f"{STOPPED_DIGEST} the session ended before done",
        f"files written: {paths(authored)}",
        *([f"files deleted: {paths(deleted)}"] if deleted else []),
        f"files read: {paths(st.reads)}",
        f"last check: {st.last_check or 'none run'}",
        "last actions: " + (" -> ".join(st.actions[-_DIGEST_ACTIONS:]) or "none"),
    ]
    return "; ".join(parts)[:DIGEST_CAP]


def _fenced(st: _Loop, act: dict[str, Any]) -> str | None:
    """The refusal of a fixer's write or delete outside its target (``st.fence``)
    that states no ``reason``, else None (an outside write with one is recorded,
    and its file admitted from then on).

    The escape is the model's stated reason, not the files a traceback names: a
    failing test's traceback names the code it calls, and editing that code (not the
    test) is exactly the stray fix the fence is for. A reason costs one more call,
    makes the cross-file edit deliberate, and leaves a line a reviewer can judge."""
    from foundry.agents.implementation.tickets import in_write_scope

    if st.fence is None or act.get("action") not in ("write_file", "delete_file"):
        return None
    raw = str(act.get("path", ""))
    rel = PurePosixPath(raw).as_posix() if raw else raw
    if not rel or in_write_scope(rel, st.fence):
        return None
    reason = " ".join(str(act.get("reason") or "").split())
    if reason:
        st.fence = (*st.fence, rel)  # admitted for the rest of the session
        st.outside.append(f"fix pass changed {rel} outside its target: {reason[:300]}")
        return None
    return (
        f"ERROR: not written — {rel} is outside this fix pass's target. Edit only: "
        + ", ".join(st.fence)
        + f". If the failure really needs {rel} changed, repeat the call with a "
        "`reason` saying why."
    )


# What a fenced session's `run` guard reads per run (before and after): past it the
# run goes unguarded, and the observation says so.
_FENCE_GUARD_MAX = 32 << 20


def _fence_snapshot(st: _Loop) -> dict[str, bytes] | str | None:
    """A fenced session's files outside its target before a ``run`` (a note when the
    tree is too large to guard), else None: unfenced."""
    if st.fence is None:
        return None
    try:
        tree = walk_worktree(st.worktree, max_total=_FENCE_GUARD_MAX)
    except TreeTooLargeError:
        return "\nNOTE: the tree is too large to guard this run's edits outside the target."
    return _fenced_out(st, tree)


def _fenced_out(st: _Loop, tree: dict[str, bytes]) -> dict[str, bytes]:
    from foundry.agents.implementation.tickets import in_write_scope

    fence = st.fence or ()
    return {rel: data for rel, data in tree.items() if not in_write_scope(rel, fence)}


def _revert_fenced_out(st: _Loop, before: dict[str, bytes] | str) -> str:
    """Undo what a fenced session's ``run`` did outside its target — ``sed -i``, a
    heredoc, ``rm`` — and say so. A shell command is never parsed: the tree is
    compared before and after, so the fence holds however the edit was spelled, and
    a deliberate cross-file edit goes through ``write_file`` with its ``reason``."""
    if isinstance(before, str):
        return before
    try:
        after = _fenced_out(st, walk_worktree(st.worktree, max_total=_FENCE_GUARD_MAX))
    except TreeTooLargeError:
        return "\nNOTE: the tree grew too large to guard this run's edits outside the target."
    changed = sorted(
        rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)
    )
    if not changed:
        return ""
    reverted: list[str] = []
    for rel in changed:
        try:
            _restore(st.worktree, rel, before.get(rel))
            reverted.append(rel)
        except OSError as exc:
            reverted.append(f"{rel} (NOT reverted: {exc.strerror or exc})")
    return (
        "\nREVERTED: this run changed files outside this fix pass's target, and those "
        "changes were undone: "
        + ", ".join(reverted)
        + ". Edit only: "
        + ", ".join(st.fence or ())
        + ". If the failure really needs another file changed, use write_file (or "
        "delete_file) with a `reason` saying why — not a shell command."
    )


def _restore(worktree: Path, rel: str, data: bytes | None) -> None:
    """Put ``rel`` back to ``data`` (None: the run created it — remove it). The run
    may have left a link at any component, so nothing is written through one."""
    root = worktree.resolve()
    path = root / rel
    if not path.parent.resolve().is_relative_to(root):
        raise OSError(f"{rel}: its directory now leads out of the worktree")
    if data is None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        return
    try:
        # In place, so the file keeps its mode (a script stays executable).
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError:
        # A link or directory in its place: removed, then the file written fresh.
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _unchanged_done(st: _Loop, triage: list[str]) -> str | None:
    """The refusal of a repair session's first ``done`` while no file has changed and
    a turn remains, else None. A local fixer once read every failing file, named each
    fix in its done summary ("Let me fix these issues one at a time") and ended with an
    empty diff, which halted the wave as no progress. A second ``done`` always ends
    it: a failure the session cannot fix by editing is reported, not looped on."""
    if st.done_refused or st.left < 1 or st.start is None:
        return None
    authored, deleted, _ = changed_files(st.start, walk_worktree(st.worktree))
    if authored or deleted:
        return None
    st.done_refused = True
    todo = "".join(f"\n- {t}" for t in triage[:8])
    return (
        "ERROR: not done — no file in the worktree has changed yet, and done ends the "
        "session: only file edits are kept, a summary or plan of what to change is "
        "discarded, and so is a package installed in this sandbox (declare it, e.g. a "
        "types-* stub, in the project's dependency manifest). "
        "Make the changes now (write_file), re-run the build/typecheck and "
        "the tests, then declare done. If the task truly needs no change, call done "
        "again." + (f"\nYour triage names this unfinished work:{todo}" if todo else "")
    )


def _undeclared_done(st: _Loop) -> str | None:
    """The refusal of the first ``done`` while a package this session pip-installed
    is still undeclared and a turn remains, else None. A local engineer installed
    bcrypt in its sandbox, so its own runs passed; the gates install only the
    manifest, so every module importing it failed to collect and the run was lost."""
    if st.install_refused or st.left < 1 or not st.installed:
        return None
    from foundry.gates.pydeps import undeclared_distributions

    missing = undeclared_distributions(st.installed, st.worktree)
    if not missing:
        return None
    st.install_refused = True
    return (
        "ERROR: not done —"
        + "".join(f"\n- {_install_note(d)}" for d in missing)
        + "\nDeclare them now (write_file), then declare done. If the project does not "
        "import them, call done again."
    )


_PIP = re.compile(r"(?:.*/)?pip[\d.]*")
_PYTHON = re.compile(r"(?:.*/)?python[\d.]*")
_ENV_ASSIGN = re.compile(r"[A-Za-z_]\w*=.*")
_SHELL_SEP = re.compile(r"&&|\|\||[;|\n]")
_REDIRECT = re.compile(r"\d*[<>]")
# pip / uv pip install options whose value is the next argument.
_PIP_VALUE_OPTS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-e",
        "--editable",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-t",
        "--target",
        "--prefix",
        "--root",
        "--src",
        "-p",
        "--python",
        "--platform",
        "--python-version",
        "--implementation",
        "--abi",
        "--upgrade-strategy",
        "--progress-bar",
        "--no-binary",
        "--only-binary",
        "--trusted-host",
        "--cache-dir",
        "--log",
        "--proxy",
        "--retries",
        "--timeout",
        "--exists-action",
        "--cert",
        "--client-cert",
        "-C",
        "--config-settings",
        "--global-option",
        "--index-strategy",
        "--extra",
    }
)


def _pip_installs(cmd: str) -> list[str]:
    """The distributions a shell line's ``pip install`` / ``python -m pip install`` /
    ``uv pip install`` names; requirement files (``-r``), editables (``-e .``), paths,
    URLs and archives are left out: they install nothing by a plain name."""
    from foundry.gates.pydeps import requirement_name

    names: list[str] = []
    for part in _SHELL_SEP.split(cmd):
        try:
            toks = shlex.split(part)
        except ValueError:
            continue
        while toks and _ENV_ASSIGN.fullmatch(toks[0]):
            toks = toks[1:]
        at = next(
            (
                i
                for i, t in enumerate(toks)
                if t == "install" and i > 0 and _PIP.fullmatch(toks[i - 1])
            ),
            None,
        )
        if at is None or not _pip_launcher(toks[: at - 1]):
            continue
        skip = False
        for tok in toks[at + 1 :]:
            if skip:
                skip = False
            elif _REDIRECT.match(tok):
                skip = _REDIRECT.fullmatch(tok) is not None  # `> log`: its target too
            elif tok.startswith("-"):
                skip = tok in _PIP_VALUE_OPTS
            elif not (
                tok.startswith((".", "/", "~"))
                or "://" in tok
                or tok.endswith((".whl", ".tar.gz", ".zip"))
            ) and (name := requirement_name(tok)):
                names.append(name)
    return names


def _pip_launcher(prefix: list[str]) -> bool:
    """``pip`` itself, ``python -m pip`` or ``uv pip`` (not ``echo pip install x``)."""
    return (
        not prefix
        or prefix == ["uv"]
        or (len(prefix) == 2 and _PYTHON.fullmatch(prefix[0]) is not None and prefix[1] == "-m")
    )


def _install_note(dist: str) -> str:
    return (
        f"`{dist}` is installed only in this sandbox; the gates install only what "
        "requirements.txt / pyproject [project].dependencies declares, so add it there "
        "(or a types-* stub for typing)"
    )


def _install_notes(st: _Loop, cmd: str, observation: str) -> str:
    """A note per distribution a successful ``run`` pip-installed that the project's
    manifest does not declare; each is remembered for the done check."""
    dists = _pip_installs(cmd)
    if not dists or not (
        observation.startswith("exit 0")
        or "Successfully installed" in observation
        or "already satisfied" in observation
    ):
        return ""
    from foundry.gates.pydeps import undeclared_distributions

    st.installed.extend(d for d in dists if d not in st.installed)
    return "".join(
        f"\nNOTE: {_install_note(d)}" for d in undeclared_distributions(dists, st.worktree)
    )


def _step_hints(out: str, worktree: Path) -> str:
    """The Test gate's explanation of each step argument a pytest-bdd run in the
    session could not find, so a session that runs the scenarios itself reads the
    same why the gate would report (a local fixer once spent its whole budget on
    one such error, rewriting the same step definitions), and of each step text a
    step module defines twice, on any failing run."""
    missing, failed = "not found" in out, "FAILED" in out
    if not missing and not failed:
        return ""
    from foundry.gates.test import duplicate_step_notes, step_argument_hints
    from foundry.schemas.implementation import FEATURE_DIR

    root = worktree.resolve()
    features = [
        p.relative_to(root).as_posix() for p in sorted((root / FEATURE_DIR).rglob("*.feature"))
    ]
    hints = (
        *(step_argument_hints(out, root, features) if missing else ()),
        *(duplicate_step_notes(root, features) if failed else ()),
    )
    return "".join(f"\nNOTE: {h}" for h in hints)


def _opening(
    packet: TaskPacket, *, native: bool, listing: str = "", check: str | None = None
) -> list[dict[str, Any]]:
    """The first message; ``check`` is the opening gate check's report (``None``:
    the session has no ``check``)."""
    if check is None:
        epilogue = _NATIVE_EPILOGUE if native else _EPILOGUE
    elif native:
        epilogue = _NATIVE_CHECK_EPILOGUE
    else:
        epilogue = _CHECK_EPILOGUE
    if check:
        epilogue = f"{check}\n\n{epilogue}"
    if listing:
        epilogue = f"{listing}\n\n{epilogue}"
    return [{"role": "user", "content": assemble_prompt(packet, epilogue=epilogue)}]


def _open_check(packet: TaskPacket) -> tuple[SessionCheck | None, str | None]:
    """The session's gate check (from the engineering node's or the fixer's
    :func:`~foundry.gates.self_check.check_scope`) and its opening report, run on
    the starting tree; ``(None, None)`` when this session has none."""
    from foundry.gates.self_check import SessionCheck, current_check, render, wanted

    spec = current_check()
    if spec is None or not wanted(spec.base.settings.mode):
        return None, None
    checker = SessionCheck(spec, label=str(packet.task.get("id", "")))
    try:
        verdict = checker.opening()
    except Exception as exc:  # an aid, not a gate: a broken check leaves the session as before
        progress.log(
            f"proxy-agent: no gate check for {packet.task.get('id')}: {type(exc).__name__}: {exc}"
        )
        return None, None
    return checker, render(verdict, left=checker.left, opening=True)


def _with_rules(packet: TaskPacket, rewrites: Mapping[str, str]) -> TaskPacket:
    """``packet`` with each rule ``rewrites`` names replaced (verify -> check)."""
    rules = packet.task.get("rules")
    if not isinstance(rules, list):
        return packet
    return packet.model_copy(
        update={
            "task": {
                **packet.task,
                "rules": [rewrites.get(r, r) if isinstance(r, str) else r for r in rules],
            }
        }
    )


def _changes(st: _Loop) -> tuple[dict[str, str], list[str]]:
    authored, deleted, _ = changed_files(st.origin, walk_worktree(st.worktree))
    return authored, deleted


def _check(st: _Loop) -> str:
    """The ``check`` action: the gates on the current tree, cached per tree."""
    from foundry.gates.self_check import render

    c = st.checker
    if c is None:
        return "ERROR: check is unavailable in this session; run the commands in `verify`"
    files, deleted = _changes(st)
    if (cached := c.cached(files, deleted)) is not None:
        return render(cached, left=c.left, cached=True)
    if c.left <= 0:
        return (
            "ERROR: no checks left this session: run the commands in `verify` to test "
            "further edits, then call done."
        )
    try:
        return render(c.check(files, deleted), left=c.left)
    except Exception as exc:
        return f"ERROR: the check could not run: {type(exc).__name__}: {exc}"


def _red_done(st: _Loop) -> str | None:
    """The refusal of a ``done`` while the gates fail on the session's own work, else
    None. A tree no check has judged is checked now (a check left in the budget);
    refused at most :data:`_CHECK_REFUSALS` times, and only while a turn remains."""
    from foundry.gates.self_check import render

    c = st.checker
    if c is None or st.check_refusals >= _CHECK_REFUSALS or st.left < 1:
        return None
    files, deleted = _changes(st)
    verdict = c.cached(files, deleted)
    if verdict is None:
        if c.left <= 0:
            return None
        try:
            verdict = c.check(files, deleted)
        except Exception as exc:
            progress.log(f"proxy-agent: gate check failed at done: {type(exc).__name__}: {exc}")
            return None
        st.last_check = "check at done: " + _head(render(verdict, left=c.left))
    if not verdict.red:
        return None
    st.check_refusals += 1
    last = st.check_refusals >= _CHECK_REFUSALS
    return (
        "ERROR: not done — the gates fail on your work in this tree:\n"
        + render(verdict, left=c.left)
        + (
            "\nFix what is listed as yours, then call check and done. "
            if c.left > 0
            else "\nNo checks are left: fix what is listed as yours, then call done. "
        )
        + (
            "If it cannot be fixed by editing your files, call done again and name it in triage."
            if last
            else ""
        )
    )


def _listing(tree: dict[str, bytes]) -> str:
    """The worktree's files as the session starts, capped; empty for an empty tree."""
    skip = _SEARCH_SKIP_DIRS | _TREE_SKIP_DIRS
    paths = [p for p in sorted(tree) if not skip & set(PurePosixPath(p).parts[:-1])]
    if not paths:
        return ""
    more = f"\n... and {len(paths) - _LIST_MAX} more" if len(paths) > _LIST_MAX else ""
    return f"FILES IN THE WORKTREE ({len(paths)}):\n" + "\n".join(paths[:_LIST_MAX]) + more


def _countdown(st: _Loop) -> str:
    """The remaining-turns note on a long session's last few turns (none after the
    last: no model call reads it)."""
    if st.budget <= 2 * _LOW_TURNS or not 0 < st.left <= _LOW_TURNS:
        return ""
    if st.left == 1:
        return (
            "\nTURN BUDGET: 1 turn left. Use it to declare done: say what you changed and "
            "list in triage what is unfinished or still failing, so the next session can "
            "continue from it."
        )
    return (
        f"\nTURN BUDGET: {st.left} turns left. Stop exploring: finish the edit "
        "in progress (update every caller of anything you renamed or moved), re-run the "
        "build/typecheck and the tests on it, then declare done, listing in triage what "
        "is still unfinished."
    )


def _mask(messages: list[dict[str, Any]], st: _Loop) -> None:
    """Observation masking: past the verbatim budget, elide old observations
    except the tail and reads still showing a file's current content."""
    pinned = _pinned(st.verbatim, st.worktree)
    tail = {k.key for k in st.verbatim[-_OBS_KEEP:]}
    pinned_size = sum(k.size for k in st.verbatim if k.key in pinned - tail)
    if len(st.verbatim) - len(pinned) <= _OBS_MAX and pinned_size <= _PIN_CAP:
        return
    # Elide in batches, not one per turn: every rewrite invalidates the
    # prompt-prefix cache from that message on, so batching moves the
    # prefix once every 4 turns (3 kept + 4 new).
    # A placeholder is never rewritten again.
    keep = set(tail)
    budget = _PIN_CAP if pinned_size <= _PIN_CAP else _PIN_CAP // 2
    for k in reversed(st.verbatim):
        if k.key not in pinned or k.key in keep:
            continue
        if k.size > budget:
            break  # oldest go first: no older read slips into the gap
        keep.add(k.key)
        budget -= k.size
    for k in st.verbatim:
        if k.key in keep:
            continue
        note = f"[output of {k.what} elided — re-run or re-read if needed]"
        if k.block < 0:
            messages[k.idx] = {"role": "user", "content": f"OBSERVATION: {note}"}
        else:
            blocks = list(messages[k.idx]["content"])
            blocks[k.block] = {**blocks[k.block], "content": note}
            messages[k.idx] = {**messages[k.idx], "content": blocks}
    st.verbatim = [k for k in st.verbatim if k.key in keep]


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


def _rereads_verbatim(
    kept: list[_Kept], worktree: Path, rel: str, stamp: tuple[int, int], act: dict[str, Any]
) -> bool:
    """True iff at least two thirds of the lines a read asks for are still verbatim in
    context from earlier reads of the unchanged file. Any overlap used to count: a
    local fixer paging a 614-line test module in ranges that shared their boundary
    line (1-100, 100-200, ...) was stopped as thrashing twice, before its first edit."""
    try:
        lo = max(1, int(act.get("start_line") or 1))
        hi = int(act.get("end_line") or sys.maxsize)
        total = len(
            _contained(worktree, rel).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except (TypeError, ValueError, OSError):
        return False  # _read_file reports the bad range or path
    hi = min(hi, total)
    if hi < lo:
        return False
    spans = sorted(
        (max(lo, k.read.lo), min(hi, k.read.hi))
        for k in kept
        if k.read is not None and k.read.path == rel and k.read.stamp == stamp
    )
    covered, upto = 0, lo - 1
    for a, b in spans:
        a = max(a, upto + 1)
        if b >= a:
            covered += b - a + 1
            upto = b
    return 3 * covered >= 2 * (hi - lo + 1)


def _pinned(kept: list[_Kept], worktree: Path) -> set[tuple[int, int]]:
    """Keys of reads still showing a file's current content and not
    covered by a newer read of the same file — masking keeps these verbatim."""
    pinned: set[tuple[int, int]] = set()
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
        pinned.add(k.key)
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


def _syntax_check(rel: str, content: str) -> tuple[str | None, str]:
    """``(error, note)`` for content about to be written to ``rel``: an error
    rejects the write; a note (a checker that crashed rather than found an error)
    is appended to the accepted write's observation. Types without a cheap,
    build-free checker pass unchecked."""
    name = PurePosixPath(rel).name
    suffix = PurePosixPath(rel).suffix
    try:
        if suffix == ".py":
            # Host Python is also what the gate's compileall and pytest run on.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(content, rel, "exec")
        elif suffix == ".json" and not _jsonc(rel, name):
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
        elif suffix in (".yaml", ".yml") and "{{" not in content and "{%" not in content:
            import yaml

            # compose, not load: a custom tag (CloudFormation's !Ref) is valid YAML.
            for _ in yaml.compose_all(content, Loader=yaml.SafeLoader):
                pass
        elif suffix == ".feature":
            from gherkin.parser import Parser

            Parser().parse(content)
        elif suffix in _CLI_CHECKERS:
            return _cli_check(rel, content, _CLI_CHECKERS[suffix]), ""
    except SyntaxError as exc:
        return _located(content, exc.lineno, exc.offset, str(exc.msg), "Python syntax error"), ""
    except json.JSONDecodeError as exc:
        return _located(content, exc.lineno, exc.colno, exc.msg, "invalid JSON"), ""
    except tomllib.TOMLDecodeError as exc:
        m = re.search(r"at line (\d+), column (\d+)", str(exc))
        line, col = (int(m[1]), int(m[2])) if m else (None, None)
        return _located(content, line, col, str(exc), "invalid TOML"), ""
    except ImportError:
        return None, ""  # yaml / gherkin absent from this install: unchecked
    except Exception as exc:
        yaml_error = sys.modules.get("yaml")
        if yaml_error is not None and isinstance(exc, yaml_error.MarkedYAMLError):
            mark = exc.problem_mark
            line, col = (mark.line + 1, mark.column + 1) if mark is not None else (None, None)
            msg = " ".join(str(p) for p in (exc.context, exc.problem) if p)
            return _located(content, line, col, msg, "invalid YAML"), ""
        errors = getattr(exc, "errors", None)
        if suffix == ".feature" and type(exc).__module__.startswith("gherkin"):
            first = errors[0] if errors else exc
            loc = getattr(first, "location", None) or {}
            return _located(
                content, loc.get("line"), loc.get("column"), str(first), "malformed Gherkin"
            ), ""
        if suffix == ".py":
            host = f"{sys.version_info.major}.{sys.version_info.minor}"
            return None, (
                f"\nNOTE: the Python {host} compile check failed with "
                f"{type(exc).__name__}: {exc} — not a verdict on the file"
            )
        return None, ""
    return None, ""


# Comment-tolerant JSON dialects that json.loads would wrongly reject.
_JSONC_NAMES = ("tsconfig", "jsconfig", ".eslintrc", ".babelrc", "devcontainer")


def _jsonc(rel: str, name: str) -> bool:
    return name.startswith(_JSONC_NAMES) or ".vscode/" in f"/{rel}"


# Parse-only checkers on the host toolchain (no build, no execution). Plain .js is
# left out: JSX in .js is common and node rejects it.
_CLI_CHECKERS: dict[str, tuple[str, ...]] = {
    ".go": ("gofmt", "-e"),
    ".mjs": ("node", "--check", "--input-type=module", "-"),
    ".cjs": ("node", "--check", "--input-type=commonjs", "-"),
}
_CLI_CHECK_TIMEOUT_S = 10


def _cli_check(rel: str, content: str, argv: tuple[str, ...]) -> str | None:
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            input=content,
            capture_output=True,
            text=True,
            timeout=_CLI_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return None
    err = proc.stderr.strip()
    if argv[0] == "gofmt":
        m = re.match(r"<standard input>:(\d+):(\d+): (.*)", err)
        if m:
            return _located(content, int(m[1]), int(m[2]), m[3], "Go syntax error")
    elif "SyntaxError" in err:
        m = re.match(r"\[stdin\]:(\d+)", err)
        msg = next((ln for ln in err.splitlines() if ln.startswith("SyntaxError")), err)
        return _located(content, int(m[1]) if m else None, None, msg, "JavaScript syntax error")
    return None  # the checker failed for some other reason: not a verdict


def _located(content: str, line: int | None, col: int | None, msg: str, kind: str) -> str:
    """``kind`` at line/col with a few numbered lines of context and a caret."""
    where = f" at line {line}" + (f", column {col}" if col else "") if line else ""
    out = f"has a {kind}{where}: {msg}"
    lines = content.splitlines()
    if not line or not lines:
        return out
    at = min(line, len(lines))
    ctx = []
    for n in range(max(1, at - 2), min(len(lines), at + 2) + 1):
        ctx.append(f"{n:>5} | {lines[n - 1]}")
        if n == at and col:
            ctx.append("      | " + " " * (col - 1) + "^")
    return out + "\n" + "\n".join(ctx)


# Fast type/import feedback on each written .py: --follow-imports=silent reports only
# the written file. The cache sits beside the worktree (never in the diff) and lives
# as long as the sandbox: with /dev/null every write re-analysed every import from
# scratch, and a project importing a few heavy libraries ran past the timeout, which
# was dropped silently.
_MYPY_WRITE = ("--follow-imports=silent", "--no-error-summary", "--show-error-codes")
_MYPY_CACHE = "mypy-cache"
_MYPY_TIMEOUT_S = 15
_MYPY_MAX_LINES = 10


def _write_check_flags() -> tuple[str, ...]:
    """The per-write mypy's flags: the Build gate's lenient mypy flags, plus
    ``--strict`` when the stack's typecheck is strict. On whatever the self-check
    mode: with the cache it is cheap, and a cloud-tier or self-check-off session
    otherwise learns of a type or import error only at the gate."""
    from foundry.gates.profiles import _MYPY_LENIENT
    from foundry.gates.self_check import current_check

    spec = current_check()
    stack = spec.base.state.get("stack") if spec is not None else None
    strict = stack is not None and "--strict" in stack.typecheck.split()
    return (*shlex.split(_MYPY_LENIENT)[1:], *(("--strict",) if strict else ()))


def _mypy_advisory(sandbox: Sandbox, rel: str, flags: Sequence[str]) -> str:
    """mypy's errors in just-written ``rel`` on the sandbox's interpreter, as advice
    (a ticket's modules are expected to be partial mid-session, so a type error never
    rejects a write). Empty when mypy is absent or crashes."""
    cache = Path(sandbox.worktree).parent / _MYPY_CACHE
    argv = ["python", "-m", "mypy", *_MYPY_WRITE, f"--cache-dir={cache}", *flags, rel]
    try:
        result = sandbox.run(argv, timeout_s=_MYPY_TIMEOUT_S)
    except OSError:
        return ""
    if result.timed_out:
        return (
            f"\nNOTE: type check timed out ({_MYPY_TIMEOUT_S}s, mypy on this file only) — "
            "run the project's typecheck before done"
        )
    if result.returncode not in (0, 1):
        return ""
    # Errors only: notes (an [annotation-unchecked] on every annotated line of an
    # untyped function) would crowd the capped lines.
    errors = [
        ln for ln in result.stdout.splitlines() if ln.startswith(f"{rel}:") and ": error:" in ln
    ]
    if not errors:
        return ""
    shown = errors[:_MYPY_MAX_LINES]
    more = f"\n  ... {len(errors) - len(shown)} more" if len(errors) > len(shown) else ""
    return (
        "\nADVISORY (mypy on this file only; the write was kept — fix what is real, "
        "ignore what an unfinished module explains):\n  " + "\n  ".join(shown) + more
    )


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
