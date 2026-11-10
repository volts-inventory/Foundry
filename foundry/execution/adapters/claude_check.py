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

_SCRIPT_BODY = """#!/bin/sh
# Foundry's gate check: asks the host to run the gates on this tree; prints the report.
d=$(cd "$(dirname "$0")" && pwd) || exit 2
id="r$$-$(date +%s)"
: > "$d/$id.tmp" && mv "$d/$id.tmp" "$d/$id.req" || exit 2
i=0
while [ ! -f "$d/$id.out" ]; do
  i=$((i + 1))
  if [ "$i" -gt {wait_s} ]; then
    echo "foundry-check: no answer after {wait_s}s; run it again (cached if unchanged)"
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
        f"GATE CHECK: `{CHECK_COMMAND}` (run it with Bash, timeout 600000) runs the gates "
        "themselves — the build and this task's acceptance scenarios — on a gate-shaped "
        "copy of this tree, as the wave check will judge it once integrated. Below and in "
        "its reports, `check` means that command. It prints what is YOURS to fix and what "
        "was PRE-EXISTING, and exits 1 while anything is yours. Run it after your edits "
        "and finish when it reports nothing as yours. An unchanged tree answers from "
        "cache, free; if the command times out, run it again. Never edit .claude/foundry."
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
        self.answered = 0

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
        except OSError:
            self._close()
            raise
        self._fd = self._fds[-1]
        wait_s = int(3 * self._checker.spec.base.settings.timeout_s) + 120
        self._write(SCRIPT, _SCRIPT_BODY.format(wait_s=wait_s))
        self._thread = threading.Thread(target=self._serve, name="foundry-check", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()  # an answer in flight finishes before the base goes
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)

    def _serve(self) -> None:
        while not self._stop.is_set():
            for name in sorted(os.listdir(self._fd)):
                if (m := _REQUEST.match(name)) is None:
                    continue
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=self._fd)
                report, rc = self.answer()
                rid = m.group(1)
                try:
                    self._write(f"{rid}.rc", str(rc))
                    self._write(f"{rid}.out", report + "\n")
                except OSError as exc:
                    progress.log(f"claude-code: foundry-check answer lost: {exc}")
                self.answered += 1
            self._stop.wait(_POLL_S)

    def answer(self) -> tuple[str, int]:
        """The report for the tree as it is now, and the script's exit code."""
        from foundry.execution.adapters.claude_code import changed_files, walk_worktree
        from foundry.gates.self_check import render

        c = self._checker
        files, deleted, _ = changed_files(self._origin, walk_worktree(self._worktree))
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
