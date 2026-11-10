"""``foundry-check`` for claude-code sessions: the in-session gate check, relayed.

A proxy-agent session calls ``check`` in-process. A claude-code session runs the
``claude`` CLI in the cage, so it gets a shell script instead, placed in the
worktree's ``.claude/foundry/`` (outside the authored slice: ``walk_worktree``
skips ``.claude``, and the dir is removed before the snapshot). The script drops a
request file and waits; a host thread answers with the same
:class:`~foundry.gates.self_check.SessionCheck` run proxy-agent's ``check`` makes
(its gates in their own gate sandbox, on a gate-shaped copy). The cage's env and
confinement are unchanged, and nothing crosses into it but the rendered report,
which names the tree as ``.``.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from foundry import progress

if TYPE_CHECKING:
    from foundry.execution.adapter_contract import TaskPacket
    from foundry.gates.self_check import SessionCheck

__all__ = ["CHECK_COMMAND", "CheckRelay", "check_note", "open_check"]

RELAY_DIR = ".claude/foundry"
SCRIPT = "foundry-check"
CHECK_COMMAND = f"sh {RELAY_DIR}/{SCRIPT}"
# The script's exit codes: green (or not judged), own failures, no answer/no check.
_RED, _UNAVAILABLE = 1, 2
_REQUEST = re.compile(r"^([A-Za-z0-9_-]{1,64})\.req$")
_POLL_S = 0.5
# What one check reads from the live tree: a session can't make the host buffer more.
MAX_FILE = 8 << 20
MAX_TREE = 64 << 20
# Claude Code's longest Bash timeout. The script gives up a minute short of it, so the
# session sees its "run it again" line, never a killed command; the rerun is answered
# from cache once the check it asked for lands.
BASH_TIMEOUT_MS = 600_000
SCRIPT_WAIT_MAX_S = BASH_TIMEOUT_MS // 1000 - 60

_SCRIPT_BODY = """#!/bin/sh
# Foundry's gate check: asks the host to run the gates on this tree; prints the report.
d=$(cd "$(dirname "$0")" && pwd) || exit 2
id="r$$-$(date +%s)"
: > "$d/$id.tmp" && mv "$d/$id.tmp" "$d/$id.req" || exit 2
i=0
while [ ! -f "$d/$id.out" ]; do
  i=$((i + 1))
  if [ "$i" -gt {wait_s} ]; then
    echo "foundry-check: no answer yet after {wait_s}s; run it again (cached once ready)"
    exit 2
  fi
  sleep 1
done
cat "$d/$id.out"
rc=$(cat "$d/$id.rc" 2>/dev/null)
rm -f "$d/$id.out" "$d/$id.rc"
exit "${{rc:-0}}"
"""


def open_check(packet: TaskPacket) -> tuple[SessionCheck | None, str | None]:
    """The session's gate check and its opening report on the starting tree, or
    ``(None, None)``: no :func:`~foundry.gates.self_check.check_scope` spec, or
    ``FOUNDRY_SELF_CHECK`` keeps this session out (``off``; ``local`` outside a
    local-tier session)."""
    from foundry.gates.self_check import SessionCheck, current_check, render, wanted

    spec = current_check()
    if spec is None or not wanted(spec.base.settings.mode):
        return None, None
    checker = SessionCheck(spec, label=str(packet.task.get("id", "")))
    try:
        verdict = checker.opening()
    except Exception as exc:  # an aid, not a gate: a broken check leaves the session as before
        progress.log(
            f"claude-code: no gate check for {packet.task.get('id')}: {type(exc).__name__}: {exc}"
        )
        return None, None
    return checker, render(verdict, left=checker.left, opening=True)


def check_note(opening: str) -> str:
    """The prompt's paragraph for a session with ``foundry-check``."""
    return (
        f"GATE CHECK: `{CHECK_COMMAND}` (run it with Bash, timeout {BASH_TIMEOUT_MS}) runs "
        "the gates themselves — the build and this task's acceptance scenarios — on a gate-shaped "
        "copy of this tree, as the wave check will judge it once integrated. Below and in "
        "its reports, `check` means that command. It prints what is YOURS to fix and what "
        "was PRE-EXISTING, and exits 1 while anything is yours. Run it after your edits "
        "and finish when it reports nothing as yours. An unchanged tree answers from "
        "cache, free; if it says there is no answer yet, run it again unchanged — the "
        "answer comes from cache once ready. Never edit .claude/foundry."
        f"\n\n{opening}"
    )


class CheckRelay:
    """Answers the session's ``foundry-check`` requests while it runs (a context
    manager around the CLI call). Every file operation goes through a directory
    descriptor opened without following links, so a session that swaps the relay
    dir or plants a link at an answer's name can't make the host write elsewhere."""

    def __init__(self, checker: SessionCheck, worktree: Path, origin: dict[str, bytes]) -> None:
        self._checker = checker
        self._worktree = worktree
        self._origin = origin
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fds: list[int] = []
        self._fd = -1
        self._made: list[str] = []
        self._lock = threading.Lock()
        self._served = False
        self._orphaned = False
        # How long one answer can take (its gates' timeouts), for the join at exit.
        self._wait_s = int(3 * checker.spec.base.settings.timeout_s) + 120
        self.answered = 0

    @property
    def script_wait_s(self) -> int:
        return min(self._wait_s, SCRIPT_WAIT_MAX_S)

    def __enter__(self) -> CheckRelay:
        """Raises ``OSError`` when the relay dir can't be made (a link in its place)."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self._fds = [os.open(self._worktree, os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for name in RELAY_DIR.split("/"):
                try:
                    os.mkdir(name, dir_fd=self._fds[-1])
                    self._made.append(name)
                except FileExistsError:
                    pass
                self._fds.append(os.open(name, flags, dir_fd=self._fds[-1]))
            self._fd = self._fds[-1]
            self._write(SCRIPT, _SCRIPT_BODY.format(wait_s=self.script_wait_s))
        except OSError:
            self._close()
            raise
        self._thread = threading.Thread(target=self._serve, name="foundry-check", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            # An answer in flight finishes before the base goes, within its gates'
            # timeouts; one that overruns cleans up after itself (the descriptors
            # stay open for it, never closed under it).
            self._thread.join(self._wait_s)
            with self._lock:
                if not self._served:
                    self._orphaned = True
                    progress.log("claude-code: foundry-check still answering at session end")
                    return
        self._cleanup()

    def _cleanup(self) -> None:
        for name in os.listdir(self._fd):
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=self._fd)
        self._close()

    def _close(self) -> None:
        """Close the descriptors, removing the dirs this relay made (by descriptor:
        a link the session put in their place is never followed)."""
        names = RELAY_DIR.split("/")
        while self._fds:
            os.close(self._fds.pop())
            name = names[len(self._fds) - 1] if self._fds else None
            if name is not None and name in self._made:
                with contextlib.suppress(OSError):
                    os.rmdir(name, dir_fd=self._fds[-1])

    def _write(self, name: str, text: str) -> None:
        tmp = f".{name}.host"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=self._fd)
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self._fd
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self._fd)
            raise

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                self._answer_newest()
                self._stop.wait(_POLL_S)
        finally:
            with self._lock:
                self._served = True
                orphaned = self._orphaned
            if orphaned:
                self._cleanup()

    def _answer_newest(self) -> None:
        """Answer the newest pending request with a gate run; any older one is told
        it was superseded (one run per poll, however many the session queued)."""
        pending: list[tuple[float, str, str]] = []
        for name in os.listdir(self._fd):
            if (m := _REQUEST.match(name)) is None:
                continue
            with contextlib.suppress(OSError):
                st = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
                pending.append((st.st_mtime, name, m.group(1)))
        if not pending:
            return
        pending.sort()
        for _, name, _rid in pending:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self._fd)
        for _, _name, rid in pending[:-1]:
            self._reply(rid, "ERROR: superseded by a newer foundry-check run", _UNAVAILABLE)
        self._reply(pending[-1][2], *self.answer())
        self.answered += 1

    def _reply(self, rid: str, report: str, rc: int) -> None:
        try:
            self._write(f"{rid}.rc", str(rc))
            self._write(f"{rid}.out", report + "\n")
        except OSError as exc:
            progress.log(f"claude-code: foundry-check answer lost: {exc}")

    def answer(self) -> tuple[str, int]:
        """The report for the tree as it is now, and the script's exit code."""
        from foundry.execution.adapters.claude_code import (
            TreeTooLargeError,
            changed_files,
            walk_worktree,
        )
        from foundry.gates.self_check import render

        c = self._checker
        try:
            tree = walk_worktree(self._worktree, max_file=MAX_FILE, max_total=MAX_TREE)
        except TreeTooLargeError as exc:
            return f"ERROR: the check could not run: {exc}", _UNAVAILABLE
        files, deleted, _ = changed_files(self._origin, tree)
        if (cached := c.cached(files, deleted)) is not None:
            return render(cached, left=c.left, cached=True), _RED if cached.red else 0
        if c.left <= 0:
            return (
                "ERROR: no checks left this session: run the task's verification "
                "commands to test further edits, then finish.",
                _UNAVAILABLE,
            )
        try:
            verdict = c.check(files, deleted)
        except Exception as exc:
            # The exception's text may name host paths: it stays in the host's log.
            progress.log(f"claude-code: foundry-check failed: {type(exc).__name__}: {exc}")
            return f"ERROR: the check could not run ({type(exc).__name__})", _UNAVAILABLE
        return render(verdict, left=c.left), _RED if verdict.red else 0
