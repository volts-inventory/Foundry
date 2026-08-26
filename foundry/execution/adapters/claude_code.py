"""Claude Code adapter — the AGENTIC coding substrate. (ROADMAP Phase 1a)

Runs the ``claude`` CLI *agentically inside the task sandbox worktree*: Claude
Code reads/edits files, runs builds and tests, and iterates — the inner-loop
sophistication the adapter contract says "lives in the substrate by design"
(`adapter_contract.py`). This is the load-bearing difference from
:class:`~foundry.execution.adapters.raw_react.RawReactAdapter` (one completion,
one diff, applies nothing): here the substrate mutates the worktree and the
adapter OBSERVES the resulting tree.

Contract mapping:

* **prompt** — ``assemble_prompt`` unchanged (pinned constraints VERBATIM +
  FIRST; ``prior_failures`` keeps powering the gate fix loop), plus an epilogue
  telling the agent to implement in the working directory.
* **turn budget** — ``--max-turns`` from ``packet.turn_budget``; ``turns_used``
  is observed from the result envelope's ``num_turns``.
* **diff** — the worktree is walked BEFORE and AFTER the run; the changed file
  set is recorded as a ``full-file-snapshot`` blob
  (``{"kind": "full-file-snapshot", "files": {path: content}}``) — the exact
  body shape ``gate_wiring._diff_files`` materializes directly, which is what
  lets MODIFIED files integrate (the additions-only unified-diff parser is not
  in this path). Deletions ride the snapshot's ``deleted`` list (integration
  unlinks them); non-UTF-8 (binary) files are surfaced as triage notes, not
  silently dropped (the asset pipeline is ROADMAP Phase 6).
* **lineage** — the served model is observed from the envelope's ``modelUsage``
  and carried in ``adapter_self_reported_model``; it stays IGNORED for lineage
  per arch §2b (with a CLI substrate the credential locus moves into the cage —
  the documented trade; see `cli_transports.py` TRUST NOTES and ROADMAP 1b).
* **budgets** — cost is observed from the envelope (``total_cost_usd``) and
  reported via triage; enforcement stays EXTERNAL (sandbox wall-clock/turn
  caps), per the contract's "adapter-reported budget is advisory only".

Auth (dual billing path, ROADMAP 1b): the ``claude`` binary authenticates from
whatever the sandbox's TYPED credential grant admits — ``CLAUDE_CODE_OAUTH_TOKEN``
or a copied ``~/.claude`` login session (Max plan, no API billing), or
``ANTHROPIC_API_KEY`` (metered). The adapter itself never reads or holds a
credential; it declares what the sandbox should grant via
:attr:`credential_grant` / :attr:`home_passthrough`, which the Junior Engineer
passes to the ``Sandbox`` constructor (fail-closed on non-model-auth names).

Permissions: the subprocess runs with ``--dangerously-skip-permissions``
INSIDE the sandbox — per arch §2 the cage (worktree isolation, scrubbed env,
process-group hard-kill) is the enforcement boundary, not the CLI's prompt-
level permission UX. Nothing outside the worktree is writable in production
(mount namespace); dev-grade, the worktree copy is disposable either way.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.execution.adapter_contract import (
    AdapterResult,
    AdapterUnavailableError,
    TaskPacket,
    assemble_prompt,
)
from foundry.execution.sandbox import Sandbox, SandboxResult
from foundry.schemas.gates import GateTriage
from foundry.state.seq import Seq

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    # The injectable exec seam: (sandbox, argv, timeout_s) -> SandboxResult.
    # Default runs through Sandbox.run (the cage); tests inject a fake that
    # writes files into sandbox.worktree and returns a canned envelope.
    # A seam used WITH `on_activity` must also accept the `on_stdout_line` keyword
    # (that's the streaming path); one without it keeps the 3-argument shape.
    SandboxExec = Callable[..., SandboxResult]

__all__ = ["ClaudeCodeAdapter", "changed_files", "walk_worktree"]

_EPILOGUE = (
    "You are working in a disposable sandbox worktree (your current working "
    "directory). IMPLEMENT the task by creating and editing files directly in "
    "this directory — write real, complete code (no placeholders, no TODOs), "
    "plus tests where the task implies them. Run any build/test commands you "
    "need and fix what fails. Do NOT run git commands, do NOT touch anything "
    "outside the current directory. When done, reply with a short summary of "
    "what you built; prefix any known risk, gap, or follow-up with 'TRIAGE: ' "
    "(one per line)."
)

# Worktree walk exclusions: VCS internals and the substrate's own scratch dirs
# are never part of the authored tree slice.
_EXCLUDED_TOP_DIRS = frozenset({".git", ".claude", "node_modules", "__pycache__"})


def walk_worktree(root: Path) -> dict[str, bytes]:
    """The worktree's file map (relative POSIX path -> raw bytes), excluding
    VCS/scratch dirs. Symlinks are skipped (never followed — a link out of the
    tree must not pull external content into the authored slice)."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _EXCLUDED_TOP_DIRS:
            continue
        if any(part == "__pycache__" for part in rel.parts):
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def changed_files(
    before: dict[str, bytes], after: dict[str, bytes]
) -> tuple[dict[str, str], list[str], list[str]]:
    """(added+modified UTF-8 files, deleted paths, skipped-binary paths).

    Only CHANGED files enter the snapshot slice — the base tree is already on
    the integration side, and a slice carrying the whole tree would collide
    across parallel tasks at merge. Non-UTF-8 content is skipped (surfaced as
    triage, not corrupted): the asset pipeline is a later phase (ROADMAP 6).
    """
    authored: dict[str, str] = {}
    skipped_binary: list[str] = []
    for path, data in after.items():
        if before.get(path) == data:
            continue
        try:
            authored[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped_binary.append(path)
    deleted = sorted(p for p in before if p not in after)
    return authored, deleted, skipped_binary


def _default_exec(
    sandbox: Sandbox,
    argv: Sequence[str],
    timeout_s: float | None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> SandboxResult:
    return sandbox.run(list(argv), timeout_s=timeout_s, on_stdout_line=on_stdout_line)


# ---- live activity: what the agent is doing INSIDE one step ------------------
# A step is one blocking CLI call, so without this the whole agentic pass — every
# edit, every test run, sometimes tens of minutes — reports nothing until it exits.
# Streaming turns each tool call into one short phrase.
#
# The phrase is built from a STRICT allowlist of argument fields (paths, commands,
# patterns), never from the content-bearing ones. `content` / `new_string` / `old_string`
# carry whole file bodies — those must never leave the sandbox on a progress channel.
_ACTIVITY_VERB: dict[str, str] = {
    "Edit": "editing", "MultiEdit": "editing", "Write": "writing", "NotebookEdit": "editing",
    "Read": "reading", "Bash": "running", "Glob": "looking for", "Grep": "searching for",
    "WebFetch": "fetching", "WebSearch": "searching the web for", "Task": "delegating",
    "TodoWrite": "planning",
}
_ACTIVITY_ARG: tuple[str, ...] = ("file_path", "path", "notebook_path", "command",
                                  "pattern", "query", "url")
_ACTIVITY_MAX = 72          # one short line — never a payload


def _activity_phrase(tool: str, args: Any) -> str | None:
    """`editing src/store.py` / `running pytest -q` for one tool call, or None when
    there's nothing safe and useful to say."""
    verb = _ACTIVITY_VERB.get(tool)
    if not verb:
        # An unknown/MCP tool: name it, never its arguments (unknown shape = unknown risk).
        return f"using {tool}" if tool else None
    detail = ""
    if isinstance(args, dict):
        for key in _ACTIVITY_ARG:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                detail = " ".join(val.split())          # collapse newlines/tabs
                break
    if not detail:
        return verb if tool != "Bash" else "running a command"
    if len(detail) > _ACTIVITY_MAX:
        detail = detail[: _ACTIVITY_MAX - 1] + "…"
    return f"{verb} {detail}"


def _stream_activities(line: str) -> list[str]:
    """Activity phrases from ONE stream-json line (NDJSON from ``claude -p``).

    Tolerant by design: an unparseable or unrecognised line yields nothing, so a
    format change degrades to silence instead of breaking a build.
    """
    text = line.strip()
    if not text.startswith("{"):
        return []
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return []
    msg = obj.get("message")
    blocks = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(blocks, list):
        return []
    out: list[str] = []
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            phrase = _activity_phrase(str(blk.get("name") or ""), blk.get("input"))
            if phrase:
                out.append(phrase)
    return out


def _stream_envelope(stdout: str) -> dict[str, Any]:
    """The final ``result`` object out of a stream-json stdout (NDJSON).

    Same shape the non-streaming ``--output-format json`` returns, so everything
    downstream (summary, is_error, num_turns, model self-report) is unchanged.
    """
    envelope: dict[str, Any] = {}
    for line in stdout.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            envelope = obj          # keep the LAST one
    return envelope or _lenient_json_object(stdout)


def _lenient_json_object(text: str) -> dict[str, Any]:
    """Strict parse first, else the outermost ``{...}`` span, else ``{}`` —
    tolerant of CLI chatter around the JSON envelope (same discipline as
    ``proxy.cli_transports``, duplicated here to keep execution/ free of a
    proxy-layer import)."""
    text = text.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _observed_model(envelope: dict[str, Any]) -> str | None:
    """The model the envelope SAYS served the run (``modelUsage`` key with the
    most tokens, else top-level ``model``). Carried for cross-checking only —
    IGNORED for lineage (arch §2b)."""
    usage_by_model = envelope.get("modelUsage")
    if isinstance(usage_by_model, dict) and usage_by_model:
        def _tokens(item: tuple[str, Any]) -> int:
            v = item[1]
            if not isinstance(v, dict):
                return 0
            total = 0
            for key in ("inputTokens", "outputTokens", "input_tokens", "output_tokens"):
                n = v.get(key)
                total += n if isinstance(n, int) and not isinstance(n, bool) else 0
            return total

        keys = [(k, v) for k, v in usage_by_model.items() if isinstance(k, str)]
        if keys:
            return max(keys, key=_tokens)[0]
    model = envelope.get("model")
    return model if isinstance(model, str) and model else None


def _usage_from_envelope(envelope: dict[str, Any]) -> dict[str, int]:
    """Advisory token counts the CLI reported for this session, normalised to the
    token-manifest keys (``input_tokens`` / ``output_tokens`` /
    ``cache_read_input_tokens``). Prefers the top-level ``usage`` aggregate; falls back
    to summing per-model ``modelUsage`` (camelCase). Empty when none is reported."""
    def _int(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens")
    top = envelope.get("usage")
    if isinstance(top, dict):
        out = {k: _int(top.get(k)) for k in keys}
        if any(out.values()):
            return out
    by_model = envelope.get("modelUsage")
    if isinstance(by_model, dict):
        def _pick(per: dict[str, Any], *names: str) -> int:
            for n in names:
                got = _int(per.get(n))
                if got:
                    return got
            return 0

        agg = dict.fromkeys(keys, 0)
        for per in by_model.values():
            if isinstance(per, dict):
                agg["input_tokens"] += _pick(per, "inputTokens", "input_tokens")
                agg["output_tokens"] += _pick(per, "outputTokens", "output_tokens")
                agg["cache_read_input_tokens"] += _pick(per, "cacheReadInputTokens")
        if any(agg.values()):
            return agg
    return {}


class ClaudeCodeAdapter:
    """Agentic Claude Code substrate: the CLI edits the worktree; the adapter
    observes the authored tree slice and records it content-addressed."""

    supports_turn_snapshots: bool = True
    name = "claude-code"

    # Read by the Junior Engineer when constructing the task Sandbox (the
    # adapter DECLARES its grant; the Sandbox validates + applies it).
    credential_grant: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
    home_passthrough: bool = True

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        run_id: str = "dev",
        node_id: str = "claude-code",
        model: str | None = None,
        timeout_s: float | None = 1800.0,
        exec_fn: SandboxExec | None = None,
        extra_args: Sequence[str] = (),
        on_activity: Callable[[str], None] | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self._bin = claude_bin
        self._run_id = run_id
        self._node_id = node_id
        self._model = model
        self._timeout_s = timeout_s
        self._exec: SandboxExec = exec_fn or _default_exec
        self._extra_args = tuple(extra_args)
        # B2: when set, resume the prior coding session (`--resume <id>`) instead of
        # re-priming. Off by default; wiring it into the fix loop trades reset-to-base
        # idempotency (LATENCY_PARALLELISM §5) — this only plumbs the flag.
        self._resume_session_id = resume_session_id
        # Opt-in live progress. Passing it switches the CLI to stream-json so each
        # tool call can be reported while the step runs; without it the argv, the
        # parsing and the result are exactly as before.
        self._on_activity = on_activity

    def _pump_activity(self, line: str) -> None:
        """One stdout line -> zero or more activity phrases -> the listener. Advisory:
        a listener that raises can never affect the build."""
        cb = self._on_activity
        if cb is None:
            return
        for phrase in _stream_activities(line):
            with contextlib.suppress(Exception):
                cb(phrase)

    def available(self) -> bool:
        return shutil.which(self._bin) is not None

    def build_prompt(self, packet: TaskPacket) -> str:
        return assemble_prompt(packet, epilogue=_EPILOGUE)

    def _argv(self, packet: TaskPacket) -> list[str]:
        # stream-json (NDJSON, one object per event) only when someone is listening;
        # it needs --verbose, and the final `result` object carries the same envelope
        # the plain json format returns.
        fmt = ["stream-json", "--verbose"] if self._on_activity else ["json"]
        argv = [
            self._bin,
            "-p",
            self.build_prompt(packet),
            "--output-format",
            *fmt,
            "--max-turns",
            str(max(1, packet.turn_budget)),
            # The SANDBOX is the enforcement boundary (arch §2), not the CLI's
            # permission prompts — headless agentic runs inside the cage.
            "--dangerously-skip-permissions",
        ]
        if self._model:
            argv += ["--model", self._model]
        if self._resume_session_id:
            argv += ["--resume", self._resume_session_id]  # B2: continue the prior session
        argv += list(self._extra_args)
        return argv

    def run(self, packet: TaskPacket, sandbox: Sandbox) -> AdapterResult:
        if self._exec is _default_exec and not self.available():
            raise AdapterUnavailableError(
                f"the {self.name!r} adapter needs the {self._bin!r} binary on PATH "
                f"(install Claude Code, or pick --adapter raw-react)"
            )
        before = walk_worktree(sandbox.worktree)
        if self._on_activity is not None:
            # STREAMING: each stdout line is an event; report the tool calls as they
            # happen. `on_stdout_line` is only passed in this branch, so a custom
            # exec seam that predates it is untouched unless it opts into activity.
            result = self._exec(
                sandbox, self._argv(packet), self._timeout_s,
                on_stdout_line=self._pump_activity,
            )
        else:
            result = self._exec(sandbox, self._argv(packet), self._timeout_s)
        after = walk_worktree(sandbox.worktree)

        envelope = (_stream_envelope(result.stdout) if self._on_activity is not None
                    else _lenient_json_object(result.stdout))
        if result.timed_out:
            # Budget breach, never a verdict: record what was authored so far —
            # the gates decide whether the partial tree passes.
            summary = "claude hard-killed at the sandbox wall-clock cap"
        elif envelope.get("is_error"):
            summary = f"claude reported is_error: {str(envelope.get('result'))[:400]}"
        elif not envelope and result.returncode != 0:
            raise AdapterUnavailableError(
                f"claude exit {result.returncode} with no parseable envelope: "
                f"{result.stderr[:400] or '(no stderr)'}"
            )
        else:
            raw = envelope.get("result")
            summary = raw if isinstance(raw, str) else ""

        authored, deleted, skipped_binary = changed_files(before, after)
        payload: dict[str, Any] = {"kind": "full-file-snapshot", "files": authored}
        if deleted:
            payload["deleted"] = deleted  # integration unlinks these (Phase 2)
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        diff_uri = sandbox.put_blob(blob.encode("utf-8"))

        notes = self._triage_notes(summary, sandbox, deleted, skipped_binary, envelope)
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

        num_turns = envelope.get("num_turns")
        session = envelope.get("session_id")
        return AdapterResult(
            diff_uri=diff_uri,
            test_source_uris=(),
            triage=triage,
            adapter_self_reported_model=_observed_model(envelope),
            turns_used=num_turns if isinstance(num_turns, int) and num_turns > 0 else 1,
            usage=_usage_from_envelope(envelope),
            snapshot_digest=sandbox.snapshot(),
            session_id=session if isinstance(session, str) and session else None,
        )

    def _triage_notes(
        self,
        summary: str,
        sandbox: Sandbox,
        deleted: list[str],
        skipped_binary: list[str],
        envelope: dict[str, Any],
    ) -> list[str]:
        """Advisory notes only (gov §1c): the agent's TRIAGE lines, plus what the
        snapshot slice could NOT carry, plus the observed cost + the credential
        grant record (auditable, never a secret value)."""
        notes = [
            line.removeprefix("TRIAGE:").strip()
            for line in summary.splitlines()
            if line.strip().startswith("TRIAGE:")
        ]
        for path in deleted:
            notes.append(f"deleted from the tree slice: {path}")
        for path in skipped_binary:
            notes.append(f"non-UTF-8 (binary) file skipped from snapshot slice: {path}")
        cost = envelope.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            notes.append(f"observed total_cost_usd={cost} (advisory; 0 on subscription auth)")
        grant = sandbox.credential_grant_record
        if grant:
            notes.append(f"model-auth grant active in sandbox: {', '.join(grant)}")
        if not notes:
            notes.append(summary.strip()[:500] or "run completed with no summary text")
        return notes
